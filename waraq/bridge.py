from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from . import __version__
from .config import defaults
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

    def __init__(self, store, config, secrets, scheduler):
        super().__init__()
        self.store, self.config, self.secrets, self.scheduler = store, config, secrets, scheduler
        self._jobs, self._workers, self._logs, self._source = [], [], [], []
        self._running = False
        self._source_title = ""
        self._busy = False
        self.discovery_cancel = threading.Event()
        self.sourceReady.connect(self._accept_source)
        self.backgroundError.connect(self._error)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.refresh()

    @Property("QVariantList", notify=changed)
    def jobs(self): return self._jobs

    @Property("QVariantList", notify=changed)
    def workers(self): return self._workers

    @Property("QVariantList", notify=changed)
    def logs(self): return self._logs

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
                "steps": [{"name": name, "label": STAGE_LABELS[name], "status": stages[name]["status"]} for name in STAGES],
            })
        workers = self.scheduler.workers()
        for w in workers:
            w["stageLabel"] = STAGE_LABELS[w["stage"]]
        logs = [{**r, "time": time.strftime("%H:%M:%S", time.localtime(r["ts"]))} for r in self.store.events(100)]
        running = self.scheduler.running
        # The timer fires while idle too; republishing identical state would
        # make QML re-evaluate every binding for nothing.
        if result == self._jobs and workers == self._workers and logs == self._logs and running == self._running:
            return
        self._jobs, self._workers, self._logs, self._running = result, workers, logs, running
        self.changed.emit()

    @Slot(str)
    def _accept_source(self, raw):
        data = json.loads(raw)
        self._source = data["items"]
        self._source_title = data["title"]
        self._busy = False
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
            self.sourceChanged.emit()
        except ValueError:
            self.notice.emit("اكتب نطاقًا مثل 1-10,15,20-25", True)

    @Slot(int, int)
    def moveSource(self, index, direction):
        target = index + direction
        if 0 <= target < len(self._source):
            self._source[index], self._source[target] = self._source[target], self._source[index]
            self.sourceChanged.emit()

    @Slot(str)
    def enqueue(self, preset):
        try:
            cfg = self.config.snapshot()
            if preset != "custom":
                cfg["download_enabled"] = True
                cfg["deepgram_enabled"] = preset != "download"
                cfg["opencode_enabled"] = preset == "full"
            selected = [dict(row) for row in self._source if row["selected"]]
            if not selected:
                raise ValueError("حدد فيديو واحدًا على الأقل.")
            # Preserve original lesson numbers; Store saves the visual queue order separately.
            added = self.store.add(selected, cfg)
            self.notice.emit(f"أضيفت {len(added)} مهمة. اضغط تشغيل لبدء المعالجة.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    @Slot()
    def importLocal(self):
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
                                 "url": "", "available": True}], cfg, input_path=str(path))
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

    @Slot(str, bool)
    def pauseStage(self, name, paused):
        self.scheduler.pause_stage(name, paused)
        self.refresh()

    @Slot(str)
    def cancelJob(self, ident):
        self.scheduler.cancel_job(ident)
        self.refresh()

    @Slot(str, str, bool)
    def retry(self, ident, stage, rerun):
        try:
            self.store.retry(ident, stage, rerun)
            if not rerun:
                job = self.store.get(ident)
                for name in STAGES[STAGES.index(stage) + 1:]:
                    if job["stages"][name]["status"] == "cancelled":
                        self.store.set_stage(ident, name, "pending")
            self.notice.emit("المهمة جاهزة للمحاولة. شغّل الطابور إذا كان متوقفًا.", False)
            self.refresh()
        except Exception as exc:
            self.notice.emit(str(exc), True)

    @Slot(str)
    def openPath(self, path):
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
        else:
            self.notice.emit("الملف أو المجلد غير موجود بعد.", True)

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
            self.settingsChanged.emit()
            self.notice.emit("حُفظت الإعدادات. المهام الجديدة تستخدم إعدادات المعالجة الجديدة.", False)
            return True
        except Exception as exc:
            self.notice.emit(str(exc), True)
            return False

    @Slot(str, str)
    def saveKey(self, worker, key):
        try:
            self.secrets.set(worker, key)
            self.scheduler.disabled_workers.pop(worker, None)
            self.notice.emit("حُفظ المفتاح في مخزن بيانات اعتماد Windows.", False)
        except Exception as exc:
            self.notice.emit("تعذر حفظ المفتاح: " + str(exc), True)

    @Slot(str)
    def resetWorker(self, worker):
        self.scheduler.disabled_workers.pop(worker, None)
        self.refresh()

    @Slot()
    def importLegacy(self):
        path, _ = QFileDialog.getOpenFileName(None, "استيراد إعدادات السكربت", str(Path.home()), "JSON (*.json)")
        if path:
            try:
                self.config.import_legacy(Path(path), self.secrets)
                self.scheduler.disabled_workers.clear()
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
