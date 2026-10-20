"""Pinned payload, transport and OS process ownership without remote auth."""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
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


def junction(alias, target):
    """Create only temporary junction fixtures, without requiring symlink privilege."""
    import _winapi
    alias.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(alias))


def windows_acl_bytes(path):
    """Read the object's own DACL through an OPEN_REPARSE_POINT handle."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi.GetKernelObjectSecurity.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    handle = kernel.CreateFileW(str(path), 0x20080, 3, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        count = wintypes.DWORD()
        advapi.GetKernelObjectSecurity(handle, 4, None, 0, ctypes.byref(count))
        data = ctypes.create_string_buffer(count.value)
        assert advapi.GetKernelObjectSecurity(handle, 4, data, len(data), ctypes.byref(count))
        return data.raw
    finally:
        kernel.CloseHandle(handle)


def windows_denies_world_list_directory(descriptor):
    """Inspect the self-relative DACL, independent of the caller's privileges."""
    assert int.from_bytes(descriptor[2:4], "little") & 0x8000  # SE_SELF_RELATIVE
    dacl = int.from_bytes(descriptor[16:20], "little")
    assert dacl and dacl + 8 <= len(descriptor)
    count = int.from_bytes(descriptor[dacl + 4:dacl + 6], "little")
    cursor = dacl + 8
    world_sid = bytes((1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0))  # S-1-1-0
    for _ in range(count):
        size = int.from_bytes(descriptor[cursor + 2:cursor + 4], "little")
        assert size >= 4 and cursor + size <= len(descriptor)
        if (descriptor[cursor] == 1 and  # ACCESS_DENIED_ACE_TYPE
                int.from_bytes(descriptor[cursor + 4:cursor + 8], "little") & 1 and
                descriptor[cursor + 8:cursor + size] == world_sid):
            return True
        cursor += size
    return False


def test_profile_preparation_cancellation_is_terminal_and_releases_lease(tmp_path, monkeypatch):
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path))
    entered = threading.Event()
    errors = []
    original = native.prepare_private_profile
    def preparing(root, cancelled=None, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(0.005)
        return original(root, cancelled, **kwargs)
    monkeypatch.setattr(native, "prepare_private_profile", preparing)
    manifest = Mock()
    monkeypatch.setattr(native, "PackagedPayload", manifest)
    def ensure():
        try:
            manager.ensure()
        except native.EmbeddedRuntimeError as exc:
            errors.append(exc.reason)
    worker = threading.Thread(target=ensure)
    worker.start()
    assert entered.wait(2)
    manager.close()
    worker.join(2)
    assert not worker.is_alive()
    assert errors == ["engine_closed"]
    manifest.assert_not_called()
    native._lock_profile(manager.root).close()


def test_repeated_stop_does_not_query_closed_job_or_release_lease_twice(tmp_path):
    current = runtime()
    current.process = None
    current.profile = tmp_path
    current.instance_id = "synthetic_instance"
    current._lock = threading.RLock()
    job = current._job = Mock()
    job.empty.return_value = True
    lease = current._lease = Mock()
    current.stop()
    current.stop()
    job.close.assert_called_once()
    lease.close.assert_called_once()
    assert current._job is None and current._lease is None


def test_payload_hash_checks_cancellation_between_chunks(tmp_path, monkeypatch):
    packaged = payload(tmp_path)
    cancelled = threading.Event()
    original = Path.open
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            assert size <= 1024 * 1024
            cancelled.set()
            return self.stream.read(size)
    def opening(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Reader(stream) if path == packaged.node and args == ("rb",) else stream
    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(native.EmbeddedRuntimeError, match="engine_closed"):
        native.PackagedPayload(tmp_path, cancelled=cancelled.is_set)
    assert cancelled.is_set()
    assert not any(t.name.startswith("ThreadPoolExecutor") for t in threading.enumerate())


def test_preparation_and_lock_precede_hashing_with_single_lease(tmp_path, monkeypatch):
    packaged = payload(tmp_path / "payload")
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path), packaged.root)
    original = native.PackagedPayload
    seen = []
    def checked(*args, **kwargs):
        for name in ("home", "config/opencode", "tmp"):
            assert (manager.root / name).is_dir()
        with pytest.raises(native.ExecutionOwnershipError):
            native._lock_profile(manager.root)
        seen.append("hashed")
        return original(*args, **kwargs)
    monkeypatch.setattr(native, "PackagedPayload", checked)
    monkeypatch.setattr(native.EmbeddedRuntime, "_start", lambda current, timeout: None)
    current = manager.ensure()
    try:
        assert seen == ["hashed"]
        with pytest.raises(native.ExecutionOwnershipError):
            native._lock_profile(manager.root)
    finally:
        manager.close()
    native._lock_profile(manager.root).close()


def test_close_interrupts_real_payload_validation_without_live_hash_workers(tmp_path, monkeypatch):
    packaged = payload(tmp_path / "payload")
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path), packaged.root)
    original = Path.open
    entered, errors = threading.Event(), []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            entered.set()
            while not manager._closed:
                time.sleep(0.005)
            return self.stream.read(size)
    def opening(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Reader(stream) if path == packaged.node and args == ("rb",) else stream
    monkeypatch.setattr(Path, "open", opening)
    start = Mock()
    monkeypatch.setattr(native.EmbeddedRuntime, "_start", start)
    def ensure():
        try:
            manager.ensure()
        except native.EmbeddedRuntimeError as exc:
            errors.append(exc.reason)
    worker = threading.Thread(target=ensure)
    worker.start()
    assert entered.wait(2)
    begin = time.monotonic()
    manager.close()
    worker.join(2)
    assert time.monotonic() - begin < 2
    assert not worker.is_alive() and errors == ["engine_closed"]
    assert not any(t.name.startswith("ThreadPoolExecutor") for t in threading.enumerate())
    start.assert_not_called()
    native._lock_profile(manager.root).close()


@pytest.mark.parametrize("member", ["", "home", "data", "config/opencode"])
def test_non_directory_profile_components_fail_before_hashing(tmp_path, monkeypatch, member):
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path))
    file = manager.root / member if member else manager.root
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("user data is preserved")
    manifest = Mock()
    monkeypatch.setattr(native, "PackagedPayload", manifest)
    with pytest.raises(native.EmbeddedRuntimeError, match="private_permissions_failed"):
        manager.ensure()
    manifest.assert_not_called()
    assert file.read_text() == "user data is preserved"


def test_directory_symlink_is_rejected_even_at_exact_cache_alias(tmp_path):
    root = tmp_path / "profile"
    alias = root / native._CACHE_ALIAS
    target = alias.parent / "IE"
    target.mkdir(parents=True)
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable; real junctions are covered separately")
        raise
    try:
        with pytest.raises(native.EmbeddedRuntimeError, match="private_path_is_link"):
            native.prepare_private_profile(root)
    finally:
        alias.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_exact_windows_cache_junction_is_preserved_without_alias_acl_or_walk(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    alias = root / native._CACHE_ALIAS
    target = alias.parent / "IE"
    target.mkdir(parents=True)
    (target / "cache.txt").write_text("preserved")
    junction(alias, target)
    seen = []
    original = native._windows_acl_handle
    original_open = native._PrivateTree.open
    handles = {}
    def opening(tree, path, *args, **kwargs):
        node = original_open(tree, path, *args, **kwargs)
        handles[node.handle] = path
        return node
    def secure(handle, sid):
        seen.append(handles[handle])
        return original(handle, sid)
    monkeypatch.setattr(native._PrivateTree, "open", opening)
    monkeypatch.setattr(native, "_windows_acl_handle", secure)
    try:
        assert native.prepare_private_profile(root) == root
        assert alias.is_junction()
        assert alias not in seen
        assert target in seen and target / "cache.txt" in seen
        assert not any(path.is_relative_to(alias) for path in seen)
        assert (target / "cache.txt").read_text() == "preserved"
        with pytest.raises(native.EmbeddedRuntimeError, match="private_path_is_link"):
            native.private_directory(root, recursive=True)
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows compatibility-junction deny-list ACL")
@pytest.mark.parametrize("probe_access", ["list_directory", "metadata"])
def test_windows_cache_alias_deny_list_directory_is_supported_and_acl_preserved(tmp_path, probe_access):
    from ctypes import wintypes
    root = tmp_path / "profile"
    alias = root / native._CACHE_ALIAS
    target = alias.parent / "IE"
    target.mkdir(parents=True)
    junction(alias, target)
    sid = native._owner_sid()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    advapi.SetKernelObjectSecurity.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    descriptor = ctypes.c_void_p()
    node = native._WindowsPath(alias, writable=True)
    try:
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(D;;0x1;;;WD)(A;;FA;;;SY)(A;;FA;;;" + sid + ")", 1, ctypes.byref(descriptor), None)
        assert advapi.SetKernelObjectSecurity(node.handle, 0x80000004, descriptor)
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        node.close()
    try:
        before = windows_acl_bytes(alias)
        assert windows_denies_world_list_directory(before)
        before_target = os.readlink(alias)
        assert Path(before_target).samefile(target)
        probe = None
        try:
            # Elevated/backup-enabled runners may grant LIST_DIRECTORY despite
            # this deny ACE. Metadata mode exercises successful-probe cleanup
            # even on ordinary Windows accounts where the broader open fails.
            probe = native._WindowsPath(alias, writable=True, metadata_only=probe_access == "metadata")
        except native.EmbeddedRuntimeError as exc:
            assert probe_access == "list_directory" and exc.reason == "private_permissions_failed"
        finally:
            if probe is not None:
                probe.close()
        if probe_access == "metadata":
            assert probe is not None and probe.handle is None
        native.prepare_private_profile(root)
        assert windows_acl_bytes(alias) == before
        assert os.readlink(alias) == before_target
        assert alias.is_junction()
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows cache-candidate handle contract")
def test_ordinary_directory_at_cache_alias_is_secured_and_walked(tmp_path):
    root = tmp_path / "profile"
    candidate = root / native._CACHE_ALIAS
    candidate.mkdir(parents=True)
    child = candidate / "ordinary-cache.txt"
    child.write_text("preserved")
    native.prepare_private_profile(root)
    assert not candidate.is_junction() and child.read_text() == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical DOS path contract")
def test_cache_target_canonical_comparison_accepts_profile_short_path(tmp_path):
    from ctypes import wintypes
    root = tmp_path / "profile-with-a-long-directory-name"
    alias = root / native._CACHE_ALIAS
    target = alias.parent / "IE"
    target.mkdir(parents=True)
    junction(alias, target)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    data = ctypes.create_unicode_buffer(32768)
    try:
        assert kernel.GetShortPathNameW(str(root), data, len(data))
        if data.value == str(root):
            pytest.skip("8.3 names disabled on the temporary filesystem")
        native.prepare_private_profile(Path(data.value))
        assert alias.is_junction()
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("kind", ["outside", "different", "dangling", "other_alias", "linked_target", "linked_ancestor"])
def test_unapproved_profile_junctions_fail_before_payload_hash(tmp_path, monkeypatch, kind):
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path))
    alias = manager.root / native._CACHE_ALIAS
    expected = alias.parent / "IE"
    expected.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("untouched")
    outside_acl = windows_acl_bytes(outside)
    sentinel_acl = windows_acl_bytes(outside / "sentinel.txt")
    links = []
    if kind == "outside":
        target = outside
    elif kind == "different":
        target = alias.parent / "other"
        target.mkdir()
    elif kind == "dangling":
        target = expected
    elif kind == "other_alias":
        alias = alias.parent / "another-cache-link"
        target = expected
    elif kind == "linked_target":
        expected.rmdir()
        junction(expected, outside)
        links.append(expected)
        target = expected
    else:
        home = manager.root / "home"
        home.rename(manager.root / "old-home")
        junction(home, outside)
        links.append(home)
        alias = None
        target = None
    if alias is not None:
        junction(alias, target)
        links.insert(0, alias)
        if kind == "dangling":
            expected.rmdir()
    manifest = Mock()
    monkeypatch.setattr(native, "PackagedPayload", manifest)
    try:
        with pytest.raises(native.EmbeddedRuntimeError, match="private_path_is_link"):
            manager.ensure()
        manifest.assert_not_called()
        assert (outside / "sentinel.txt").read_text() == "untouched"
        assert windows_acl_bytes(outside) == outside_acl
        assert windows_acl_bytes(outside / "sentinel.txt") == sentinel_acl
        native._lock_profile(manager.root).close()
    finally:
        for link in links:
            link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle replacement contract")
def test_profile_handles_prevent_replacement_during_acl_and_alias_validation(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    alias = root / native._CACHE_ALIAS
    target = alias.parent / "IE"
    target.mkdir(parents=True)
    junction(alias, target)
    original = native._WindowsPath.junction_target
    checks = []
    def target_read(node):
        # Both the alias and its real target's parent remain pinned until validation ends.
        for path in (alias, alias.parent, root):
            with pytest.raises(PermissionError):
                path.rename(path.with_name(path.name + "-replaced"))
            checks.append(path)
        return original(node)
    monkeypatch.setattr(native._WindowsPath, "junction_target", target_read)
    try:
        native.prepare_private_profile(root)
        assert checks == [alias, alias.parent, root]
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle replacement contract")
def test_link_inserted_before_open_is_rejected_without_securing_target(tmp_path, monkeypatch):
    root, outside = tmp_path / "profile", tmp_path / "outside"
    (root / "home").mkdir(parents=True)
    outside.mkdir()
    original_open = native._PrivateTree.open
    secured = []
    handles = {}
    def opening(tree, path, *args, **kwargs):
        if path == root:
            (root / "home").rename(root / "original-home")
            junction(root / "home", outside)
        node = original_open(tree, path, *args, **kwargs)
        handles[node.handle] = path
        return node
    monkeypatch.setattr(native._PrivateTree, "open", opening)
    monkeypatch.setattr(native, "_windows_acl_handle", lambda handle, sid: secured.append(handles[handle]))
    try:
        with pytest.raises(native.EmbeddedRuntimeError, match="private_path_is_link"):
            native.prepare_private_profile(root)
        assert outside not in secured and root / "home" not in secured
    finally:
        (root / "home").rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows lock-file junction contract")
def test_linked_lock_file_never_acquires_ownership_or_hashes(tmp_path, monkeypatch):
    manager = native.EmbeddedRuntimeSupervisor(SimpleNamespace(home=tmp_path))
    manager.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    lock = manager.root / "runtime.lock"
    junction(lock, outside)
    manifest = Mock()
    monkeypatch.setattr(native, "PackagedPayload", manifest)
    try:
        with pytest.raises(native.EmbeddedRuntimeError, match="private_path_is_link"):
            manager.ensure()
        manifest.assert_not_called()
        assert not list(outside.iterdir())
    finally:
        lock.rmdir()
