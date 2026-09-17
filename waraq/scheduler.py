from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from .config import PROVIDER_MAP, compose_model, credential_name, key_env_for, provider_of
from .library import Library
from .services import Cancelled, Deepgram, OpenCode, ServiceError, Youtube
from .store import STAGES


class Scheduler:
    def __init__(self, store, config, secrets, youtube=None, deepgram=None, opencode=None, library=None,
                 retry_delays=(300, 900, 2700), limit_retry_delay=3600):
        self.store, self.config, self.secrets = store, config, secrets
        self.youtube = youtube or Youtube(store)
        self.deepgram = deepgram or Deepgram()
        self.opencode = opencode or OpenCode()
        self.library = library or Library()
        self.retry_delays = tuple(retry_delays)
        self.limit_retry_delay = limit_retry_delay
        self.running = False
        self.stop_event = threading.Event()
        self.active = {}
        self.lock = threading.RLock()
        self.paused_stages = set()
        self.disabled_workers = {}
        self.provider_override = ""
        self.provider_override_model = ""
        self._keys = {}
        self.thread = None

    def set_provider_override(self, provider, model=""):
        """Session-only: force one provider (and model) on every OpenCode worker."""
        with self.lock:
            self.provider_override = provider if provider in PROVIDER_MAP else ""
            self.provider_override_model = str(model or "")
            self.disabled_workers.clear()

    def provider_of_worker(self, worker):
        """Effective OpenCode provider: the queue override wins over the worker default."""
        return self.provider_override or provider_of(worker)

    def effective_worker(self, worker):
        """Apply a queue-level override to one OpenCode worker."""
        if not self.provider_override:
            return worker
        return {**worker, "provider": self.provider_override, "variant": "",
                "model": compose_model(self.provider_override, self.provider_override_model)}

    def key_state(self, stage, worker, provider=""):
        """Whether the OS store holds a credential for this worker; cached briefly.

        The worker list is republished several times per second, and one credential
        read per worker per refresh would hammer the Windows store for nothing.
        """
        name = credential_name(stage, worker["id"], provider)
        now = time.time()
        with self.lock:
            cached = self._keys.get(name)
            if cached and now - cached[0] < 10:
                return cached[1]
        value = bool(self.secrets.get(name)) if self.secrets else False
        with self.lock:
            self._keys[name] = (now, value)
        return value

    def forget_keys(self):
        with self.lock:
            self._keys.clear()

    def start(self):
        self.running = True
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True, name="scheduler")
            self.thread.start()

    def pause(self):
        self.running = False

    def pause_stage(self, stage, pause):
        with self.lock:
            if pause:
                self.paused_stages.add(stage)
            else:
                self.paused_stages.discard(stage)

    def cancel_job(self, ident):
        with self.lock:
            for active in self.active.values():
                if active["job_id"] == ident:
                    active["cancel"].set()
        job = self.store.get(ident)
        if job:
            for name, stage in job["stages"].items():
                if stage["status"] in ("pending", "paused"):
                    self.store.set_stage(ident, name, "cancelled")

    def shutdown(self):
        self.running = False
        self.stop_event.set()
        with self.lock:
            for active in self.active.values():
                active["cancel"].set()

    def idle(self):
        with self.lock:
            return not self.active

    @staticmethod
    def human_delay(seconds):
        if seconds >= 3600:
            return f"{seconds / 3600:g} ساعة"
        if seconds >= 60:
            return f"{seconds // 60:g} دقيقة"
        return f"{int(seconds)} ثانية"

    def workers(self):
        cfg = self.config.snapshot()
        with self.lock:
            results = []
            for stage, workers in [("download", [{"id": "yt1", "name": "YouTube", "enabled": True}]),
                                   ("deepgram", cfg["deepgram_workers"]), ("opencode", cfg["opencode_workers"])]:
                for w in workers:
                    active = self.active.get(w["id"], {})
                    entry = {**w, "stage": stage, "job_id": active.get("job_id", ""),
                             "elapsed": round(time.time() - active["started"]) if active else 0,
                             "paused": stage in self.paused_stages,
                             "error": self.disabled_workers.get(w["id"], ""),
                             "provider": "", "providerLabel": "", "keyEnv": "", "hasKey": False,
                             "modelResolved": w.get("model", "")}
                    if stage == "opencode":
                        provider = self.provider_of_worker(w)
                        entry.update({"provider": provider, "providerLabel": PROVIDER_MAP[provider]["name"],
                                      "keyEnv": key_env_for(provider, w),
                                      "modelResolved": compose_model(provider, w.get("model", "")),
                                      "hasKey": self.key_state(stage, w, provider)})
                    elif stage == "deepgram":
                        entry["hasKey"] = self.key_state(stage, w)
                    results.append(entry)
            return results

    def ready(self, job, stage, jobs):
        record = job["stages"][stage]
        if record["status"] != "pending" or record["next_retry"] > time.time():
            return False
        index = STAGES.index(stage)
        if any(job["stages"][s]["status"] not in ("completed", "skipped") for s in STAGES[:index]):
            return False
        if job["config"].get("strict_order"):
            for prior in jobs:
                if prior["batch"] == job["batch"] and prior["queue_order"] < job["queue_order"]:
                    if prior["stages"][stage]["status"] not in ("completed", "skipped"):
                        return False
        return True

    def loop(self):
        next_prune = time.time() + 3600
        while not self.stop_event.wait(0.25):
            if not self.running:
                continue
            if time.time() >= next_prune:
                next_prune = time.time() + 3600
                try:
                    self.store.prune()
                except Exception as exc:
                    self.store.event("", "تعذر تقليم السجل: " + str(exc), "error")
            try:
                self.tick()
            except Exception as exc:
                self.store.event("", "توقف المنظم للمراجعة: " + str(exc), "error")
                self.running = False

    def tick(self):
        cfg = self.config.snapshot()
        jobs = self.store.jobs()
        pending_audio = sum(bool(j["audio"]) and j["stages"]["deepgram"]["status"] not in ("completed", "skipped", "cancelled") for j in jobs)
        for stage, pool in [("download", [{"id": "yt1", "enabled": True}]),
                            ("deepgram", cfg["deepgram_workers"]), ("opencode", cfg["opencode_workers"])]:
            if stage in self.paused_stages:
                continue
            if stage == "download" and (pending_audio >= cfg["max_pending_audio"] or time.time() < self.youtube.cooldown_until):
                continue
            for worker in pool:
                wid = worker["id"]
                with self.lock:
                    if not worker["enabled"] or wid in self.active or wid in self.disabled_workers:
                        continue
                    if stage == "deepgram":
                        concurrent = sum(a["stage"] == stage and a.get("project") == worker["project"] for a in self.active.values())
                        if concurrent >= cfg["project_concurrency"]:
                            continue
                secret = ""
                if stage != "download":
                    provider = self.provider_of_worker(worker) if stage == "opencode" else ""
                    # Login-based OpenCode workers use whatever OpenCode already holds.
                    if stage == "deepgram" or key_env_for(provider, worker):
                        secret = self.secrets.get(credential_name(stage, wid, provider)) if self.secrets else ""
                        if not secret:
                            if stage == "deepgram":
                                label = "API"
                            elif provider == "custom":
                                label = key_env_for(provider, worker)
                            else:
                                label = PROVIDER_MAP[provider]["name"]
                            self.disabled_workers[wid] = f"أضف مفتاح {label} لهذا العامل"
                            continue
                for job in jobs:
                    if not self.ready(job, stage, jobs):
                        continue
                    if stage == "download":
                        root = Path(job["config"]["output_root"])
                        root.mkdir(parents=True, exist_ok=True)
                        if shutil.disk_usage(root).free < cfg["min_free_gb"] * 1024 ** 3:
                            self.store.set_stage(job["id"], stage, "blocked", "المساحة الحرة أقل من الحد المحدد.")
                            self.pause_stage(stage, True)
                            break
                    if not self.store.claim(job["id"], stage, wid):
                        continue
                    job["stages"][stage]["status"] = "running"
                    cancel = threading.Event()
                    with self.lock:
                        self.active[wid] = {"job_id": job["id"], "stage": stage, "cancel": cancel,
                                            "project": worker.get("project"), "started": time.time()}
                    thread = threading.Thread(target=self.execute, args=(job, stage, worker.copy(), secret, cancel), daemon=True, name=wid)
                    thread.start()
                    break

    def execute(self, job, stage, worker, secret, cancel):
        wid, ident = worker["id"], job["id"]
        cfg = job["config"].copy()
        if stage != "download":
            saved_workers = cfg[stage + "_workers"]
            saved = next((w for w in saved_workers if w["id"] == wid), saved_workers[0])
            worker = {**saved, "id": wid, "name": worker["name"], "project": worker.get("project", "")}
            if stage == "opencode":
                worker = self.effective_worker(worker)
        # Cookie repairs and executable paths apply without recreating queued work.
        live = self.config.snapshot()
        for k in ("cookies_file", "opencode_executable", "opencode_context", "opencode_data_root"):
            cfg[k] = live[k]
        self.store.event(ident, f"بدء {stage} — {wid}")
        progress = lambda value: self.store.progress(ident, stage, value)
        try:
            if stage == "download":
                output = self.youtube.download(job, cfg, cancel, progress)
                kind = "audio"
            elif stage == "deepgram":
                output = self.deepgram.transcribe(job, worker, cfg, secret, cancel, progress)
                kind = "transcript"
            else:
                output = self.opencode.process(job, worker, cfg, secret, cancel, lambda line: self.store.event(ident, line))
                kind = "document"
            self.store.artifact(ident, kind, output)
            self.store.set_stage(ident, stage, "completed", progress=100)
            self.store.event(ident, f"اكتملت {stage}")
            self.publish_library(ident)
        except ServiceError as exc:
            kind = exc.kind
            used = int(job["stages"][stage].get("retries", 0))
            # Jobs snapshotted before these settings existed keep sane defaults.
            budget = cfg.get("opencode_limit_retries", 5) if kind == "rate_limited" else cfg.get("auto_retry_attempts", 3)
            retryable = (kind == "rate_limited" and stage == "opencode") or \
                        (kind == "failed" and stage in ("download", "opencode"))
            if retryable and used < budget:
                delay = self.limit_retry_delay if kind == "rate_limited" else self.retry_delays[min(used, len(self.retry_delays) - 1)]
                count = self.store.defer_retry(ident, stage, str(exc), delay)
                self.store.event(ident, f"أُعيدت الجدولة بعد {self.human_delay(delay)} "
                                        f"(محاولة {count}/{budget}): {str(exc)[:300]}")
            elif kind == "rate_limited":
                message = f"استُنفدت محاولات الانتظار ({budget}) وظل حد استخدام OpenCode ممتلئًا — غالبًا الحد الأسبوعي. أعد المحاولة يدويًا لاحقًا."
                self.store.set_stage(ident, stage, "failed", message)
                self.store.event(ident, message, "error")
            else:
                self.store.set_stage(ident, stage, kind if kind != "split" else "failed", str(exc))
                self.store.event(ident, str(exc), "error")
                if kind == "blocked":
                    if stage == "download":
                        self.pause_stage(stage, True)
                    else:
                        self.disabled_workers[wid] = str(exc)
        except Exception as exc:
            # Remove API key before storing any unexpected exception.
            message = str(exc).replace(secret, "[محجوب]") if secret else str(exc)
            self.store.set_stage(ident, stage, "failed", message)
            self.store.event(ident, message, "error")
        finally:
            with self.lock:
                self.active.pop(wid, None)

    def publish_library(self, ident):
        """Mirror finished artifacts into the readable library; never fails the stage."""
        live = self.config.snapshot()
        if not live.get("library_auto", True):
            return
        try:
            job = self.store.get(ident)
            copied = self.library.publish(job, live) if job else []
            if copied:
                self.store.event(ident, "نُسخ إلى المكتبة: " + "، ".join(copied))
        except Exception as exc:
            self.store.event(ident, "تعذر التنظيم في المكتبة: " + str(exc), "error")
