from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor
import datetime
import ipaddress
import json
from pathlib import Path
import queue
import ssl
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QThread

from waraq.config import atomic_json
from waraq.internet import InternetAccess, gateway_url
from waraq.remote_common import RemoteError, REQUEST_CONTEXT


class Vault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@pytest.fixture
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def wait_for(app, predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return
        time.sleep(.003)
    assert predicate()


def make_connector(tmp_path, executor, *, identity=True, vault=None, url="https://localhost", device="device_12345678"):
    vault = vault or Vault()
    bridge = QObject()
    bridge.config = SimpleNamespace(home=tmp_path)
    bridge.secrets = vault
    if identity:
        atomic_json(tmp_path / "internet-device.json", {"url": url, "device_id": device, "name": device})
        vault.set("internet:" + device, "x" * 48)
    obj = InternetAccess(bridge, executor)
    return obj, bridge, vault


def activate(obj):
    obj._generation = "connection"
    obj._stop.clear()


def dispatch(obj, app, action, data, ident="request_12345678", deadline=None):
    future, cancel = Future(), threading.Event()
    obj._request.emit((ident, future, cancel, obj._generation, action, data, deadline or time.time()+5))
    wait_for(app, future.done)
    return future


def test_async_library_result_is_journaled_after_completion_and_replayed(tmp_path, app):
    child = Future()
    contexts = []
    def execute(action, data):
        contexts.append(REQUEST_CONTEXT.get())
        return child
    obj, bridge, vault = make_connector(tmp_path, execute)
    activate(obj)
    outer, cancel = Future(), threading.Event()
    obj._request.emit(('library_delete_123', outer, cancel, obj._generation, 'library_delete',
                       {'file_id':'opaque','expected_version':'v1','confirm':True}, time.time()+5))
    wait_for(app, lambda: bool(contexts))
    key = obj._identity['device_id']+':library_delete_123'
    assert not outer.done() and not obj._journal[key]['done']
    assert contexts[0]['cancel'] is cancel
    assert REQUEST_CONTEXT.get() is None
    child.set_result({'message':'حُذف','revision':'r2'})
    assert outer.result(1)['revision'] == 'r2'
    assert obj._journal[key]['done']
    assert dispatch(obj, app, 'library_delete', {'file_id':'opaque','expected_version':'v1','confirm':True},
                    ident='library_delete_123').result()['revision'] == 'r2'
    assert len(contexts) == 1
    obj.stop()


def test_async_library_known_failure_is_replayed_without_mutating_again(tmp_path, app):
    child = Future()
    obj, bridge, vault = make_connector(tmp_path, lambda *_:child)
    activate(obj)
    outer, cancel = Future(), threading.Event()
    obj._request.emit(('library_delete_456', outer, cancel, obj._generation, 'library_delete', {}, time.time()+5))
    wait_for(app, lambda: hasattr(outer, '_waraq_child'))
    child.set_exception(RemoteError('تغير الملف',409))
    with pytest.raises(RemoteError): outer.result()
    assert obj._journal[obj._identity['device_id']+':library_delete_456']['done']
    with pytest.raises(RemoteError) as error:
        dispatch(obj, app, 'library_delete', {}, ident='library_delete_456').result()
    assert error.value.status == 409
    obj.stop()


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1", "https://user:pass@example.com",
                                 "https://example.com/a", "https://example.com?x=1", "https://example.com#secret",
                                 "https://example.com\\evil", "https://example.com:0", "https://example.com\n"])
def test_production_origins_reject_unsafe_urls(url):
    # Leading/trailing spaces are intentionally normalized, interior controls aren't.
    if url.endswith("\n"):
        url = "https://exam\nple.com"
    with pytest.raises(RemoteError):
        gateway_url(url)


def test_local_http_requires_explicit_test_injection():
    assert gateway_url("http://localhost:1234", allow_local_http=True) == "http://localhost:1234"
    with pytest.raises(RemoteError):
        gateway_url("http://192.168.1.1", allow_local_http=True)
    assert gateway_url("https://example.com/") == "https://example.com"


def test_identity_cannot_be_restored_without_vault_or_export_secrets(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: {})
    assert obj.state()["registered"]
    assert "x"*48 not in json.dumps(obj.state())
    assert "secret" not in (tmp_path/"internet-device.json").read_text()
    bridge.secrets = Vault()
    copied = InternetAccess(bridge, lambda a,d: {})
    assert not copied.state()["registered"]
    assert copied.state()["error"]
    obj.stop()
    copied.stop()


def test_qt_dispatch_and_persistent_dedup(tmp_path, app):
    calls = []
    def execute(action, data):
        assert QThread.currentThread() == app.thread()
        calls.append((action, data))
        return {"count": len(calls)}
    obj, bridge, vault = make_connector(tmp_path, execute)
    activate(obj)
    assert dispatch(obj, app, "queue", {"running": True}).result() == {"count": 1}
    assert dispatch(obj, app, "queue", {"running": True}).result() == {"count": 1}
    assert len(calls) == 1
    assert "running" not in (tmp_path/"internet-requests.json").read_text()
    reloaded = InternetAccess(bridge, execute)
    activate(reloaded)
    assert dispatch(reloaded, app, "queue", {"running": True}).result() == {"count": 1}
    with pytest.raises(RemoteError):
        dispatch(reloaded, app, "queue", {"running": False}).result()
    assert len(calls) == 1
    obj.stop()
    reloaded.stop()


def test_uncertain_crash_is_never_reexecuted(tmp_path, app):
    calls = []
    def crash(action, data):
        calls.append(action)
        raise RuntimeError("simulated crash after effect")
    obj, bridge, vault = make_connector(tmp_path, crash)
    activate(obj)
    with pytest.raises(RuntimeError):
        dispatch(obj, app, "upload", {"files": "sensitive-content"}).result()
    assert "sensitive-content" not in (tmp_path/"internet-requests.json").read_text()
    restarted = InternetAccess(bridge, lambda a,d: calls.append(a))
    activate(restarted)
    with pytest.raises(RemoteError, match="غير مؤكدة"):
        dispatch(restarted, app, "upload", {"files": "sensitive-content"}).result()
    assert len(calls) == 1
    obj.stop()
    restarted.stop()


def test_corrupt_journal_blocks_connect_and_commands(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: pytest.fail("executed"))
    (tmp_path/"internet-requests.json").write_text("{broken")
    restarted = InternetAccess(bridge, lambda a,d: pytest.fail("executed"))
    restarted.start()
    assert not restarted.state()["connected"]
    assert restarted._thread is None
    activate(restarted)
    with pytest.raises(RemoteError):
        dispatch(restarted, app, "queue", {}).result()
    obj.stop()
    restarted.stop()


def test_expiry_and_connection_generation_prevent_queued_execution(tmp_path, app):
    calls = []
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: calls.append(a))
    activate(obj)
    with pytest.raises(RemoteError):
        dispatch(obj, app, "queue", {}, deadline=time.time()-.1).result()
    future, cancel = Future(), threading.Event()
    obj._request.emit(("request_12345678", future, cancel, "old_connection", "queue", {}, time.time()+5))
    wait_for(app, future.done)
    with pytest.raises(RemoteError):
        future.result()
    assert not calls
    obj.stop()


class Wire:
    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def send(self, raw):
        with self.lock:
            self.frames.append(json.loads(raw))


def test_request_timeout_cancels_before_gui_executes(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: pytest.fail("expired command executed"))
    activate(obj)
    wire = Wire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        obj._receive_request({"type":"request", "protocol":1, "id":"request_12345678", "action":"queue",
                              "data":{}, "deadline":time.time()+.08}, obj._generation, wire, pool)
        # Deliberately do not process Qt's queued dispatch until the worker times out.
        time.sleep(.15)
    app.processEvents()
    assert wire.frames[-1]["ok"] is False
    assert not obj._pending
    obj.stop()


def test_unknown_action_never_reaches_executor(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: pytest.fail("executed"))
    activate(obj)
    wire = Wire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        obj._receive_request({"type":"request", "protocol":1, "id":"request_12345678", "action":"shell",
                              "data":{}, "deadline":time.time()+5}, obj._generation, wire, pool)
    assert wire.frames[-1]["status"] == 400
    obj.stop()


def test_forget_offline_preserves_server_identity(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: {})
    obj.forgetDevice()
    assert obj.state()["registered"]
    assert (tmp_path/"internet-device.json").exists()
    assert vault.get("internet:device_12345678")
    obj.stop()


def tls_contexts(tmp_path):
    crypto = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    certificate, keyfile = tmp_path/"test.pem", tmp_path/"test.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(certificate), str(keyfile))
    client = ssl.create_default_context(cafile=str(certificate))
    return server, client


def test_real_wss_device_isolation_management_and_file_stream(tmp_path, app, monkeypatch):
    from websockets.sync.server import serve
    import websockets.sync.client
    server_tls, client_tls = tls_contexts(tmp_path)
    original_connect = websockets.sync.client.connect
    # Pin a test CA; certificate validation and hostname checking remain enabled.
    monkeypatch.setattr(websockets.sync.client, "connect", lambda *a,**kw: original_connect(*a, ssl=client_tls, **kw))
    outcomes = queue.Queue()
    content = ("درس عربي\n" * 18000).encode()
    document = tmp_path / "result.docx"
    document.write_bytes(content)

    def handler(ws):
        try:
            assert ws.request.headers["Authorization"] == "Bearer " + "x"*48
            from urllib.parse import parse_qs, urlsplit
            device = parse_qs(urlsplit(ws.request.path).query)["device_id"][0]
            ws.send(json.dumps({"type":"welcome", "protocol":1, "device_id":device}))
            ws.send(json.dumps({"type":"request", "protocol":1, "id":"state_12345678",
                                "action":"state", "data":{}, "deadline":time.time()+30}))
            ws.send(json.dumps({"type":"request", "protocol":1, "id":"file_12345678",
                                "action":"file", "data":{}, "deadline":time.time()+30}))
            chunks = []
            got_state = got_file = got_pair = False
            while not (got_state and got_file and got_pair):
                frame = json.loads(ws.recv(timeout=30))
                if frame["type"] == "control":
                    result = {"phones": []}
                    if frame["action"] == "pair_create":
                        result = {"url":"https://localhost/pair#ticket", "ticket":"ticket", "expires_at":time.time()+120}
                        got_pair = True
                    ws.send(json.dumps({"type":"control_result", "id":frame["id"], "ok":True, "result":result}))
                elif frame["type"] == "response":
                    assert frame["result"] == {"device": device}
                    got_state = True
                elif frame["type"] == "file_chunk":
                    chunk = base64.b64decode(frame["data"])
                    assert len(chunk) <= 65536
                    chunks.append(chunk)
                elif frame["type"] == "file_end":
                    assert b"".join(chunks) == content
                    got_file = True
            outcomes.put(device)
            try:
                while True:
                    ws.recv(timeout=10)
            except Exception:
                pass
        except Exception as exc:
            outcomes.put(exc)

    # Bind/connect the same address family on Windows runners; localhost may
    # resolve IPv6 first while the ephemeral listener is IPv4 (or vice versa).
    server = serve(handler, "127.0.0.1", 0, ssl=server_tls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    connectors, bridges = [], []
    try:
        for name in ("device_A12345678", "device_B12345678"):
            def execute(action, data, device=name):
                assert QThread.currentThread() == app.thread()
                return document if action == "file" else {"device":device}
            connector, bridge, vault = make_connector(tmp_path/name, execute, device=name, url=f"https://127.0.0.1:{port}")
            connectors.append(connector)
            bridges.append(bridge)
            connector.start()
        wait_for(app, lambda: all(c.state()["connected"] for c in connectors), timeout=15)
        for connector in connectors:
            connector.createPair()
        wait_for(app, lambda: outcomes.qsize() >= 2, timeout=35)
        values = [outcomes.get_nowait(), outcomes.get_nowait()]
        assert all(isinstance(value, str) for value in values), (values, [c.state() for c in connectors])
        assert sorted(values) == ["device_A12345678", "device_B12345678"], values
        wait_for(app, lambda: all(c.state()["pairUrl"] for c in connectors))
        assert all("x"*48 not in json.dumps(c.state()) for c in connectors)
    finally:
        for connector in connectors:
            connector.stop()
        server.shutdown()
        thread.join(timeout=3)
        wait_for(app, lambda: all(not c._thread.is_alive() for c in connectors), timeout=12)


def test_enrollment_is_async_and_identity_contains_no_credential(tmp_path, app, monkeypatch):
    import requests
    calls = []
    response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {
        "device_id":"enrolled_12345678", "device_secret":"secret_"*8, "name":"المكتب"})
    def enroll(url, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        assert kwargs["allow_redirects"] is False
        assert kwargs["json"] == {"name":"المكتب", "code":"enroll-code"}
        calls.append(url)
        return response
    monkeypatch.setattr(requests, "post", enroll)
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: {}, identity=False)
    monkeypatch.setattr(obj, "start", lambda: None)
    obj.register("https://example.com", "enroll-code", "المكتب")
    wait_for(app, lambda: obj.state()["registered"] and not obj.busy)
    assert calls == ["https://example.com/api/enroll"]
    assert vault.get("internet:enrolled_12345678") == "secret_"*8
    assert "secret_" not in json.dumps(obj.state())
    assert "secret_" not in (tmp_path/"internet-device.json").read_text()
    obj.stop()


def test_stop_discards_controls_instead_of_replaying_after_reconnect(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: {})
    activate(obj)
    obj._connected = True
    obj._ws = SimpleNamespace(close=lambda: None)
    obj.createPair()
    obj.revokePhone("phone_12345678")
    assert obj._outbox.qsize() == 2
    obj.stop()
    assert obj._outbox.empty()
    assert not obj._controls
    assert obj._ws is None


def test_revoke_ack_survives_immediate_socket_close(tmp_path, app):
    obj, bridge, vault = make_connector(tmp_path, lambda a,d: {})
    obj._epoch = 3
    # The receiver captures this tuple before disconnect cleanup clears controls.
    obj._event.emit((3, "control", ({"id":"control_12345678", "ok":True, "result":{}},
                                   ("device_revoke", time.monotonic()+15, {}))))
    obj._controls.clear()
    app.processEvents()
    assert not obj.state()["registered"]
    assert not (tmp_path/"internet-device.json").exists()
    assert not vault.get("internet:device_12345678")
