"""GUI-thread command executor shared by LAN and Internet transports.

Each transport owns its discovery selection and cancellation generation; all
queue changes still use the same Bridge, scheduler and durable task store.
"""
from __future__ import annotations

import base64
import ntpath
import secrets
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot

from . import __version__
from .remote_common import MAX_UPLOAD, RemoteError, REQUEST_CONTEXT
from .remote_library import RemoteLibrary, CAPABILITIES
from .store import STAGES


class RemoteCommands(QObject):
    _source_ready = Signal(object)

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        if not hasattr(bridge, "_remote_library"):
            bridge._remote_library = RemoteLibrary(bridge)
        self.library = bridge._remote_library
        self._generation = secrets.token_hex(16)
        self._account_display_keys = {}
        self._source = {"revision": "", "busy": False, "title": "", "items": [], "error": ""}
        self._discovery_cancel = threading.Event()
        self._source_ready.connect(self._accept_source, Qt.QueuedConnection)

    @property
    def busy(self):
        return self._source["busy"] or self.library.busy

    def stop(self):
        self._discovery_cancel.set()
        self._generation = secrets.token_hex(16)
        self._account_display_keys.clear()
        self._source = {"revision": "", "busy": False, "title": "", "items": [], "error": ""}

    def _snapshot(self):
        b = self.bridge
        b.refresh()
        fields = ("id", "title", "position", "status", "statusLabel", "current", "stageLabel", "error",
                  "progress", "retryAt", "template", "folder_name", "steps")
        workers = ("id", "name", "stage", "stageLabel", "enabled", "paused", "autoBlocked", "job_id",
                   "elapsed", "error", "cooldownSeconds", "providerLabel", "modelResolved",
                   "accountName", "accountStatus", "accountWaitUntil")
        # This is a display-only projection of the GUI cache. Native identifiers,
        # auth/profile metadata and arbitrary error strings never reach a phone.
        account_usage = []
        account_rows = getattr(getattr(b, "accountsModel", None), "_items", [])
        live_ids = {row["id"] for row in account_rows}
        self._account_display_keys = {ident: key for ident, key in self._account_display_keys.items() if ident in live_ids}
        for row in account_rows:
            ident = row["id"]
            usage = row.get("usage") or {}
            projected = {key: usage.get(key) for key in ("status", "stale", "lastSuccessAt", "lastAttemptAt", "messageCode", "source")}
            for key in ("fiveHour", "weekly"):
                window = usage.get(key) or {}
                projected[key] = {field: window.get(field) for field in ("status", "remainingPercent", "resetsAt", "windowMinutes")}
            account_usage.append({"displayKey": self._account_display_keys.setdefault(ident, secrets.token_hex(12)),
                                  "alias": row["alias"], "usageRefreshing": bool(row.get("usageRefreshing")),
                                  "usage": projected})
        return {"capabilities": dict(CAPABILITIES), "version": __version__, "running": b.running, "autoStop": b._auto_stop, "stats": b.stats,
                "jobs": [{**{key: job.get(key, "") for key in fields},
                          "files": [kind for kind in ("document", "transcript") if job.get(kind)]} for job in b.jobs],
                "workers": [{key: worker.get(key, "") for key in workers} for worker in b.workers],
                "accountUsage": account_usage,
                "templates": [{"id": t["id"], "name": t["name"]} for t in b.templates],
                "folders": b.folders, "source": self._source}

    def execute(self, action, data):
        b = self.bridge
        if action.startswith("library_"):
            cfg = b.config.snapshot()
            context = REQUEST_CONTEXT.get()
            if action == "library_list": return self.library.list(data, cfg, context)
            if action == "library_file": return self.library.file(data, cfg, context)
            if action == "library_preview": return self.library.preview(data, cfg, context)
            if action == "library_delete": return self.library.delete(data, cfg, context)
            if action == "library_organize": return self.library.organize(cfg, context)
        if action == "state":
            return self._snapshot()
        if action == "file":
            job = b.store.get(str(data.get("id", "")))
            kind = data.get("kind")
            if not job or kind not in ("document", "transcript") or not job.get(kind):
                raise RemoteError("الملف غير متاح بعد.", 404)
            path = Path(job[kind]).resolve()
            if not path.is_relative_to(Path(job["folder"]).resolve()) or not path.is_file():
                raise RemoteError("الملف غير متاح في مجلد المهمة.", 404)
            return path
        if action == "queue":
            desired = data.get("running")
            if type(desired) is not bool:
                raise RemoteError("حالة تشغيل غير صالحة.")
            if b.running != desired:
                b.toggleRunning()
            return {"message": "شُغّل الطابور." if desired else "توقف توزيع المهام؛ العمال يكملون مهامهم الحالية."}
        if action in ("cancel", "retry"):
            job = b.store.get(str(data.get("id", "")))
            if not job:
                raise RemoteError("المهمة غير موجودة.", 404)
            if action == "cancel":
                b.cancelJob(job["id"])
                return {"message": "أُرسل طلب إلغاء المهمة."}
            stage = data.get("stage")
            if stage not in STAGES:
                raise RemoteError("مرحلة غير صالحة.")
            if job["stages"][stage]["status"] not in ("failed", "blocked", "uncertain", "paused", "cancelled", "pending"):
                raise RemoteError("تغيّرت حالة المهمة؛ حدّث القائمة أولًا.", 409)
            if job["stages"][stage]["status"] == "uncertain" and data.get("confirm_uncertain") is not True:
                raise RemoteError("قد تؤدي إعادة طلب التفريغ غير المؤكد إلى تكلفة إضافية. يلزم تأكيدك.", 409)
            return self._with_notice(lambda: b.retry(job["id"], stage, False), "أُعيدت المهمة إلى الطابور.")
        if action == "stage":
            stage, paused = data.get("stage"), data.get("paused")
            if stage not in STAGES or type(paused) is not bool:
                raise RemoteError("إعداد خدمة غير صالح.")
            b.pauseStage(stage, paused)
            return {"message": "حُدّثت حالة الخدمة."}
        if action == "discover":
            if self.busy:
                raise RemoteError("انتظر اكتمال قراءة المصدر الحالي.", 409)
            url = str(data.get("url", ""))
            if len(url) > 2048:
                raise RemoteError("الرابط طويل جدًا.")
            b.scheduler.youtube.check_url(url)
            cfg = b.config.snapshot()
            revision, generation = secrets.token_hex(16), self._generation
            cancel = self._discovery_cancel = threading.Event()
            self._source = {"revision": revision, "busy": True, "title": "", "items": [], "error": ""}
            def discover():
                try:
                    result = b.scheduler.youtube.discover(url, cfg, cancel)
                except Exception as exc:
                    result = {"error": str(exc), "items": [], "title": ""}
                self._source_ready.emit((generation, revision, result))
            threading.Thread(target=discover, daemon=True, name="mobile-source").start()
            return {"message": "جارٍ قراءة المصدر…"}
        if action == "enqueue":
            if self.busy or data.get("revision") != self._source["revision"] or not self._source["revision"]:
                raise RemoteError("تغيّر المصدر. راجع الفيديوهات ثم أعد الإضافة.", 409)
            indices = data.get("indices")
            if (not isinstance(indices, list) or not indices or len(indices) > len(self._source["items"]) or
                    any(type(i) is not int or not 0 <= i < len(self._source["items"]) for i in indices)):
                raise RemoteError("حدد فيديو واحدًا على الأقل من المصدر الحالي.")
            preset = data.get("preset", "full")
            if preset not in ("full", "transcribe", "download"):
                raise RemoteError("مراحل معالجة غير صالحة.")
            cfg = self._task_config(data)
            cfg.update(download_enabled=True, deepgram_enabled=preset != "download", opencode_enabled=preset == "full")
            items = [{**self._source["items"][i], "selected": True} for i in dict.fromkeys(indices)
                     if self._source["items"][i].get("available")]
            if not items:
                raise RemoteError("الفيديوهات المحددة غير متاحة.")
            added = b.store.add(items, cfg, folder_id=str(data.get("folder_id", "")))
            # A successful submission consumes this selection, preventing accidental resubmission.
            self._source = {"revision": "", "busy": False, "title": "", "items": [], "error": ""}
            b.refresh()
            return {"message": f"أضيفت {len(added)} مهمة إلى الطابور.", "count": len(added)}
        if action == "upload":
            files = data.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 20:
                raise RemoteError("اختر من ملف واحد إلى 20 ملف TXT.")
            self._task_config(data)  # Validate the template and folder before creating temporary files.
            decoded, total, names = [], 0, set()
            for item in files:
                if not isinstance(item, dict):
                    raise RemoteError("ملف غير صالح.")
                name = item.get("name", "")
                if (not isinstance(name, str) or len(name) > 160 or ntpath.isreserved(name) or any(c in name for c in '/\\') or
                        not name.lower().endswith(".txt") or name.casefold() in names):
                    raise RemoteError("اختر ملفات TXT بأسماء مختلفة دون مسارات.")
                names.add(name.casefold())
                try:
                    content = base64.b64decode(item.get("content", ""), validate=True)
                    total += len(content)
                    if total > MAX_UPLOAD:
                        raise RemoteError("الحد الإجمالي للتفريغات هو 10 ميغابايت.", 413)
                    if not content.decode("utf-8-sig").strip() or b"\x00" in content:
                        raise ValueError()
                except (ValueError, TypeError, UnicodeError):
                    raise RemoteError("أحد التفريغات فارغ أو ليس نصًا صالحًا بترميز UTF-8.") from None
                decoded.append((name, content))
            with tempfile.TemporaryDirectory(prefix="waraq-mobile-") as directory:
                paths = []
                for name, content in decoded:
                    path = Path(directory) / name
                    path.write_bytes(content)
                    paths.append(str(path))
                return self._with_notice(lambda: b.importTranscriptPaths(paths, str(data.get("template_id", "")),
                                                                         str(data.get("folder_id", ""))),
                                         "أضيفت التفريغات إلى الطابور.")
        raise RemoteError("الأمر غير مدعوم.", 404)

    def _task_config(self, data):
        cfg = self.bridge.config.snapshot()
        template_id = data.get("template_id", "")
        template = next((t for t in cfg["opencode_templates"] if t["id"] == template_id), None)
        if template_id and not template:
            raise RemoteError("القالب المحدد غير موجود؛ اختر قالبًا آخر.")
        if template:
            cfg["prompt"], cfg["template_name"] = template["text"], template["name"]
        self.bridge.store.folder_name(str(data.get("folder_id", "")))
        return cfg

    def _with_notice(self, operation, fallback):
        notices = []
        def collect(message, error):
            notices.append((message, error))
        self.bridge.notice.connect(collect, Qt.DirectConnection)
        try:
            operation()
        finally:
            self.bridge.notice.disconnect(collect)
        if notices and notices[-1][1]:
            raise RemoteError(notices[-1][0])
        return {"message": notices[-1][0] if notices else fallback}

    @Slot(object)
    def _accept_source(self, payload):
        generation, revision, result = payload
        if generation == self._generation and revision == self._source["revision"]:
            self._source = {"revision": revision, "busy": False, "title": result.get("title", ""),
                            "items": result.get("items", []), "error": result.get("error", "")}
