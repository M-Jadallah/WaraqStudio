from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from .config import atomic_json, compose_model, key_env_for, provider_isolated, provider_of
from .store import fingerprint


class ServiceError(Exception):
    def __init__(self, message, kind="failed", retry_after=0):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class Cancelled(ServiceError):
    def __init__(self):
        super().__init__("أُلغيت المهمة؛ الملفات المكتملة محفوظة.", "cancelled")


def checkpoint(cancel):
    if cancel.is_set():
        raise Cancelled()


def wait_cancel(cancel, seconds):
    if cancel.wait(seconds):
        raise Cancelled()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_title(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip().rstrip(". ")[:55] or "درس"


LIMIT_MESSAGE = "بلغ OpenCode حد استخدام اشتراك ChatGPT (نافذة الخمس ساعات)، ولم يُنتج الملف بعد."


def worker_env(worker, provider, secret, cfg):
    """Environment overrides for one OpenCode worker.

    A worker that authenticates with a key also gets its own OpenCode data and state
    directories: five parallel runs would otherwise share one SQLite database, and the
    login directory must stay untouched so a subscription session is never lost.
    """
    env = {}
    if provider_isolated(provider, cfg) and cfg.get("opencode_data_root"):
        base = Path(cfg["opencode_data_root"]) / worker["id"]
        env["XDG_DATA_HOME"] = str(base / "data")
        env["XDG_STATE_HOME"] = str(base / "state")
        # Parallel workers must not race an auto-update over one shared binary.
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    name = key_env_for(provider, cfg)
    if name and secret:
        env[name] = secret
    return env


def limit_reached(line):
    """Match the subscription quota error on an opencode stdout line, not the exit code."""
    lowered = line.lower()
    if "usage limit has been reached" in lowered:
        return True
    if '"type":"error"' in line or '"type": "error"' in line:
        try:
            event = json.loads(line)
        except ValueError:
            return False
        error = event.get("error") if isinstance(event, dict) else None
        payload = error.get("data") if isinstance(error, dict) else None
        return isinstance(payload, dict) and payload.get("statusCode") == 429
    return False


def validate_docx(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        raise ServiceError("لم ينشئ OpenCode ملف Word المتوقع.")
    try:
        with zipfile.ZipFile(path) as z:
            # Size caps come first: testzip() decompresses every entry, so an
            # oversized package must be refused before spending memory on it.
            total = 0
            for info in z.infolist():
                total += info.file_size
                if info.file_size > 100_000_000 or total > 300_000_000:
                    raise ValueError("package too large")
            if z.testzip():
                raise ValueError("CRC")
            for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                if name not in z.namelist():
                    raise ValueError(name)
            root = ET.fromstring(z.read("word/document.xml"))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            text = "".join(n.text or "" for n in root.iter(ns + "t"))
            if not text.strip():
                raise ValueError("empty document")
            return {"characters": len(text), "sha256": digest(path)}
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, OSError) as exc:
        raise ServiceError("ملف Word غير صالح أو لا يحتوي نصًا قابلًا للتحرير.") from exc


def run_process(command, cwd, cancel, timeout, log=lambda s: None, env=None):
    """No shell; stream output, bound runtime and terminate child process trees."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=flags, text=True, encoding="utf-8", errors="replace")
    lines = queue.Queue()
    def reader():
        for line in proc.stdout:
            lines.put(line.rstrip())
    threading.Thread(target=reader, daemon=True).start()
    started = time.monotonic()
    tail = []
    try:
        while proc.poll() is None or not lines.empty():
            checkpoint(cancel)
            if time.monotonic() - started > timeout:
                raise ServiceError("تجاوزت العملية المهلة المحددة.")
            try:
                line = lines.get(timeout=0.15)
                tail.append(line)
                tail = tail[-30:]
                log(line)
            except queue.Empty:
                pass
        if proc.returncode:
            raise ServiceError("فشلت العملية الخارجية:\n" + "\n".join(tail)[-2500:])
        return "\n".join(tail)
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=flags, timeout=15)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stdout:
            proc.stdout.close()


class Youtube:
    def __init__(self, store):
        self.store = store
        self.gate = threading.Lock()
        # YouTube throttles per network/IP, so the cooldown must outlive a restart.
        self.cooldown_until = float((store.state_get("youtube_cooldown_until") if store else None) or 0)
        self.last_request = 0

    def remember_cooldown(self, until):
        self.cooldown_until = max(self.cooldown_until, until)
        if self.store:
            self.store.state_put("youtube_cooldown_until", self.cooldown_until)

    def expire_cooldown(self):
        if self.cooldown_until and time.time() >= self.cooldown_until:
            self.cooldown_until = 0
            if self.store:
                self.store.state_clear("youtube_cooldown_until")

    @staticmethod
    def _retry_after(exc):
        for name in ("retry_after", "retryAfter"):
            value = getattr(exc, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
        headers = getattr(exc, "headers", None) or getattr(getattr(exc, "resp", None), "headers", None)
        try:
            return max(0, int(float(headers.get("Retry-After"))))
        except Exception:
            return 0

    def classify(self, exc, cfg):
        """Map a yt-dlp/network failure onto the retry policy: rate_limited waits
        out a persisted cooldown, failed retries a limited number of times, blocked
        needs the user to fix cookies/access, permanent never retries."""
        text = str(exc)
        low = text.lower()
        if "429" in text or "too many requests" in low:
            wait = max(int(cfg.get("youtube_cooldown", 900)), self._retry_after(exc))
            self.remember_cooldown(time.time() + wait)
            return ServiceError("YouTube قيّد الطلبات (429). تُهدأ الطلبات تلقائيًا ثم يُستأنف التنزيل دون تدخل.", "rate_limited", wait)
        if any(s in low for s in ("video unavailable", "removed by the uploader", "private video", "terminated",
                                  "copyright", "available in your country", "geo-restrict", "geo restrict",
                                  "unsupported url", "404", "does not exist")):
            return ServiceError("فشل دائم خاص بهذا الفيديو (محذوف أو خاص أو مقيّد) ولن يُعاد تلقائيًا: " + text[-300:], "permanent")
        if any(s in low for s in ("sign in", "not a bot", "login required", "confirm your age", "age-restrict", "cookies", "403")):
            return ServiceError("YouTube رفض الوصول. حدّث ملف الكوكيز وتوافق أدوات التنزيل ثم أعد المحاولة يدويًا.", "blocked")
        return ServiceError("تعذر التنزيل: " + text[-300:], "failed")

    def pace(self, cfg, cancel):
        self.expire_cooldown()
        if time.time() < self.cooldown_until:
            raise ServiceError("طلبات YouTube معلقة مؤقتًا بسبب التقييد حتى انتهاء التهدئة.",
                               "rate_limited", int(self.cooldown_until - time.time()))
        wait_cancel(cancel, max(0, self.last_request + cfg["youtube_request_delay"] - time.time()))
        self.last_request = time.time()

    def check_url(self, url):
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or p.hostname not in (
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
        ):
            raise ValueError("أدخل رابط YouTube صالحًا.")
        return url.strip()

    def options(self, cfg, cancel, cookie_copy=None):
        class Logger:
            def debug(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): pass
        opts = {
            "quiet": True, "no_warnings": True, "logger": Logger(),
            "retries": 2, "fragment_retries": 2, "extractor_retries": 1,
            "socket_timeout": 30, "concurrent_fragment_downloads": 1,
            "sleep_interval_requests": cfg["youtube_request_delay"],
            "sleep_interval": cfg["youtube_sleep_min"],
            "max_sleep_interval": cfg["youtube_sleep_max"],
            "windowsfilenames": True,
            "progress_hooks": [lambda _: checkpoint(cancel)],
        }
        runtimes = {}
        for name in ("deno", "node", "quickjs"):
            found = shutil.which(name)
            if found:
                runtimes[name] = {"path": found}
        if runtimes:
            opts["js_runtimes"] = runtimes
        if cfg.get("youtube_client"):
            opts["extractor_args"] = {"youtube": {"player_client": [cfg["youtube_client"]]}}
        if cookie_copy:
            opts["cookiefile"] = str(cookie_copy)
        return opts

    def cookies(self, cfg, tempdir):
        source = cfg.get("cookies_file")
        if not source:
            return None
        source = Path(source)
        if not source.is_file():
            raise ServiceError("ملف الكوكيز المحدد غير موجود.", "blocked")
        sample = source.read_text(encoding="utf-8-sig", errors="replace")[:8192]
        if "HTTP Cookie File" not in sample and not any(x.count("\t") >= 6 for x in sample.splitlines()):
            raise ServiceError("الكوكيز يجب أن تكون بصيغة Netscape.", "blocked")
        copy = Path(tempdir) / "cookies.txt"
        shutil.copyfile(source, copy)
        return copy

    def discover(self, url, cfg, cancel, refresh=False):
        import tempfile
        import yt_dlp
        url = self.check_url(url)
        key = "playlist:" + url
        cached = None if refresh else self.store.cache_get(key)
        if cached is not None:
            return cached
        with self.gate, tempfile.TemporaryDirectory(prefix="waraq-yt-") as temp:
            self.pace(cfg, cancel)
            opts = self.options(cfg, cancel, self.cookies(cfg, temp))
            opts.update({"extract_flat": "in_playlist", "skip_download": True,
                         "ignoreerrors": True, "lazy_playlist": False})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    data = ydl.extract_info(url, download=False)
                checkpoint(cancel)
                if not data:
                    raise ServiceError("تعذر قراءة المصدر.")
                entries = data.get("entries", [data])
                items = []
                for index, e in enumerate(entries, 1):
                    if not e:
                        continue
                    vid = e.get("id", "")
                    available = e.get("availability") not in ("private", "premium_only", "subscriber_only", "needs_auth")
                    items.append({"video_id": vid, "title": e.get("title") or vid,
                                  "position": e.get("playlist_index") or index,
                                  "url": f"https://www.youtube.com/watch?v={vid}",
                                  "duration": e.get("duration") or 0, "available": available,
                                  "selected": available})
                result = {"title": data.get("title", "YouTube"), "items": items}
                self.store.cache_put(key, result)
                return result
            except Cancelled:
                raise
            except Exception as exc:
                raise self.classify(exc, cfg) from exc

    def download(self, job, cfg, cancel, progress):
        import tempfile
        import yt_dlp
        import imageio_ffmpeg
        folder = Path(job["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "audio.mp3"
        marker = folder / "audio.complete.json"
        if target.exists() and marker.exists():
            m = json.loads(marker.read_text())
            if m.get("sha256") == digest(target):
                return target
        def hook(d):
            checkpoint(cancel)
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(99, 100 * d.get("downloaded_bytes", 0) / total))
        with self.gate, tempfile.TemporaryDirectory(prefix="waraq-yt-") as temp:
            self.pace(cfg, cancel)
            opts = self.options(cfg, cancel, self.cookies(cfg, temp))
            opts.update({"noplaylist": True, "format": "bestaudio/best",
                         "outtmpl": str(folder / "source.%(ext)s"),
                         "progress_hooks": [hook]})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(job["url"], download=True)
                    source = Path(ydl.prepare_filename(info))
                checkpoint(cancel)
                self.last_request = time.time()
            except Cancelled:
                raise
            except Exception as exc:
                raise self.classify(exc, cfg) from exc
        if not source.is_file():
            raise ServiceError("لم يُعثر على الملف الصوتي المنزل.")
        temporary = folder / "audio.tmp.mp3"
        run_process([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                     "-vn", "-codec:a", "libmp3lame", "-b:a", f'{cfg["audio_bitrate"]}k',
                     "-ar", str(cfg["audio_rate"]), "-ac", str(cfg["audio_channels"]), str(temporary)],
                    folder, cancel, 3600)
        if not temporary.exists() or temporary.stat().st_size < 100:
            raise ServiceError("لم ينتج تحويل الصوت ملفًا صالحًا.")
        os.replace(temporary, target)
        atomic_json(marker, {"sha256": digest(target), "video_id": job["video_id"]})
        source.unlink(missing_ok=True)
        return target


def transcript_from(data):
    if "combined_transcript" in data:
        text = data["combined_transcript"]
    else:
        try:
            alt = data["results"]["channels"][0]["alternatives"][0]
            text = (alt.get("paragraphs") or {}).get("transcript") or alt.get("transcript", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ServiceError("استجابة Deepgram لا تحتوي تفريغًا صالحًا.") from exc
    if not str(text).strip():
        raise ServiceError("أعاد Deepgram نصًا فارغًا.")
    return text.strip()


class Deepgram:
    def request(self, audio, worker, cfg, secret, cancel, raw_path):
        import requests
        params = {"model": worker["model"], "smart_format": "true", "paragraphs": "true", "utterances": "true"}
        params["detect_language" if worker["language"] == "auto" else "language"] = "true" if worker["language"] == "auto" else worker["language"]
        if worker.get("diarize"):
            params["diarize"] = "true"
        with requests.Session() as session:
            for attempt in range(cfg["max_retries"]):
                checkpoint(cancel)
                try:
                    with Path(audio).open("rb") as f:
                        response = session.post(worker["endpoint"] + "/v1/listen", params=params,
                                                headers={"Authorization": "Token " + secret, "Content-Type": "audio/mpeg"},
                                                data=f, timeout=(30, cfg["http_timeout"]))
                except (requests.Timeout, requests.ConnectionError) as exc:
                    raise ServiceError("انقطع الاتصال بعد محاولة إرسال الصوت؛ النتيجة غير مؤكدة. راجع الخدمة قبل إعادة الإرسال.", "uncertain") from exc
                if response.ok:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise ServiceError("استجابة غير قابلة للقراءة؛ قد تكون الخدمة أكملت الطلب.", "uncertain") from exc
                    # Commit response before processing text or honoring cancellation.
                    atomic_json(raw_path, data)
                    checkpoint(cancel)
                    return data
                if response.status_code in (401, 403):
                    raise ServiceError("رفض Deepgram المفتاح أو صلاحيات المشروع.", "blocked")
                if response.status_code in (413, 504):
                    raise ServiceError("يتطلب الملف التقسيم.", "split")
                if response.status_code in (408, 425, 429, 500, 502, 503):
                    if attempt + 1 < cfg["max_retries"]:
                        try:
                            delay = min(300, max(1, int(response.headers.get("Retry-After", 15 * (attempt + 1)))))
                        except ValueError:
                            delay = 15 * (attempt + 1)
                        wait_cancel(cancel, delay)
                        continue
                raise ServiceError(f"فشل Deepgram: HTTP {response.status_code} — {response.text[:400]}")

    def transcribe(self, job, worker, cfg, secret, cancel, progress):
        import imageio_ffmpeg
        audio = Path(job["audio"])
        if not audio.is_file():
            raise ServiceError("الملف الصوتي المطلوب غير موجود.", "blocked")
        folder = Path(job["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        if audio.suffix.lower() != ".mp3":
            normalized = folder / "local-audio.mp3"
            normalized_marker = folder / "local-audio.json"
            input_hash = digest(audio)
            expected = {"source": input_hash, "rate": cfg["audio_rate"], "bitrate": cfg["audio_bitrate"], "channels": cfg["audio_channels"]}
            if not (normalized.exists() and normalized_marker.exists() and json.loads(normalized_marker.read_text()) == expected):
                temporary = folder / "local-audio.tmp.mp3"
                run_process([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio),
                             "-vn", "-codec:a", "libmp3lame", "-b:a", f'{cfg["audio_bitrate"]}k',
                             "-ar", str(cfg["audio_rate"]), "-ac", str(cfg["audio_channels"]), str(temporary)],
                            folder, cancel, 3600)
                os.replace(temporary, normalized)
                atomic_json(normalized_marker, expected)
            audio = normalized
        version = fingerprint({"audio": digest(audio), "worker": {k: worker[k] for k in ("model", "language", "diarize", "endpoint")},
                               "revision": cfg.get("revision_deepgram", "")})
        raw = folder / f"deepgram-{version}.json"
        txt = folder / f"transcript-{version}.txt"
        if raw.exists():
            data = json.loads(raw.read_text(encoding="utf-8"))
        else:
            must_split = job.get("duration", 0) >= cfg["split_after_minutes"] * 60 or audio.stat().st_size >= 1_500_000_000
            if not must_split:
                try:
                    data = self.request(audio, worker, cfg, secret, cancel, raw)
                except ServiceError as exc:
                    if exc.kind != "split":
                        raise
                    must_split = True
            if must_split:
                chunks = folder / ("chunks-" + version)
                chunks.mkdir(exist_ok=True)
                manifest = chunks / "manifest.json"
                if not manifest.exists():
                    run_process([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio),
                                 "-f", "segment", "-segment_time", str(cfg["chunk_seconds"]), "-reset_timestamps", "1",
                                 "-codec:a", "libmp3lame", "-b:a", f'{cfg["audio_bitrate"]}k',
                                 "-ar", str(cfg["audio_rate"]), "-ac", str(cfg["audio_channels"]), str(chunks / "%05d.mp3")],
                                folder, cancel, 3600)
                    names = [p.name for p in sorted(chunks.glob("*.mp3")) if p.stat().st_size > 100]
                    if not names:
                        raise ServiceError("لم ينتج تقسيم الصوت أجزاء صالحة.")
                    atomic_json(manifest, names)
                names = json.loads(manifest.read_text())
                responses = []
                for index, name in enumerate(names):
                    checkpoint(cancel)
                    result_file = chunks / (name + ".json")
                    response = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else self.request(chunks / name, worker, cfg, secret, cancel, result_file)
                    transcript_from(response)
                    responses.append({"index": index, "offset_seconds": index * cfg["chunk_seconds"], "response": response})
                    progress(100 * (index + 1) / len(names))
                data = {"combined_transcript": "\n\n".join(transcript_from(r["response"]) for r in responses), "chunks": responses}
                atomic_json(raw, data)
        text = transcript_from(data)
        temporary = txt.with_suffix(".part")
        temporary.write_text(text + "\n", encoding="utf-8-sig")
        os.replace(temporary, txt)
        return txt


def resolve_opencode(value):
    path = Path(value) if Path(value).is_file() else Path(shutil.which(value) or "__missing__")
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        candidate = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if candidate.is_file():
            return candidate
        raise ServiceError("اختر ملف opencode.exe التنفيذي مباشرة بدل ملف BAT/CMD.", "blocked")
    if not path.is_file():
        raise ServiceError("لم يُعثر على OpenCode؛ حدد مسار opencode.exe.", "blocked")
    return path.resolve()


class OpenCode:
    def prepare_context(self, work, cfg):
        """Copy only skill sources, never credentials, cookies or legacy user inputs."""
        sources = [Path.home() / ".agents" / "skills" / "arabic-lesson-formatter-sequential"]
        if cfg.get("opencode_context"):
            sources.append(Path(cfg["opencode_context"]) / ".opencode" / "agent" / "skills" / "arabic-lesson-formatter-sequential-last")
        module_paths = []
        for source in sources:
            if source.is_dir():
                dest = work / ".agents" / "skills" / "arabic-lesson-formatter-sequential"
                shutil.copytree(source, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", ".git", "__pycache__"))
                if (source / "node_modules").is_dir():
                    module_paths.append(str(source / "node_modules"))
                break
        return module_paths

    def process(self, job, worker, cfg, secret, cancel, log):
        transcript = Path(job["transcript"])
        if not transcript.is_file() or not transcript.read_text(encoding="utf-8-sig").strip():
            raise ServiceError("ملف التفريغ فارغ أو غير موجود.", "blocked")
        original_hash = digest(transcript)
        version = fingerprint({"input": original_hash, "worker": worker, "prompt": cfg["prompt"],
                               "revision": cfg.get("revision_opencode", "")})
        work = Path(job["folder"]) / ("word-" + version)
        work.mkdir(parents=True, exist_ok=True)
        output = work / f'{job["position"]:03d}-{safe_title(job["title"])}.docx'
        verified = work / "validated.json"
        if output.exists() and verified.exists():
            previous = json.loads(verified.read_text(encoding="utf-8"))
            if previous.get("input_sha256") == original_hash and previous.get("sha256") == digest(output):
                validate_docx(output)
                return output
        if output.exists():
            # A valid ZIP can still be an unfinished document from a killed agent.
            # Only a committed validation record qualifies for automatic reuse.
            output.rename(work / ("unverified-" + str(time.time_ns()) + ".docx"))
        module_paths = self.prepare_context(work, cfg)
        # The session is sandboxed to --dir: reads outside it hit OpenCode's
        # external_directory permission, which headless runs auto-reject. The
        # agent therefore works on a copy inside its workspace; the original
        # TXT stays out of reach and its hash is still verified after the run.
        local_transcript = work / transcript.name
        shutil.copyfile(transcript, local_transcript)
        prompt = cfg["prompt"]
        for key, value in {"transcript_path": str(local_transcript.resolve()), "output_path": str(output.resolve()),
                           "title": job["title"], "position": str(job["position"])}.items():
            prompt = prompt.replace("{" + key + "}", value)
        # Exact output contract always applies, even to a custom formatting prompt.
        prompt += f'\nالمخرج المطلوب مستند Word واحد بالمسار: "{output.resolve()}". لا تعدّل ملف التفريغ.'
        env = os.environ.copy()
        if module_paths:
            env["NODE_PATH"] = os.pathsep.join(module_paths + ([env["NODE_PATH"]] if env.get("NODE_PATH") else []))
        provider = provider_of(worker, cfg)
        model = compose_model(provider, worker["model"])
        env.update(worker_env(worker, provider, secret, cfg))
        log(f"المزود: {provider} · النموذج: {model}")
        command = [str(resolve_opencode(cfg["opencode_executable"])), "run", "--model", model]
        if str(worker.get("variant", "")).strip():
            # Reasoning effort is provider specific; empty means the flag is left out.
            command += ["--variant", worker["variant"]]
        command += ["--agent", worker["agent"], "--dir", str(work), "--format", "json", prompt]
        denied = []
        limited = []
        def track(line):
            if "auto-rejecting" in line:
                denied.append(line)
            if limit_reached(line):
                limited.append(line)
            log(line)
        try:
            run_process(command, work, cancel, cfg["opencode_timeout"], track, env)
        except Cancelled:
            raise
        except ServiceError:
            # A quota stop is retryable; other failures keep their own diagnosis.
            if limited:
                raise ServiceError(LIMIT_MESSAGE, "rate_limited") from None
            raise
        finally:
            if digest(transcript) != original_hash:
                raise ServiceError("تغيّر TXT الأصلي أثناء OpenCode؛ أوقفت اعتماد الناتج للمراجعة.", "blocked")
        if not output.is_file() and denied:
            raise ServiceError("منع OpenCode الوصول إلى مسار خارج مجلد عمله: " + denied[0], "failed")
        if not output.is_file() and limited:
            raise ServiceError(LIMIT_MESSAGE, "rate_limited")
        report = validate_docx(output)
        atomic_json(verified, {**report, "input_sha256": original_hash, "output": str(output)})
        return output
