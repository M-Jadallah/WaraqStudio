from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from .config import atomic_json
from .services import safe_title

FOLDERS = {"document": "Word", "transcript": "التفريغات", "audio": "الصوت"}
MANIFEST = ".waraq-library.json"


class Library:
    """Mirror finished artifacts under subject folders, retaining stable names."""

    def __init__(self):
        self._lock = threading.RLock()

    def root(self, cfg):
        return Path(cfg["library_root"])

    def _manifest(self, root):
        path = root / MANIFEST
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (ValueError, OSError):
                pass
        return {}

    def _relative(self, job, kind):
        group = job.get("folder_name") or ""
        if group and (Path(group).name != group or group in (".", "..") or any(c in group for c in '<>:"/\\|?*')):
            raise ValueError("اسم مجلد غير صالح.")
        return Path(group) / FOLDERS[kind] if group else Path(FOLDERS[kind])

    def _old_path(self, root, kind, value):
        """Resolve old filename-only entries and new relative manifest paths."""
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        if len(candidate.parts) == 1:
            candidate = Path(FOLDERS[kind]) / candidate
        path = (root / candidate).resolve()
        return path if path.is_relative_to(root.resolve()) else None

    def _name(self, job, source, folder):
        stem = f'{job["position"]:03d}-{safe_title(job["title"])}'
        suffix = source.suffix or ".bin"
        name, index = stem + suffix, 2
        while (folder / name).exists():
            name = f"{stem} ({index}){suffix}"
            index += 1
        return name

    def publish(self, job, cfg):
        with self._lock:
            return self._publish(job, cfg)

    def publish_current(self, store, job_id, cfg, check=None):
        """Read the latest assignment while holding the mirror lock."""
        with self._lock:
            if check: check()
            job = store.get(job_id)
            return self._publish(job, cfg, check) if job else []

    def _publish(self, job, cfg, check=None):
        if check: check()
        root = self.root(cfg)
        if check: check(root)
        root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(root)
        copied, dirty = [], False
        for kind in FOLDERS:
            if check: check()
            if kind == "audio" and not cfg.get("library_copy_audio"):
                continue
            source = Path(job.get(kind) or "")
            if not source.is_file():
                continue
            folder = root / self._relative(job, kind)
            if check: check(folder)
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.resolve().is_relative_to(root.resolve()):
                raise ValueError("مسار المجلد يخرج من المكتبة.")
            key = f'{job["id"]}|{kind}'
            old = self._old_path(root, kind, manifest[key]) if manifest.get(key) else None
            name = old.name if old else self._name(job, source, folder)
            target = folder / name
            if old != target and target.exists():
                target = folder / self._name(job, source, folder)
            if check: check(target)
            needs_copy = True
            if target.exists():
                stat, origin = target.stat(), source.stat()
                needs_copy = stat.st_size != origin.st_size or stat.st_mtime < origin.st_mtime - 1
            if needs_copy:
                if check: check()
                temporary = target.with_name(target.name + ".part")
                if check: check(temporary)
                shutil.copy2(source, temporary)
                if check: check()
                os.replace(temporary, target)
                copied.append(target.name)
            if old and old != target and old.is_file():
                if check: check(old)
                old.unlink()
            relative = str(target.relative_to(root))
            if manifest.get(key) != relative:
                manifest[key] = relative
                dirty = True
        if dirty:
            if check: check(root / MANIFEST)
            atomic_json(root / MANIFEST, manifest)
        return copied

    def organize_all(self, jobs, cfg):
        return sum(len(self.publish(job, cfg)) for job in jobs)

    def organize_current(self, store, cfg, check=None):
        return sum(len(self.publish_current(store, job["id"], cfg, check)) for job in store.jobs())

    def scan(self, cfg, limit=3000):
        root = self.root(cfg)
        items = []
        if not root.is_dir():
            return items
        folders = [root / name for name in FOLDERS.values()]
        for group in root.iterdir():
            if group.is_dir() and group.name not in FOLDERS.values() and group.resolve().is_relative_to(root.resolve()):
                folders.extend(group / name for name in FOLDERS.values())
        reverse = {value: key for key, value in FOLDERS.items()}
        for folder in folders:
            if not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve()):
                continue
            for path in folder.iterdir():
                try:
                    if not path.is_file() or path.name.startswith(".") or path.suffix == ".part":
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                items.append({"name": path.name, "path": str(path), "kind": reverse[folder.name],
                              "folder": str(folder), "group": folder.parent.name if folder.parent != root else "",
                              "size": stat.st_size, "modified": stat.st_mtime})
        items.sort(key=lambda item: (item["group"].casefold(), item["name"].casefold()))
        return items[:limit]

    def remove(self, cfg, path):
        with self._lock:
            return self._remove(cfg, path)

    def _remove(self, cfg, path):
        root = self.root(cfg).resolve()
        target = Path(path)
        if not target.is_file():
            raise ValueError("الملف غير موجود في المكتبة.")
        resolved = target.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("الحذف مقصور على مجلدات المكتبة.")
        parts = resolved.relative_to(root).parts
        if not (len(parts) == 2 and parts[0] in FOLDERS.values() or
                len(parts) == 3 and parts[1] in FOLDERS.values()):
            raise ValueError("الحذف مقصور على مجلدات المكتبة.")
        resolved.unlink()
        manifest = self._manifest(root)
        pruned = {key: value for key, value in manifest.items()
                  if key.rsplit("|", 1)[-1] not in FOLDERS or
                  self._old_path(root, key.rsplit("|", 1)[-1], value) != resolved}
        if len(pruned) != len(manifest):
            atomic_json(root / MANIFEST, pruned)

    def export_documents(self, jobs, cfg, destination):
        """Copy Word files into the selected device folder, preserving subjects."""
        root = Path(destination)
        if not root.is_dir():
            raise ValueError("اختر مجلد تصدير موجودًا.")
        count = 0
        for job in jobs:
            source = Path(job.get("document") or "")
            if not source.is_file():
                continue
            folder = root / self._relative(job, "document")
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f'{job["position"]:03d}-{safe_title(job["title"])}-{job["id"]}.docx'
            temporary = target.with_name(target.name + ".part")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            count += 1
        return count
