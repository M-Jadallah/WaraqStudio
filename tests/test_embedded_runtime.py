"""Pinned payload, transport and OS process ownership without remote auth."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from waraq import embedded_runtime as native


def payload(root):
    files = {"node/node.exe": b"node", "opencode/opencode.exe": b"opencode", "plugin/dist/src/index.js": b"export default {}", "plugin/index.js": b"export default {}"}
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = {"schema": 1, "platform": "win32-x64", "node_version": "24.22.0", "opencode_version": "2.0.32",
                "plugin_entry": "plugin/dist/src/index.js", "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return native.PackagedPayload(root)


def runtime():
    result = object.__new__(native.EmbeddedRuntime)
    result.process = SimpleNamespace(poll=lambda: None)
    result.password, result._capability = "private-password", "private-rpc-capability"
    result.url = "http://127.0.0.1:34567"
    result.profile = Path.cwd()
    result._stopping = False
    return result


def test_manifest_requires_exact_versions_all_entry_hashes_and_local_members(tmp_path):
    built = payload(tmp_path)
    assert built.node == tmp_path / "node" / "node.exe"
    (tmp_path / "opencode/opencode.exe").write_bytes(b"tampered")
    with pytest.raises(native.EmbeddedRuntimeError, match="engine_integrity_failed"):
        native.PackagedPayload(tmp_path)


@pytest.mark.parametrize("change", [lambda m: m.update(node_version="latest"), lambda m: m.update(plugin_entry="../outside.js"),
                                  lambda m: m.update(plugin_entry="\\outside.js"), lambda m: m.update(plugin_entry="C:outside.js"),
                                  lambda m: m.update(files={})])
def test_manifest_cannot_select_unpinned_or_external_executables(tmp_path, change):
    payload(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(native.EmbeddedRuntimeError):
        native.PackagedPayload(tmp_path)


def test_environment_uses_packaged_paths_and_private_home_only(tmp_path):
    packaged = payload(tmp_path / "payload")
    root = tmp_path / "accounts-v2"
    inherited = {"PATH": "C:/global-tools", "NODE_PATH": "C:/global-modules", "NODE_OPTIONS": "--require malicious",
                 "OPENCODE_CONFIG_CONTENT": "global-config", "OPENCODE_PASSWORD": "global-password", "OPENAI_API_KEY": "paid-key",
                 "CUSTOM_TOKEN": "secret", "WARAQ_ACCOUNTS_CAPABILITY": "inherited", "XDG_DATA_HOME": "global-data", "SystemRoot": "C:/Windows"}
    env = native.isolated_environment(root, packaged, inherited)
    assert "global" not in json.dumps(env)
    assert "paid-key" not in json.dumps(env) and "secret" not in json.dumps(env)
    assert "WARAQ_ACCOUNTS_CAPABILITY" not in env
    assert env["HOME"] == str(root / "home") and env["TEMP"] == str(root / "tmp")
    assert env["PATH"].split(os.pathsep)[:2] == [str(packaged.node.parent), str(packaged.opencode.parent)]
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"


def test_profile_lock_excludes_second_waraq_process(tmp_path):
    first = native._lock_profile(tmp_path)
    try:
        with pytest.raises(native.ExecutionOwnershipError):
            native._lock_profile(tmp_path)
    finally:
        first.close()
    native._lock_profile(tmp_path).close()


def test_http_transport_is_loopback_authenticated_no_proxy_and_no_redirect(monkeypatch, tmp_path):
    current = runtime()
    seen = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return b'{"output":{"ok":true,"data":{"apiVersion":1}}}'
    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            return Response()
    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()
    monkeypatch.setattr(native, "build_opener", build)
    current.rpc("health", directory=tmp_path)
    request = seen["request"]
    assert request.full_url.startswith(current.url + "/api/rpc/waraq-accounts/health?")
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"opencode:private-password").decode()
    assert json.loads(request.data) == {"input": {"apiVersion": 1, "capability": "private-rpc-capability"}}
    assert isinstance(seen["handlers"][0], native.ProxyHandler)
    assert seen["handlers"][0].proxies == {}
    assert isinstance(seen["handlers"][1], native._NoRedirect)


def test_session_environment_cannot_receive_rpc_or_provider_secrets(tmp_path):
    current = runtime()
    current._lock = threading.RLock()
    current.sessions = set()
    current.env = {"HOME": str(tmp_path), "PATH": "packaged"}
    current.request = Mock(side_effect=[{"data": {"id": "session_1"}}, None])
    assert current.create_session(tmp_path, {"WARAQ_ACCOUNTS_CAPABILITY": "secret", "OPENAI_API_KEY": "secret", "OPENCODE_PASSWORD": "secret", "SAFE": "yes"}) == "session_1"
    variables = current.request.call_args.args[2]["variables"]
    assert variables == {"HOME": str(tmp_path), "PATH": "packaged", "SAFE": "yes",
                         "OPENCODE_PASSWORD": "", "WARAQ_ACCOUNTS_CAPABILITY": "", "WARAQ_ENGINE_INSTANCE_ID": ""}


def test_abort_requires_acknowledgement_and_confirmed_absence():
    current = runtime()
    current.request = Mock(side_effect=native.EmbeddedRuntimeError("engine_connection_failed"))
    current.inactive = Mock(return_value=True)
    assert not current.abort_and_wait("session_1", timeout=0.1)
    current.inactive.assert_not_called()
    current.request = Mock(return_value=None)
    current.release_session = Mock()
    assert current.abort_and_wait("session_1", timeout=0.1)
    current.release_session.assert_called_once_with("session_1")


def test_active_child_prevents_parent_inactivity_proof():
    current=runtime()
    current.request=Mock(side_effect=[{"data":{"child_1":{"type":"running"}}},
                                      {"data":{"id":"child_1","parentID":"session_1"}}])
    assert not current.inactive("session_1")


def test_unrelated_worker_does_not_block_inactivity_proof():
    current=runtime()
    current.request=Mock(side_effect=[{"data":{"other_1":{"type":"running"}}},
                                      {"data":{"id":"other_1"}}])
    assert current.inactive("session_1")


def test_unknown_or_cyclic_active_parentage_never_proves_inactivity():
    current=runtime()
    current.request=Mock(side_effect=[{"data":{"child_1":{"type":"running"}}},
                                      {"data":{"id":"child_1","parentID":"child_1"}}])
    with pytest.raises(native.EmbeddedRuntimeError):
        current.inactive("session_1")


def test_unknown_owner_and_legacy_pid_reuse_never_authorize_replay(tmp_path, monkeypatch):
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path))
    assert not manager.confirm_stopped(instance_id="missing")
    profile = tmp_path / "opencode-accounts" / "account123"
    profile.mkdir(parents=True)
    (profile / "runtime-owner.json").write_text(json.dumps({"pid": 123, "instance_id": "legacy_1"}))
    monkeypatch.setattr(native, "_process_identity", lambda pid: "live_creation")
    assert not manager.confirm_stopped(instance_id="legacy_1", legacy=True, account_id="account123")
    monkeypatch.setattr(native, "_process_identity", lambda pid: None)
    assert manager.confirm_stopped(instance_id="legacy_1", legacy=True, account_id="account123")
    assert not manager.confirm_stopped(instance_id="other", legacy=True, account_id="account123")
    assert not manager.confirm_stopped(instance_id="legacy_1", legacy=True, account_id="../escape")


def test_new_server_preserves_exact_dead_instance_proof(tmp_path, monkeypatch):
    packaged = payload(tmp_path / "payload")
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path), packaged.root)
    manager.root.mkdir()
    owner = {"pid": 123, "created": "old_creation", "instance_id": "old_instance"}
    (manager.root / "runtime-owner.json").write_text(json.dumps(owner))
    monkeypatch.setattr(native, "_process_identity", lambda pid: None)
    monkeypatch.setattr(native, "private_directory", lambda path, recursive=False: path.mkdir(parents=True, exist_ok=True))
    def start(current, timeout):
        current.process = SimpleNamespace(pid=456, poll=lambda: None)
        native._atomic_json(current.profile / "runtime-owner.json", {"pid": 456, "created": "new_creation", "instance_id": current.instance_id})
    monkeypatch.setattr(native.EmbeddedRuntime, "_start", start)
    current = manager.ensure()
    try:
        assert json.loads((manager.root / "runtime-owner.json").read_text())["instance_id"] != "old_instance"
        assert manager.confirm_stopped(instance_id="old_instance")
        assert not manager.confirm_stopped(instance_id="unknown_instance")
    finally:
        current._lease.close()


@pytest.mark.parametrize("owner,identity", [({"pid": 123, "created": "creation", "instance_id": "old_instance"}, "creation"),
                                           ({"pid": 123, "instance_id": "old_instance"}, None),
                                           ({"pid": True, "created": "creation", "instance_id": "old_instance"}, None)])
def test_alive_or_malformed_owner_never_produces_exit_proof(tmp_path, monkeypatch, owner, identity):
    packaged = payload(tmp_path / "payload")
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path), packaged.root)
    manager.root.mkdir()
    (manager.root / "runtime-owner.json").write_text(json.dumps(owner))
    monkeypatch.setattr(native, "_process_identity", lambda pid: identity)
    monkeypatch.setattr(native, "private_directory", lambda path, recursive=False: path.mkdir(parents=True, exist_ok=True))
    with pytest.raises(native.ExecutionOwnershipError):
        manager.ensure()
    assert not (manager.root / "runtime-exits.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership contract")
def test_windows_job_assigns_suspended_child_and_verifies_tree_exit():
    job = native._WindowsJob()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)
    try:
        job.assign_and_resume(process)
        assert not job.empty()
        job.terminate()
        process.wait(timeout=5)
        assert job.empty()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        job.close()
