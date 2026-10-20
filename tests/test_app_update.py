from __future__ import annotations

import hashlib
import threading
import time
import os
from pathlib import Path

import pytest
import requests
from qt_test_support import dispose_bridge

from waraq import app_update
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


CONTENT = b"MZ" + b"installer-content" * 100


def release_data(version="1.0.0.20", content=CONTENT):
    name = f"WaraqStudio-{version}-setup.exe"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": name, "state": "uploaded", "size": len(content),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                        "url": f"https://api.github.com/repos/{app_update.REPOSITORY}/releases/assets/123",
                        "browser_download_url": f"https://github.com/{app_update.REPOSITORY}/releases/download/v{version}/{name}"}]}


class Response:
    def __init__(self, data=None, content=CONTENT, status=200, before_chunk=None):
        self.data, self.content, self.status_code = data, content, status
        self.ok = status < 400
        self.before_chunk = before_chunk

    def json(self):
        return self.data

    def iter_content(self, size):
        for chunk in (self.content[:2], self.content[2:]):
            if self.before_chunk:
                self.before_chunk()
            yield chunk

    def __enter__(self): return self
    def __exit__(self, *args): pass


class Session:
    def __init__(self, responses):
        self.headers, self.responses, self.calls = {}, list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def __enter__(self): return self
    def __exit__(self, *args): pass


def fake_session(monkeypatch, *responses):
    session = Session(responses)
    monkeypatch.setattr(app_update.requests, "Session", lambda: session)
    return session


@pytest.mark.parametrize("older,newer", [("1.0.0.9", "1.0.0.10"), ("1.0.0", "1.0.0.1"), ("1.0.0.99", "1.0.1")])
def test_version_order_is_numeric(older, newer):
    assert app_update.version_tuple(newer) > app_update.version_tuple(older)
    assert app_update.version_tuple("v1.0.0") == app_update.version_tuple("1.0.0.0")


def test_latest_stable_release_and_private_auth(monkeypatch):
    session = fake_session(monkeypatch, Response(release_data()))
    result = app_update.check_latest("1.0.0.19", "private-token")
    assert result["available"] and result["version"] == "1.0.0.20"
    assert session.headers["Authorization"] == "Bearer private-token"
    assert session.calls[0][0] == app_update.RELEASE_API


def test_equal_or_older_release_does_not_offer_downgrade(monkeypatch):
    fake_session(monkeypatch, Response(release_data()))
    assert not app_update.check_latest("1.0.0.20")["available"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(prerelease=True),
    lambda d: d.update(tag_name="v1.0.bad"),
    lambda d: d.update(assets=[]),
    lambda d: d["assets"][0].update(digest=None),
    lambda d: d["assets"][0].update(size=True),
    lambda d: d["assets"][0].update(browser_download_url="https://evil.example/setup.exe"),
    lambda d: d["assets"][0].update(url="https://api.github.com/repos/other/repo/releases/assets/123"),
])
def test_rejects_unverifiable_release_metadata(monkeypatch, mutation):
    data = release_data()
    mutation(data)
    fake_session(monkeypatch, Response(data))
    with pytest.raises(app_update.UpdateError):
        app_update.check_latest("1.0.0")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_github_access_failures_are_actionable_and_hide_credentials(monkeypatch, status):
    fake_session(monkeypatch, Response(status=status))
    with pytest.raises(app_update.UpdateError) as error:
        app_update.check_latest("1.0.0", "secret-token")
    assert "secret-token" not in str(error.value)
    assert str(error.value)


def checked_release(monkeypatch, content=CONTENT):
    fake_session(monkeypatch, Response(release_data(content=content)))
    return app_update.check_latest("1.0.0")


def test_download_checks_hash_size_and_atomic_commit(monkeypatch, tmp_path):
    release = checked_release(monkeypatch)
    session = fake_session(monkeypatch, Response())
    progress = []
    target = app_update.download_installer(tmp_path, release, threading.Event(), progress.append, "token")
    assert target.read_bytes() == CONTENT
    assert progress[-1] == 100
    assert session.calls[0][0] == release["api_url"]
    assert session.calls[0][1]["headers"]["Accept"] == "application/octet-stream"
    assert not list(target.parent.glob("*.part"))
    app_update.verify_installer(target, release)
    target.write_bytes(CONTENT[:-1] + b"x")
    with pytest.raises(app_update.UpdateError):
        app_update.verify_installer(target, release)


@pytest.mark.parametrize("content", [CONTENT[:-3], CONTENT + b"extra", b"MZ" + b"x" * (len(CONTENT) - 2)],
                         ids=["truncated", "oversized", "hash-mismatch"])
def test_failed_download_preserves_existing_file_and_cleans_partial(monkeypatch, tmp_path, content):
    release = checked_release(monkeypatch)
    target = tmp_path / "updates" / release["name"]
    target.parent.mkdir()
    target.write_bytes(b"previous")
    fake_session(monkeypatch, Response(content=content))
    with pytest.raises(app_update.UpdateError):
        app_update.download_installer(tmp_path, release, threading.Event())
    assert target.read_bytes() == b"previous"
    assert list(target.parent.iterdir()) == [target]


def test_rejects_non_windows_file_even_with_matching_digest(monkeypatch, tmp_path):
    release = checked_release(monkeypatch, b"not-executable")
    fake_session(monkeypatch, Response(content=b"not-executable"))
    with pytest.raises(app_update.UpdateError):
        app_update.download_installer(tmp_path, release, threading.Event())
    assert not list((tmp_path / "updates").iterdir())


@pytest.mark.parametrize("before_start", [True, False])
def test_cancel_removes_partial_file(monkeypatch, tmp_path, before_start):
    release = checked_release(monkeypatch)
    cancel = threading.Event()
    if before_start:
        cancel.set()
    fake_session(monkeypatch, Response(before_chunk=cancel.set))
    with pytest.raises(app_update.UpdateCancelled):
        app_update.download_installer(tmp_path, release, cancel)
    assert not list((tmp_path / "updates").iterdir())


def test_network_failure_does_not_leak_private_token(monkeypatch):
    fake_session(monkeypatch, requests.ConnectionError("secret-token appears in error"))
    with pytest.raises(app_update.UpdateError) as error:
        app_update.check_latest("1.0.0", "secret-token")
    assert "secret-token" not in str(error.value)


class Secrets:
    def __init__(self): self.values = {}
    def get(self, name): return self.values.get(name, "")
    def set(self, name, value): self.values[name] = value
    def delete(self, name): self.values.pop(name, None)


@pytest.fixture
def bridge_env(tmp_path, scheduler_harness, qt_app):
    app = qt_app
    cfg, store, secrets = Config(tmp_path), Store(tmp_path / "tasks.db"), Secrets()
    scheduler = Scheduler(store, cfg, secrets)
    bridge = Bridge(store, cfg, secrets, scheduler)
    bridge.timer.stop()
    try:
        yield app, bridge, store, cfg, scheduler, secrets
    finally:
        bridge.closeAccounts()
        scheduler_harness.shutdown_and_join()
        dispose_bridge(bridge)
        with store.lock:
            store.db.close()


def wait_state(app, bridge, status):
    deadline = time.monotonic() + 3
    while bridge.appUpdate["status"] != status and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert bridge.appUpdate["status"] == status, bridge.appUpdate


def test_bridge_check_download_and_secret_store(monkeypatch, bridge_env, tmp_path):
    app, bridge, store, cfg, scheduler, secrets = bridge_env
    bridge.saveUpdateToken("secret-token")
    assert bridge.appUpdate["hasToken"]
    assert "secret-token" not in cfg.path.read_text(encoding="utf-8")
    release = checked_release(monkeypatch)
    called = []
    def check(version, token):
        called.append(token)
        return release
    def download(home, selected, cancel, progress, token):
        assert selected == release and token == "secret-token"
        progress(60)
        return tmp_path / release["name"]
    monkeypatch.setattr(app_update, "check_latest", check)
    monkeypatch.setattr(app_update, "download_installer", download)
    bridge.checkAppUpdate()
    wait_state(app, bridge, "available")
    bridge.downloadAppUpdate()
    wait_state(app, bridge, "downloaded")
    assert bridge.appUpdate["progress"] == 100 and called == ["secret-token"]
    bridge.saveUpdateToken("")
    assert not bridge.appUpdate["hasToken"] and not secrets.values


def test_bridge_update_error_is_retryable(monkeypatch, bridge_env):
    app, bridge, *_ = bridge_env
    def check(*args): raise app_update.UpdateError("اتصال غير متاح")
    monkeypatch.setattr(app_update, "check_latest", check)
    bridge.checkAppUpdate()
    wait_state(app, bridge, "error")
    monkeypatch.setattr(app_update, "check_latest", lambda *a: {"available": False, "version": "1.0.0"})
    bridge.checkAppUpdate()
    wait_state(app, bridge, "up_to_date")


@pytest.mark.parametrize("busy", ["running", "library", "youtube"])
def test_bridge_installer_requires_paused_drained_queue(monkeypatch, bridge_env, tmp_path, busy):
    app, bridge, store, cfg, scheduler, secrets = bridge_env
    bridge._update_release = {"name": "installer.exe"}
    bridge._update_installer = tmp_path / "installer.exe"
    bridge._set_app_update(status="downloaded")
    if busy == "running":
        scheduler.running = True
    else:
        setattr(bridge, "_library_busy" if busy == "library" else "_youtube_updating", True)
    notices = []
    bridge.notice.connect(lambda text, error: notices.append((text, error)))
    bridge.installAppUpdate()
    assert notices[-1][1] and "أوقف الطابور" in notices[-1][0]


@pytest.mark.skipif(os.name != "nt", reason="Windows installer launch")
def test_bridge_installer_verifies_launches_then_quits(monkeypatch, bridge_env, tmp_path):
    import waraq.bridge as bridge_module
    app, bridge, store, cfg, scheduler, secrets = bridge_env
    release = checked_release(monkeypatch)
    target = tmp_path / release["name"]
    target.write_bytes(CONTENT)
    bridge._update_release, bridge._update_installer = release, target
    bridge._set_app_update(status="downloaded")
    launched, quit_calls = [], []
    monkeypatch.setattr(bridge_module.os, "startfile", lambda path, verb: launched.append((path, verb)))
    monkeypatch.setattr(bridge_module.QGuiApplication, "quit", lambda: quit_calls.append(True))
    bridge.installAppUpdate()
    assert launched == [(str(target), "runas")]
    assert quit_calls == [True] and scheduler.stop_event.is_set()


@pytest.mark.skipif(os.name != "nt", reason="Windows installer launch")
def test_bridge_installer_uac_cancel_keeps_app_running(monkeypatch, bridge_env, tmp_path):
    import waraq.bridge as bridge_module
    app, bridge, store, cfg, scheduler, secrets = bridge_env
    release = checked_release(monkeypatch)
    target = tmp_path / release["name"]
    target.write_bytes(CONTENT)
    bridge._update_release, bridge._update_installer = release, target
    bridge._set_app_update(status="downloaded")
    def reject(*args): raise OSError("cancelled")
    quit_calls = []
    monkeypatch.setattr(bridge_module.os, "startfile", reject)
    monkeypatch.setattr(bridge_module.QGuiApplication, "quit", lambda: quit_calls.append(True))
    bridge.installAppUpdate()
    assert not quit_calls and not scheduler.stop_event.is_set()
    assert bridge.appUpdate["status"] == "downloaded"


def test_txt_queue_display_exposes_word_only(bridge_env, tmp_path):
    app, bridge, store, cfg, scheduler, secrets = bridge_env
    path = tmp_path / "lesson.TXT"
    path.write_text("نص الدرس", encoding="utf-8")
    bridge.importTranscriptPaths([path])
    bridge.refresh()
    assert [step["name"] for step in bridge.jobs[0]["steps"]] == ["opencode"]
    assert set(store.jobs()[0]["stages"]) == {"download", "deepgram", "opencode"}


def test_packaged_version_stamp(monkeypatch, tmp_path):
    import waraq
    package_file = tmp_path / "__init__.py"
    monkeypatch.setattr(waraq, "__file__", str(package_file))
    stamp = tmp_path / "_build_version.txt"
    stamp.write_text("1.0.0.20", encoding="utf-8")
    assert waraq._packaged_version() == "1.0.0.20"
    stamp.write_text("invalid", encoding="utf-8")
    assert waraq._packaged_version() == waraq.__version__
