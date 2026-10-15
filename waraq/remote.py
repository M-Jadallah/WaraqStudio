"""Opt-in LAN access to the running desktop instance.

HTTP workers never call Qt/Bridge methods directly: a cancellable Future is
delivered to the GUI thread using a queued signal. No second scheduler/database
owner, public tunnel, remote shell, or arbitrary filesystem endpoint is created.
"""
from __future__ import annotations

from concurrent.futures import CancelledError, Future, TimeoutError
from collections import deque
import hmac
import ipaddress
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

from .remote_common import DEFAULT_PORT, MAX_REQUEST, RemoteError
from .remote_commands import RemoteCommands


LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8"))
ASSETS = Path(__file__).with_name("mobile")


def local_address(value):
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in LOCAL_NETWORKS)
    except ValueError:
        return False


def lan_addresses():
    addresses = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not flags & QNetworkInterface.IsUp or flags & QNetworkInterface.IsLoopBack:
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            value = address.toString()
            if address.protocol() == QAbstractSocket.IPv4Protocol and local_address(value):
                addresses.append(value)
    # Normal Wi-Fi/Ethernet addresses precede link-local addresses.
    return sorted(set(addresses), key=lambda ip: (ip.startswith("169.254."), ip))


class RemoteAccess(QObject):
    changed = Signal()
    _request = Signal(object)
    _auth_changed = Signal()

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._generation = ""
        self._pending = set()
        self._sessions = {}
        self._attempts = {}
        self._global_attempts = deque()
        self._hosts = {"127.0.0.1", "localhost"}
        self._port = DEFAULT_PORT
        self._code = ""
        self._code_until = 0
        self._display_until = 0
        self._urls = []
        self._qr = ""
        self._error = ""
        self.commands = RemoteCommands(bridge)
        self._request.connect(self._dispatch, Qt.QueuedConnection)
        self._auth_changed.connect(self.changed, Qt.QueuedConnection)

    @property
    def busy(self):
        return self.commands.busy

    def state(self):
        with self._lock:
            self._prune_sessions()
            return {"enabled": self._server is not None, "port": self._port,
                    "urls": list(self._urls), "code": self._code, "qr": self._qr,
                    "codeExpires": time.strftime("%H:%M", time.localtime(self._display_until)) if self._code else "",
                    "clients": len(self._sessions), "error": self._error}

    def refresh_addresses(self):
        import qrcode
        import qrcode.image.svg
        addresses = lan_addresses()
        with self._lock:
            self._hosts = set(addresses) | {"127.0.0.1", "localhost"}
            self._urls = [f"http://{ip}:{self._port}" for ip in addresses]
            self._qr = ""
            if self._urls:
                svg = qrcode.make(self._urls[0], image_factory=qrcode.image.svg.SvgPathImage,
                                  border=4).to_string().decode("utf-8")
                self._qr = "data:image/svg+xml," + quote(svg, safe="")
        self.changed.emit()

    def rotate_code(self):
        with self._lock:
            self._code = "".join(secrets.choice("0123456789") for _ in range(8))
            self._code_until = time.monotonic() + 600
            # Wall time is only for display; expiry decisions use a monotonic clock.
            self._display_until = time.time() + 600
            self._sessions.clear()
            self._attempts.clear()
            self._global_attempts.clear()
        self.changed.emit()

    def start(self, port=DEFAULT_PORT):
        if self._server is not None:
            return
        dispatcher, socket_map = None, {}
        try:
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("اختر منفذًا بين 1024 و65535.")
            from waitress import create_server
            from waitress.task import ThreadedTaskDispatcher
            dispatcher = ThreadedTaskDispatcher()
            self._port = port
            self.refresh_addresses()
            self._stop = threading.Event()
            self._generation = secrets.token_hex(16)
            self.rotate_code()
            app = create_app(self)
            server = create_server(app, map=socket_map, _dispatcher=dispatcher,
                                   host="0.0.0.0", port=port, threads=4,
                                   connection_limit=32, channel_timeout=30,
                                   max_request_body_size=MAX_REQUEST, max_request_header_size=16384,
                                   clear_untrusted_proxy_headers=True, ident="Waraq")
            dispatcher.set_thread_count(4)
            self._server = server
            self._error = ""
            self._thread = threading.Thread(target=self._serve, args=(server, self._stop),
                                            daemon=True, name="waraq-lan")
            self._thread.start()
        except Exception as exc:
            if dispatcher:
                from waitress import wasyncore
                wasyncore.close_all(map=socket_map, ignore_all=True)
                dispatcher.shutdown(timeout=1)
            self._server = None
            self._code = ""
            self._error = (str(exc) if isinstance(exc, ValueError) else
                           "تعذر بدء الوصول من الهاتف. قد يكون المنفذ مستخدمًا؛ اختر منفذًا آخر وتحقق من تثبيت متطلبات التطبيق.")
            self.bridge.notice.emit(self._error, True)
        self.changed.emit()

    @staticmethod
    def _serve(server, stop):
        try:
            while not stop.is_set():
                server.asyncore.loop(timeout=0.1, map=server._map, count=1)
        finally:
            server.asyncore.close_all(map=server._map, ignore_all=True)
            server.task_dispatcher.shutdown(timeout=2)

    def stop(self):
        with self._lock:
            self._stop.set()
            self._generation = ""
            self._sessions.clear()
            self._code = ""
            for future in self._pending:
                future.cancel()
            self._pending.clear()
        self.commands.stop()
        if self._thread is not None:
            self._thread.join(3)
        self._thread = self._server = None
        self._urls, self._qr = [], ""
        self.changed.emit()

    def _prune_sessions(self):
        now = time.monotonic()
        self._sessions = {token: expiry for token, expiry in self._sessions.items() if expiry > now}

    def pair(self, code, address):
        with self._lock:
            now = time.monotonic()
            self._attempts = {ip: hits for ip, hits in self._attempts.items() if hits and hits[-1] > now - 60}
            hits = self._attempts.setdefault(address, deque())
            for bucket in (hits, self._global_attempts):
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
            if len(hits) >= 5 or len(self._global_attempts) >= 30:
                raise RemoteError("محاولات كثيرة. انتظر دقيقة ثم حاول مجددًا.", 429)
            hits.append(now)
            self._global_attempts.append(now)
            candidate = str(code).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")).replace(" ", "").replace("-", "")
            if (not self._code or now >= self._code_until or
                    not hmac.compare_digest(candidate.encode(), self._code.encode())):
                raise RemoteError("رمز الدخول غير صحيح أو انتهت صلاحيته. راجع الرمز في ورق على الكمبيوتر.", 401)
            self._prune_sessions()
            if len(self._sessions) >= 16:
                raise RemoteError("بلغ عدد الأجهزة المتصلة الحد المسموح. جدّد رمز الدخول من الكمبيوتر.", 429)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + 12 * 3600
        self._auth_changed.emit()
        return token

    def authorize(self, token):
        with self._lock:
            self._prune_sessions()
            return bool(token) and token in self._sessions

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)
        self._auth_changed.emit()

    def call(self, action, data, token, timeout=10):
        future = Future()
        with self._lock:
            if not self._server or self._stop.is_set() or not self.authorize(token):
                raise RemoteError("انتهت جلسة الاتصال. أدخل رمز الدخول مجددًا.", 401)
            if len(self._pending) >= 16:
                raise RemoteError("التطبيق مشغول. حاول بعد لحظات.", 503)
            self._pending.add(future)
            generation = self._generation
        self._request.emit((future, generation, token, action, data))
        try:
            return future.result(timeout)
        except CancelledError:
            raise RemoteError("قُطع الاتصال من الكمبيوتر؛ لم يُنفذ الطلب.", 503) from None
        except TimeoutError:
            cancelled = future.cancel()
            raise RemoteError("الكمبيوتر مشغول. " + ("لم يُنفذ الطلب؛ حاول بعد لحظات." if cancelled else
                              "تحقق من حالة المهمة قبل إعادة إرسال الأمر."), 503) from None
        finally:
            with self._lock:
                self._pending.discard(future)

    @Slot(object)
    def _dispatch(self, request):
        future, generation, token, action, data = request
        if not future.set_running_or_notify_cancel():
            return
        try:
            if generation != self._generation or not self.authorize(token):
                raise RemoteError("انتهت جلسة الاتصال. أدخل رمز الدخول مجددًا.", 401)
            result = self._execute(action, data)
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)

    def _execute(self, action, data):
        return self.commands.execute(action, data)


def create_app(remote):
    from flask import Flask, jsonify, request, send_file, g
    from werkzeug.exceptions import HTTPException
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_REQUEST)
    app.json.ensure_ascii = False
    cookie_name = f"waraq_session_{remote._port}"

    @app.before_request
    def guard():
        if not local_address(request.remote_addr or ""):
            raise RemoteError("الوصول متاح من الشبكة المحلية فقط.", 403)
        host = urlsplit("http://" + request.host)
        with remote._lock:
            valid_host = host.hostname in remote._hosts and host.port == remote._port
            enabled = remote._server is not None and not remote._stop.is_set()
        if not enabled:
            raise RemoteError("الوصول من الهاتف متوقف.", 503)
        if not valid_host:
            raise RemoteError("استخدم عنوان الشبكة المعروض في ورق على الكمبيوتر.", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RemoteError("طلب من موقع آخر غير مسموح.", 403)
        origin = request.headers.get("Origin")
        if origin and origin != "http://" + request.host:
            raise RemoteError("مصدر الطلب غير مسموح.", 403)
        if request.method != "GET" and (not request.is_json or request.headers.get("X-Waraq-Client") != "1"):
            raise RemoteError("صيغة الطلب غير مسموحة.", 403)
        g.token = request.cookies.get(cookie_name, "")
        if request.path.startswith("/api/") and request.path != "/api/pair" and not remote.authorize(g.token):
            raise RemoteError("أدخل رمز الدخول الظاهر في ورق على الكمبيوتر.", 401)

    @app.after_request
    def security_headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Cross-Origin-Resource-Policy": "same-origin",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.errorhandler(RemoteError)
    def remote_error(exc):
        response = jsonify(error=str(exc))
        response.status_code = exc.status
        if exc.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @app.errorhandler(Exception)
    def other_error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error="الطلب غير صالح أو حجمه أكبر من المسموح."), exc.code
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        return jsonify(error="تعذر تنفيذ الطلب. تحقق من ورق على الكمبيوتر ثم حدّث الصفحة."), 500

    @app.get("/")
    def index():
        return send_file(ASSETS / "index.html")

    @app.get("/assets/<name>")
    def asset(name):
        if name not in ("app.js", "style.css", "icon.svg"):
            raise RemoteError("الملف غير موجود.", 404)
        return send_file(Path(__file__).with_name("icon.svg") if name == "icon.svg" else ASSETS / name)

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise RemoteError("بيانات الطلب غير صالحة.")
        return value

    @app.post("/api/pair")
    def pair():
        token = remote.pair(body().get("code", ""), request.remote_addr)
        response = jsonify(message="تم الاتصال بورق.")
        response.set_cookie(cookie_name, token, max_age=12 * 3600, httponly=True, samesite="Strict", path="/api")
        return response

    @app.post("/api/logout")
    def logout():
        remote.logout(g.token)
        response = jsonify(message="قُطع اتصال هذا الهاتف.")
        response.delete_cookie(cookie_name, path="/api", httponly=True, samesite="Strict")
        return response

    @app.get("/api/state")
    def state():
        return jsonify(remote.call("state", {}, g.token))

    @app.post("/api/action/<action>")
    def action(action):
        if action not in ("queue", "cancel", "retry", "stage", "discover", "enqueue", "upload"):
            raise RemoteError("الأمر غير مدعوم.", 404)
        return jsonify(remote.call(action, body(), g.token))

    @app.get("/api/files/<ident>/<kind>")
    def download(ident, kind):
        path = remote.call("file", {"id": ident, "kind": kind}, g.token)
        return send_file(path, as_attachment=True, conditional=False)

    return app
