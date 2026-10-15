"""Opt-in outbound Internet relay connector; all application commands run on Qt.

The relay identity is deliberately outside settings exports. Only the per-device
credential lives in the OS vault. Requests are never queued across connections.
"""
from __future__ import annotations

import base64
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
import ipaddress
import hashlib
import os
import json
import math
import mimetypes
from pathlib import Path
import queue
import random
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit, urlencode

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .config import atomic_json
from .remote_common import RemoteError

ACTIONS = frozenset(("state", "file", "queue", "cancel", "retry", "stage", "discover", "enqueue", "upload"))
MUTATING = ACTIONS - {"state", "file"}
MAX_MESSAGE = 15 * 1024 * 1024
MAX_FILE = 100 * 1024 * 1024
MAX_JOURNAL = 4096
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def gateway_url(value, *, allow_local_http=False):
    """Accept an HTTPS origin, without userinfo, query, fragment or base path."""
    value = str(value).strip().rstrip("/")
    try:
        parts = urlsplit(value)
        if (not parts.hostname or parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or parts.path or parts.port == 0):
            raise ValueError()
        local = parts.hostname.lower() == "localhost"
        try:
            local = local or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
        if parts.scheme != "https" and not (allow_local_http and local and parts.scheme == "http"):
            raise ValueError()
        # Reject whitespace, controls and backslashes that URL parsers handle differently.
        if any(ord(c) <= 32 or c == "\\" for c in value):
            raise ValueError()
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    except (TypeError, ValueError):
        raise RemoteError("أدخل عنوان بوابة HTTPS فقط، مثل https://waraq.example.com.") from None


class InternetAccess(QObject):
    changed = Signal()
    _request = Signal(object)
    _event = Signal(object)

    def __init__(self, bridge, executor, *, allow_local_http=False):
        super().__init__(bridge)
        self.bridge, self.executor = bridge, executor
        self._allow_local_http = allow_local_http
        self._home = Path(bridge.config.home)
        self._identity_path = self._home / "internet-device.json"
        self._journal_path = self._home / "internet-requests.json"
        self._lock = threading.RLock()
        self._identity = {}
        self._journal = {}
        self._epoch = 0
        self._generation = ""
        self._stop = threading.Event()
        self._stop.set()
        self._thread = None
        self._ws = None
        self._send_lock = threading.Lock()
        self._outbox = queue.Queue(maxsize=32)
        self._controls = {}
        self._pending = {}
        self._registering = False
        self._connected = False
        self._status = "متوقف"
        self._error = ""
        self._pair_url = ""
        self._match_code = ""
        self._claims = []
        self._phones = []
        self._request.connect(self._dispatch, Qt.QueuedConnection)
        self._event.connect(self._accept_event, Qt.QueuedConnection)
        self._load()

    def _load(self):
        try:
            saved = json.loads(self._identity_path.read_text(encoding="utf-8"))
            origin = gateway_url(saved["url"], allow_local_http=self._allow_local_http)
            ident = saved["device_id"]
            if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident):
                raise ValueError()
            if not self.bridge.secrets or not self.bridge.secrets.get("internet:" + ident):
                raise ValueError()
            self._identity = {"url": origin, "device_id": ident, "name": str(saved["name"])[:120]}
        except FileNotFoundError:
            pass
        except Exception:
            self._error = "هوية الإنترنت أو اعتمادها غير متاح على هذا الجهاز. سجّل الجهاز من جديد."
        if self._identity:
            try:
                self._journal = json.loads(self._journal_path.read_text(encoding="utf-8"))
                if not isinstance(self._journal, dict):
                    raise ValueError()
            except FileNotFoundError:
                pass
            except Exception:
                # Never silently discard an unreadable replay journal.
                self._journal = None
                self._error = "تعذر قراءة سجل أوامر الإنترنت؛ يلزم إصلاحه قبل الاتصال."

    @property
    def busy(self):
        with self._lock:
            return self._registering or any(f.running() for f, _ in self._pending.values())

    def state(self):
        with self._lock:
            now = time.time()
            self._claims = [c for c in self._claims if c.get("expires_at", 0) > now]
            return {"registered": bool(self._identity), "connected": self._connected,
                    "status": self._status, "deviceId": self._identity.get("device_id", ""),
                    "name": self._identity.get("name", ""), "url": self._identity.get("url", ""),
                    "error": self._error, "pairUrl": self._pair_url, "matchCode": self._match_code,
                    "pendingPhones": list(self._claims), "phones": list(self._phones), "busy": self.busy}

    @Slot(str, str, str)
    def register(self, url, code, name):
        try:
            url = gateway_url(url, allow_local_http=self._allow_local_http)
            name, code = str(name).strip(), str(code).strip()
            if not name or len(name) > 120 or not code or len(code) > 256:
                raise RemoteError("أدخل اسم الجهاز ورمز تسجيل صالحًا.")
            with self._lock:
                if self._identity or self._registering:
                    raise RemoteError("هذا الجهاز مسجل بالفعل أو يجري تسجيله.", 409)
                if not self.bridge.secrets:
                    raise RemoteError("مخزن اعتماد Windows غير متاح؛ لا يمكن حفظ الهوية بأمان.")
                self._registering = True
                self._epoch += 1
                epoch = self._epoch
                self._error, self._status = "", "جارٍ التسجيل"
            self.changed.emit()
            threading.Thread(target=self._enroll, args=(epoch, url, code, name), daemon=True,
                             name="waraq-internet-enroll").start()
        except RemoteError as exc:
            self._error = str(exc)
            self.changed.emit()

    def _enroll(self, epoch, url, code, name):
        import requests
        try:
            response = requests.post(url + "/api/enroll", json={"code": code, "name": name},
                                     timeout=(10, 15), allow_redirects=False)
            if response.status_code != 200:
                raise RemoteError("تعذر التسجيل. تحقق من عنوان البوابة ورمز التسجيل وصلاحيته.")
            if len(response.content) > 16384:
                raise ValueError()
            payload = response.json()
            ident, token = payload["device_id"], payload["device_secret"]
            if (not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident) or
                    not isinstance(token, str) or not 24 <= len(token) <= 1024):
                raise ValueError()
            with self._lock:
                if epoch != self._epoch:
                    return
                identity = {"url": url, "device_id": ident, "name": str(payload.get("name", name))[:120]}
                key = "internet:" + ident
                try:
                    self.bridge.secrets.set(key, token)
                    if self.bridge.secrets.get(key) != token:
                        raise ValueError()
                    atomic_json(self._identity_path, identity)
                except Exception:
                    self.bridge.secrets.delete(key)
                    raise RemoteError("تعذر حفظ اعتماد الجهاز في مخزن Windows الآمن.") from None
                self._identity, self._journal = identity, {}
                atomic_json(self._journal_path, self._journal)
            self._event.emit((epoch, "enrolled", None))
        except Exception as exc:
            message = str(exc) if isinstance(exc, RemoteError) else "تعذر الاتصال بالبوابة وتسجيل الجهاز. تحقق من الشبكة وشهادة HTTPS."
            self._event.emit((epoch, "enroll_error", message))

    @Slot()
    def start(self):
        with self._lock:
            if self._registering or (self._thread and self._thread.is_alive() and not self._stop.is_set()):
                return
            if not self._identity or self._journal is None:
                self._error = self._error or "سجّل هذا الجهاز أولًا."
                self.changed.emit()
                return
            self._epoch += 1
            epoch = self._epoch
            self._stop = threading.Event()
            self._status, self._error = "جارٍ الاتصال", ""
            self._thread = threading.Thread(target=self._run, args=(epoch, self._stop), daemon=True,
                                            name="waraq-internet")
            self._thread.start()
        self.changed.emit()

    connectToGateway = start

    @Slot()
    def stop(self):
        with self._lock:
            self._epoch += 1
            self._stop.set()
            self._registering = False
            self._generation = ""
            self._connected, self._status = False, "متوقف"
            self._pair_url, self._match_code = "", ""
            self._claims = []
            for future, cancel in self._pending.values():
                cancel.set()
                future.cancel()
            ws, self._ws = self._ws, None
            self._controls.clear()
            while not self._outbox.empty():
                try:
                    self._outbox.get_nowait()
                except queue.Empty:
                    break
        if ws:
            # Closing a socket may wait for a close handshake; keep the GUI free.
            threading.Thread(target=lambda: ws.close(), daemon=True).start()
        self.changed.emit()

    def _run(self, epoch, stop):
        from websockets.sync.client import connect
        retry = 0
        while not stop.is_set():
            with self._lock:
                if epoch != self._epoch:
                    return
                identity = dict(self._identity)
            try:
                token = self.bridge.secrets.get("internet:" + identity["device_id"])
                if not token:
                    raise RemoteError("اعتماد الجهاز غير متاح. سجّل الجهاز من جديد.", 401)
                url = identity["url"].replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                url += "/ws/device?" + urlencode({"device_id": identity["device_id"]})
                with connect(url, additional_headers={"Authorization": "Bearer " + token},
                             open_timeout=10, close_timeout=1, max_size=MAX_MESSAGE,
                             ping_interval=20, ping_timeout=20, proxy=None) as ws:
                    welcome = json.loads(ws.recv(timeout=10))
                    if (welcome.get("type") != "welcome" or welcome.get("protocol") != 1 or
                            welcome.get("device_id") != identity["device_id"]):
                        raise RemoteError("إصدار بوابة الإنترنت غير متوافق أو هوية الجهاز غير صحيحة.", 401)
                    generation = secrets.token_hex(16)
                    with self._lock:
                        if stop.is_set() or epoch != self._epoch:
                            return
                        self._generation, self._ws = generation, ws
                    self._event.emit((epoch, "connected", None))
                    retry = 0
                    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="internet-request") as pool:
                        while not stop.is_set():
                            self._flush_controls(ws)
                            try:
                                raw = ws.recv(timeout=0.2)
                            except TimeoutError:
                                self._expire_controls()
                                continue
                            frame = json.loads(raw)
                            kind = frame.get("type")
                            if kind == "request":
                                self._receive_request(frame, generation, ws, pool)
                            elif kind == "cancel":
                                with self._lock:
                                    pending = self._pending.get(frame.get("id"))
                                    if pending:
                                        pending[1].set()
                                        pending[0].cancel()
                            elif kind == "control_result":
                                with self._lock:
                                    entry = self._controls.pop(frame.get("id"), None)
                                self._event.emit((epoch, "control", (frame, entry)))
                            elif kind == "pair_claim":
                                self._event.emit((epoch, "claim", frame))
            except Exception as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                http_status = getattr(getattr(exc, "response", None), "status_code", None)
                permanent = code in (4401, 4403, 4409) or http_status in (401, 403, 409) or isinstance(exc, RemoteError)
                message = (str(exc) if isinstance(exc, RemoteError) else
                           "رُفض اعتماد الجهاز أو يوجد اتصال آخر له. أعد التسجيل عند الحاجة." if permanent else
                           "انقطع الاتصال بالبوابة؛ ستجري إعادة المحاولة تلقائيًا.")
                self._event.emit((epoch, "offline", message))
                if permanent:
                    stop.set()
            finally:
                with self._lock:
                    if epoch == self._epoch:
                        self._ws, self._generation = None, ""
                        for future, cancel in self._pending.values():
                            cancel.set()
                            future.cancel()
                        self._controls.clear()
                        while not self._outbox.empty():
                            try:
                                self._outbox.get_nowait()
                            except queue.Empty:
                                break
            if not stop.is_set():
                delay = min(30, 2 ** min(retry, 5)) * random.uniform(0.75, 1.25)
                retry += 1
                stop.wait(delay)

    def _send(self, ws, frame):
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            raise RemoteError("حجم الحالة أكبر من حد النقل. قلّل عناصر الطابور المكتملة ثم حدّث الصفحة.", 413)
        with self._send_lock:
            ws.send(encoded)

    def _receive_request(self, frame, generation, ws, pool):
        ident = frame.get("id")
        if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident):
            raise RemoteError("معرّف طلب غير صالح.")
        try:
            action, data, deadline = frame.get("action"), frame.get("data"), frame.get("deadline")
            if (frame.get("protocol") != 1 or action not in ACTIONS or not isinstance(data, dict) or
                    type(deadline) not in (float, int) or not math.isfinite(deadline) or
                    deadline <= time.time() or deadline > time.time() + 65):
                raise RemoteError("طلب غير صالح أو انتهت صلاحيته.", 400)
            with self._lock:
                if generation != self._generation or self._stop.is_set():
                    raise RemoteError("انقطع الاتصال؛ لم ينفذ الطلب.", 503)
                if ident in self._pending:
                    raise RemoteError("الطلب قيد التنفيذ؛ حدّث الحالة قبل إعادة المحاولة.", 409)
                if len(self._pending) >= 4:
                    raise RemoteError("التطبيق مشغول. حاول بعد لحظات.", 503)
                future, cancel = Future(), threading.Event()
                self._pending[ident] = (future, cancel)
            self._request.emit((ident, future, cancel, generation, action, data, deadline))
            pool.submit(self._answer, ident, future, cancel, generation, deadline, ws)
        except RemoteError as exc:
            self._send(ws, {"type": "response", "id": ident, "ok": False, "error": str(exc), "status": exc.status})

    def _record(self, ident, value):
        if self._journal is None:
            raise RemoteError("سجل الأوامر غير متاح؛ لم ينفذ الطلب.", 503)
        key = self._identity["device_id"] + ":" + ident
        if key not in self._journal and len(self._journal) >= MAX_JOURNAL:
            # Older than the protocol's maximum lifetime plus a conservative week.
            cutoff = time.time() - 7 * 86400
            self._journal = {k: v for k, v in self._journal.items() if v.get("at", 0) >= cutoff}
            if len(self._journal) >= MAX_JOURNAL:
                raise RemoteError("سجل الأوامر ممتلئ. حاول لاحقًا.", 503)
        self._journal[key] = value
        atomic_json(self._journal_path, self._journal)

    @Slot(object)
    def _dispatch(self, request):
        ident, future, cancel, generation, action, data, deadline = request
        if not future.set_running_or_notify_cancel():
            return
        try:
            with self._lock:
                if (cancel.is_set() or self._stop.is_set() or generation != self._generation or
                        time.time() >= deadline or action not in ACTIONS or not isinstance(data, dict)):
                    raise RemoteError("انتهت صلاحية الطلب أو انقطع الاتصال؛ لم ينفذ الطلب.", 503)
                key = self._identity["device_id"] + ":" + ident
                fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if action in MUTATING:
                    if self._journal is None:
                        raise RemoteError("سجل الأوامر غير متاح؛ لم ينفذ الطلب.", 503)
                    if key in self._journal:
                        cached = self._journal[key]
                        if cached.get("action") != action or cached.get("fingerprint") != fingerprint:
                            raise RemoteError("معرّف الطلب مستخدم لأمر آخر.", 409)
                        if cached.get("done"):
                            result = cached["result"]
                            if not result.get("ok"):
                                raise RemoteError(result["error"], result["status"])
                            future.set_result(result["result"])
                            return
                        raise RemoteError("نتيجة طلب سابق غير مؤكدة. حدّث الحالة قبل إرسال أمر جديد.", 409)
                    self._record(ident, {"action": action, "fingerprint": fingerprint, "at": time.time(), "done": False})
            try:
                result = self.executor(action, data)
                response = {"ok": True, "result": result}
            except RemoteError as exc:
                response = {"ok": False, "error": str(exc), "status": exc.status}
                if action in MUTATING:
                    with self._lock:
                        self._record(ident, {"action": action, "fingerprint": fingerprint, "at": time.time(), "done": True, "result": response})
                raise
            if action in MUTATING:
                with self._lock:
                    self._record(ident, {"action": action, "fingerprint": fingerprint, "at": time.time(), "done": True, "result": response})
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)

    def _answer(self, ident, future, cancel, generation, deadline, ws):
        try:
            result = future.result(max(0, deadline - time.time()))
            if cancel.is_set() or generation != self._generation or time.time() >= deadline:
                return
            if isinstance(result, Path):
                self._stream_file(ws, ident, result, cancel, generation, deadline)
            else:
                self._send(ws, {"type": "response", "id": ident, "ok": True, "result": result})
        except Exception as exc:
            if isinstance(exc, (CancelledError, TimeoutError)):
                future.cancel()
                cancel.set()
            if generation == self._generation:
                error = str(exc) if isinstance(exc, RemoteError) else "تعذر إكمال الطلب. حدّث الحالة قبل إعادة إرسال الأمر."
                try:
                    self._send(ws, {"type": "response", "id": ident, "ok": False,
                                    "error": error, "status": getattr(exc, "status", 503)})
                except Exception:
                    pass
        finally:
            with self._lock:
                if self._pending.get(ident, (None,))[0] is future:
                    self._pending.pop(ident, None)

    def _stream_file(self, ws, ident, path, cancel, generation, deadline):
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_FILE:
                raise RemoteError("الملف يتجاوز حد النقل البالغ 100 ميغابايت.", 413)
            self._send(ws, {"type": "file_start", "id": ident, "name": path.name, "size": size,
                            "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
            sent = 0
            while True:
                if cancel.is_set() or generation != self._generation or time.time() >= deadline:
                    raise RemoteError("انقطع تنزيل الملف.", 503)
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > size:
                    raise RemoteError("تغير الملف أثناء التنزيل. حاول مجددًا.", 409)
                self._send(ws, {"type": "file_chunk", "id": ident, "data": base64.b64encode(chunk).decode("ascii")})
            if sent != size:
                raise RemoteError("تغير الملف أثناء التنزيل. حاول مجددًا.", 409)
            self._send(ws, {"type": "file_end", "id": ident})

    def _control(self, action, data=None):
        with self._lock:
            if not self._connected or not self._ws:
                self._error = "اتصل بالبوابة أولًا."
            else:
                ident = secrets.token_hex(16)
                try:
                    frame = {"type": "control", "id": ident, "action": action, "data": data or {}}
                    self._outbox.put_nowait((self._epoch, self._generation, frame))
                    self._controls[ident] = (action, time.monotonic() + 15, data or {})
                    self._error = ""
                except queue.Full:
                    self._error = "طلبات كثيرة. حاول بعد لحظات."
        self.changed.emit()

    def _flush_controls(self, ws):
        for _ in range(32):
            with self._lock:
                if ws is not self._ws or self._stop.is_set():
                    return
                try:
                    epoch, generation, frame = self._outbox.get_nowait()
                except queue.Empty:
                    break
                if epoch != self._epoch or generation != self._generation:
                    continue
            self._send(ws, frame)

    def _expire_controls(self):
        with self._lock:
            expired = [key for key, value in self._controls.items() if value[1] < time.monotonic()]
            for key in expired:
                self._controls.pop(key, None)
        if expired:
            self._event.emit((self._epoch, "error", "انتهت مهلة الطلب. حدّث قائمة الهواتف قبل المحاولة مجددًا."))

    @Slot()
    def createPair(self):
        self._control("pair_create")

    @Slot(str)
    def approvePair(self, claim_id):
        self._control("pair_approve", {"claim_id": claim_id, "approved": True})

    @Slot(str)
    def rejectPair(self, claim_id):
        self._control("pair_approve", {"claim_id": claim_id, "approved": False})

    @Slot()
    def refreshPhones(self):
        self._control("phones")

    @Slot(str)
    def revokePhone(self, phone_id):
        self._control("revoke", {"phone_id": phone_id})

    @Slot()
    def forgetDevice(self):
        if self._connected:
            self._control("device_revoke")
        else:
            self._error = "اتصل بالبوابة أولًا لإلغاء تسجيل الجهاز والهواتف المقترنة به."
            self.changed.emit()

    def _forget_local(self):
        self.stop()
        with self._lock:
            ident = self._identity.get("device_id")
            if ident and self.bridge.secrets:
                self.bridge.secrets.delete("internet:" + ident)
            self._identity_path.unlink(missing_ok=True)
            self._journal_path.unlink(missing_ok=True)
            self._identity, self._journal = {}, {}
            self._phones, self._claims = [], []
            self._error, self._registering = "", False
        self.changed.emit()

    @Slot(object)
    def _accept_event(self, event):
        epoch, kind, payload = event
        if epoch != self._epoch:
            return
        with self._lock:
            if kind == "enrolled":
                self._registering = False
                self.start()
            elif kind == "enroll_error":
                self._registering, self._status, self._error = False, "متوقف", payload
            elif kind == "connected":
                self._connected, self._status, self._error = True, "متصل", ""
                self.refreshPhones()
            elif kind == "offline":
                self._connected, self._status, self._error = False, "غير متصل", payload
                self._claims, self._pair_url, self._match_code = [], "", ""
            elif kind == "error":
                self._error = payload
            elif kind == "claim":
                if (isinstance(payload.get("claim_id"), str) and
                        isinstance(payload.get("expires_at"), (int, float)) and
                        payload["expires_at"] > time.time() and len(self._claims) < 16):
                    self._claims = [c for c in self._claims if c["claim_id"] != payload["claim_id"]]
                    self._claims.append({key: payload.get(key, "") for key in
                                         ("claim_id", "phone_name", "match_code", "expires_at")})
                    self._match_code = str(payload.get("match_code", ""))
            elif kind == "control":
                payload, entry = payload
                if not entry:
                    return
                action, _, data = entry
                if not payload.get("ok"):
                    detail = payload.get("error")
                    self._error = detail[:400] if isinstance(detail, str) and detail else "تعذر إكمال الطلب في البوابة. حدّث الحالة وأعد المحاولة."
                else:
                    result = payload.get("result") or {}
                    if action == "pair_create":
                        self._pair_url = str(result.get("url", ""))
                    elif action == "phones":
                        self._phones = result.get("phones", [])
                    elif action == "pair_approve":
                        self._claims = [c for c in self._claims if c["claim_id"] != data.get("claim_id")]
                        self._pair_url, self._match_code = "", ""
                        self.refreshPhones()
                    elif action == "revoke":
                        self.refreshPhones()
                    elif action == "device_revoke":
                        self._forget_local()
        self.changed.emit()
