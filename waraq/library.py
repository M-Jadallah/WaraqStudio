from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .config import atomic_json
from .services import safe_title

# Type folders inside the library root, in the order the file page lists them.
FOLDERS = {"document": "Word", "transcript": "التفريغات", "audio": "الصوت"}
MANIFEST = ".waraq-library.json"


class Library:
    """Readable mirror of finished artifacts, grouped by type.

    Job folders stay the source of truth (one opaque id per job); this mirror is what a
    person browses. The manifest pins the name chosen for every (job, kind) pair, so
    re-publishing updates one file instead of scattering copies when two lessons happen
    to share a title.
    """

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

    def _name(self, job, source, folder):
        stem = f'{job["position"]:03d}-{safe_title(job["title"])}'
        suffix = source.suffix or ".bin"
        name = stem + suffix
        index = 2
        while (folder / name).exists():
            name = f"{stem} ({index}){suffix}"
            index += 1
        return name

    def publish(self, job, cfg):
        """Copy this job's artifacts into the library; returns the file names written."""
        root = self.root(cfg)
        root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(root)
        copied, dirty = [], False
        for kind, folder_name in FOLDERS.items():
            if kind == "audio" and not cfg.get("library_copy_audio"):
                continue
            source = Path(job.get(kind) or "")
            if not source.is_file():
                continue
            folder = root / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            key = f'{job["id"]}|{kind}'
            target = folder / manifest[key] if manifest.get(key) else None
            if target is None:
                target = folder / self._name(job, source, folder)
            if target.exists():
                stat, origin = target.stat(), source.stat()
                # copy2 keeps the source timestamp, so an unchanged source is detected here.
                if stat.st_size == origin.st_size and stat.st_mtime >= origin.st_mtime - 1:
                    continue
            temporary = target.with_name(target.name + ".part")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            copied.append(target.name)
            if manifest.get(key) != target.name:
                manifest[key] = target.name
                dirty = True
        if dirty:
            atomic_json(root / MANIFEST, manifest)
        return copied

    def organize_all(self, jobs, cfg):
        """Publish every job that has artifacts; returns the number of files written."""
        total = 0
        for job in jobs:
            total += len(self.publish(job, cfg))
        return total

    def scan(self, cfg, limit=3000):
        """Files currently in the library folders, newest first."""
        root = self.root(cfg)
        items = []
        for kind, folder_name in FOLDERS.items():
            folder = root / folder_name
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                try:
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                items.append({"name": path.name, "path": str(path), "kind": kind, "folder": str(folder),
                              "size": stat.st_size, "modified": stat.st_mtime})
        items.sort(key=lambda item: item["modified"], reverse=True)
        return items[:limit]

    def remove(self, cfg, path):
        """Delete one library file. Anything outside the library folders is refused."""
        root = self.root(cfg).resolve()
        target = Path(path)
        if not target.is_file():
            raise ValueError("الملف غير موجود في المكتبة.")
        resolved = target.resolve()
        allowed = {(root / name).resolve() for name in FOLDERS.values()}
        if resolved.parent not in allowed:
            raise ValueError("الحذف مقصور على مجلدات المكتبة.")
        resolved.unlink()
        manifest = self._manifest(root)
        pruned = {key: value for key, value in manifest.items() if value != resolved.name}
        if len(pruned) != len(manifest):
            atomic_json(root / MANIFEST, pruned)
