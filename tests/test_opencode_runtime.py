"""Account boundaries and replay proof, without real credentials or model calls."""
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from waraq import opencode_runtime as native
from waraq import services
from waraq.config import defaults


def config(root):
    return SimpleNamespace(home=root, snapshot=lambda: defaults(root))


def jwt(subject="person-1", account="workspace-1"):
    payload = {"sub": subject, "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "plus"},
               "https://api.openai.com/profile": {"email": "person@example.test"}}
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


def credential(access=None, method="chatgpt-token-sharing", active=True):
    return {"id": "cred_test", "integrationID": "openai", "active": active,
            "value": {"type": "oauth", "methodID": method, "access": access or jwt(),
                      "refresh": "secret-refresh", "metadata": {"clientID": "shared-client-id"}}}


def test_environment_discards_global_identity_configuration_and_provider_secrets(tmp_path):
    inherited = {"PATH": "tools", "SystemRoot": "windows", "NODE_PATH": "modules",
                 "OPENCODE_DB": "global.sqlite", "OPENCODE_CONFIG_CONTENT": "global settings",
                 "OPENCODE_PASSWORD": "old", "OPENAI_API_KEY": "paid", "CUSTOM_APIKEY": "paid",
                 "HF_TOKEN": "token", "AWS_PROFILE": "default", "XDG_DATA_HOME": "global"}
    env = native.isolated_environment(tmp_path, inherited)
    assert env["PATH"] == "tools"
    assert not {"OPENCODE_DB", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PASSWORD", "OPENAI_API_KEY",
                "CUSTOM_APIKEY", "HF_TOKEN", "AWS_PROFILE"}.intersection(env)
    for key in ("DATA", "STATE", "CACHE", "CONFIG"):
        assert env[f"XDG_{key}_HOME"] == str(tmp_path / key.lower())
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
    assert env["HOME"] == str(tmp_path / "home")


def test_safe_metadata_keeps_only_stable_identity_and_labels():
    first = native._safe_credential_metadata([credential()])
    assert first["authenticated"] and first["identity_verified"]
    assert len(first["identity"]) == 64
    assert first["email"] == "person@example.test"
    assert first["subscription"] == "plus"
    assert "secret-refresh" not in json.dumps(first)
    assert "shared-client-id" not in json.dumps(first)
    assert first["identity"] == native._safe_credential_metadata([credential()])["identity"]
    assert first["identity"] != native._safe_credential_metadata([credential(jwt("person-2"))])["identity"]


def test_opaque_token_never_uses_oauth_client_id_as_person_identity():
    result = native._safe_credential_metadata([credential("opaque-token")])
    assert result["authenticated"]
    assert result["identity"] == "" and not result["identity_verified"]
    assert not native._safe_credential_metadata([credential(active=False)])["authenticated"]


@pytest.mark.parametrize("ident", ["../escape", "x", "C:/absolute", "..\\bad", "a" * 81])
def test_profile_id_cannot_escape_root(tmp_path, ident):
    with pytest.raises(native.RuntimeError):
        native.AccountRuntimeManager(config(tmp_path)).profile_path(ident)


def test_two_processes_cannot_claim_profile(tmp_path):
    lease = native._profile_lock(tmp_path)
    try:
        with pytest.raises(native.ExecutionOwnershipError):
            native._profile_lock(tmp_path)
    finally:
        lease.close()
    native._profile_lock(tmp_path).close()


def test_reconcile_does_not_spawn_and_live_marker_cannot_clear_hold(tmp_path, monkeypatch):
    manager = native.AccountRuntimeManager(config(tmp_path))
    profile = manager.profile_path("account123")
    profile.mkdir(parents=True)
    (profile / "runtime-owner.json").write_text('{"pid":1234,"instance_id":"old"}')
    monkeypatch.setattr(native, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(manager, "ensure", lambda *a: pytest.fail("reconciliation started native process"))
    assert not manager.confirm_stopped("account123", "session", "old")
    monkeypatch.setattr(native, "_pid_alive", lambda pid: False)
    assert manager.confirm_stopped("account123", "session", "old")


@pytest.mark.parametrize("phase", ["owner_write", "reader_start"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_startup_failure_preserves_owner_until_child_exit_is_proved(tmp_path, monkeypatch, phase, cleanup_fails):
    manager = native.AccountRuntimeManager(config(tmp_path))
    profile = manager.profile_path("account123")
    (profile / "home").mkdir(parents=True)
    lease = Mock()
    process = Mock(pid=1234)
    process.poll.return_value = None if cleanup_fails else 0
    if cleanup_fails:
        process.wait.side_effect = subprocess.TimeoutExpired("native", 5)
        monkeypatch.setattr(native.subprocess, "run", Mock(side_effect=OSError("kill failed")))
        monkeypatch.setattr(native.os, "killpg", Mock(side_effect=OSError("kill failed")), raising=False)
    monkeypatch.setattr(native, "_profile_lock", lambda _: lease)
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=process))
    if phase == "owner_write":
        monkeypatch.setattr("waraq.config.atomic_json", Mock(side_effect=OSError("disk unavailable")))
    else:
        monkeypatch.setattr(native.threading, "Thread", Mock(return_value=SimpleNamespace(
            start=Mock(side_effect=OSError("cannot create reader")))))
    expected = native.ExecutionOwnershipError if cleanup_fails else native.RuntimeError
    with pytest.raises(expected):
        native.AccountRuntime(manager, "account123", profile, "opencode")
    process.stdin.close.assert_called_once()
    process.wait.assert_called()
    if cleanup_fails:
        lease.close.assert_not_called()
        assert manager._runtimes["account123"].process is process
        process.wait.side_effect = None
        process.poll.return_value = 0
        manager.close()
        lease.close.assert_called_once()
        assert not manager._runtimes
    else:
        lease.close.assert_called()
        assert not manager._runtimes


@pytest.mark.parametrize("body", ["[]", "null", "0", "{}", "{broken"])
def test_malformed_owner_record_cannot_clear_hold_or_start_second_owner(tmp_path, monkeypatch, body):
    manager = native.AccountRuntimeManager(config(tmp_path))
    profile = manager.profile_path("account123")
    profile.mkdir(parents=True)
    (profile / "runtime-owner.json").write_text(body)
    assert not manager.confirm_stopped("account123", "session", "old")
    monkeypatch.setattr(native, "_private_directory", lambda path, **kw: Path(path))
    monkeypatch.setattr(native.subprocess, "Popen", Mock(side_effect=AssertionError("second owner started")))
    with pytest.raises(native.ExecutionOwnershipError):
        manager.ensure("account123")


def test_reconcile_startup_failure_stops_idle_owner_but_preserves_other_sessions(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    runtime = Mock(instance_id="instance", sessions={"another-session"}, _lock=threading.RLock())
    runtime.request.return_value = {"data": {}}
    manager._runtimes["account123"] = runtime
    assert not manager.confirm_stopped("account123", "")
    runtime.stop.assert_not_called()
    runtime.sessions.clear()
    runtime.stop.side_effect = native.RuntimeError("shutdown unverified")
    assert not manager.confirm_stopped("account123", "")
    assert manager._runtimes["account123"] is runtime
    runtime.stop.side_effect = None
    assert manager.confirm_stopped("account123", "")
    assert not manager._runtimes
    runtime.abort_and_wait.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], {"data": None}, {"data": []}, {}])
def test_malformed_active_sessions_never_proves_inactivity(payload):
    runtime = object.__new__(native.AccountRuntime)
    runtime.request = Mock(return_value=payload)
    runtime._lock = threading.RLock()
    runtime.sessions = {"session"}
    with pytest.raises(native.RuntimeError):
        runtime.inactive("session")
    assert not runtime.abort_and_wait("session", timeout=0.1)
    assert runtime.sessions == {"session"}


@pytest.mark.parametrize("pid", [None, "123", -1, 0, True, 2**40])
def test_invalid_pid_never_proves_old_execution_stopped(pid):
    assert native._pid_alive(pid)


@pytest.mark.parametrize("wait_result,alive", [(258, True), (0xFFFFFFFF, True), (0, False)])
def test_windows_64bit_handle_is_preserved_and_wait_failure_is_closed(monkeypatch, wait_result, alive):
    kernel = SimpleNamespace(OpenProcess=Mock(return_value=2**48 + 123),
                             WaitForSingleObject=Mock(return_value=wait_result), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    assert native._windows_pid_alive(123) is alive
    kernel.WaitForSingleObject.assert_called_once_with(2**48 + 123, 0)
    kernel.CloseHandle.assert_called_once_with(2**48 + 123)
    assert kernel.OpenProcess.restype == ctypes.wintypes.HANDLE


@pytest.mark.parametrize("last_error,alive", [(87, False), (5, True), (6, True)])
def test_windows_pid_lookup_unknown_failure_does_not_release_hold(monkeypatch, last_error, alive):
    kernel = SimpleNamespace(OpenProcess=Mock(return_value=0), WaitForSingleObject=Mock(), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    assert native._windows_pid_alive(123) is alive


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DACL")
def test_windows_profile_repairs_preexisting_explicit_everyone_grant(tmp_path):
    import subprocess
    profile = tmp_path / "profile"
    profile.mkdir()
    database = profile / "credential.sqlite"
    database.write_bytes(b"fake-secret-test")
    subprocess.run(["icacls", str(database), "/grant", "*S-1-1-0:F"], capture_output=True, check=True)
    native._private_directory(profile, recursive=True)
    result = subprocess.run(["icacls", str(database)], capture_output=True, text=True, check=True)
    assert "Everyone" not in result.stdout and "S-1-1-0" not in result.stdout


def test_oauth_uses_discovered_method_and_cancel_releases_ownership(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    calls = []
    def request(method, path, data=None):
        calls.append((method, path, data))
        if path == "/api/integration":
            return {"data": [{"id": "openai", "methods": [{"id": "chatgpt-token-sharing", "type": "oauth", "label": "ChatGPT"},
                {"type": "key"}]}]}
        if method == "POST":
            return {"data": {"attemptID": "con_test", "url": "https://auth.openai.com/authorize?state=native",
                             "mode": "auto", "time": {"expires": 4000000000000}}}
        if method == "GET":
            return {"data": {"status": "pending"}}
    runtime = SimpleNamespace(request=request)
    manager.ensure = lambda *args: runtime
    result = manager.begin_login("account123")
    assert result["mode"] == "auto" and result["attempt_id"] == "con_test"
    assert calls[-1][2] == {"methodID": "chatgpt-token-sharing"}
    assert result["method_id"] == "chatgpt-token-sharing"
    with pytest.raises(native.RuntimeError, match="current"):
        manager.begin_login("account456")
    assert manager.finish_login("account123")["status"] == "pending"
    manager.cancel_login("account456")
    assert manager._login is not None
    manager.cancel_login("account123")
    assert manager._login is None
    assert calls[-1][0] == "DELETE"


def test_oauth_rejects_unsafe_url_and_cancels_native_attempt(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    runtime = SimpleNamespace(request=Mock(return_value={"data": {"attemptID": "con_test",
                "url": "http://attacker.invalid/", "mode": "auto"}}))
    manager.ensure = lambda *a: runtime
    manager.auth_methods = lambda *a: [{"id": "chatgpt", "label": "ChatGPT"}]
    with pytest.raises(native.RuntimeError, match="authorization URL"):
        manager.begin_login("account123")
    assert runtime.request.call_args.args[0] == "DELETE"
    assert manager._login is None


def test_auth_methods_are_known_oauth_allowlist_without_native_labels(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    response = {"data": [{"id": "other", "methods": [{"id": "chatgpt-browser", "type": "oauth"}]},
        {"id": "openai", "methods": [None, {}, {"id": [], "type": "oauth"},
            {"id": "chatgpt-token-sharing", "type": "oauth", "label": "PRIVATE_NATIVE_TEXT"},
            {"id": "unknown-chatgpt", "type": "oauth", "label": "ChatGPT"},
            {"id": "chatgpt-browser", "type": "key"},
            {"id": "chatgpt-browser", "type": "oauth", "label": "PRIVATE_NATIVE_TEXT"},
            {"id": "chatgpt-headless", "type": "oauth"},
            {"id": "chatgpt-browser", "type": "oauth"}]}]}
    runtime = SimpleNamespace(native_version="2.0.27", request=Mock(return_value=response))
    manager.ensure = Mock(return_value=runtime)
    methods = manager.auth_methods("account123")
    assert [m["id"] for m in methods] == ["chatgpt-token-sharing", "chatgpt-browser", "chatgpt-headless"]
    assert methods[0]["kind"] == "siwc" and methods[0]["usage"]["source"] == ""
    assert methods[1]["kind"] == "legacy_codex" and methods[1]["usage"]["status"] == "available"
    assert "PRIVATE_NATIVE_TEXT" not in json.dumps(methods)


@pytest.mark.parametrize("method", ["chatgpt-browser", "chatgpt-headless"])
def test_explicit_legacy_login_selects_exact_method_even_when_siwc_is_first(tmp_path, method):
    manager = native.AccountRuntimeManager(config(tmp_path))
    calls = []
    def request(verb, path, data=None):
        calls.append((verb, path, data))
        if path == "/api/integration":
            return {"data": [{"id": "openai", "methods": [
                {"id": ident, "type": "oauth"} for ident in
                ("chatgpt-token-sharing", "chatgpt-browser", "chatgpt-headless")]}]}
        return {"data": {"attemptID": "con_synthetic", "url": "https://auth.openai.com/authorize?state=synthetic",
                         "mode": "auto", "time": {"expires": 4000000000000}}}
    manager.ensure = Mock(return_value=SimpleNamespace(native_version="2.0.27", request=request))
    result = manager.begin_login("account123", method=method)
    assert calls[-1] == ("POST", "/api/integration/openai/connect/oauth", {"methodID": method})
    assert result["method_id"] == method


@pytest.mark.parametrize("method", ["unknown-chatgpt", "chatgpt", "CHATGPT-BROWSER", "", [], {}, 1, False])
def test_explicit_unknown_login_method_is_rejected_before_native_access(tmp_path, method):
    manager = native.AccountRuntimeManager(config(tmp_path))
    manager.ensure = Mock(side_effect=AssertionError("unknown method reached native"))
    with pytest.raises(native.RuntimeError, match="Unknown"):
        manager.begin_login("account123", method=method)
    manager.ensure.assert_not_called()
    assert manager._login is None and not manager._usage_epochs


@pytest.mark.parametrize("method", ["chatgpt-browser", "chatgpt-headless"])
def test_explicit_unavailable_login_method_never_falls_back_to_siwc(tmp_path, method):
    manager = native.AccountRuntimeManager(config(tmp_path))
    runtime = SimpleNamespace(native_version="2.0.27", request=Mock(return_value={"data": [
        {"id": "openai", "methods": [{"id": "chatgpt-token-sharing", "type": "oauth"}]}]}))
    manager.ensure = Mock(return_value=runtime)
    with pytest.raises(native.RuntimeError, match="unavailable"):
        manager.begin_login("account123", method=method)
    assert runtime.request.call_args.args == ("GET", "/api/integration")
    assert runtime.request.call_count == 1 and manager._login is None and not manager._usage_epochs


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}, {"data": []},
    {"data": [{"id": "openai", "methods": {}}]}])
def test_malformed_native_auth_catalog_never_creates_login(tmp_path, payload):
    manager = native.AccountRuntimeManager(config(tmp_path))
    runtime = SimpleNamespace(native_version="2.0.27", request=Mock(return_value=payload))
    manager.ensure = Mock(return_value=runtime)
    with pytest.raises(native.RuntimeError):
        manager.begin_login("account123", method="chatgpt-browser")
    assert runtime.request.call_count == 1 and manager._login is None


def test_subscription_gate_rejects_active_key_even_if_profile_configured(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    runtime = SimpleNamespace(request=lambda *a: {"data": [{"integrationID": "openai", "active": True,
                                                           "value": {"type": "key", "key": "paid"}}]})
    manager.ensure = lambda *a: runtime
    manager.auth_methods = lambda *a: [{"id": "chatgpt-token-sharing"}]
    with pytest.raises(services.ServiceError) as exc:
        manager.require_subscription("account123")
    assert exc.value.kind == "account_auth"


def test_close_stops_all_servers_after_cancel_error(tmp_path):
    manager = native.AccountRuntimeManager(config(tmp_path))
    first, second = Mock(), Mock()
    first.request.side_effect = native.RuntimeError("down")
    manager._runtimes = {"account123": first, "account456": second}
    manager._login = ("account123", first, "con_test", "auto")
    manager.close()
    first.stop.assert_called_once()
    second.stop.assert_called_once()
    assert manager._login is None


@pytest.mark.parametrize("payload,kind", [
    ({"status": 429, "message": "Too many requests"}, "throttled"),
    ({"status": 429, "body": '{"error":{"code":"subscription_sharing_usage_limit_exceeded","reset_at":2000000000}}'}, "rate_limited"),
    ({"status": 401}, "account_auth"),
    ({"status": 403, "code": "unsupported_country"}, "blocked"),
    ({"status": 403}, "failed"),
    ({"code": "subscription_sharing_route_not_supported"}, "blocked"),
    ({"code": "subscription_sharing_unsupported_capability"}, "blocked"),
    ({"code": "model_not_found"}, "model_unavailable"),
])
def test_structured_error_classification(payload, kind):
    error = services.opencode_error(json.dumps({"type": "error", "error": payload}), "account123")
    assert error.kind == kind and error.account_id == "account123"
    if kind == "rate_limited":
        assert error.confirmed and error.reset_at == 2000000000
        assert error.scope == "account"


def test_model_text_quoting_quota_error_is_not_classified():
    line = json.dumps({"type": "text", "part": {"text": "subscription_sharing_usage_limit_exceeded"}})
    assert not services.limit_reached(line)
    assert services.opencode_error(line) is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, "Infinity", "NaN"])
def test_nonfinite_or_negative_reset_metadata_is_ignored(value):
    error = services.opencode_error(json.dumps({"type": "error", "error": {
        "code": "subscription_sharing_usage_limit_exceeded", "reset_at": value, "retry_after": value}}))
    assert error.reset_at == 0 and error.retry_after == 0


def test_confirmed_quota_is_not_lost_to_unknown_wrapper():
    specific = services.opencode_error('{"type":"error","error":{"code":"subscription_sharing_usage_limit_exceeded"}}')
    summary = services.opencode_error('{"type":"error","error":{"message":"command failed"}}')
    assert services.preferred_opencode_error([specific, summary]) is specific


def write_docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>test</w:t></w:r></w:p></w:body></w:document>')


@pytest.fixture
def managed(tmp_path, monkeypatch):
    transcript = tmp_path / "source.txt"
    transcript.write_text("immutable source", encoding="utf-8")
    cfg = defaults(tmp_path)
    cfg["_chatgpt_account_id"] = "account123"
    job = {"transcript": str(transcript), "folder": str(tmp_path / "job"), "title": "test", "position": 1}
    worker = {"id": "oc1", "model": "openai/mock", "agent": "build", "variant": "high"}
    runtime = Mock(executable="opencode.exe", url="http://127.0.0.1:1234", password="private-password",
                   env={}, instance_id="instance")
    runtime.create_session.return_value = "ses_test"
    runtime.inactive.return_value = True
    runtime.abort_and_wait.return_value = True
    manager = SimpleNamespace(ensure=lambda *a: runtime, require_subscription=lambda *a: None)
    service = services.OpenCode(manager)
    monkeypatch.setattr(service, "prepare_context", lambda work, cfg: ["skill_modules"])
    return service, runtime, job, worker, cfg


def test_managed_uses_exact_session_env_and_commits_only_success(managed, monkeypatch):
    service, runtime, job, worker, cfg = managed
    commands, started = [], []
    cfg["_chatgpt_session_started"] = lambda *args: started.append(args)
    def run(command, cwd, cancel, timeout, track, env):
        commands.append(command)
        assert env["PWD"] == str(cwd.resolve()) and env["NODE_PATH"] == "skill_modules"
        assert env["OPENCODE_PASSWORD"] == "private-password"
        write_docx(cwd / "001-test.docx")
    monkeypatch.setattr(services, "run_process", run)
    output = service.process(job, worker, cfg, "ignored-api-key", threading.Event(), lambda _: None)
    assert output.is_file() and (output.parent / "validated.json").is_file()
    assert "--standalone" not in commands[0] and "--server" in commands[0]
    assert commands[0][commands[0].index("--session") + 1] == "ses_test"
    assert len(started) == 1
    runtime.release_session.assert_called_with("ses_test")


@pytest.mark.parametrize("abort_ok,kind", [(True, "cancelled"), (False, "execution_uncertain")])
def test_cancel_proves_server_inactive_before_releasing_attempt(managed, monkeypatch, abort_ok, kind):
    service, runtime, job, worker, cfg = managed
    runtime.abort_and_wait.return_value = abort_ok
    cancel = threading.Event()
    def run(command, cwd, *args):
        write_docx(cwd / "001-test.docx")
        cancel.set()
        raise services.Cancelled()
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", cancel, lambda _: None)
    assert exc.value.kind == kind
    runtime.abort_and_wait.assert_called()
    assert not list(Path(job["folder"]).glob("word-*/validated.json"))


def test_callback_cancel_also_interrupts_native_session(managed, monkeypatch):
    service, runtime, job, worker, cfg = managed
    cancel = threading.Event()
    cfg["_chatgpt_session_started"] = lambda *a: cancel.set()
    monkeypatch.setattr(services, "run_process", lambda *a: pytest.fail("cancelled job submitted"))
    with pytest.raises(services.Cancelled):
        service.process(job, worker, cfg, "", cancel, lambda _: None)
    runtime.abort_and_wait.assert_called_once()


def test_changed_txt_never_overrides_execution_uncertainty(managed, monkeypatch):
    service, runtime, job, worker, cfg = managed
    runtime.abort_and_wait.return_value = False
    def run(*args):
        Path(job["transcript"]).write_text("changed while still active")
        raise services.ServiceError("timeout")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "execution_uncertain"


def test_quota_interrupt_is_not_repeated_after_successful_proof(managed, monkeypatch):
    service, runtime, job, worker, cfg = managed
    runtime.abort_and_wait.side_effect = [True, False]
    def run(command, cwd, cancel, timeout, track, env):
        track('{"type":"error","error":{"code":"subscription_sharing_usage_limit_exceeded"}}')
        track('{"type":"error","error":{"message":"generic wrapper"}}')
        raise services.ServiceError("process failed")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited"
    runtime.abort_and_wait.assert_called_once()


def test_failed_candidate_never_adopted_on_next_account_attempt(managed, monkeypatch):
    service, runtime, job, worker, cfg = managed
    attempts = []
    def run(command, cwd, cancel, timeout, track, env):
        attempts.append(cwd)
        if len(attempts) == 1:
            write_docx(cwd / "001-test.docx")
            track(json.dumps({"type": "error", "error": {"code": "subscription_sharing_usage_limit_exceeded"}}))
            raise services.ServiceError("CLI failed")
        assert not (cwd / "001-test.docx").exists()
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(services.ServiceError) as exc:
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited"
    cfg["_chatgpt_account_id"] = "account456"
    with pytest.raises(services.ServiceError, match="لم ينشئ"):
        service.process(job, worker, cfg, "", threading.Event(), lambda _: None)
    assert attempts[0] != attempts[1]
    assert not list(Path(job["folder"]).glob("word-*/validated.json"))
