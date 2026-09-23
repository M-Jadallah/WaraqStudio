"""Install the official stable yt-dlp Windows binary into app data.

The packaged app cannot safely replace an imported Python module in its own
PyInstaller archive. An updated official binary is used by Youtube on later
requests, while the bundled Python package remains a working fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import requests


RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"


def binary_path(home):
    return Path(home) / "tools" / "yt-dlp.exe"


def binary_version(home):
    binary = binary_path(home)
    if not binary.is_file():
        return ""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                            timeout=15, creationflags=flags)
    if result.returncode or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?", result.stdout.strip()):
        raise ValueError("تعذر التحقق من نسخة yt-dlp الموجودة.")
    return result.stdout.strip()


def update(home):
    """Fetch stable exe and published SHA256, verify, then atomically replace."""
    if os.name != "nt":
        raise RuntimeError("التحديث من داخل التطبيق متاح لحزمة Windows فقط.")
    with requests.Session() as session:
        session.headers.update({"Accept": "application/vnd.github+json", "User-Agent": "WaraqStudio"})
        response = session.get(RELEASE_API, timeout=(15, 45))
        response.raise_for_status()
        release = response.json()
        assets = {item["name"]: item["browser_download_url"] for item in release.get("assets", [])}
        if not {"yt-dlp.exe", "SHA2-256SUMS"}.issubset(assets):
            raise RuntimeError("الإصدار الرسمي لا يحتوي الملف التنفيذي وبصمته.")
        sums = session.get(assets["SHA2-256SUMS"], timeout=(15, 45))
        sums.raise_for_status()
        match = re.search(r"(?m)^([a-fA-F0-9]{64})\s+\*?yt-dlp\.exe\s*$", sums.text)
        if not match:
            raise RuntimeError("تعذر العثور على بصمة yt-dlp.exe الرسمية.")
        expected = match.group(1).lower()
        target = binary_path(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("yt-dlp.new.exe")
        hasher, size = hashlib.sha256(), 0
        try:
            with session.get(assets["yt-dlp.exe"], stream=True, timeout=(15, 60)) as download:
                download.raise_for_status()
                with temporary.open("wb") as file:
                    for chunk in download.iter_content(1024 * 1024):
                        if chunk:
                            size += len(chunk)
                            if size > 100_000_000:
                                raise RuntimeError("حجم حزمة yt-dlp أكبر من المتوقع.")
                            hasher.update(chunk)
                            file.write(chunk)
            if hasher.hexdigest() != expected:
                raise RuntimeError("بصمة حزمة yt-dlp لا تطابق البصمة الرسمية؛ أُلغي التحديث.")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            checked = subprocess.run([str(temporary), "--version"], capture_output=True, text=True,
                                     timeout=20, creationflags=flags)
            version = checked.stdout.strip()
            if checked.returncode or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?", version):
                raise RuntimeError("تعذر تشغيل نسخة yt-dlp الجديدة؛ أُلغي التحديث.")
            os.replace(temporary, target)
            return version
        finally:
            temporary.unlink(missing_ok=True)
