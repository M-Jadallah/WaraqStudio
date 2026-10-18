"""Native-session proof gates account release, replay, and the Word commit."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid
import zipfile

import pytest

from waraq.config import defaults
import waraq.services as services


def write_docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>test</w:t></w:r></w:p></w:body></w:document>')


@pytest.fixture
def managed(tmp_path, monkeypatch):
    transcript = tmp_path / "source.txt"
    transcript.write_text("immutable source", encoding="utf-8")
    cfg = dict(defaults(tmp_path), _embedded_accounts=True)
    job = {"id": "job", "transcript": str(transcript), "folder": str(tmp_path / "job"), "title": "test", "position": 1}
    worker = {"id": "oc1", "model": "openai/mock", "agent": "build", "variant": "high"}
    runtime = Mock(executable="opencode.exe", url="http://127.0.0.1:1234", password="private-password",
                   env={}, instance_id="instance")
    runtime.create_session.return_value = "ses_test"
    runtime.inactive.return_value = True
    runtime.abort_and_wait.return_value = True
    accounts = Mock(supervisor=SimpleNamespace(ensure=lambda *a: runtime))
    accounts.resolve_attempt.return_value = {"failure": None}
    accounts.begin_attempt.side_effect = lambda *a, **kw: {"attempt_id": uuid.uuid4().hex, "account_id": "opaque", "alias": "Account"}
    service = services.OpenCode(accounts=accounts)
    monkeypatch.setattr(service, "prepare_context", lambda work, cfg: ["skill_modules"])
    return service, accounts, runtime, job, worker, cfg


def test_exact_session_and_audit_precede_model_request(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    audited, finished = [], []
    cfg["_execution_started"] = lambda attempt: audited.append(("begin", dict(attempt)))
    cfg["_execution_session"] = lambda *args: audited.append(("session", args))
    cfg["_execution_finished"] = lambda *args: finished.append(args)
    def run(command, cwd, cancel, timeout, track, env):
        assert [item[0] for item in audited] == ["begin", "session"]
        assert audited[0][1]["workpath"] == str(cwd.resolve())
        assert env["PWD"] == str(cwd.resolve()) and env["NODE_PATH"] == "skill_modules"
        assert env["OPENCODE_PASSWORD"] == "private-password"
        assert command[command.index("--session") + 1] == "ses_test"
        assert command[command.index("--model") + 1] == "openai/mock#high"
        write_docx(cwd / "001-test.docx")
    monkeypatch.setattr(services, "run_process", run)
    output = service.process(job, worker, cfg, "ignored-api-key", threading.Event(), lambda _: None)
    assert output.is_file() and (output.parent / "validated.json").is_file()
    runtime.release_session.assert_called_once_with("ses_test")
    accounts.close_attempt.assert_called_once_with(audited[0][1]["attempt_id"], outcome="completed")
    assert finished == [(audited[0][1]["attempt_id"], "completed")]
    # Restart/retry adopts a previously validated commit without a new model request.
    assert service.process(job, worker, cfg, "", threading.Event(), lambda _: None) == output
    assert accounts.begin_attempt.call_count == 1


@pytest.mark.parametrize("abort_ok,kind", [(True, "cancelled"), (False, "execution_uncertain")])
def test_cancel_preserves_binding_until_server_stop_is_proven(managed, monkeypatch, abort_ok, kind):
    service, accounts, runtime, job, worker, cfg = managed
    runtime.abort_and_wait.return_value = abort_ok
    cancel, finished = threading.Event(), []
    cfg["_execution_started"] = lambda _: None
    cfg["_execution_finished"] = lambda *args: finished.append(args)
    def run(command, cwd, *args):
        write_docx(cwd / "001-test.docx")
        cancel.set()
        raise services.Cancelled()
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", cancel, lambda _: None)
    assert exc.value.kind == kind and finished[0][1] == kind
    assert accounts.close_attempt.call_count == int(abort_ok)
    assert not list(Path(job["folder"]).glob("word-*/validated.json"))


def test_callback_cancel_aborts_before_native_request(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    cancel = threading.Event()
    cfg["_execution_session"] = lambda *a: cancel.set()
    monkeypatch.setattr(services, "run_process", lambda *a: pytest.fail("cancelled job submitted"))
    with pytest.raises(services.Cancelled):
        service.process(job, worker, cfg, "", cancel, lambda _: None)
    runtime.abort_and_wait.assert_called_once()


def test_changed_txt_does_not_hide_uncertain_execution(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    runtime.abort_and_wait.return_value = False
    def run(*args):
        Path(job["transcript"]).write_text("changed while still active")
        raise services.ServiceError("timeout")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "execution_uncertain"
    accounts.close_attempt.assert_not_called()


def test_quota_aborts_once_and_next_attempt_has_fresh_directory(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    attempts = []
    def run(command, cwd, cancel, timeout, track, env):
        attempts.append(cwd)
        if len(attempts) == 1:
            write_docx(cwd / "001-test.docx")
            track('{"type":"error","error":{"code":"subscription_sharing_usage_limit_exceeded"}}')
            track('{"type":"error","error":{"message":"generic wrapper"}}')
            raise services.ServiceError("CLI failed")
        assert not (cwd / "001-test.docx").exists()
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited" and exc.value.confirmed
    runtime.abort_and_wait.assert_called_once()
    assert accounts.close_attempt.call_args.kwargs["outcome"] == "limit"
    with pytest.raises(services.ServiceError, match="لم ينشئ"):
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert attempts[0] != attempts[1]
    assert not list(Path(job["folder"]).glob("word-*/validated.json"))


def test_failed_binding_close_creates_replay_hold(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    finished = []
    cfg["_execution_started"] = lambda _: None
    cfg["_execution_finished"] = lambda *args: finished.append(args)
    accounts.close_attempt.side_effect = RuntimeError("do not expose native body")
    monkeypatch.setattr(services, "run_process", lambda command, cwd, *args: write_docx(cwd / "001-test.docx"))
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "execution_uncertain" and finished[0][1] == "execution_uncertain"


def test_plugin_quota_classification_survives_generic_cli_wrapper(managed, monkeypatch):
    service, accounts, runtime, job, worker, cfg = managed
    accounts.resolve_attempt.return_value = {"failure": {"kind": "quota", "confirmed": True, "retry_at": 2000000000}}
    def run(*args):
        raise services.ServiceError("CLI failed")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited" and exc.value.confirmed and exc.value.reset_at == 2000000000
    runtime.abort_and_wait.assert_called_once()
    accounts.close_attempt.assert_called_once()
    assert accounts.close_attempt.call_args.kwargs["outcome"] == "limit"


@pytest.mark.parametrize("payload,kind", [
    ({"status": 429, "body": '{"error":{"code":"subscription_sharing_usage_limit_exceeded","reset_at":2000000000}}'}, "rate_limited"),
    ({"status": 401}, "account_auth"), ({"status": 403, "code": "unsupported_country"}, "blocked"),
    ({"status": 403}, "failed"), ({"code": "model_not_found"}, "model_unavailable"),
])
def test_structured_error_classification(payload, kind):
    error = services.opencode_error(json.dumps({"type": "error", "error": payload}), "opaque")
    assert error.kind == kind and error.account_id == "opaque"


def test_model_text_quoting_quota_error_is_not_classified():
    line = json.dumps({"type": "text", "part": {"text": "subscription_sharing_usage_limit_exceeded"}})
    assert not services.limit_reached(line) and services.opencode_error(line) is None
