from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from .services import Cancelled, Deepgram, OpenCode, ServiceError, Youtube
from .store import STAGES


class Scheduler:
    def __init__(self, store, config, secrets, youtube=None, deepgram=None, opencode=None):
        self.store, self.config, self.secrets = store, config, secrets
        self.youtube = youtube or Youtube(store)
        self.deepgram = deepgram or Deepgram()
        self.opencode = opencode or OpenCode()
        self.running = False
        self.stop_event = threading.Event()
        self.active = {}
        self.lock = threading.RLock()
        self.paused_stages = set()
        self.disabled_workers = {}
        self.thread = None

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

    def workers(self):
        cfg = self.config.snapshot()
        with self.lock:
            results = []
            for stage, workers in [("download", [{"id": "yt1", "name": "YouTube", "enabled": True}]),
                                   ("deepgram", cfg["deepgram_workers"]), ("opencode", cfg["opencode_workers"])]:
                for w in workers:
                    active = self.active.get(w["id"], {})
                    results.append({**w, "stage": stage, "job_id": active.get("job_id", ""),
                                    "elapsed": round(time.time() - active["started"]) if active else 0,
                                    "paused": stage in self.paused_stages,
                                    "error": self.disabled_workers.get(w["id"], "")})
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
        while not self.stop_event.wait(0.25):
            if not self.running:
                continue
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
                if stage == "deepgram":
                    secret = self.secrets.get(wid)
                    if not secret:
                        self.disabled_workers[wid] = "أضف مفتاح API لهذا العامل"
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
        # Cookie repairs and executable paths apply without recreating queued work.
        live = self.config.snapshot()
        for k in ("cookies_file", "opencode_executable", "opencode_context"):
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
                output = self.opencode.process(job, worker, cfg, cancel, lambda line: self.store.event(ident, line))
                kind = "document"
            self.store.artifact(ident, kind, output)
            self.store.set_stage(ident, stage, "completed", progress=100)
            self.store.event(ident, f"اكتملت {stage}")
        except ServiceError as exc:
            self.store.set_stage(ident, stage, exc.kind if exc.kind != "split" else "failed", str(exc))
            self.store.event(ident, str(exc), "error")
            if exc.kind == "blocked":
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
