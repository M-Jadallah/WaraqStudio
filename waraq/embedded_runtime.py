"""Private, pinned OpenCode process used by the embedded account engine.

There is one writable home and one server, independent of worker/account count.
Credentials and account selection belong to the packaged plugin, never Python.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


OPENCODE_VERSION = "2.0.32"
NODE_VERSION = "24.22.0"
PLUGIN_ID = "waraq-accounts"
API_VERSION = 1


class EmbeddedRuntimeError(Exception):
    """Only fixed error codes cross the runtime boundary, never server bodies."""
    def __init__(self, reason="engine_unavailable"):
        self.reason = reason
        super().__init__(reason)


class ExecutionOwnershipError(EmbeddedRuntimeError):
    kind = "execution_uncertain"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _reject_links(path):
    for part in (Path(path), *Path(path).parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise EmbeddedRuntimeError("private_path_is_link")


def _check_cancelled(cancelled=None):
    if cancelled is not None and cancelled():
        raise EmbeddedRuntimeError("engine_closed")


class _WindowsPath:
    """Pin a path without following its final reparse point or sharing delete."""
    def __init__(self, path, writable=False, create=False, share_write=False, metadata_only=False):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        self.kernel.DeviceIoControl.restype = wintypes.BOOL
        # Include READ_DATA/LIST_DIRECTORY: attribute-only handles do not enforce
        # Windows sharing restrictions and therefore cannot prevent replacement.
        access = 0x60081 if writable else 0x81  # READ_CONTROL, WRITE_DAC, READ_ATTRIBUTES, READ_DATA
        if metadata_only:
            # Windows' compatibility junction denies LIST_DIRECTORY to Everyone.
            # DELETE access still enforces no-delete sharing without reading its contents.
            access = 0x70080 if writable else 0x10080
        if create:
            access |= 0xC0000000  # lock file needs read/write
        self.handle = self.kernel.CreateFileW(str(path), access, 3 if create or share_write else 1, None, 4 if create else 3,
                                              0x02200000, None)  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise EmbeddedRuntimeError("private_permissions_failed")
        info = (wintypes.DWORD * 2)()
        if not self.kernel.GetFileInformationByHandleEx(self.handle, 9, info, ctypes.sizeof(info)):
            self.close()
            raise EmbeddedRuntimeError("private_permissions_failed")
        self.attributes, self.tag = info

    def junction_target(self):
        from ctypes import wintypes
        data, count = ctypes.create_string_buffer(16384), wintypes.DWORD()
        if self.tag != 0xA0000003 or not self.kernel.DeviceIoControl(
                self.handle, 0x900A8, None, 0, data, len(data), ctypes.byref(count), None):
            raise EmbeddedRuntimeError("private_path_is_link")
        raw = data.raw[:count.value]
        try:
            tag, size, _, offset, length, _, _ = struct.unpack_from("<IHHHHHH", raw)
            if tag != 0xA0000003 or size + 8 > len(raw) or length % 2 or 16 + offset + length > len(raw):
                raise ValueError()
            target = raw[16 + offset:16 + offset + length].decode("utf-16-le")
            if not target.startswith("\\??\\"):
                raise ValueError()
            return target[4:]
        except (ValueError, struct.error, UnicodeError):
            raise EmbeddedRuntimeError("private_path_is_link") from None

    def canonical_path(self):
        from ctypes import wintypes
        self.kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        self.kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        count = self.kernel.GetFinalPathNameByHandleW(self.handle, None, 0, 0)
        data = ctypes.create_unicode_buffer(count + 1)
        if not count or not self.kernel.GetFinalPathNameByHandleW(self.handle, data, len(data), 0):
            raise EmbeddedRuntimeError("private_path_is_link")
        value = data.value
        return value[4:] if value.startswith("\\\\?\\") else value

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _windows_acl_handle(handle, sid):
    """Set only this pinned object's ACL; never propagate through reparse aliases."""
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetKernelObjectSecurity.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)
    advapi.SetKernelObjectSecurity.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    descriptor = ctypes.c_void_p()
    # New children inherit privacy; existing children are secured through their own handles.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid + ")", 1, ctypes.byref(descriptor), None):
        raise EmbeddedRuntimeError("private_permissions_failed")
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present:
            raise EmbeddedRuntimeError("private_permissions_failed")
        # Unlike SetSecurityInfo's tree propagation, this changes this object only.
        if not advapi.SetKernelObjectSecurity(handle, 0x80000004, descriptor):
            raise EmbeddedRuntimeError("private_permissions_failed")
    finally:
        kernel.LocalFree(descriptor)


def _owner_sid():
    from ctypes import wintypes
    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    token, count, text = wintypes.HANDLE(), wintypes.DWORD(), wintypes.LPWSTR()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise EmbeddedRuntimeError("private_owner_unknown")
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(count))
        data = ctypes.create_string_buffer(count.value)
        if not advapi.GetTokenInformation(token, 1, data, len(data), ctypes.byref(count)):
            raise EmbeddedRuntimeError("private_owner_unknown")
        sid = ctypes.c_void_p.from_buffer(data).value
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise EmbeddedRuntimeError("private_owner_unknown")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


_CACHE_ALIAS = Path("home/AppData/Local/Microsoft/Windows/INetCache/Content.IE5")


class _PrivateTree:
    """Pin ancestors before inspecting descendants, and mutate only opened objects."""
    def __init__(self, root, cancelled=None):
        self.root, self.cancelled = Path(root).absolute(), cancelled
        self.stack = ExitStack()
        self.descendants = ExitStack()
        self.nodes = {}
        try:
            for path in reversed((self.root, *self.root.parents)):
                _check_cancelled(cancelled)
                parent = self.nodes.get(path.parent)
                if parent is not None:
                    try:
                        if os.name == "nt":
                            path.mkdir(exist_ok=True)
                        else:
                            os.mkdir(path.name, dir_fd=parent)
                    except FileExistsError:
                        pass
                node = self.open(path, writable=path == self.root)
                self.reject(node, directory=True)
                self.nodes[path] = node
        except BaseException:
            self.close()
            raise

    def open(self, path, writable=True):
        _check_cancelled(self.cancelled)
        if os.name == "nt":
            cache_candidate = path == self.root / _CACHE_ALIAS
            node = _WindowsPath(path, writable=writable,
                                share_write=not path.is_relative_to(self.root) or path == self.root / "runtime.lock",
                                metadata_only=cache_candidate)
            stack = self.descendants if path != self.root and path.is_relative_to(self.root) else self.stack
            stack.callback(node.close)
        else:
            parent = self.nodes.get(path.parent)
            try:
                node = os.open(path.name if parent is not None else path,
                               os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (40, 62):
                    raise EmbeddedRuntimeError("private_path_is_link") from None
                raise EmbeddedRuntimeError("private_permissions_failed") from None
            stack = self.descendants if path != self.root and path.is_relative_to(self.root) else self.stack
            stack.callback(os.close, node)
        return node

    @staticmethod
    def reject(node, directory=False):
        if os.name == "nt" and node.attributes & 0x400:
            raise EmbeddedRuntimeError("private_path_is_link")
        if directory and not (node.attributes & 0x10 if os.name == "nt" else stat.S_ISDIR(os.fstat(node).st_mode)):
            raise EmbeddedRuntimeError("private_permissions_failed")

    def prepare(self, recursive=True, allow_cache_alias=False, required=()):
        sid = _owner_sid() if os.name == "nt" else None
        def secure(node):
            _check_cancelled(self.cancelled)
            if os.name == "nt":
                _windows_acl_handle(node.handle, sid)
            else:
                os.fchmod(node, 0o700 if stat.S_ISDIR(os.fstat(node).st_mode) else 0o600)
        # Pin required directories before walking. Never create a directory via a link.
        for suffix in required:
            path = self.root
            for name in Path(suffix).parts:
                parent, path = path, path / name
                if path in self.nodes:
                    continue
                try:
                    if os.name == "nt":
                        path.mkdir(exist_ok=True)
                    else:
                        os.mkdir(name, dir_fd=self.nodes[parent])
                except FileExistsError:
                    pass
                node = self.open(path)
                self.reject(node, directory=True)
                self.nodes[path] = node
        secure(self.nodes[self.root])
        aliases = []
        def walk(path):
            _check_cancelled(self.cancelled)
            names = os.listdir(path if os.name == "nt" else self.nodes[path])
            for name in names:
                _check_cancelled(self.cancelled)
                child = path / name
                node = self.nodes.get(child)
                if node is None:
                    node = self.open(child)
                    if os.name == "nt" and node.attributes & 0x400:
                        if allow_cache_alias and child == self.root / _CACHE_ALIAS:
                            target = node.junction_target()
                            expected = child.parent / "IE"
                            aliases.append((expected, target))
                            continue  # Neither ACL mutation nor traversal through the alias.
                        self.reject(node)
                    self.nodes[child] = node
                secure(node)
                directory = bool(node.attributes & 0x10) if os.name == "nt" else stat.S_ISDIR(os.fstat(node).st_mode)
                if directory:
                    walk(child)
        if recursive:
            walk(self.root)
        for target, substitute in aliases:
            # Walking the real tree has opened/pinned and secured IE and every ancestor.
            node = self.nodes.get(target)
            if node is None or not node.attributes & 0x10:
                raise EmbeddedRuntimeError("private_path_is_link")
            # Compare only the exact real sibling (including the canonical DOS
            # spelling when the profile itself was supplied through an 8.3 path).
            allowed = {os.path.normcase(str(target)), os.path.normcase(node.canonical_path())}
            if os.path.normcase(substitute) not in allowed:
                raise EmbeddedRuntimeError("private_path_is_link")
        return self.root

    def close(self):
        self.descendants.close()
        self.stack.close()

    def release_descendants(self):
        self.descendants.close()
        self.nodes = {path: node for path, node in self.nodes.items() if path == self.root or not path.is_relative_to(self.root)}

    def allow_runtime_writes(self):
        if os.name == "nt":
            # Overlap the pins: retain replacement protection while allowing the
            # owned engine to create/rename its cache files beneath the root.
            old = self.nodes[self.root]
            node = _WindowsPath(self.root, writable=True, share_write=True)
            self.stack.callback(node.close)
            self.reject(node)
            self.nodes[self.root] = node
            old.close()


def prepare_private_profile(root, cancelled=None, *, _tree=None):
    """Secure a runtime profile, with the single Windows cache alias exception."""
    tree = _tree or _PrivateTree(root, cancelled)
    try:
        return tree.prepare(allow_cache_alias=True,
                            required=("home", "data", "state", "cache", "config", "config/opencode", "tmp"))
    finally:
        if _tree is None:
            tree.close()


def _windows_acl(path, sid):
    path = Path(path).absolute()
    tree = _PrivateTree(path.parent)
    try:
        node = tree.open(path)
        tree.reject(node)
        _windows_acl_handle(node.handle, sid)
    finally:
        tree.close()


def private_directory(path, recursive=False):
    tree = _PrivateTree(path)
    try:
        return tree.prepare(recursive=recursive)
    finally:
        tree.close()


def isolated_environment(root, payload, inherited=None):
    """Remove ambient config/credentials and use only packaged Node/OpenCode."""
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        upper = key.upper()
        if (upper.startswith(("OPENCODE_", "CODEX_", "WARAQ_", "XDG_", "NPM_", "NODE_", "BUN_")) or
                re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|BEARER)", upper) or
                upper.startswith(("AWS_", "AZURE_", "GOOGLE_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_", "GITHUB_", "GH_"))):
            env.pop(key, None)
    root = Path(root)
    for kind in ("DATA", "STATE", "CACHE", "CONFIG"):
        env["XDG_" + kind + "_HOME"] = str(root / kind.lower())
    path = [str(payload.node.parent), str(payload.opencode.parent)]
    if os.name == "nt":
        path.append(str(Path(env.get("SystemRoot", "C:/Windows")) / "System32"))
    env.update(PATH=os.pathsep.join(path), HOME=str(root / "home"), USERPROFILE=str(root / "home"),
               APPDATA=str(root / "config"), LOCALAPPDATA=str(root / "data"),
               TEMP=str(root / "tmp"), TMP=str(root / "tmp"),
               PWD=str(root / "home"), NODE_PATH=str(payload.root / "node_modules"),
               OPENCODE_CONFIG_DIR=str(root / "config" / "opencode"),
               OPENCODE_TEST_HOME=str(root / "home"), OPENCODE_CONFIG_PROJECT_DISABLE="1",
               OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_MODELS_FETCH="1")
    return env


class _ProfileLease:
    def __init__(self, handle, tree):
        self.handle, self.tree = handle, tree

    def close(self):
        try:
            self.handle.close()
        finally:
            self.tree.close()


def _lock_profile(root, cancelled=None):
    tree = _PrivateTree(root, cancelled)
    handle = None
    try:
        path = tree.root / "runtime.lock"
        if os.name == "nt":
            import msvcrt
            node = _WindowsPath(path, create=True)
            try:
                tree.reject(node)
                descriptor = msvcrt.open_osfhandle(node.handle, os.O_RDWR | os.O_BINARY)
                node.handle = None  # transferred to the Python file
                handle = os.fdopen(descriptor, "r+b")
            finally:
                node.close()
        else:
            descriptor = os.open("runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                                 dir_fd=tree.nodes[tree.root])
            handle = os.fdopen(descriptor, "r+b")
        if not handle.seek(0, 2):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _ProfileLease(handle, tree)
    except OSError:
        if handle:
            handle.close()
        tree.close()
        raise ExecutionOwnershipError("engine_already_owned") from None
    except BaseException:
        if handle:
            handle.close()
        tree.close()
        raise


def _atomic_json(path, value):
    from .config import atomic_json
    atomic_json(Path(path), value)


def _record_exit(root, owner):
    """Keep exact verified exits when a later server replaces the owner marker."""
    if (not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or isinstance(owner.get("pid"), bool) or
            owner["pid"] <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", str(owner.get("instance_id", ""))) or
            not isinstance(owner.get("created"), str) or not owner["created"]):
        raise ExecutionOwnershipError("engine_owner_invalid")
    path = Path(root) / "runtime-exits.json"
    try:
        proofs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(proofs, dict):
            proofs = {}
    except (OSError, ValueError):
        proofs = {}
    proofs[owner["instance_id"]] = {"pid": owner["pid"], "created": owner["created"], "exited_at": time.time()}
    proofs = dict(sorted(proofs.items(), key=lambda item: item[1].get("exited_at", 0) if isinstance(item[1], dict) else 0)[-256:])
    _atomic_json(path, proofs)


class PackagedPayload:
    """Manifest paths cannot escape the installed immutable payload directory."""
    def __init__(self, root=None, cancelled=None):
        default_root = (Path(sys._MEIPASS) / "embedded-runtime" if hasattr(sys, "_MEIPASS") else
                        Path(__file__).resolve().parent.parent / "build" / "embedded-runtime")
        self.root = Path(root or default_root).absolute()
        self._checked_paths = set()
        interrupted = threading.Event()
        self._cancelled = lambda: interrupted.is_set() or (cancelled is not None and cancelled())
        try:
            _check_cancelled(self._cancelled)
            self._check_links(self.root)
            manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("schema") != 1 or manifest.get("platform") != "win32-x64":
                raise EmbeddedRuntimeError("engine_manifest_invalid")
            if manifest.get("opencode_version") != OPENCODE_VERSION or manifest.get("node_version") != NODE_VERSION:
                raise EmbeddedRuntimeError("engine_version_mismatch")
            self.node = self._member("node/node.exe")
            self.opencode = self._member("opencode/opencode.exe")
            self.plugin = self._member(manifest["plugin_entry"])
            plugin_root = self._member("plugin/index.js")
            self.manifest = manifest
            checksums = manifest["files"]
            if not isinstance(checksums, dict) or not checksums:
                raise EmbeddedRuntimeError("engine_manifest_invalid")
            for member in (self.node, self.opencode, self.plugin, plugin_root):
                if member.relative_to(self.root).as_posix() not in checksums:
                    raise EmbeddedRuntimeError("engine_manifest_invalid")
            def verify_member(item):
                _check_cancelled(self._cancelled)
                name, digest = item
                member = self._member(name)
                if not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
                    raise EmbeddedRuntimeError("engine_manifest_invalid")
                with member.open("rb") as stream:
                    digest_state = hashlib.sha256()
                    while True:
                        _check_cancelled(self._cancelled)
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        digest_state.update(block)
                    actual = digest_state.hexdigest()
                if actual != digest:
                    raise EmbeddedRuntimeError("engine_integrity_failed")
            # Independent reads are bounded so cold Windows file inspection
            # cannot serialize startup behind every dependency's first open.
            with ThreadPoolExecutor(max_workers=min(8, len(checksums))) as readers:
                try:
                    for _ in readers.map(verify_member, checksums.items()):
                        _check_cancelled(self._cancelled)
                except BaseException:
                    interrupted.set()
                    raise
            actual_files = set()
            for member in self.root.rglob("*"):
                _check_cancelled(self._cancelled)
                self._check_links(member)
                if member.is_file() and member != self.root / "manifest.json":
                    actual_files.add(member.relative_to(self.root).as_posix())
            if actual_files != set(checksums):
                raise EmbeddedRuntimeError("engine_integrity_failed")
        except EmbeddedRuntimeError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise EmbeddedRuntimeError("engine_payload_missing") from None

    def _member(self, name):
        if not isinstance(name, str) or Path(name).anchor or ":" in name or ".." in Path(name).parts:
            raise EmbeddedRuntimeError("engine_manifest_invalid")
        member = self.root / name
        self._check_links(member)
        if not member.is_file():
            raise EmbeddedRuntimeError("engine_payload_missing")
        return member

    def _check_links(self, member):
        # A payload has thousands of files under the same small directory tree.
        # Check each ancestor once per validation, avoiding hundreds of thousands
        # of repeated Windows reparse-point queries. Every file is still hashed.
        for part in (member, *member.parents):
            _check_cancelled(self._cancelled)
            if part in self._checked_paths:
                break
            if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                raise EmbeddedRuntimeError("private_path_is_link")
            self._checked_paths.add(part)


class _WindowsJob:
    """The server starts suspended: no child may escape before assignment."""
    def __init__(self):
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint64) for field in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise EmbeddedRuntimeError("engine_job_failed")

    def assign_and_resume(self, process):
        from ctypes import wintypes
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise EmbeddedRuntimeError("engine_job_failed")
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        if ntdll.NtResumeProcess(int(process._handle)) != 0:
            raise EmbeddedRuntimeError("engine_job_failed")

    def empty(self):
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION; ActiveProcesses at byte 40.
        info = ctypes.create_string_buffer(48)
        if not self.kernel.QueryInformationJobObject(self.handle, 1, info, len(info), None):
            return False
        return ctypes.c_uint32.from_buffer(info, 40).value == 0

    def terminate(self):
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise ExecutionOwnershipError("engine_shutdown_unverified")

    def close(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _process_identity(pid):
    """Identify a process creation, not just its reusable PID; None proves exit."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ExecutionOwnershipError("engine_owner_invalid")
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise ExecutionOwnershipError("engine_owner_unverified")
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                raise ExecutionOwnershipError("engine_owner_unverified")
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        raise ExecutionOwnershipError("engine_owner_unverified") from None
    except (OSError, IndexError):
        raise ExecutionOwnershipError("engine_owner_unverified") from None


class EmbeddedRuntime:
    def __init__(self, supervisor, payload, root, startup_timeout=30, *, lease=None):
        self.supervisor, self.payload, self.profile = supervisor, payload, Path(root)
        self.executable = payload.opencode
        self.password, self.instance_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        self._capability = secrets.token_urlsafe(32)
        self.url, self.process, self._job, self._created = "", None, None, None
        self._lock, self._stopping = threading.RLock(), False
        self.sessions = set()
        self._lease = lease or _lock_profile(root)
        try:
            if lease is None:
                self._lease.tree.allow_runtime_writes()
            self.env = isolated_environment(root, payload)
            _check_cancelled(lambda: supervisor._closed)
            owner = self.profile / "runtime-owner.json"
            if owner.exists():
                try:
                    previous = json.loads(owner.read_text(encoding="utf-8"))
                    if not isinstance(previous, dict):
                        raise ValueError()
                except (OSError, ValueError):
                    raise ExecutionOwnershipError("engine_owner_invalid") from None
                actual = _process_identity(previous.get("pid"))
                if actual is not None and actual == previous.get("created"):
                    raise ExecutionOwnershipError("engine_already_owned")
                if actual is not None and not previous.get("created"):
                    raise ExecutionOwnershipError("engine_owner_unverified")
                _record_exit(self.profile, previous)
            self._start(startup_timeout)
        except BaseException:
            try:
                self.stop()
            except ExecutionOwnershipError:
                supervisor._runtime = self
                raise
            raise

    def _start(self, timeout):
        if self.supervisor._closed:
            raise EmbeddedRuntimeError("engine_closed")
        # OpenCode 2 resolves local plugins as directories with a root index.js.
        config = {"plugins": [(self.payload.root / "plugin").as_uri()]}
        env = dict(self.env, OPENCODE_PASSWORD=self.password,
                   OPENCODE_CONFIG_CONTENT=json.dumps(config), WARAQ_ACCOUNTS_CAPABILITY=self._capability,
                   WARAQ_ACCOUNTS_STORAGE_DIR=str(self.profile), WARAQ_ENGINE_INSTANCE_ID=self.instance_id)
        flags = 0
        if os.name == "nt":
            self._job = _WindowsJob()
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        self.process = subprocess.Popen(
            [str(self.executable), "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"],
            cwd=self.profile / "home", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != "nt",
            text=True, encoding="utf-8", errors="replace")
        if self._job:
            try:
                self._job.assign_and_resume(self.process)
            except BaseException:
                self.process.kill()
                self.process.wait(timeout=5)
                raise
        self._created = _process_identity(self.process.pid)
        _atomic_json(self.profile / "runtime-owner.json", {"pid": self.process.pid,
                     "created": self._created, "instance_id": self.instance_id})
        ready = queue.Queue(maxsize=2)
        def drain():
            try:
                for line in self.process.stdout:
                    try:
                        value = json.loads(line)
                        url = value.get("url") if isinstance(value, dict) else None
                        if isinstance(url, str) and len(url) <= 100 and not ready.full():
                            ready.put_nowait(url)
                    except (ValueError, queue.Full):
                        pass
            finally:
                self.process.stdout.close()
                if not ready.full():
                    ready.put_nowait(None)
        threading.Thread(target=drain, daemon=True, name="embedded-engine-stdout").start()
        deadline = time.monotonic() + timeout
        url = None
        while time.monotonic() < deadline and not self.supervisor._closed:
            try:
                url = ready.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                break
            except queue.Empty:
                continue
        parsed = urlsplit(url or "")
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or
                parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise EmbeddedRuntimeError("engine_start_timeout")
        self.url = url.rstrip("/")
        while True:
            _check_cancelled(lambda: self.supervisor._closed)
            try:
                self.request("GET", "/api/info", timeout=1)
                health = self.rpc("health", {}, timeout=1)
                if not isinstance(health, dict) or health.get("ok") is not True or health.get("data", {}).get("apiVersion") != API_VERSION:
                    raise EmbeddedRuntimeError("engine_protocol_mismatch")
                break
            except EmbeddedRuntimeError as exc:
                if exc.reason not in ("engine_connection_failed", "engine_http_error") or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def request(self, method, path, data=None, directory=None, timeout=15):
        if not isinstance(path, str) or not path.startswith("/api/") or "://" in path or "#" in path:
            raise EmbeddedRuntimeError("engine_api_invalid")
        if not self.process or self.process.poll() is not None or self._stopping:
            raise EmbeddedRuntimeError("engine_unavailable")
        if directory is not None:
            path += ("&" if "?" in path else "?") + urlencode({"location[directory]": str(Path(directory).resolve())})
        auth = base64.b64encode(("opencode:" + self.password).encode()).decode()
        payload = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Authorization": "Basic " + auth, "Content-Type": "application/json"}
        if directory is not None:
            headers["x-opencode-directory"] = str(Path(directory).resolve())
        req = Request(self.url + path, data=payload, method=method, headers=headers)
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=timeout) as response:
                body = response.read(16_000_001)
                if len(body) > 16_000_000:
                    raise EmbeddedRuntimeError("engine_response_too_large")
                return json.loads(body) if body else None
        except HTTPError as exc:
            status = exc.code
            exc.close()
            error = EmbeddedRuntimeError("engine_http_error")
            error.http_status = status
            raise error from None
        except (URLError, OSError, ValueError, TimeoutError):
            raise EmbeddedRuntimeError("engine_connection_failed") from None

    def rpc(self, method, params=None, timeout=30, directory=None):
        result = self.request("POST", "/api/rpc/" + PLUGIN_ID + "/" + quote(method, safe=""), {
            "input": {**(params or {}), "apiVersion": API_VERSION, "capability": self._capability}},
            timeout=max(timeout, 180) if method in ("usage.refresh", "accounts.export", "accounts.import") else timeout,
            directory=directory or self.profile / "home")
        if not isinstance(result, dict):
            raise EmbeddedRuntimeError("engine_protocol_mismatch")
        output = result.get("output")
        if not isinstance(output, dict):
            raise EmbeddedRuntimeError("engine_protocol_mismatch")
        return output

    def healthy(self):
        try:
            self.request("GET", "/api/info", timeout=2)
            return True
        except EmbeddedRuntimeError:
            return False

    def create_session(self, directory, env=None):
        with self._lock:
            result = self.request("POST", "/api/session", {"location": {"directory": str(Path(directory).resolve())}})
            try:
                ident = result["data"]["id"]
                if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ident):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise EmbeddedRuntimeError("engine_session_invalid") from None
            self.sessions.add(ident)
            variables = dict(self.env)
            variables.update(env or {})
            for key in list(variables):
                if re.search(r"(?:API_?KEY|CAPABILITY|TOKEN|PASSWORD|SECRET|CREDENTIAL|BEARER)", key.upper()):
                    variables.pop(key, None)
            # The host's process environment contains startup-only secrets.
            # Explicit blanks prevent tool subprocesses from inheriting them
            # when native session environments overlay the parent environment.
            variables.update(WARAQ_ACCOUNTS_CAPABILITY="", OPENCODE_PASSWORD="", WARAQ_ENGINE_INSTANCE_ID="")
            self.request("PUT", "/api/session/" + quote(ident, safe="") + "/environment", {"variables": variables})
            return ident

    def inactive(self, session_id):
        result = self.request("GET", "/api/session/active", timeout=3)
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise EmbeddedRuntimeError("engine_session_invalid")
        active = result["data"]
        if session_id in active:
            return False
        if len(active) > 128:
            raise EmbeddedRuntimeError("engine_session_invalid")
        # A completed parent may still have a background child. The active map
        # is global: follow authoritative parent links before proving inactivity,
        # without interrupting another worker's unrelated session.
        unrelated = set()
        deadline = time.monotonic() + 3
        for ident in active:
            chain = set()
            while ident and ident not in unrelated:
                if ident == session_id:
                    return False
                if ident in chain or len(chain) >= 64 or time.monotonic() >= deadline:
                    raise EmbeddedRuntimeError("engine_session_invalid")
                if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ident):
                    raise EmbeddedRuntimeError("engine_session_invalid")
                chain.add(ident)
                row = self.request("GET", "/api/session/" + quote(ident, safe=""), timeout=max(0.1, deadline-time.monotonic()))
                info = row.get("data") if isinstance(row, dict) else None
                if not isinstance(info, dict) or info.get("id") != ident:
                    raise EmbeddedRuntimeError("engine_session_invalid")
                ident = info.get("parentID")
            unrelated.update(chain)
        return True

    def abort_and_wait(self, session_id, directory=None, timeout=15):
        try:
            self.request("POST", "/api/session/" + quote(session_id, safe="") + "/interrupt", directory=directory, timeout=min(timeout, 10))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.inactive(session_id):
                    self.release_session(session_id)
                    return True
                time.sleep(0.1)
        except EmbeddedRuntimeError:
            pass
        return False

    def release_session(self, session_id):
        with self._lock:
            self.sessions.discard(session_id)

    def stop(self):
        with self._lock:
            self._stopping = True
            proc = self.process
            if proc:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    if self._job:
                        self._job.terminate()
                    elif proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    try:
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        raise ExecutionOwnershipError("engine_shutdown_unverified") from None
            if self._job:
                # A finished parent can still have descendants.
                if not self._job.empty():
                    self._job.terminate()
                deadline = time.monotonic() + 5
                while not self._job.empty() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not self._job.empty():
                    raise ExecutionOwnershipError("engine_shutdown_unverified")
                self._job.close()
                self._job = None
            owner = self.profile / "runtime-owner.json"
            if proc and self._created:
                _record_exit(self.profile, {"pid": proc.pid, "created": self._created, "instance_id": self.instance_id})
            try:
                previous = json.loads(owner.read_text(encoding="utf-8"))
                if previous.get("instance_id") == self.instance_id:
                    owner.unlink()
            except (OSError, ValueError, AttributeError):
                pass
            self.password = self._capability = ""
            if self._lease:
                self._lease.close()
                self._lease = None


class EmbeddedRuntimeSupervisor:
    def __init__(self, config, payload_root=None):
        self.config, self.payload_root = config, payload_root
        self.root = Path(config.home) / "accounts-v2"
        self._lock, self._runtime, self._closed = threading.RLock(), None, False

    def ensure(self, cfg=None):
        with self._lock:
            if self._closed:
                raise EmbeddedRuntimeError("engine_closed")
            if self._runtime and self._runtime.process and self._runtime.process.poll() is None:
                if self._runtime._stopping:
                    raise ExecutionOwnershipError("engine_shutdown_unverified")
                return self._runtime
            if self._runtime:
                self._runtime.stop()
                self._runtime = None
            cancelled = lambda: self._closed
            lease = _lock_profile(self.root, cancelled)
            try:
                prepare_private_profile(self.root, cancelled, _tree=lease.tree)
                payload = PackagedPayload(self.payload_root, cancelled=cancelled)
                _check_cancelled(cancelled)
                # ACL inspection has completed. Permit the engine to replace its own
                # cache/owner files, while the lease still pins the root and ancestors.
                lease.tree.release_descendants()
                lease.tree.allow_runtime_writes()
                owned_lease, lease = lease, None
                self._runtime = EmbeddedRuntime(self, payload, self.root, lease=owned_lease)
                return self._runtime
            finally:
                if lease is not None:
                    lease.close()

    def confirm_stopped(self, session_id="", instance_id="", workpath=None, **ignored):
        """Old attempts can replay only after this exact server is proven absent."""
        with self._lock:
            if ignored.get("legacy"):
                account_id = ignored.get("account_id", "")
                if not isinstance(account_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", account_id):
                    return False
                legacy_root = Path(self.config.home).absolute() / "opencode-accounts"
                profile = legacy_root / account_id
                try:
                    _reject_links(profile)
                    if profile.resolve().parent != legacy_root.resolve():
                        return False
                    previous = json.loads((profile / "runtime-owner.json").read_text(encoding="utf-8"))
                    return previous.get("instance_id") == instance_id and _process_identity(previous.get("pid")) is None
                except (OSError, ValueError, AttributeError, EmbeddedRuntimeError):
                    return False
            try:
                proofs = json.loads((self.root / "runtime-exits.json").read_text(encoding="utf-8"))
                proof = proofs.get(instance_id)
                if (isinstance(proof, dict) and isinstance(proof.get("pid"), int) and not isinstance(proof.get("pid"), bool) and
                        proof["pid"] > 0 and isinstance(proof.get("created"), str) and proof["created"] and
                        isinstance(proof.get("exited_at"), (int, float)) and proof["exited_at"] > 0):
                    return True
            except (OSError, ValueError, AttributeError):
                pass
            if self._runtime and self._runtime.instance_id == instance_id:
                if self._runtime.process and self._runtime.process.poll() is None:
                    try:
                        stopped = bool(session_id) and self._runtime.inactive(session_id)
                        if stopped:
                            self._runtime.release_session(session_id)
                        return stopped
                    except EmbeddedRuntimeError:
                        return False
                try:
                    self._runtime.stop()  # Parent exit alone cannot prove descendants exited.
                    return self._runtime.process is not None
                except (EmbeddedRuntimeError, OSError):
                    return False
            try:
                previous = json.loads((self.root / "runtime-owner.json").read_text(encoding="utf-8"))
                if previous.get("instance_id") != instance_id:
                    return False  # Different/unknown owner is not evidence of exit.
                if not isinstance(previous.get("created"), str) or not previous["created"]:
                    return False
                actual = _process_identity(previous.get("pid"))
                return actual is None or actual != previous.get("created")
            except (OSError, ValueError, AttributeError, EmbeddedRuntimeError):
                return False

    def close(self):
        self._closed = True  # Wake startup before waiting on its serialization lock.
        with self._lock:
            if self._runtime:
                self._runtime.stop()


# Explicit name for callers migrating from per-account runtime managers.
EmbeddedRuntimeManager = EmbeddedRuntimeSupervisor
