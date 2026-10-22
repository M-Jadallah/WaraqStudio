"""Session-only source preparation, separate from the legacy phone import API.

Files and discovered videos stay here until the user confirms one complete batch.
No managed transcript copy or runnable job is created while editing the draft.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from PySide6.QtCore import QObject, Property, Qt, QUrl, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from .models import DictListModel


KINDS = ("youtube", "transcript", "audio")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")
PRESETS = ("full", "transcript", "download", "custom")
ITEM_ROLES = ("id", "video_id", "title", "position", "available", "selected", "input_path", "url", "duration")


class SourceDraft(QObject):
    changed = Signal()
    _discoveryReady = Signal(int, object, str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent or bridge)
        self._bridge = bridge
        self._kind = "youtube"
        self._template_id = self._folder_id = self._output_root = self._preset = self._url = ""
        self._loaded_url = ""
        self._rows = {kind: [] for kind in KINDS}
        self._titles = {"youtube": "", "transcript": "تفريغات TXT", "audio": "ملفات صوتية"}
        self._model = DictListModel(ITEM_ROLES, self)
        self._output_choices = []
        self._busy = self._enqueue_busy = False
        self._last_errors = {}
        self._generation = 0
        self._cancel = threading.Event()
        self._thread_lock = threading.Lock()
        self._threads = set()
        self._closing = False
        self._discoveryReady.connect(self._apply_discovery, Qt.ConnectionType.QueuedConnection)
        bridge.settingsChanged.connect(self._settings_changed)
        bridge.foldersChanged.connect(self._settings_changed)

    @Property(str, notify=changed)
    def kind(self): return self._kind

    @Property(str, notify=changed)
    def templateId(self): return self._template_id

    @Property(str, notify=changed)
    def folderId(self): return self._folder_id

    @Property(str, notify=changed)
    def outputRoot(self): return self._output_root

    @Property(str, notify=changed)
    def preset(self): return self._preset

    @Property(str, notify=changed)
    def url(self): return self._url

    @Property(QObject, constant=True)
    def itemsModel(self): return self._model

    @Property("QVariantList", notify=changed)
    def items(self): return [dict(row) for row in self._rows[self._kind]]

    @Property(int, notify=changed)
    def itemCount(self): return len(self._rows[self._kind])

    @Property(int, notify=changed)
    def selectedCount(self):
        return sum(bool(row["selected"] and row["available"]) for row in self._rows[self._kind])

    @Property(str, notify=changed)
    def title(self): return self._titles[self._kind]

    @Property(bool, notify=changed)
    def busy(self): return self._busy

    @Property(bool, notify=changed)
    def enqueueBusy(self): return self._enqueue_busy

    @Property(bool, notify=changed)
    def canEnqueue(self):
        return not self._busy and not self._enqueue_busy and not self._validation_errors()

    @Property("QVariantMap", notify=changed)
    def fieldErrors(self):
        # Empty initial choices use neutral placeholders. A choice the user
        # already made that becomes invalid must immediately show its own error,
        # including after another unrelated field has been edited.
        current = self._validation_errors()
        chosen = {"templateId": self._template_id, "folderId": self._folder_id, "outputRoot": self._output_root}
        invalid = {key: current[key] for key, value in chosen.items() if value and key in current}
        return {**invalid, **self._last_errors}

    @Property(str, notify=changed)
    def validationMessage(self):
        if self._enqueue_busy:
            return "تُضاف العناصر إلى الطابور…"
        if self._busy:
            return "انتظر انتهاء قراءة مصدر YouTube أو ألغِ القراءة."
        errors = {**self._validation_errors(), **self._last_errors}
        return next(iter(errors.values()), "الإعدادات مكتملة؛ يمكنك إضافة العناصر المحددة إلى الطابور.")

    @Property("QVariantList", notify=changed)
    def outputChoices(self):
        saved = str(self._bridge.config.snapshot().get("output_root", "") or "")
        values = ([saved] if saved else []) + self._output_choices
        seen, result = set(), []
        for value in values:
            canonical = self._output_key(value)
            if canonical in seen:
                continue
            seen.add(canonical)
            label = ("افتراضي التطبيق — " if value == saved else "") + value
            result.append({"id": value, "name": label})
        return result

    @staticmethod
    def _output_key(value):
        try:
            return os.path.normcase(str(Path(value).expanduser().resolve()))
        except (OSError, ValueError):
            return os.path.normcase(str(value))

    def _publish(self):
        self._model.update_items([dict(row) for row in self._rows[self._kind]])
        self.changed.emit()

    def _clear_errors(self):
        self._last_errors = {}

    @Slot()
    def _settings_changed(self):
        # Identifiers remain the source of truth; never select the first row
        # just because a ComboBox's options have been refreshed.
        self._clear_errors()
        self.changed.emit()

    @Slot(str)
    def setKind(self, value):
        if value in KINDS and not self._enqueue_busy and self._kind != value:
            self._kind = value
            self._clear_errors()
            self._publish()

    @Slot(str)
    def setTemplateId(self, value):
        if not self._enqueue_busy:
            self._template_id = str(value or "")
            self._clear_errors()
            self.changed.emit()

    @Slot(str)
    def setFolderId(self, value):
        if not self._enqueue_busy:
            self._folder_id = str(value or "")
            self._clear_errors()
            self.changed.emit()

    @Slot(str)
    def setOutputRoot(self, value):
        if self._enqueue_busy:
            return
        self._output_root = str(value or "").strip()
        if self._output_root:
            canonical = self._output_key(self._output_root)
            # Keep the displayed choice's identifier when the user browses to
            # the same directory with another spelling/case or a symlink.
            self._output_root = next((item["id"] for item in self.outputChoices
                                      if self._output_key(item["id"]) == canonical), self._output_root)
        if self._output_root and self._output_root not in self._output_choices:
            self._output_choices.append(self._output_root)
        self._clear_errors()
        self.changed.emit()

    @Slot(str)
    def setPreset(self, value):
        if not self._enqueue_busy:
            self._preset = value if value in PRESETS else ""
            self._clear_errors()
            self.changed.emit()

    @Slot(str)
    def setUrl(self, value):
        if not self._enqueue_busy:
            value = str(value or "")
            if value != self._url:
                if self._busy:
                    self.cancelDiscovery()
                self._url = value
                self._clear_errors()
                self.changed.emit()

    @Slot()
    def chooseOutputFolder(self):
        if self._enqueue_busy:
            return
        value = QFileDialog.getExistingDirectory(None, "اختيار مجلد النتائج", self._output_root)
        if value:
            self.setOutputRoot(value)

    @Slot()
    def chooseFiles(self):
        if self._enqueue_busy or self._kind == "youtube":
            return
        kind = self._kind
        caption, pattern = (("اختيار ملفات التفريغ", "تفريغات TXT (*.txt *.TXT)") if kind == "transcript" else
                            ("اختيار ملفات صوتية", "ملفات صوتية (*.mp3 *.wav *.m4a *.flac *.ogg)"))
        paths, _ = QFileDialog.getOpenFileNames(None, caption, "", pattern)
        self.stagePaths(paths, kind)

    @staticmethod
    def _natural_order(path):
        return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                     for part in re.split(r"(\d+)", path.name))

    @Slot()
    def chooseFolder(self):
        if self._enqueue_busy or self._kind != "transcript":
            return
        value = QFileDialog.getExistingDirectory(None, "اختيار مجلد التفريغات", "")
        if not value:
            return
        try:
            paths = sorted((path for path in Path(value).iterdir() if path.is_file() and path.suffix.lower() == ".txt"),
                           key=lambda path: (self._natural_order(path), path.name.casefold(), path.name))
            if not paths:
                self._bridge.notice.emit("لا توجد ملفات TXT مباشرة في المجلد المحدد.", True)
                return
            self.stagePaths(paths, "transcript")
        except OSError as exc:
            self._bridge.notice.emit("تعذر قراءة مجلد التفريغات: " + str(exc), True)

    @staticmethod
    def _local_path(value, kind):
        path = Path(value).resolve(strict=True)
        if not path.is_file():
            raise ValueError("المسار ليس ملفًا.")
        extensions = (".txt",) if kind == "transcript" else AUDIO_EXTENSIONS
        if path.suffix.lower() not in extensions:
            raise ValueError("صيغة غير مدعومة؛ اختر ملف TXT." if kind == "transcript" else "صيغة الصوت غير مدعومة.")
        if kind == "transcript":
            if not path.read_text(encoding="utf-8-sig").strip():
                raise ValueError("ملف التفريغ فارغ.")
        else:
            with path.open("rb") as source:
                if not source.read(1):
                    raise ValueError("الملف الصوتي فارغ.")
        return path

    @Slot("QVariantList", str, result=int)
    def stagePaths(self, paths, kind):
        if kind not in ("transcript", "audio") or self._enqueue_busy:
            return 0
        rows = self._rows[kind]
        seen = {os.path.normcase(row["input_path"]) for row in rows}
        added, errors = 0, []
        for value in paths:
            try:
                path = self._local_path(value, kind)
                canonical = os.path.normcase(str(path))
                if canonical in seen:
                    continue
                seen.add(canonical)
                rows.append({"id": uuid.uuid4().hex, "video_id": "local-" + path.stem,
                             "title": path.stem, "position": len(rows) + 1, "input_path": str(path),
                             "url": "", "duration": 0, "available": True, "selected": True})
                added += 1
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                errors.append(f"تعذر تجهيز «{Path(str(value)).name}»: {exc}")
        self._clear_errors()
        self._publish()
        if errors:
            self._bridge.notice.emit("\n".join(dict.fromkeys(errors)), True)
        return added

    @Slot("QVariantList", result=int)
    def stageDroppedUrls(self, urls):
        if self._enqueue_busy:
            return 0
        paths, errors = [], []
        for value in urls:
            url = value if isinstance(value, QUrl) else QUrl(str(value))
            if not url.isLocalFile() or url.hasQuery() or url.hasFragment():
                errors.append("يُقبل سحب ملفات TXT المحلية فقط.")
            else:
                paths.append(url.toLocalFile())
        if errors:
            self._bridge.notice.emit("\n".join(dict.fromkeys(errors)), True)
        count = self.stagePaths(paths, "transcript")
        if count:
            self.setKind("transcript")
        return count

    @Slot(int, bool)
    def selectItem(self, index, selected):
        rows = self._rows[self._kind]
        if not self._enqueue_busy and 0 <= index < len(rows):
            rows[index]["selected"] = bool(selected and rows[index]["available"])
            self._clear_errors()
            self._publish()

    @Slot(str)
    def selectRange(self, spec):
        if self._enqueue_busy:
            return
        spec = spec.strip()
        wanted = set()
        try:
            if spec not in ("all", "none"):
                for part in spec.replace("،", ",").split(","):
                    bits = [int(value.strip()) for value in part.split("-")]
                    if len(bits) == 1 and bits[0] > 0:
                        wanted.add(bits[0])
                    elif len(bits) == 2 and 0 < bits[0] <= bits[1] <= 100000:
                        wanted.update(range(bits[0], bits[1] + 1))
                    else:
                        raise ValueError()
            for row in self._rows[self._kind]:
                row["selected"] = bool(row["available"] and (spec == "all" or row["position"] in wanted))
            self._clear_errors()
            self._publish()
        except ValueError:
            self._bridge.notice.emit("اكتب نطاقًا مثل 1-10,15,20-25", True)

    @Slot(int, int)
    def moveItem(self, index, direction):
        rows = self._rows[self._kind]
        target = index + direction
        if not self._enqueue_busy and 0 <= index < len(rows) and 0 <= target < len(rows):
            rows[index], rows[target] = rows[target], rows[index]
            self._publish()

    @Slot(str)
    @Slot(str, bool)
    def discover(self, url, refresh=False):
        if self._closing or self._enqueue_busy:
            return
        self._cancel.set()
        self._generation += 1
        generation = self._generation
        self._cancel = cancel = threading.Event()
        self._url = str(url or "").strip()
        if not self._url:
            self._busy = False
            self._last_errors = {"items": "أدخل رابط YouTube لقراءة المصدر."}
            self._publish()
            return
        self._busy = True
        self._clear_errors()
        # Keep the previous review visible if the new read fails or is
        # cancelled. Its loaded URL is retained separately, so it cannot be
        # submitted while the input names a different source.
        self._publish()
        cfg = self._bridge.config.snapshot()
        source_url = self._url
        youtube = self._bridge.scheduler.youtube

        def work():
            data, error = None, ""
            try:
                data = youtube.discover(source_url, cfg, cancel, refresh)
            except Exception as exc:
                error = str(exc)
            finally:
                with self._thread_lock:
                    self._threads.discard(threading.current_thread())
                    if not self._closing and not cancel.is_set():
                        self._discoveryReady.emit(generation, data, error)

        thread = threading.Thread(target=work, daemon=True, name="source-draft-discovery")
        with self._thread_lock:
            self._threads.add(thread)
        thread.start()

    @Slot(int, object, str)
    def _apply_discovery(self, generation, data, error):
        if self._closing or generation != self._generation:
            return
        self._busy = False
        if error:
            self._last_errors = {"items": "تعذر قراءة المصدر: " + error}
            self._bridge.notice.emit(self._last_errors["items"], True)
        elif not isinstance(data, dict) or not isinstance(data.get("items"), list):
            self._last_errors = {"items": "لم يُرجع المصدر قائمة عناصر صالحة."}
            self._bridge.notice.emit(self._last_errors["items"], True)
        else:
            rows = []
            for index, item in enumerate(data["items"], 1):
                row = dict(item)
                row.update(id=uuid.uuid4().hex, input_path="")
                row.setdefault("video_id", "")
                row.setdefault("title", row["video_id"])
                row.setdefault("position", index)
                row.setdefault("url", "")
                row.setdefault("duration", 0)
                row["available"] = bool(row.get("available", True))
                row["selected"] = bool(row["available"] and row.get("selected", True))
                rows.append(row)
            self._rows["youtube"] = rows
            self._titles["youtube"] = str(data.get("title", "YouTube"))
            self._loaded_url = self._url
            self._clear_errors()
        self._publish()

    @Slot()
    def cancelDiscovery(self):
        self._cancel.set()
        self._generation += 1
        self._busy = False
        self._clear_errors()
        self.changed.emit()

    def _validation_errors(self):
        errors = {}
        cfg = self._bridge.config.snapshot()
        if not self._template_id:
            errors["templateId"] = "اختر قالب البرومبت."
        elif not any(template["id"] == self._template_id for template in cfg.get("opencode_templates", [])):
            errors["templateId"] = "القالب المحدد لم يعد موجودًا؛ اختر قالبًا آخر."
        if not self._folder_id:
            errors["folderId"] = "اختر مجلد المادة / الدرس."
        elif not any(folder["id"] == self._folder_id for folder in self._bridge.store.folders()):
            errors["folderId"] = "مجلد المادة المحدد لم يعد موجودًا؛ اختر مجلدًا آخر."
        if not self._output_root:
            errors["outputRoot"] = "اختر مجلد النتائج."
        else:
            try:
                path = Path(self._output_root).expanduser().resolve()
                ancestor = path
                while not ancestor.exists() and ancestor != ancestor.parent:
                    ancestor = ancestor.parent
                if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
                    errors["outputRoot"] = "مجلد النتائج غير قابل للكتابة."
            except (OSError, ValueError):
                errors["outputRoot"] = "مسار مجلد النتائج غير صالح."
        if self._kind == "youtube" and self._preset not in PRESETS:
            errors["preset"] = "اختر مراحل المعالجة المطلوبة."
        elif self._kind == "youtube" and self._preset == "custom":
            if not cfg["download_enabled"]:
                errors["preset"] = "فعّل التنزيل في الإعدادات المخصصة لمصدر YouTube."
            elif cfg["opencode_enabled"] and not cfg["deepgram_enabled"]:
                errors["preset"] = "فعّل التفريغ في الإعدادات المخصصة لإنشاء Word."
        if self._kind == "youtube" and self._rows["youtube"] and self._url.strip() != self._loaded_url:
            errors["source"] = "اقرأ الرابط الحالي قبل إضافة عناصره إلى الطابور."
        if not self.selectedCount:
            errors["items"] = "حدد عنصرًا صالحًا واحدًا على الأقل لإضافته."
        return errors

    @Slot(result="QVariantMap")
    def enqueue(self):
        if self._closing or self._enqueue_busy:
            return {"ok": False, "count": 0, "message": "تجري إضافة الدفعة بالفعل."}
        errors = self._validation_errors()
        if self._busy:
            errors["items"] = "انتظر انتهاء قراءة المصدر أو ألغِ القراءة قبل الإضافة."
        if errors:
            self._last_errors = errors
            message = next(iter(errors.values()))
            self.changed.emit()
            self._bridge.notice.emit(message, True)
            return {"ok": False, "count": 0, "message": message}
        self._enqueue_busy = True
        self._clear_errors()
        self.changed.emit()
        error_field = "batch"
        try:
            cfg = copy.deepcopy(self._bridge.config.snapshot())
            template = next(template for template in cfg["opencode_templates"] if template["id"] == self._template_id)
            cfg["prompt"], cfg["template_name"] = template["text"], template["name"]
            output = Path(self._output_root).expanduser().resolve()
            cfg["output_root"] = str(output)
            if self._kind == "youtube":
                if self._preset != "custom":
                    cfg["download_enabled"] = True
                    cfg["deepgram_enabled"] = self._preset != "download"
                    cfg["opencode_enabled"] = self._preset == "full"
                if not cfg["download_enabled"]:
                    raise ValueError("مصدر YouTube يحتاج تفعيل التنزيل في الإعدادات المخصصة.")
                if cfg["opencode_enabled"] and not cfg["deepgram_enabled"]:
                    raise ValueError("إنشاء Word يحتاج تفعيل التفريغ في الإعدادات المخصصة.")
            else:
                cfg["download_enabled"] = False
                cfg["deepgram_enabled"] = self._kind == "audio"
                cfg["opencode_enabled"] = True
            items = [dict(row) for row in self._rows[self._kind] if row["selected"] and row["available"]]
            # Validate every selected input before Store starts its atomic insert
            # and managed TXT copy transaction. A partial batch is never added.
            input_errors = []
            for row in items:
                if self._kind != "youtube":
                    try:
                        self._local_path(row["input_path"], self._kind)
                    except (OSError, UnicodeError, ValueError) as exc:
                        input_errors.append(f"«{row['title']}»: {exc}")
            if input_errors:
                error_field = "items"
                raise ValueError("تعذر إضافة الدفعة؛ راجع الملفات المحددة:\n" + "\n".join(input_errors))
            error_field = "outputRoot"
            output.mkdir(parents=True, exist_ok=True)
            # os.access is advisory (especially on Windows ACLs). Confirm actual
            # write access without leaving a marker or copying any user input.
            with tempfile.TemporaryFile(dir=output) as probe:
                probe.write(b"waraq")
                probe.flush()
            error_field = "batch"
            added = self._bridge.store.add(items, cfg, folder_id=self._folder_id)
            count = len(added)
        except Exception as exc:
            message = "تعذر إضافة الدفعة إلى الطابور: " + str(exc)
            notes = getattr(exc, "__notes__", ())
            if notes:
                message += "\n" + "\n".join(notes)
            self._last_errors = {error_field: message}
            self._enqueue_busy = False
            self.changed.emit()
            self._bridge.notice.emit(message, True)
            return {"ok": False, "count": 0, "message": message}
        self._enqueue_busy = False
        self.reset()
        self._bridge.refresh()
        message = f"أضيفت {count} مهمة إلى الطابور. اضغط تشغيل لبدء المعالجة."
        self._bridge.notice.emit(message, False)
        return {"ok": True, "count": count, "message": message}

    @Slot()
    def reset(self):
        if self._enqueue_busy:
            return
        self.cancelDiscovery()
        self._kind = "youtube"
        self._template_id = self._folder_id = self._output_root = self._preset = self._url = ""
        self._loaded_url = ""
        self._rows = {kind: [] for kind in KINDS}
        self._titles = {"youtube": "", "transcript": "تفريغات TXT", "audio": "ملفات صوتية"}
        self._clear_errors()
        self._publish()

    def close(self):
        """Invalidate queued replies and cooperatively stop outstanding reads."""
        self._cancel.set()
        with self._thread_lock:
            self._closing = True
            threads = tuple(self._threads)
        self._generation += 1
        self._busy = False
        # Discovery uses cancellable subprocess/network checkpoints. Never hold
        # the UI indefinitely if a third-party network call ignores cancellation.
        deadline = time.monotonic() + 0.2
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
