"""Download a verified release installer without replacing a running application."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

REPOSITORY = "M-Jadallah/WaraqStudio"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
TOKEN_KEY = "github-release-download"
MAX_INSTALLER_SIZE = 1_000_000_000


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


def version_tuple(value):
    value = str(value).removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){2,3}", value):
        raise UpdateError("صيغة إصدار التطبيق غير معروفة.")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def _session(token):
    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json", "User-Agent": "WaraqStudio"})
    if token:
        session.headers["Authorization"] = "Bearer " + token
    return session


def _check_response(response):
    if response.status_code == 404:
        raise UpdateError("تعذر الوصول إلى الإصدار. إذا كان المستودع خاصًا، احفظ رمز GitHub له صلاحية قراءة محتوياته؛ وقد لا يوجد إصدار منشور بعد.")
    if response.status_code == 401:
        raise UpdateError("رفض GitHub الرمز. راجع صلاحيته ثم أعد المحاولة.")
    if response.status_code in (403, 429):
        raise UpdateError("رفض GitHub الطلب بسبب الصلاحيات أو حد الطلبات. راجع صلاحيات الرمز أو حاول لاحقًا.")
    if not response.ok:
        raise UpdateError(f"تعذر الاتصال بإصدارات GitHub (HTTP {response.status_code}).")


def _safe_url(value, api=False):
    parsed = urlsplit(str(value))
    prefix = f"/repos/{REPOSITORY}/releases/assets/" if api else f"/{REPOSITORY}/releases/download/"
    host = "api.github.com" if api else "github.com"
    if (parsed.scheme != "https" or parsed.netloc != host or not parsed.path.startswith(prefix)
            or parsed.query or parsed.fragment):
        raise UpdateError("رابط ملف التحديث لا ينتمي إلى مستودع التطبيق.")
    return str(value)


def check_latest(current_version, token=""):
    try:
        with _session(token) as session:
            response = session.get(RELEASE_API, timeout=(15, 30))
            _check_response(response)
            release = response.json()
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise UpdateError("لم يُعثر على إصدار مستقر منشور.")
        version = str(release.get("tag_name", "")).removeprefix("v")
        available = version_tuple(version) > version_tuple(current_version)
        if not available:
            return {"available": False, "version": version}
        name = f"WaraqStudio-{version}-setup.exe"
        asset = next((item for item in release.get("assets", [])
                      if item.get("name") == name and item.get("state") == "uploaded"), None)
        if not asset:
            raise UpdateError("الإصدار الجديد لا يحتوي مُثبّت Windows جاهزًا. حاول بعد اكتمال النشر.")
        digest = str(asset.get("digest", ""))
        size = asset.get("size")
        if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
            raise UpdateError("الإصدار لا يحتوي بصمة SHA256 صالحة؛ تعذر اعتماد التنزيل.")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_INSTALLER_SIZE:
            raise UpdateError("حجم ملف التحديث غير صالح.")
        return {"available": True, "version": version, "name": name, "size": size,
                "sha256": digest.split(":", 1)[1].lower(),
                "url": _safe_url(asset.get("browser_download_url")),
                "api_url": _safe_url(asset.get("url"), api=True)}
    except UpdateError:
        raise
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        # Never surface a request URL/header or a private credential in a notice.
        raise UpdateError("تعذر فحص التحديث. تحقق من الاتصال بالإنترنت ثم أعد المحاولة.") from None


def _checkpoint(cancel):
    if cancel.is_set():
        raise UpdateCancelled("أُلغي تنزيل التحديث.")


def download_installer(home, release, cancel, progress=lambda value: None, token=""):
    version_tuple(release["version"])
    if release["name"] != f"WaraqStudio-{release['version']}-setup.exe":
        raise UpdateError("اسم ملف التحديث غير صالح.")
    expected = str(release["sha256"])
    size = release["size"]
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or not 0 < size <= MAX_INSTALLER_SIZE:
        raise UpdateError("بيانات التحقق من التحديث غير صالحة.")
    url = _safe_url(release["api_url"], api=True) if token else _safe_url(release["url"])
    folder = Path(home) / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / release["name"]
    fd, name = tempfile.mkstemp(prefix=".update-", suffix=".part", dir=folder)
    temporary = Path(name)
    try:
        _checkpoint(cancel)
        file = os.fdopen(fd, "wb")
        fd = None
        with file, _session(token) as session:
            # The private asset API redirects to a signed download URL. Requests
            # strips Authorization when the redirect crosses to the asset host.
            with session.get(url, headers={"Accept": "application/octet-stream"},
                             stream=True, timeout=(15, 45)) as response:
                _check_response(response)
                hasher, received, last_notice = hashlib.sha256(), 0, 0.0
                for chunk in response.iter_content(1024 * 1024):
                    _checkpoint(cancel)
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > size:
                        raise UpdateError("حجم التنزيل لا يطابق ملف الإصدار؛ أُلغي التحديث.")
                    file.write(chunk)
                    hasher.update(chunk)
                    now = time.monotonic()
                    if now - last_notice >= 0.25:
                        progress(min(99, int(received * 100 / size)))
                        last_notice = now
                _checkpoint(cancel)
                if received != size or hasher.hexdigest() != expected:
                    raise UpdateError("ملف التحديث ناقص أو بصمته لا تطابق الإصدار؛ أُلغي التحديث.")
            file.flush()
            os.fsync(file.fileno())
        with temporary.open("rb") as file:
            if file.read(2) != b"MZ":
                raise UpdateError("ملف التحديث ليس مُثبّت Windows صالحًا.")
        _checkpoint(cancel)
        os.replace(temporary, target)
        progress(100)
        return target
    except requests.RequestException:
        raise UpdateError("انقطع تنزيل التحديث. تحقق من الاتصال وأعد المحاولة.") from None
    except OSError:
        raise UpdateError("تعذر حفظ التحديث. تحقق من المساحة وصلاحية مجلد بيانات التطبيق.") from None
    finally:
        # A cancellation before fdopen must also close the temporary descriptor.
        if fd is not None:
            os.close(fd)
        failure = sys.exception()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            if failure is not None:
                failure.add_note("تعذر تنظيف ملف التحديث الجزئي في مجلد بيانات التطبيق.")
            else:
                raise UpdateError("تعذر تنظيف ملف التحديث الجزئي في مجلد بيانات التطبيق.") from None


def verify_installer(path, release):
    path = Path(path)
    if not path.is_file() or path.name != release["name"] or path.stat().st_size != release["size"]:
        raise UpdateError("ملف التحديث غير موجود أو تغيّر؛ نزّله مرة أخرى.")
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != release["sha256"]:
        raise UpdateError("تغيّر ملف التحديث بعد تنزيله؛ نزّله مرة أخرى.")
