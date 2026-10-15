from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QThread
import requests

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.remote import RemoteError, create_app, local_address
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def remote_env(tmp_path, scheduler_harness, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    config, store = Config(tmp_path), Store(tmp_path / "tasks.db")
    scheduler = Scheduler(store, config, None)
    bridge = Bridge(store, config, None, scheduler)
    bridge.timer.stop()
    monkeypatch.setattr(scheduler, "start", lambda: setattr(scheduler, "running", True))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    remote = bridge.remote
    remote.start(port)
    assert remote.state()["enabled"], remote.state()
    client = create_app(remote).test_client()
    base = f"http://127.0.0.1:{port}"
    pool = ThreadPoolExecutor(max_workers=2)

    def request(path, data=None, **kwargs):
        method = client.get if data is None else client.post
        options = {"base_url": base, **kwargs}
        if data is not None:
            options["json"] = data
            options["headers"] = {"X-Waraq-Client": "1", **options.get("headers", {})}
        future = pool.submit(method, path, **options)
        deadline = time.monotonic() + 12
        while not future.done() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        return future.result(1)

    def pair():
        response = request("/api/pair", {"code": remote.state()["code"]})
        assert response.status_code == 200, response.json
        return response

    try:
        yield bridge, remote, store, config, client, request, pair, base, app
    finally:
        remote.stop()
        pool.shutdown(wait=True)
        scheduler_harness.shutdown_and_join()
        bridge.deleteLater()
        app.processEvents()
        store.db.close()


def add_job(store, config):
    return store.add([{"video_id": "one", "title": "درس عربي", "position": 1}], config.snapshot())[0]


def test_opt_in_and_assets_do_not_expose_state(remote_env):
    bridge, remote, _, _, _, request, _, _, _ = remote_env
    assert request("/").status_code == 200
    assert b'lang="ar" dir="rtl"' in request("/").data
    for name in ("app.js", "style.css", "icon.svg"):
        response = request("/assets/" + name)
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert request("/assets/settings.json").status_code == 404
    assert request("/api/state").status_code == 401
    assert request("/api/action/queue", {"running": True}).status_code == 401
    assert not bridge.running
    assert remote.state()["qr"].startswith("data:image/svg+xml,") or not remote.state()["urls"]


def test_pair_cookie_expiry_revocation_and_arabic_digits(remote_env):
    _, remote, _, _, _, request, pair, _, _ = remote_env
    response = pair()
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/api" in cookie
    assert request("/api/state").status_code == 200
    assert remote.state()["clients"] == 1
    remote.rotate_code()
    assert request("/api/state").status_code == 401
    arabic = remote.state()["code"].translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    assert request("/api/pair", {"code": arabic}).status_code == 200
    remote._code_until = time.monotonic() - 1
    assert request("/api/pair", {"code": remote.state()["code"]}).status_code == 401
    # Expiring the pairing code doesn't interrupt an already paired phone.
    assert request("/api/state").status_code == 200
    assert request("/api/logout", {}).status_code == 200
    assert request("/api/state").status_code == 401


def test_pair_rate_limit_and_headers(remote_env):
    _, _, _, _, _, request, _, _, _ = remote_env
    for _ in range(5):
        assert request("/api/pair", {"code": "invalid"}).status_code == 401
    response = request("/api/pair", {"code": "invalid"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


@pytest.mark.parametrize("headers,environ", [
    ({"Host": "attacker.test:8765"}, {}),
    ({"Origin": "https://attacker.test"}, {}),
    ({"Sec-Fetch-Site": "cross-site"}, {}),
    ({}, {"REMOTE_ADDR": "8.8.8.8"}),
])
def test_rebinding_csrf_and_public_peer_rejected(remote_env, headers, environ):
    _, _, _, _, _, request, pair, _, _ = remote_env
    pair()
    assert request("/api/state", headers=headers, environ_overrides=environ).status_code == 403


def test_mutations_require_json_and_custom_header(remote_env):
    _, _, _, _, client, _, pair, base, _ = remote_env
    pair()
    assert client.post("/api/action/queue", base_url=base, json={"running": True}).status_code == 403
    assert client.post("/api/action/queue", base_url=base, data="running=true",
                       headers={"X-Waraq-Client": "1"}).status_code == 403


def test_snapshot_allowlist_and_same_instance_commands_on_qt_thread(remote_env, monkeypatch):
    bridge, _, store, config, _, request, pair, _, _ = remote_env
    ident = add_job(store, config)
    pair()
    response = request("/api/state")
    snapshot = response.json
    assert snapshot["jobs"][0]["id"] == ident
    assert snapshot["jobs"][0]["title"] == "درس عربي"
    for secret in ("output_root", "cookies_file", "prompt", "opencode_executable", str(config.home)):
        assert secret not in json.dumps(snapshot)
    assert "config" not in snapshot["jobs"][0]
    assert "text" not in snapshot["templates"][0]
    called = []
    original = bridge.toggleRunning
    def toggle():
        called.append(QThread.currentThread() == bridge.thread())
        original()
    monkeypatch.setattr(bridge, "toggleRunning", toggle)
    assert request("/api/action/queue", {"running": True}).status_code == 200
    assert bridge.running and called == [True]
    # Repeating an explicit desired state never toggles the queue back.
    assert request("/api/action/queue", {"running": True}).status_code == 200
    assert called == [True]
    assert request("/api/action/queue", {"running": False}).status_code == 200
    assert not bridge.running
    assert request("/api/action/stage", {"stage": "download", "paused": True}).status_code == 200
    assert "download" in bridge.scheduler.manual_paused_stages
    assert request("/api/action/queue", {"running": "false"}).status_code == 400


def test_cancel_and_uncertain_retry_require_explicit_confirmation(remote_env):
    _, _, store, config, _, request, pair, _, _ = remote_env
    ident = add_job(store, config)
    pair()
    assert request("/api/action/cancel", {"id": ident}).status_code == 200
    assert store.get(ident)["stages"]["download"]["status"] == "cancelled"
    store.set_stage(ident, "deepgram", "uncertain")
    data = {"id": ident, "stage": "deepgram"}
    assert request("/api/action/retry", data).status_code == 409
    assert store.get(ident)["stages"]["deepgram"]["status"] == "uncertain"
    assert request("/api/action/retry", {**data, "confirm_uncertain": True}).status_code == 200
    assert store.get(ident)["stages"]["deepgram"]["status"] == "pending"
    assert request("/api/action/cancel", {"id": "missing"}).status_code == 404


def test_discovery_enqueue_and_template_snapshot(remote_env, monkeypatch):
    bridge, remote, store, config, _, request, pair, _, app = remote_env
    item = {"video_id": "one", "title": "درس من الهاتف", "position": 7,
            "available": True, "selected": True, "url": "https://www.youtube.com/watch?v=one"}
    monkeypatch.setattr(bridge.scheduler.youtube, "discover", lambda *args: {"title": "قائمة", "items": [item]})
    pair()
    assert request("/api/action/discover", {"url": "http://127.0.0.1/private"}).status_code == 400
    assert request("/api/action/discover", {"url": "https://youtu.be/one"}).status_code == 200
    deadline = time.monotonic() + 2
    while remote.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert not remote.busy
    folder = store.create_folder("مادة الهاتف")
    template = config.snapshot()["opencode_templates"][0]
    data = {"revision": remote.commands._source["revision"], "indices": [0], "preset": "full",
            "template_id": template["id"], "folder_id": folder}
    assert request("/api/action/enqueue", {**data, "revision": "stale"}).status_code == 409
    assert request("/api/action/enqueue", data).json["count"] == 1
    job = store.jobs()[0]
    assert job["title"] == item["title"] and job["position"] == 7
    assert job["config"]["prompt"] == template["text"] and job["folder_id"] == folder
    assert bridge.source == []  # Mobile browsing does not replace the desktop selection.
    assert bridge.jobs[0]["id"] == job["id"]
    assert request("/api/action/enqueue", data).status_code == 409  # Consumed revision.


def test_upload_preserves_bytes_and_copies_before_temporary_cleanup(remote_env):
    bridge, _, store, config, _, request, pair, _, _ = remote_env
    pair()
    content = b"\xef\xbb\xbf" + "تفريغ عربي\r\n".encode()
    data = {"files": [{"name": "درس 01.txt", "content": base64.b64encode(content).decode()}]}
    response = request("/api/action/upload", data)
    assert response.status_code == 200, response.json
    job = store.jobs()[0]
    assert Path(job["transcript"]).read_bytes() == content
    assert not Path(job["input_path"]).exists()
    assert job["stages"]["deepgram"]["status"] == "skipped"
    assert job["stages"]["download"]["status"] == "skipped"
    assert job["stages"]["opencode"]["status"] == "pending"
    assert bridge.jobs[0]["id"] == job["id"]
    assert request("/api/files/" + job["id"] + "/transcript").data == content
    assert request("/api/action/upload", {**data, "template_id": "missing"}).status_code == 400
    assert len(store.jobs()) == 1


@pytest.mark.parametrize("files", [
    [{"name": "../secret.txt", "content": "YQ=="}],
    [{"name": "C:\\secret.txt", "content": "YQ=="}],
    [{"name": "run.exe", "content": "YQ=="}],
    [{"name": "CON.txt", "content": "YQ=="}],
    [{"name": "bad?.txt", "content": "YQ=="}],
    [{"name": "one.txt", "content": "%%%"}],
    [{"name": "one.txt", "content": ""}],
    [{"name": "one.txt", "content": "//4="}],
    [{"name": "one.txt", "content": "YQ=="}, {"name": "ONE.txt", "content": "Yg=="}],
])
def test_invalid_upload_is_atomic(remote_env, files):
    _, _, store, _, _, request, pair, _, _ = remote_env
    pair()
    assert request("/api/action/upload", {"files": files}).status_code == 400
    assert not store.jobs()


def test_download_confined_to_registered_task_outputs(remote_env, tmp_path):
    _, _, store, config, _, request, pair, _, _ = remote_env
    ident = add_job(store, config)
    job = store.get(ident)
    folder = Path(job["folder"])
    folder.mkdir(parents=True)
    document = folder / "lesson.docx"
    document.write_bytes(b"test-document")
    store.db.execute("UPDATE jobs SET document=? WHERE id=?", (str(document), ident))
    pair()
    response = request(f"/api/files/{ident}/document")
    assert response.status_code == 200 and response.data == b"test-document"
    assert "attachment" in response.headers["Content-Disposition"]
    assert request(f"/api/files/{ident}/config").status_code == 404
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    store.db.execute("UPDATE jobs SET document=? WHERE id=?", (str(outside), ident))
    assert request(f"/api/files/{ident}/document").status_code == 404


def test_live_waitress_http_and_shutdown_closes_socket(remote_env):
    _, remote, _, _, _, _, _, base, app = remote_env
    result = []
    def phone():
        with requests.Session() as session:
            headers = {"X-Waraq-Client": "1"}
            response = session.post(base + "/api/pair", json={"code": remote.state()["code"]}, headers=headers, timeout=5)
            result.append(response.status_code)
            result.append(session.get(base + "/api/state", timeout=5).json()["stats"]["total"])
    thread = threading.Thread(target=phone)
    thread.start()
    deadline = time.monotonic() + 8
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    thread.join(1)
    assert not thread.is_alive() and result == [200, 0]
    server_thread = remote._thread
    remote.stop()
    assert not server_thread.is_alive()
    assert not remote.state()["enabled"]
    with pytest.raises(requests.ConnectionError):
        requests.get(base, timeout=1)


def test_timed_out_gui_request_is_never_executed_later(remote_env):
    bridge, remote, _, _, _, _, pair, _, app = remote_env
    pair()
    token = next(iter(remote._sessions))
    result = []
    def phone():
        try:
            remote.call("queue", {"running": True}, token, timeout=0.03)
        except RemoteError as exc:
            result.append(exc.status)
    thread = threading.Thread(target=phone)
    thread.start()
    thread.join(1)  # Deliberately don't pump the GUI event loop until after timeout.
    assert result == [503]
    app.processEvents()
    assert not bridge.running


def test_local_address_scope():
    for address in ("127.0.0.1", "192.168.1.5", "10.0.0.2", "172.31.1.1", "169.254.2.1"):
        assert local_address(address)
    for address in ("8.8.8.8", "0.0.0.0", "172.32.0.1", "::1", "bad"):
        assert not local_address(address)


def test_failed_bind_releases_server_resources_and_can_restart(remote_env):
    _, remote, _, _, _, _, _, _, _ = remote_env
    port = remote.state()["port"]
    remote.stop()
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", port))
        occupied.listen()
        remote.start(port)
        assert not remote.state()["enabled"]
        assert remote.state()["error"]
        assert not remote.state()["code"]
    remote.start(port)
    assert remote.state()["enabled"]
    assert not remote.state()["error"]


def test_revoked_queued_command_cannot_execute(remote_env):
    bridge, remote, _, _, _, _, pair, _, app = remote_env
    pair()
    token = next(iter(remote._sessions))
    errors = []
    def phone():
        try:
            remote.call("queue", {"running": True}, token)
        except RemoteError as exc:
            errors.append(exc.status)
    thread = threading.Thread(target=phone)
    thread.start()
    deadline = time.monotonic() + 1
    while not remote._pending and time.monotonic() < deadline:
        time.sleep(0.001)
    remote.rotate_code()
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    thread.join(1)
    assert errors == [401] and not bridge.running


def test_upload_limit_and_http_body_limit(remote_env):
    _, _, store, _, _, request, pair, _, _ = remote_env
    pair()
    content = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
    assert request("/api/action/upload", {"files": [{"name": "large.txt", "content": content}]}).status_code == 413
    assert request("/api/pair", {"code": "x" * (15 * 1024 * 1024)}).status_code == 413
    assert not store.jobs()
