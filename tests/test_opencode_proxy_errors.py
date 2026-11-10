"""Regression coverage for structured CLI Proxy provider errors."""

import json
import threading
from pathlib import Path

import pytest

from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.services import OpenCode, ServiceError
from waraq.store import Store


@pytest.fixture
def task(tmp_path):
    config = Config(tmp_path)
    config.data["opencode_source_mode"] = "cliproxy"
    config.data["cliproxy_model"] = "gpt-6.1-sol"
    config.data["opencode_workers"] = [{
        "id": "oc1", "name": "Proxy", "enabled": True, "provider": "cliproxy",
        "model": "cliproxy/gpt-6.1-sol", "variant": "", "agent": "build",
    }]
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("test transcript", encoding="utf-8")
    store = Store(tmp_path / "tasks.sqlite3")
    ident = store.add([{"video_id": "local", "title": "Test", "position": 1,
                        "url": "https://example.invalid"}], config.snapshot(),
                      input_path=str(transcript))[0]
    try:
        yield store, config, ident
    finally:
        with store.lock:
            store.db.close()


@pytest.mark.parametrize("exit_success", [False, True])
def test_cli_proxy_no_route_is_blocked_and_not_rescheduled(task, monkeypatch, exit_success):
    store, config, ident = task
    monkeypatch.setattr("waraq.services.resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr("waraq.services.opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(OpenCode, "prepare_context", lambda *args: [])
    invoked = []

    def fail_run(command, cwd, cancel, timeout, log, env):
        invoked.append(True)
        log(json.dumps({"type": "error", "error": {
            "type": "provider.no-route", "message": "No route for model",
        }}))
        if exit_success:
            import zipfile

            with zipfile.ZipFile(cwd / "001-Test.docx", "w") as document:
                document.writestr("[Content_Types].xml", "<Types/>")
                document.writestr("_rels/.rels", "<Relationships/>")
                document.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>')
        else:
            raise ServiceError("nonzero OpenCode exit")

    monkeypatch.setattr("waraq.services.run_process", fail_run)
    scheduler = Scheduler(store, config, None, opencode=OpenCode())
    scheduler._cliproxy_secret = lambda: "test-key"
    job = store.get(ident)
    scheduler.execute(job, "opencode", config.data["opencode_workers"][0], "test-key", threading.Event())

    stage = store.get(ident)["stages"]["opencode"]
    assert stage["status"] == "blocked"
    assert invoked
    assert stage["retries"] == 0
    assert stage["next_retry"] == 0
    assert "تعذر على OpenCode توجيه النموذج" in stage["error"]
    assert not any("أُعيدت الجدولة" in event["message"] for event in store.events(50))
    if exit_success:
        assert not list(Path(store.get(ident)["folder"]).rglob("validated.json"))


@pytest.mark.parametrize("event", [
    {"type": "text", "text": "provider.no-route"},
    {"type": "error", "error": {"message": "provider.no-route"}},
    {"type": "error", "error": {"data": {"code": "provider.other"}}},
])
def test_cli_proxy_no_route_requires_exact_structured_code(event):
    from waraq.services import cliproxy_error

    assert cliproxy_error(json.dumps(event)) is None


def test_cli_proxy_no_route_accepts_native_data_code():
    from waraq.services import cliproxy_error

    assert cliproxy_error(json.dumps({"type": "error", "error": {
        "data": {"code": "provider.no-route", "message": "No route for model"},
    }})).kind == "blocked"
