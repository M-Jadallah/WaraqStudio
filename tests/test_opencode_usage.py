"""Deterministic quota tests: synthetic credentials and mocked transport only."""
import hashlib
import io
import json
import ssl
import threading
import time
from email.message import Message
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler

import pytest

from waraq import opencode_runtime as native
from waraq.opencode_usage import (USAGE_REASON_CODES, classify_usage_capability,
                                 oauth_method_metadata, parse_usage_response, usage_result)

NOW = 1_800_000_000


def window(minutes=300, used=25, reset=NOW + 3600):
    return {"limit_window_seconds": minutes * 60, "used_percent": used, "reset_at": reset}


def payload():
    return {"rate_limit": {"primary_window": window(), "secondary_window": window(10080)}}


def entry(method="chatgpt-browser", access="synthetic-access", expires=(NOW + 3600) * 1000):
    return {"id": "cred_test", "active": True, "integrationID": "openai", "value": {
        "type": "oauth", "methodID": method, "access": access, "refresh": "SYNTHETIC_PRIVATE_REFRESH",
        "expires": expires, "metadata": {"sub": "synthetic-sub", "accountID": "synthetic-account"}}}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(native.time, "time", lambda: NOW)
    manager = native.AccountRuntimeManager(SimpleNamespace(home=tmp_path))
    runtime = object.__new__(native.AccountRuntime)
    runtime.manager, runtime.account_id = manager, "account123"
    runtime.instance_id, runtime.native_version = "instance", "2.0.27"
    runtime._lock, runtime._stopping = threading.RLock(), False
    runtime.sessions, runtime.env = set(), {}
    state = {"entry": entry(), "active": {"data": {}}}
    def request(method, path, data=None, **kwargs):
        assert not manager._lock._is_owned(), "diagnostic native IO held the global manager lock"
        if path == "/api/credential":
            return {"data": [state["entry"]] if state["entry"] else []}
        if path == "/api/session/active":
            return state["active"]
        if path == "/api/integration":
            return {"data": []}
        pytest.fail("Unexpected native route: " + path)
    runtime.request = Mock(side_effect=request)
    def stop():
        runtime._stopping = True
    runtime.stop = Mock(side_effect=stop)
    manager._runtimes["account123"] = runtime
    manager.ensure = Mock(side_effect=AssertionError("diagnostic must not heal runtime"))
    identity = native._safe_credential_metadata([state["entry"]])["identity"]
    binding = {"account_id": "account123", "auth_epoch": 3,
               "identity": hashlib.sha256(identity.encode()).hexdigest()}
    return manager, runtime, state, binding


def test_duration_identifies_windows_and_excess_usage_clamps_remaining():
    data = {"rate_limit": {"primary_window": window(10080, 140), "secondary_window": window(300, 0)}}
    result = parse_usage_response(data, NOW)
    assert result["status"] == "available"
    assert result["five_hour"]["remaining_percent"] == 100
    assert result["weekly"]["used_percent"] == 140 and result["weekly"]["remaining_percent"] == 0


def test_missing_meters_are_unknown_and_other_meters_never_fill_main_windows():
    result = parse_usage_response({"rate_limit": {"primary_window": window(60)},
        "additional_rate_limits": [{"rate_limit": {"primary_window": window(300)}}],
        "code_review_rate_limit": payload()["rate_limit"], "refresh": "PRIVATE"}, NOW)
    assert result["status"] == "partial"
    assert result["five_hour"] is None and result["weekly"] is None
    assert result["other_windows"][0]["window_minutes"] == 60
    assert result["buckets"][0]["five_hour"]["used_percent"] == 25
    assert "PRIVATE" not in json.dumps(result)
    assert parse_usage_response({}, NOW)["five_hour"] is None


@pytest.mark.parametrize("field,value", [
    ("used_percent", True), ("used_percent", -1), ("used_percent", "0"),
    ("used_percent", float("inf")), ("used_percent", float("nan")),
    ("reset_at", NOW), ("reset_at", NOW - 1), ("reset_at", 0),
    ("reset_at", (NOW + 10) * 1000), ("reset_at", True),
    ("limit_window_seconds", 0), ("limit_window_seconds", -1),
    ("limit_window_seconds", True),
])
def test_invalid_or_stale_windows_never_become_zero(field, value):
    data = payload()
    data["rate_limit"]["primary_window"][field] = value
    result = parse_usage_response(data, NOW)
    assert result["status"] == "partial" and result["five_hour"] is None
    assert result["weekly"] is not None


def test_duplicate_duration_is_ambiguous_and_buckets_are_bounded():
    result = parse_usage_response({"rate_limit": {"primary_window": window(), "secondary_window": window()},
        "additional_rate_limits": [{"rate_limit": payload()["rate_limit"]}] * 50}, NOW)
    assert result["five_hour"] is None
    assert len(result["buckets"]) == 8


@pytest.mark.parametrize("method,version,reason", [
    ("chatgpt-token-sharing", "2.0.27", "token_sharing_unsupported"),
    ("new-chatgpt-browser", "2.0.27", "method_unsupported"),
    ("chatgpt-browser", "2.0.28", "native_version_unverified"),
    ("chatgpt-headless", "2.0.27-beta", "native_version_unverified"),
])
def test_capability_is_exact(method, version, reason):
    assert classify_usage_capability(method, version)["reason_code"] == reason


@pytest.mark.parametrize("version", ["2.0.27", "2.0.32"])
@pytest.mark.parametrize("method", ["chatgpt-browser", "chatgpt-headless"])
def test_only_acceptance_verified_versions_enable_legacy_usage(version, method):
    assert classify_usage_capability(method, version)["status"] == "available"
    assert oauth_method_metadata(method, version)["usage"]["reason_code"] == "legacy_supported"


@pytest.mark.parametrize("method,kind,label,source", [
    ("chatgpt-token-sharing", "siwc", "Sign in with ChatGPT", ""),
    ("chatgpt-browser", "legacy_codex", "Codex browser (legacy)", "legacy_codex_usage"),
    ("chatgpt-headless", "legacy_codex", "Codex device code (legacy)", "legacy_codex_usage"),
])
def test_method_catalog_classifies_only_known_methods_without_native_labels(method, kind, label, source):
    result = oauth_method_metadata(method, "2.0.27")
    assert result == {"id": method, "type": "oauth", "label": label, "kind": kind,
                      "usage": {"status": "unsupported" if kind == "siwc" else "available",
                                "reason_code": "token_sharing_unsupported" if kind == "siwc" else "legacy_supported",
                                "source": source}}
    if kind == "legacy_codex":
        unverified = oauth_method_metadata(method, "2.1.0")
        assert unverified["usage"]["reason_code"] == "native_version_unverified"
        assert unverified["kind"] == "legacy_codex"


@pytest.mark.parametrize("version", ["2.0.27", "2.0.32"])
@pytest.mark.parametrize("method", [None, [], {}, True, "chatgpt", "CHATGPT-BROWSER", "chatgpt-browser-extra"])
def test_method_catalog_rejects_unknown_ids(method, version):
    assert oauth_method_metadata(method, version) is None
    assert classify_usage_capability(method, version)["reason_code"] == "method_unsupported"


@pytest.mark.parametrize("version", [None, [], {}, 2.027, True, "2.0.27+build"])
def test_invalid_native_version_has_controlled_capability_status(version):
    assert classify_usage_capability("chatgpt-browser", version)["reason_code"] == "native_version_unverified"


def test_capability_key_is_private_cache_only_and_changes_on_config_binary_or_native_version(tmp_path):
    first, second = tmp_path / "first.exe", tmp_path / "second.exe"
    first.write_bytes(b"synthetic native one")
    second.write_bytes(b"synthetic native two")
    settings = {"opencode_executable": str(first)}
    manager = native.AccountRuntimeManager(SimpleNamespace(home=tmp_path, snapshot=lambda: dict(settings)))
    manager.ensure = Mock(side_effect=AssertionError("capability key started native"))
    original = manager.usage_capability_key("account123")
    assert original == manager.usage_capability_key("account123")
    assert str(tmp_path) not in str(original)
    settings["opencode_executable"] = str(second)
    changed_config = manager.usage_capability_key("account123")
    assert original != changed_config
    second.write_bytes(b"synthetic replacement binary different length")
    assert manager.usage_capability_key("account123") != changed_config
    runtime = SimpleNamespace(executable=second, native_version="2.0.27")
    manager._runtimes["account123"] = runtime
    running = manager.usage_capability_key("account123")
    runtime.native_version = "2.0.32"
    assert manager.usage_capability_key("account123") != running
    manager.ensure.assert_not_called()


def test_capability_key_never_waits_for_native_lifecycle_lock(tmp_path):
    manager = native.AccountRuntimeManager(SimpleNamespace(home=tmp_path,
        snapshot=lambda: {"opencode_executable": "synthetic-no-binary"}))
    held, release = threading.Event(), threading.Event()
    def native_operation():
        with manager._lock:
            held.set()
            release.wait(2)
    worker = threading.Thread(target=native_operation)
    worker.start()
    assert held.wait(1)
    try:
        began = time.monotonic()
        key = manager.usage_capability_key("account123")
        assert key and time.monotonic() - began < 0.5
        assert not release.is_set()
    finally:
        release.set()
        worker.join(2)


def test_legacy_camelcase_account_id_controls_identity_and_header():
    first = entry()
    second = entry()
    second["value"]["metadata"]["accountID"] = "different-account"
    assert native._safe_credential_metadata([first])["identity"] != native._safe_credential_metadata([second])["identity"]
    assert native._usage_credential([first])["account"] == "synthetic-account"


def test_read_releases_all_locks_and_keeps_credentials_private(managed, monkeypatch):
    manager, runtime, _, binding = managed
    def transport(access, account, cancel):
        assert access == "synthetic-access" and account == "synthetic-account"
        assert not manager._lock._is_owned() and not runtime._lock._is_owned()
        return parse_usage_response(payload(), NOW)
    monkeypatch.setattr(native, "_usage_https", transport)
    result = manager.read_usage("account123", binding)
    assert result["status"] == "available"
    assert "synthetic" not in json.dumps(result).lower() and "PRIVATE" not in json.dumps(result)
    assert runtime.request.call_count == 2
    manager.ensure.assert_not_called()


@pytest.mark.parametrize("method,version,reason", [
    ("chatgpt-token-sharing", "2.0.27", "token_sharing_unsupported"),
    ("chatgpt-new", "2.0.27", "method_unsupported"),
    ("chatgpt-browser", "2.1.0", "native_version_unverified"),
])
def test_unsupported_makes_zero_remote_calls_even_for_opaque_identity(managed, monkeypatch, method, version, reason):
    manager, runtime, state, binding = managed
    state["entry"]["value"].update(methodID=method, metadata={})
    runtime.native_version = version
    remote = Mock(side_effect=AssertionError("unsupported backend route"))
    monkeypatch.setattr(native, "_usage_https", remote)
    assert manager.read_usage("account123", binding)["reason_code"] == reason
    remote.assert_not_called()


def test_read_does_not_start_absent_or_heal_unhealthy_runtime(managed, monkeypatch):
    manager, runtime, _, binding = managed
    runtime.request.side_effect = native.RuntimeError("SYNTHETIC_PRIVATE_REFRESH")
    assert manager.read_usage("account123", binding)["reason_code"] == "native_unavailable"
    manager._runtimes.clear()
    assert manager.read_usage("account123", binding)["reason_code"] == "runtime_pending_idle"
    manager.ensure.assert_not_called()
    runtime.stop.assert_not_called()


@pytest.mark.parametrize("change", ["identity", "id", "method", "epoch", "runtime", "deleted"])
def test_remote_result_revalidates_account_lifetime(managed, monkeypatch, change):
    manager, runtime, state, binding = managed
    def transport(*args):
        if change == "identity":
            state["entry"]["value"]["metadata"]["sub"] = "another-sub"
        elif change == "id":
            state["entry"]["id"] = "another-credential"
        elif change == "method":
            state["entry"]["value"]["methodID"] = "chatgpt-headless"
        elif change == "epoch":
            manager._usage_epochs["account123"] = 1
        elif change == "runtime":
            manager._runtimes["account123"] = object()
        else:
            state["entry"] = None
        return parse_usage_response(payload(), NOW)
    monkeypatch.setattr(native, "_usage_https", transport)
    result = manager.read_usage("account123", binding)
    assert result["status"] in ("unavailable", "auth_unavailable")
    assert result["five_hour"] is None


@pytest.mark.parametrize("rotates,identity_changes,expected_calls", [(False, False, 1), (True, False, 2), (True, True, 1)])
def test_401_retries_only_once_for_native_rotation_with_same_identity(managed, monkeypatch, rotates, identity_changes, expected_calls):
    manager, _, state, binding = managed
    calls = []
    def transport(access, *args):
        calls.append(access)
        if rotates:
            state["entry"]["value"]["access"] = "rotated-synthetic-access"
        if identity_changes:
            state["entry"]["value"]["metadata"]["sub"] = "another-sub"
        return usage_result("auth_unavailable", "unauthorized")
    monkeypatch.setattr(native, "_usage_https", transport)
    result = manager.read_usage("account123", binding)
    assert len(calls) == expected_calls
    assert result["status"] == "auth_unavailable"


def test_expiry_defers_without_remote_or_restart_and_cancel_is_immediate(managed, monkeypatch):
    manager, runtime, state, binding = managed
    state["entry"]["value"]["expires"] = NOW * 1000
    remote = Mock(side_effect=AssertionError("expired access used"))
    monkeypatch.setattr(native, "_usage_https", remote)
    assert manager.read_usage("account123", binding)["reason_code"] == "expired_pending_idle"
    cancel = threading.Event()
    cancel.set()
    assert manager.read_usage("account123", binding, cancel)["status"] == "cancelled"
    remote.assert_not_called()
    runtime.stop.assert_not_called()


class Response(io.BytesIO):
    pass


def mock_http(monkeypatch, response=None, error=None):
    calls = []
    def build(*handlers):
        calls.append(handlers)
        return SimpleNamespace(open=Mock(side_effect=error, return_value=response))
    monkeypatch.setattr(native, "build_opener", build)
    return calls


def test_https_is_fixed_get_verified_tls_without_proxy_redirect_and_allowlisted(monkeypatch):
    monkeypatch.setattr(native.time, "time", lambda: NOW)
    calls = []
    def build(*handlers):
        proxy = next(h for h in handlers if isinstance(h, ProxyHandler))
        tls = next(h for h in handlers if isinstance(h, HTTPSHandler))
        assert proxy.proxies == {}
        assert tls._context.verify_mode == ssl.CERT_REQUIRED and tls._context.check_hostname
        assert any(isinstance(h, native._NoRedirect) for h in handlers)
        def open_request(req, timeout):
            calls.append(req)
            assert req.full_url == "https://chatgpt.com/backend-api/wham/usage"
            assert req.get_method() == "GET" and req.data is None and timeout == 5
            assert req.get_header("Authorization") == "Bearer SYNTHETIC"
            assert req.get_header("Chatgpt-account-id") == "synthetic-account"
            return Response(json.dumps({**payload(), "account_id": "synthetic-account", "token": "PRIVATE"}).encode())
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(native, "build_opener", build)
    result = native._usage_https("SYNTHETIC", "synthetic-account", None)
    assert result["status"] == "available" and len(calls) == 1
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("code,status,reason", [(401, "auth_unavailable", "unauthorized"),
    (403, "unsupported", "endpoint_unavailable"), (404, "unsupported", "endpoint_unavailable"),
    (429, "unavailable", "throttled"), (500, "unavailable", "http_error"),
    (302, "unavailable", "http_error")])
def test_http_errors_are_safe_and_never_change_login(monkeypatch, code, status, reason):
    headers = Message()
    headers["Retry-After"] = "123"
    error = HTTPError(native._USAGE_URL, code, "SYNTHETIC_PRIVATE_REFRESH", headers, Response(b"PRIVATE"))
    mock_http(monkeypatch, error=error)
    result = native._usage_https("synthetic", "synthetic-account", None)
    assert result["status"] == status and result["reason_code"] == reason
    assert result.get("retry_after", 0) == (123 if code == 429 else 0)
    assert "PRIVATE" not in json.dumps(result)


def test_retry_after_http_date_and_long_delay_not_shortened(monkeypatch):
    monkeypatch.setattr(native.time, "time", lambda: NOW)
    assert native._retry_after({"Retry-After": formatdate(NOW + 120, usegmt=True)}) == 120
    assert native._retry_after({"Retry-After": "172800"}) == 172800
    for value in ("nan", "inf", "-1", "PRIVATE", "99999999999999999"):
        assert native._retry_after({"Retry-After": value}) == 0


@pytest.mark.parametrize("body,reason", [(b"x" * (256 * 1024 + 1), "response_too_large"),
    (b"{broken PRIVATE", "connection_failed"), (b'{"account_id":"OTHER"}', "binding_changed")],
    ids=["oversized", "invalid_json", "wrong_account"])
def test_oversized_invalid_or_wrong_account_response_is_private(monkeypatch, body, reason):
    mock_http(monkeypatch, response=Response(body))
    result = native._usage_https("synthetic", "synthetic-account", None)
    assert result["reason_code"] == reason and result["five_hour"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_transport_exception_and_mid_read_cancellation_are_private(monkeypatch):
    mock_http(monkeypatch, error=URLError("SYNTHETIC_PRIVATE_REFRESH"))
    assert native._usage_https("synthetic", "synthetic-account", None)["reason_code"] == "connection_failed"
    cancel = threading.Event()
    class CancellingResponse(Response):
        def read(self, count):
            cancel.set()
            return super().read(count)
    mock_http(monkeypatch, response=CancellingResponse(json.dumps(payload()).encode()))
    assert native._usage_https("synthetic", "synthetic-account", cancel)["status"] == "cancelled"


@pytest.mark.parametrize("active,tracked,reason", [
    ({"data": {"untracked": {}}}, set(), "account_busy"),
    ({"data": []}, set(), "activity_unverified"), (None, set(), "activity_unverified"),
    ({"data": {}}, {"tracked"}, "account_busy")])
def test_idle_refresh_requires_tracked_and_native_idle_proof(managed, active, tracked, reason):
    manager, runtime, state, binding = managed
    state["active"], runtime.sessions = active, tracked
    result = manager.restart_idle_for_usage("account123", {**binding, "is_valid": lambda: True})
    assert result["reason_code"] == reason
    runtime.stop.assert_not_called()
    manager.ensure.assert_not_called()
    assert not manager._usage_maintenance


def test_idle_refresh_requires_reservation_and_proven_shutdown(managed):
    manager, runtime, _, binding = managed
    assert manager.restart_idle_for_usage("account123", binding)["reason_code"] == "maintenance_unreserved"
    assert manager.restart_idle_for_usage("account123", {**binding, "is_valid": lambda: False})["status"] == "cancelled"
    runtime.stop.side_effect = native.RuntimeError("PRIVATE")
    assert manager.restart_idle_for_usage("account123", {**binding, "is_valid": lambda: True})["reason_code"] == "refresh_unavailable"
    assert manager._runtimes["account123"] is runtime
    manager.ensure.assert_not_called()


@pytest.mark.parametrize("close", [False, True])
def test_stop_cancel_or_close_between_owners_never_starts_new_owner(managed, close):
    manager, runtime, _, binding = managed
    cancel = threading.Event()
    def stop():
        if close:
            manager._closed = True
        else:
            cancel.set()
    runtime.stop.side_effect = stop
    result = manager.restart_idle_for_usage("account123", {**binding, "is_valid": lambda: True}, cancel)
    assert result["status"] == "cancelled"
    manager.ensure.assert_not_called()


def test_idle_refresh_starts_after_shutdown_and_uses_native_init_without_inference(managed):
    manager, old, _, binding = managed
    new = SimpleNamespace(_lock=threading.RLock(), _stopping=False, instance_id="new", native_version="2.0.27")
    def request(method, path, **kwargs):
        assert not manager._lock._is_owned()
        assert method == "GET"
        assert path in ("/api/credential", "/api/integration")
        return {"data": [entry()]} if path == "/api/credential" else {"data": []}
    new.request = Mock(side_effect=request)
    def ensure(account_id, **kwargs):
        old.stop.assert_called_once()
        assert old._stopping and account_id not in manager._runtimes
        assert account_id in manager._usage_maintenance
        manager._runtimes[account_id] = new
        return new
    manager.ensure = Mock(side_effect=ensure)
    result = manager.restart_idle_for_usage("account123", {**binding, "is_valid": lambda: True})
    assert result["reason_code"] == "native_refreshed"
    assert new.request.call_args.args == ("GET", "/api/integration")
    assert not manager._usage_maintenance


def test_maintenance_blocks_session_creation_before_native_request(managed):
    manager, runtime, _, _ = managed
    manager._usage_maintenance.add("account123")
    with pytest.raises(native.RuntimeError, match="reserved"):
        runtime.create_session(".")
    runtime.request.assert_not_called()


def test_controlled_reason_codes_cover_all_current_parser_runtime_outputs():
    assert {"binding_changed", "runtime_pending_idle", "expired_pending_idle", "native_refreshed"} <= USAGE_REASON_CODES


def test_start_guard_cancels_before_any_native_process(tmp_path, monkeypatch):
    manager = native.AccountRuntimeManager(SimpleNamespace(home=tmp_path))
    profile = manager.profile_path("account123")
    profile.mkdir(parents=True)
    lease, popen = Mock(), Mock()
    monkeypatch.setattr(native, "_profile_lock", lambda path: lease)
    monkeypatch.setattr(native.subprocess, "Popen", popen)
    with pytest.raises(native.RuntimeError, match="cancelled"):
        native.AccountRuntime(manager, "account123", profile, "fake-native", start_guard=lambda: False)
    popen.assert_not_called()
    lease.close.assert_called_once()


def test_close_can_cancel_cold_start_without_waiting_for_announcement_deadline(tmp_path, monkeypatch):
    manager = native.AccountRuntimeManager(SimpleNamespace(home=tmp_path, snapshot=lambda: {"opencode_executable": "fake"}))
    started, errors = threading.Event(), []
    original_thread = threading.Thread
    process = Mock(pid=1234)
    process.poll.return_value = 0
    lease = Mock()
    def private_directory(path, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        return path
    def popen(*args, **kwargs):
        assert not manager._lock._is_owned()
        return process
    monkeypatch.setattr(native, "_private_directory", private_directory)
    monkeypatch.setattr(native, "_profile_lock", lambda path: lease)
    monkeypatch.setattr(native.subprocess, "Popen", popen)
    monkeypatch.setattr("waraq.services.resolve_opencode", lambda value: "fake")
    monkeypatch.setattr("waraq.services.opencode_version", lambda value: "2.0.27")
    monkeypatch.setattr("waraq.config.atomic_json", Mock())
    # Keep the announcement queue empty; no native child or reader is started.
    monkeypatch.setattr(native.threading, "Thread", lambda **kwargs: SimpleNamespace(start=started.set))
    def ensure():
        try:
            manager.ensure("account123")
        except native.RuntimeError as exc:
            errors.append(str(exc))
    worker = original_thread(target=ensure)
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    manager.close()
    worker.join(2)
    assert not worker.is_alive() and time.monotonic() - began < 2
    assert errors == ["Account runtime startup was cancelled."]
    process.stdin.close.assert_called_once()
    lease.close.assert_called_once()
    assert not manager._runtimes
