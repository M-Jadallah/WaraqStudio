from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog

from . import __version__
from .catalog import deepgram_models, opencode_models
from .config import (BUILTIN_PROVIDERS, credential_name, defaults, key_env_for, provider_catalog,
                     provider_info, provider_of)
from .models import DictListModel
from .services import resolve_opencode
from .store import STAGES

LABELS = {"pending": "في الانتظار", "running": "قيد التنفيذ", "completed": "مكتمل", "skipped": "متجاوز",
          "failed": "فشل", "blocked": "يتطلب إجراء", "uncertain": "نتيجة غير مؤكدة", "paused": "متوقف", "cancelled": "ملغى"}
STAGE_LABELS = {"download": "التنزيل", "deepgram": "التفريغ", "opencode": "ملف Word"}


class Bridge(QObject):
    changed = Signal()
    sourceChanged = Signal()
    settingsChanged = Signal()
    notice = Signal(str, bool)
    sourceReady = Signal(str)
    backgroundError = Signal(str)
    autoStopReached = Signal()
    libraryChanged = Signal()
    foldersChanged = Signal()
    youtubeVersionChanged = Signal()
    modelCatalogChanged = Signal()
    _modelCatalogReady = Signal(object, object, str, str)

    def __init__(self, store, config, secrets, scheduler):
        super().__init__()
        self.store, self.config, self.secrets, self.scheduler = store, config, secrets, scheduler
        self._jobs, self._workers, self._logs, self._source = [], [], [], []
        self._running = False
        self._source_title = ""
        self._busy = False
        self._auto_stop = False
        self._auto_stop_seen = False
        self._auto_stop_fired = False
        self._library = []
        self._library_busy = False
        self._youtube_updating = False
        self._deepgram_models = []
        self._opencode_models = []
        self._catalog_errors = {"deepgram": "", "opencode": ""}
        self._catalog_busy = False
        self._modelCatalogReady.connect(self._apply_model_catalogs)
        # QML ListViews bind to these instead of the QVariantList properties:
        # rows are updated in place (dataChanged) so scroll position survives
        # every refresh tick, unlike a republished JS array which resets the view.
        self._jobs_model = DictListModel(parent=self)
        self._workers_model = DictListModel(parent=self)
        self._source_model = DictListModel(parent=self)
        self._results_model = DictListModel(parent=self)
        self._files_model = DictListModel(parent=self)
        self._providers_model = DictListModel(parent=self)
        self._results_query = ""
        self._files_query = ""
        self._files_kind = "all"
        self._results_folder = "all"
        self._files_folder = "all"
        # refreshLibrary() may run on a worker thread (after organizing); the
        # queued connection applies the scan result to the model on the GUI thread.
        self.libraryChanged.connect(self._publish_files)
        self.settingsChanged.connect(self._publish_providers)
        self._publish_providers()
        self.discovery_cancel = threading.Event()
        self.sourceReady.connect(self._accept_source)
        self.backgroundError.connect(self._error)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.refresh()
        if self.config.migration_note:
            note = self.config.migration_note
            self.store.event("", note)
            # Delayed so the notice lands after the window is on screen.
            QTimer.singleShot(2500, lambda: self.notice.emit(note, False))

    @Property("QVariantList", notify=changed)
    def jobs(self): return self._jobs

    @Property("QVariantList", notify=changed)
    def workers(self): return self._workers

    @Property("QVariantList", notify=modelCatalogChanged)
    def deepgramModels(self): return self._deepgram_models

    @Property("QVariantList", notify=modelCatalogChanged)
    def opencodeModels(self): return self._opencode_models

    @Property("QVariantMap", notify=modelCatalogChanged)
    def catalogErrors(self): return self._catalog_errors

    @Property(bool, notify=modelCatalogChanged)
    def catalogBusy(self): return self._catalog_busy

    @Slot()
    def refreshModelCatalogs(self):
        if self._catalog_busy:
            return
        self._catalog_busy = True
        self.modelCatalogChanged.emit()
        cfg = self.config.snapshot()
        threading.Thread(target=self._load_model_catalogs, args=(cfg,), daemon=True).start()

    def _load_model_catalogs(self, cfg):
        dg, oc, dg_error, oc_error = None, None, "", ""
        try:
            dg = deepgram_models()
        except Exception:
            dg_error = "تعذّر جلب نماذج Deepgram. يمكنك إبقاء النموذج المحفوظ أو إعادة المحاولة."
        try:
            oc = opencode_models(cfg["opencode_executable"], cfg.get("opencode_context", ""))
        except Exception:
            oc_error = "تعذّر قراءة نماذج OpenCode المحلية. تحقق من مسار البرنامج ثم أعد المحاولة."
        self._modelCatalogReady.emit(dg, oc, dg_error, oc_error)

    @Slot(object, object, str, str)
    def _apply_model_catalogs(self, dg, oc, dg_error, oc_error):
        if dg is not None:
            self._deepgram_models = dg
        if oc is not None:
            self._opencode_models = oc
        self._catalog_errors = {"deepgram": dg_error, "opencode": oc_error}
        self._catalog_busy = False
        self.modelCatalogChanged.emit()

    @Property("QVariantList", notify=changed)
    def logs(self): return self._logs

    @Property(QObject, constant=True)
    def jobsModel(self): return self._jobs_model

    @Property(QObject, constant=True)
    def workersModel(self): return self._workers_model

    @Property(QObject, constant=True)
    def sourceModel(self): return self._source_model

    @Property(QObject, constant=True)
    def resultsModel(self): return self._results_model

    @Property(QObject, constant=True)
    def filesModel(self): return self._files_model

    @Property(QObject, constant=True)
    def providersModel(self): return self._providers_model

    @Property(bool, notify=sourceChanged)
    def hasSourceSelection(self): return any(row.get("selected") for row in self._source)

    @Property(int, notify=libraryChanged)
    def libraryCount(self): return len(self._library)

    @Property("QVariantList", notify=sourceChanged)
    def source(self): return self._source

    @Property(str, notify=sourceChanged)
    def sourceTitle(self): return self._source_title

    @Property(bool, notify=sourceChanged)
    def busy(self): return self._busy

    @Property(bool, notify=changed)
    def running(self): return self.scheduler.running

    @Property(str, notify=settingsChanged)
    def settingsJson(self): return json.dumps(self.config.snapshot(), ensure_ascii=False, indent=2)

    @Property(str, constant=True)
    def version(self): return __version__

    @Property(float, notify=settingsChanged)
    def uiScale(self): return float(self.config.snapshot().get("ui_scale", 1.0))

    @Property("QVariantList", notify=settingsChanged)
    def templates(self):
        return [{"id": t["id"], "name": t["name"], "text": t["text"]} for t in self.config.snapshot()["opencode_templates"]]

    @Property("QVariantList", notify=settingsChanged)
    def providers(self):
        """Mirror of providersModel for ComboBoxes and Python-side checks."""
        return self._provider_rows()

    @Property("QVariantList", notify=libraryChanged)
    def library(self): return self._library

    @Property("QVariantList", notify=foldersChanged)
    def folders(self):
        return [{"id": "", "name": "بدون مجلد"}] + self.store.folders()

    @Slot(str, result=str)
    def createFolder(self, name):
        try:
            ident = self.store.create_folder(name)
            self.foldersChanged.emit()
            self.notice.emit("أُنشئ المجلد «" + name.strip() + "».", False)
            return ident
        except Exception as exc:
            self.notice.emit(str(exc), True)
            return ""

    @Slot(str, str)
    def assignJobFolder(self, job_id, folder_id):
        try:
            self.store.assign_folder(job_id, folder_id)
            self.refresh()
            def work():
                try:
                    self.scheduler.library.publish_current(self.store, job_id, self.config.snapshot())
                    self.notice.emit("حُدّث مجلد المهمة ونسختها في المكتبة.", False)
                except Exception as exc:
                    self.notice.emit("حُفظ تصنيف المهمة، وتعذر تحديث نسخة المكتبة: " + str(exc), True)
                finally:
                    self.refreshLibrary()
            threading.Thread(target=work, daemon=True, name="folder-assignment").start()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    @Property(bool, notify=libraryChanged)
    def libraryBusy(self): return self._library_busy

    @Property(bool, notify=youtubeVersionChanged)
    def youtubeUpdating(self): return self._youtube_updating

    @Property(str, notify=youtubeVersionChanged)
    def youtubeVersion(self):
        from .youtube_update import binary_version
        try:
            active = binary_version(self.config.home)
            if active:
                return active + " (محدّثة)"
        except Exception:
            pass
        import yt_dlp
        return yt_dlp.version.__version__ + " (مدمجة)"

    @Slot()
    def updateYoutube(self):
        if self._youtube_updating:
            return
        if not self.scheduler.idle() or self._busy:
            self.notice.emit("انتظر اكتمال قراءة المصدر أو تنزيله قبل تحديث أداة YouTube.", True)
            return
        self._youtube_updating = True
        self.youtubeVersionChanged.emit()
        def work():
            try:
                from .youtube_update import update
                with self.scheduler.youtube.gate:
                    version = update(self.config.home)
                self.notice.emit("اكتمل تحديث yt-dlp إلى " + version + " وسيُستخدم في الطلبات التالية.", False)
            except Exception as exc:
                self.notice.emit("فشل تحديث yt-dlp: " + str(exc), True)
            finally:
                self._youtube_updating = False
                self.youtubeVersionChanged.emit()
        threading.Thread(target=work, daemon=True, name="youtube-update").start()

    @Property("QVariantMap", notify=changed)
    def stats(self):
        return {"total": len(self._jobs), "active": sum(j["status"] == "running" for j in self._jobs),
                "done": sum(j["status"] == "completed" for j in self._jobs),
                "attention": sum(j["status"] in ("failed", "blocked", "uncertain") for j in self._jobs),
                "documents": sum(bool(j["document"]) for j in self._jobs)}

    @Slot()
    def refresh(self):
        result = []
        for j in self.store.jobs():
            stages = j["stages"]
            if all(s["status"] in ("completed", "skipped") for s in stages.values()):
                state, current = "completed", "opencode"
            else:
                current = next(s for s in STAGES if stages[s]["status"] not in ("completed", "skipped"))
                state = stages[current]["status"]
            result.append({k: v for k, v in j.items() if k not in ("config", "stages") } | {
                "status": state, "statusLabel": LABELS.get(state, state), "current": current,
                "stageLabel": STAGE_LABELS[current], "error": stages[current]["error"],
                "worker": stages[current]["worker"], "progress": stages[current]["progress"],
                "retryAt": self.retry_at(stages[current]["next_retry"])
                           if state == "pending" and stages[current]["next_retry"] > time.time() else "",
                "template": j["config"].get("template_name", ""),
                "steps": [{"name": name, "label": STAGE_LABELS[name], "status": stages[name]["status"]} for name in STAGES],
            })
        workers = self.scheduler.workers()
        for w in workers:
            w["stageLabel"] = STAGE_LABELS[w["stage"]]
        logs = [{**r, "time": time.strftime("%H:%M:%S", time.localtime(r["ts"]))} for r in self.store.events(100)]
        running = self.scheduler.running
        if self._auto_stop and not self._auto_stop_fired:
            if self.store.queue_drained():
                # Fire only after real work was seen, so arming on an empty queue never quits immediately.
                if self._auto_stop_seen and running and not self._busy and self.scheduler.idle():
                    self._auto_stop_fired = True
                    self.store.event("", "اكتمل تنفيذ الطابور؛ سيُغلق التطبيق تلقائيًا.", "info")
                    self.autoStopReached.emit()
            else:
                self._auto_stop_seen = True
        # The timer fires while idle too; republishing identical state would
        # make QML re-evaluate every binding for nothing.
        if result == self._jobs and workers == self._workers and logs == self._logs and running == self._running:
            return
        self._jobs, self._workers, self._logs, self._running = result, workers, logs, running
        self._jobs_model.update_items(result)
        self._workers_model.update_items(workers)
        self._publish_results()
        self.changed.emit()

    @staticmethod
    def retry_at(timestamp):
        fmt = "%H:%M" if time.localtime(timestamp)[:3] == time.localtime()[:3] else "%Y-%m-%d %H:%M"
        return time.strftime(fmt, time.localtime(timestamp))

    @Slot(str, result="QVariantMap")
    def jobById(self, ident):
        return next((j for j in self._jobs if j["id"] == ident), {})

    def _publish_results(self):
        query = self._results_query.lower()
        rows = [j for j in self._jobs
                if (j["document"] or j["transcript"] or j["audio"])
                and (not query or query in str(j["title"]).lower())
                and (self._results_folder == "all" or j.get("folder_id", "") == self._results_folder)]
        self._results_model.update_items(rows)

    @Slot(str)
    def setResultsQuery(self, text):
        text = str(text)
        if text == self._results_query:
            return
        self._results_query = text
        self._publish_results()

    @Slot(str)
    def setResultsFolder(self, ident):
        self._results_folder = ident
        self._publish_results()

    def _publish_files(self):
        query = self._files_query.lower()
        rows = [f for f in self._library
                if (self._files_kind == "all" or f["kind"] == self._files_kind)
                and (not query or query in str(f["name"]).lower())
                and (self._files_folder == "all" or f["group"] == self._files_folder)]
        self._files_model.update_items(rows)

    @Slot(str)
    def setFilesQuery(self, text):
        text = str(text)
        if text == self._files_query:
            return
        self._files_query = text
        self._publish_files()

    @Slot(str)
    def setFilesKind(self, kind):
        kind = str(kind)
        if kind == self._files_kind:
            return
        self._files_kind = kind
        self._publish_files()

    @Slot(str)
    def setFilesFolder(self, name):
        self._files_folder = name
        self._publish_files()

    def _provider_rows(self):
        """Built-in providers plus the ones the user added; only the added ones are editable."""
        builtin = {item["id"] for item in BUILTIN_PROVIDERS}
        return [{"id": p["id"], "name": p["name"], "keyEnv": p["key_env"], "isolated": p["isolated"],
                 "builtin": p["id"] in builtin} for p in provider_catalog(self.config.snapshot())]

    @Slot()
    def _publish_providers(self):
        self._providers_model.update_items(self._provider_rows())

    @Slot(str)
    def _accept_source(self, raw):
        data = json.loads(raw)
        self._source = data["items"]
        self._source_title = data["title"]
        self._busy = False
        self._source_model.update_items(self._source)
        self.sourceChanged.emit()

    @Slot(str)
    def _error(self, message):
        self._busy = False
        self.sourceChanged.emit()
        self.notice.emit(message, True)

    @Slot(str, bool)
    def discover(self, url, refresh):
        if self._busy:
            return
        self._busy = True
        self.sourceChanged.emit()
        self.discovery_cancel = threading.Event()
        cfg = self.config.snapshot()
        def work():
            try:
                data = self.scheduler.youtube.discover(url, cfg, self.discovery_cancel, refresh)
                self.sourceReady.emit(json.dumps(data, ensure_ascii=False))
            except Exception as exc:
                self.backgroundError.emit(str(exc))
        threading.Thread(target=work, daemon=True).start()

    @Slot()
    def cancelDiscovery(self):
        self.discovery_cancel.set()

    @Slot(int, bool)
    def selectSource(self, index, selected):
        if 0 <= index < len(self._source):
            self._source[index]["selected"] = selected and self._source[index]["available"]
            self._source_model.update_items(self._source)
            self.sourceChanged.emit()

    @Slot(str)
    def selectRange(self, spec):
        try:
            wanted = set()
            if spec.strip() not in ("all", "none"):
                for part in spec.replace("،", ",").split(","):
                    bits = [int(s.strip()) for s in part.split("-")]
                    if len(bits) == 1:
                        wanted.add(bits[0])
                    elif len(bits) == 2 and 0 < bits[0] <= bits[1] <= 100000:
                        wanted.update(range(bits[0], bits[1] + 1))
                    else:
                        raise ValueError()
            for row in self._source:
                row["selected"] = row["available"] and (spec == "all" or row["position"] in wanted)
            self._source_model.update_items(self._source)
            self.sourceChanged.emit()
        except ValueError:
            self.notice.emit("اكتب نطاقًا مثل 1-10,15,20-25", True)

    @Slot(int, int)
    def moveSource(self, index, direction):
        target = index + direction
        if 0 <= target < len(self._source):
            self._source[index], self._source[target] = self._source[target], self._source[index]
            # Same length, so this lands as a dataChanged on two rows: the rows
            # swap content in place and the view neither jumps nor flickers.
            self._source_model.update_items(self._source)
            self.sourceChanged.emit()

    @Slot(str, str)
    @Slot(str, str, str)
    def enqueue(self, preset, template_id="", folder_id=""):
        try:
            cfg = self.config.snapshot()
            if preset != "custom":
                cfg["download_enabled"] = True
                cfg["deepgram_enabled"] = preset != "download"
                cfg["opencode_enabled"] = preset == "full"
            template = next((t for t in cfg["opencode_templates"] if t["id"] == template_id), None)
            if template:
                cfg["prompt"] = template["text"]
                cfg["template_name"] = template["name"]
            selected = [dict(row) for row in self._source if row["selected"]]
            if not selected:
                raise ValueError("حدد فيديو واحدًا على الأقل.")
            # Preserve original lesson numbers; Store saves the visual queue order separately.
            added = self.store.add(selected, cfg, folder_id=folder_id)
            self.notice.emit(f"أضيفت {len(added)} مهمة. اضغط تشغيل لبدء المعالجة.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    @Slot()
    @Slot(str)
    def importLocal(self, folder_id=""):
        paths, _ = QFileDialog.getOpenFileNames(None, "استيراد صوت أو تفريغ", "", "Media (*.mp3 *.wav *.m4a *.flac *.ogg *.txt)")
        added = 0
        for name in paths:
            path = Path(name)
            if path.suffix.lower() == ".txt":
                # Reject unreadable transcripts here instead of failing later at the Word stage.
                try:
                    readable = path.read_text(encoding="utf-8-sig").strip()
                except (OSError, UnicodeDecodeError) as exc:
                    self.notice.emit(f"تعذر استيراد «{path.name}»: {exc}", True)
                    continue
                if not readable:
                    self.notice.emit(f"تعذر استيراد «{path.name}»: ملف التفريغ فارغ.", True)
                    continue
            cfg = self.config.snapshot()
            # Audio is normalized by FFmpeg before API upload; local TXT goes directly to Word.
            cfg["download_enabled"] = False
            cfg["deepgram_enabled"] = path.suffix.lower() != ".txt"
            cfg["opencode_enabled"] = True
            try:
                self.store.add([{"video_id": "local-" + path.stem, "title": path.stem, "position": 1,
                                 "url": "", "available": True}], cfg, input_path=str(path), folder_id=folder_id)
            except Exception as exc:
                self.notice.emit(f"تعذر استيراد «{path.name}»: {exc}", True)
                continue
            added += 1
        if added:
            self.notice.emit(f"أضيفت {added} ملفات محلية.", False)
            self.refresh()

    @Slot()
    def toggleRunning(self):
        if self.scheduler.running:
            self.scheduler.pause()
            self.notice.emit("توقف توزيع المهام. العمال النشطون يكملون مهامهم الحالية.", False)
        else:
            self.scheduler.start()
        self.refresh()

    @Slot(bool)
    def setAutoStop(self, enabled):
        """Session-only switch: when the queue fully drains, the app quits itself."""
        self._auto_stop = bool(enabled)
        if not enabled:
            self._auto_stop_seen = self._auto_stop_fired = False

    @Slot(str, bool)
    def pauseStage(self, name, paused):
        self.scheduler.pause_stage(name, paused)
        self.refresh()

    @Slot(str)
    def recheckStage(self, stage):
        """Re-check a service after its automatic block was fixed: lift the block
        and requeue the jobs the block had parked."""
        self.scheduler.unblock_stage(stage)
        for job in self.store.jobs():
            record = job["stages"].get(stage)
            if record and record["status"] == "blocked":
                self.store.set_stage(job["id"], stage, "pending")
                self.store.event(job["id"], "أُعيد فحص الخدمة فأُعيدت المهمة إلى الطابور.")
        self.notice.emit("أُعيد فحص الخدمة؛ ستُتابع المهام العمل تلقائيًا إذا كان الطابور يعمل.", False)
        self.refresh()

    @Slot(str)
    def cancelJob(self, ident):
        self.scheduler.cancel_job(ident)
        self.refresh()

    @Slot(str, str, bool)
    def retry(self, ident, stage, rerun):
        try:
            self._retry_one(ident, stage, rerun)
            # Retrying a download lifts the automatic block (cookies, disk) but
            # never the user's manual pause and never the 429 cooldown.
            manual = stage == "download" and stage in self.scheduler.manual_paused_stages
            if stage == "download":
                self.scheduler.unblock_stage(stage)
            remaining = max(0, int(self.scheduler.youtube.cooldown_until - time.time()))
            if stage == "download" and remaining > 0:
                self.notice.emit(f"أُعيدت المهمة إلى الطابور وستبدأ تلقائيًا بعد انتهاء تهدئة YouTube ({self.scheduler.human_delay(remaining)}).", False)
            elif manual:
                self.notice.emit("أُعيدت المهمة إلى الطابور، لكن خدمة التنزيل موقوفة يدويًا من صفحة العمال.", False)
            elif not self.scheduler.running:
                self.notice.emit("أُعيدت المهمة إلى الطابور. شغّل الطابور للبدء.", False)
            else:
                self.notice.emit("أُعيدت المهمة إلى الطابور وسيبدأ المنظم بتنفيذها.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    def _retry_one(self, ident, stage, rerun=False):
        self.store.retry(ident, stage, rerun)
        if not rerun:
            job = self.store.get(ident)
            for name in STAGES[STAGES.index(stage) + 1:]:
                if job["stages"][name]["status"] == "cancelled":
                    self.store.set_stage(ident, name, "pending")

    @Slot('QVariantList', str, str)
    def retryMany(self, idents, stage, status):
        """Apply one action to matching attention jobs; ignore stale UI selections."""
        if stage not in STAGES or status not in ("failed", "blocked", "uncertain"):
            self.notice.emit("اختر مجموعة مهام متشابهة لإعادة المحاولة.", True)
            return
        count = skipped = 0
        for ident in dict.fromkeys(str(value) for value in idents):
            job = self.store.get(ident)
            if not job or any(record["status"] == "running" for record in job["stages"].values()):
                skipped += 1
                continue
            current = next((name for name in STAGES if job["stages"][name]["status"] not in ("completed", "skipped")), "")
            if current != stage or job["stages"][stage]["status"] != status:
                skipped += 1
                continue
            try:
                self._retry_one(ident, stage)
                self.store.event(ident, "إعادة محاولة جماعية بطلب المستخدم.")
                count += 1
            except Exception:
                skipped += 1
        if count and stage == "download":
            self.scheduler.unblock_stage(stage)
        self.notice.emit(f"أُعيدت {count} مهمة إلى الطابور" + (f"؛ تخطيت {skipped} مهمة تغيّرت حالتها." if skipped else "."), not bool(count))
        self.refresh()

    @Slot(str)
    def openPath(self, path):
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
        else:
            self.notice.emit("الملف أو المجلد غير موجود بعد.", True)

    @Slot(str)
    def copyText(self, text):
        QGuiApplication.clipboard().setText(str(text))
        self.notice.emit("نُسخ المسار إلى الحافظة.", False)

    @Slot(str, result=str)
    def choosePath(self, kind):
        if kind == "folder":
            return QFileDialog.getExistingDirectory(None, "اختيار مجلد")
        path, _ = QFileDialog.getOpenFileName(None, "اختيار ملف", "", "All files (*)")
        return path

    @Slot(str, result=bool)
    def saveSettings(self, raw):
        try:
            self.config.save(json.loads(raw))
            self.scheduler.disabled_workers.clear()
            self.scheduler.forget_keys()
            self.settingsChanged.emit()
            self.notice.emit("حُفظت الإعدادات. المهام الجديدة تستخدم إعدادات المعالجة الجديدة.", False)
            return True
        except Exception as exc:
            self.notice.emit(str(exc), True)
            return False

    @Slot(str, str)
    def saveKey(self, worker, key):
        try:
            stage, provider = self._worker_scope(worker)
            self.secrets.set(credential_name(stage, worker, provider), key)
            self.scheduler.forget_keys()
            self.scheduler.disabled_workers.pop(worker, None)
            self.notice.emit("حُفظ المفتاح في مخزن بيانات اعتماد Windows.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit("تعذر حفظ المفتاح: " + str(exc), True)

    def _worker_scope(self, worker):
        """Stage and configured provider of a worker id, used for the credential name."""
        data = self.config.snapshot()
        for item in data["deepgram_workers"]:
            if item["id"] == worker:
                return "deepgram", ""
        for item in data["opencode_workers"]:
            if item["id"] == worker:
                return "opencode", provider_of(item, data)
        raise ValueError("لم يُعثر على العامل.")

    @Slot(str, bool)
    def setWorkerEnabled(self, worker, enabled):
        """Start or stop this worker taking new jobs, straight from the worker list."""
        data = self.config.snapshot()
        for kind in ("deepgram_workers", "opencode_workers"):
            for item in data[kind]:
                if item["id"] == worker:
                    item["enabled"] = bool(enabled)
                    self.config.save(data)
                    self.scheduler.disabled_workers.clear()
                    self.settingsChanged.emit()
                    self.notice.emit(("سُجّل العامل " if enabled else "أُوقف العامل ") + item["name"] +
                                     (" لاستقبال المهام." if enabled else " عن استقبال مهام جديدة."), False)
                    self.refresh()
                    return
        self.notice.emit("لم يُعثر على العامل.", True)

    @Slot(str, str, str)
    def setProviderOverride(self, provider, model, variant):
        """Session-only: run every OpenCode worker on one provider, model and reasoning level."""
        data = self.config.snapshot()
        self.scheduler.set_provider_override(provider, model, variant)
        if provider:
            note = f"سيُفرض مزود {provider_info(provider, data)['name']} على عمال OpenCode."
            if variant:
                note += f" مستوى التفكير: {variant}."
            missing = [w["name"] for w in data["opencode_workers"]
                       if key_env_for(provider, data) and not self.scheduler.key_state("opencode", w, provider)]
            if missing:
                note += " بلا مفتاح حتى الآن: " + "، ".join(missing) + "."
            self.notice.emit(note, False)
        else:
            self.notice.emit("أُلغي الفرض؛ كل عامل يعود إلى إعداده الخاص.", False)
        self.refresh()

    @Slot(str, result=bool)
    def saveProvider(self, raw):
        """Add or update a user-defined provider; built-ins are never touched."""
        try:
            item = json.loads(raw)
            data = self.config.snapshot()
            entry = {"id": str(item.get("id", "")).strip(), "name": str(item.get("name", "")).strip() or str(item.get("id", "")).strip(),
                     "key_env": str(item.get("keyEnv", "")).strip(), "isolated": bool(item.get("isolated", True))}
            data["opencode_providers"] = [p for p in data["opencode_providers"] if p["id"] != entry["id"]] + [entry]
            self.config.save(data)
            self.scheduler.forget_keys()
            self.settingsChanged.emit()
            self.notice.emit("حُفظ المزود " + entry["name"] + ".", False)
            self.refresh()
            return True
        except Exception as exc:
            self.notice.emit(str(exc), True)
            return False

    @Slot(str)
    def deleteProvider(self, provider):
        try:
            data = self.config.snapshot()
            used = [w["name"] for w in data["opencode_workers"] if provider_of(w, data) == provider]
            if used:
                self.notice.emit("لا يمكن حذف مزود تستخدمه: " + "، ".join(used), True)
                return
            data["opencode_providers"] = [p for p in data["opencode_providers"] if p["id"] != provider]
            self.config.save(data)
            self.scheduler.forget_keys()
            self.settingsChanged.emit()
            self.notice.emit("حُذف المزود.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    @Slot(str)
    def resetWorker(self, worker):
        self.scheduler.disabled_workers.pop(worker, None)
        self.scheduler.forget_keys()
        self.refresh()

    @Slot()
    def refreshLibrary(self):
        """Rescan the library folders. Called when the file page opens, not on the UI timer."""
        try:
            self._library = self.scheduler.library.scan(self.config.snapshot())
        except Exception as exc:
            self._library = []
            self.notice.emit("تعذر قراءة المكتبة: " + str(exc), True)
        self.libraryChanged.emit()

    @Slot()
    def organizeLibrary(self):
        """Copy every finished artifact into the library, off the UI thread."""
        if self._library_busy:
            return
        self._library_busy = True
        self.libraryChanged.emit()
        def work():
            try:
                copied = self.scheduler.library.organize_current(self.store, self.config.snapshot())
                self.notice.emit(f"نُظّم {copied} ملفًا في المكتبة.", False)
            except Exception as exc:
                self.notice.emit("تعذر التنظيم: " + str(exc), True)
            finally:
                self._library_busy = False
                self.refreshLibrary()
        threading.Thread(target=work, daemon=True, name="library").start()

    @Slot()
    def exportWord(self):
        destination = QFileDialog.getExistingDirectory(None, "تصدير ملفات Word إلى مجلد", "")
        if not destination or self._library_busy:
            return
        self._library_busy = True
        self.libraryChanged.emit()
        def work():
            try:
                count = self.scheduler.library.export_documents(self.store.jobs(), self.config.snapshot(), destination)
                self.notice.emit(f"صُدّر {count} مستند Word إلى {destination} وفق مجلدات المواد والدروس.", False)
            except Exception as exc:
                self.notice.emit("تعذر تصدير Word: " + str(exc), True)
            finally:
                self._library_busy = False
                self.libraryChanged.emit()
        threading.Thread(target=work, daemon=True, name="word-export").start()

    @Slot(str)
    def deleteLibraryFile(self, path):
        try:
            self.scheduler.library.remove(self.config.snapshot(), path)
            self.notice.emit("حُذف الملف من المكتبة؛ النسخة الأصلية في مجلد المهمة باقية.", False)
        except Exception as exc:
            self.notice.emit(str(exc), True)
        self.refreshLibrary()

    @Slot()
    def importLegacy(self):
        path, _ = QFileDialog.getOpenFileName(None, "استيراد إعدادات السكربت", str(Path.home()), "JSON (*.json)")
        if path:
            try:
                self.config.import_legacy(Path(path), self.secrets)
                self.scheduler.disabled_workers.clear()
                self.scheduler.forget_keys()
                self.settingsChanged.emit()
                self.notice.emit("استوردت الإعدادات ومفتاح العامل الأول. أضف مفتاحًا مستقلًا للعامل الثاني.", False)
            except Exception as exc:
                self.notice.emit(str(exc), True)

    @Slot()
    def checkEnvironment(self):
        def work():
            import importlib.util
            import shutil
            messages = []
            for name in ("yt_dlp", "yt_dlp_ejs", "imageio_ffmpeg", "requests", "keyring"):
                messages.append(name + (": جاهز" if importlib.util.find_spec(name) else ": غير مثبت"))
            messages.append("JavaScript: " + (shutil.which("node") or shutil.which("deno") or "غير موجود"))
            try:
                messages.append("OpenCode: " + str(resolve_opencode(self.config.data["opencode_executable"])))
            except Exception as exc:
                messages.append(str(exc))
            self.backgroundError.emit("\n".join(messages))
        threading.Thread(target=work, daemon=True).start()

    @Slot(result=bool)
    def prepareClose(self):
        from PySide6.QtWidgets import QMessageBox
        if not self.scheduler.idle() or self._busy:
            answer = QMessageBox.question(None, "إغلاق ورق", "توجد عمليات نشطة. الإغلاق يوقف العمليات المحلية وقد يترك طلب Deepgram بنتيجة غير مؤكدة. هل تريد الإغلاق؟")
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.discovery_cancel.set()
        self.scheduler.shutdown()
        return True

    @Slot()
    def exportReport(self):
        path, _ = QFileDialog.getSaveFileName(None, "حفظ تقرير", "تقرير-المعالجة.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            wb = Workbook()
            ws = wb.active
            ws.title = "المعالجة"
            ws.sheet_view.rightToLeft = True
            ws.append(["الترتيب", "العنوان", "الحالة", "المرحلة", "الرابط", "Word", "الخطأ"])
            for j in self._jobs:
                # Do not interpret untrusted titles as spreadsheet formulas.
                values = [j["position"], j["title"], j["statusLabel"], j["stageLabel"], j["url"], j["document"], j["error"]]
                ws.append(values)
                for cell in ws[ws.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="142338")
            for col in "ABCDEFG":
                ws.column_dimensions[col].width = 35 if col != "B" else 65
            ws.freeze_panes = "A2"
            wb.save(path)
            self.notice.emit("حُفظ تقرير المعالجة.", False)
        except Exception as exc:
            self.notice.emit(str(exc), True)
