"""Native OpenCode 2 account processes. Credentials persist only in native profiles.

Protocol pinned to anomalyco/opencode v2.0.27, packages/protocol/src/groups/
{integration,credential,session,server}.ts and packages/cli/src/server-process.ts.
The stdio pipe is a lifetime lease; the HTTP port is loopback and authenticated.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import ssl
import subprocess
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .opencode_usage import (classify_usage_capability, finite_number,
                             parse_usage_response, usage_result)
from . import __version__

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_USAGE_LIMIT = 256 * 1024


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _retry_after(headers):
    # Server messages are private; only bounded numeric delay is telemetry.
    try:
        raw = headers.get("Retry-After", "")
        try:
            value = float(raw)
        except (ValueError, TypeError):
            value = parsedate_to_datetime(raw).timestamp() - time.time()
        return value if finite_number(value) is not None and 0 <= value <= 366 * 86400 else 0
    except (TypeError, ValueError, OverflowError, AttributeError):
        return 0


def _usage_https(access, account, cancel):
    """The only remote usage route. No body, header or exception escapes raw."""
    if _cancelled(cancel):
        return usage_result("cancelled", "cancelled")
    headers = {"Authorization": "Bearer " + access, "Accept": "application/json",
               "User-Agent": "WaraqStudio/" + __version__ + " (account-usage)"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    req = Request(_USAGE_URL, method="GET", headers=headers)
    try:
        # Explicit verified TLS, no environment proxies, no redirects. A small
        # socket timeout bounds cancellation while an HTTP read is blocked.
        from urllib.request import HTTPSHandler
        opener = build_opener(ProxyHandler({}), _NoRedirect(),
                              HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(req, timeout=5) as response:
            chunks, count, deadline = [], 0, time.monotonic() + 10
            while True:
                if _cancelled(cancel):
                    return usage_result("cancelled", "cancelled")
                if time.monotonic() >= deadline:
                    return usage_result("unavailable", "timeout")
                block = response.read(min(16384, _USAGE_LIMIT + 1 - count))
                if not block:
                    break
                chunks.append(block)
                count += len(block)
                if count > _USAGE_LIMIT:
                    return usage_result("unavailable", "response_too_large")
            if _cancelled(cancel):
                return usage_result("cancelled", "cancelled")
            payload = json.loads(b"".join(chunks))
            if isinstance(payload, dict) and "account_id" in payload and payload["account_id"] != account:
                return usage_result("auth_unavailable", "binding_changed")
            return parse_usage_response(payload, time.time())
    except HTTPError as exc:
        code = exc.code
        delay = _retry_after(exc.headers) if code == 429 else 0
        exc.close()
        if _cancelled(cancel):
            return usage_result("cancelled", "cancelled")
        if code == 401:
            return usage_result("auth_unavailable", "unauthorized")
        if code in (403, 404):
            return usage_result("unsupported", "endpoint_unavailable")
        if code == 429:
            return usage_result("unavailable", "throttled", retry_after=delay)
        return usage_result("unavailable", "http_error")
    except (URLError, OSError, ValueError, TypeError, OverflowError):
        return usage_result("cancelled", "cancelled") if _cancelled(cancel) else usage_result("unavailable", "connection_failed")


class RuntimeError(Exception):
    """Deliberately excludes native response bodies, which may contain tokens."""


class ExecutionOwnershipError(RuntimeError):
    kind = "execution_uncertain"


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 0xFFFFFFFF:
        return True  # Malformed ownership cannot establish absence.
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _windows_pid_alive(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel.CloseHandle(handle)


def _windows_private_acl(path, sid):
    """Replace the DACL atomically; inheritance removal alone leaves old ACEs."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid + ")"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise RuntimeError("Cannot construct private account permissions.")
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present:
            raise RuntimeError("Cannot read private account permissions.")
        if advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None):
            raise RuntimeError("Cannot secure the account profile permissions.")
    finally:
        kernel.LocalFree(descriptor)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _private_directory(path, recursive=False):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise RuntimeError("Account profile cannot be a link.")
    if os.name == "nt":
        # Explicit SID avoids localised names and inheritance from a shared home.
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        match = re.search(r"S-1-[0-9-]+", result.stdout)
        if result.returncode or not match:
            raise RuntimeError("Cannot determine the account profile owner.")
        _windows_private_acl(path, match.group(0))
        if recursive:
            for child in path.rglob("*"):
                _reject_links(child)
                _windows_private_acl(child, match.group(0))
    else:
        path.chmod(0o700)
        if recursive:
            for child in path.rglob("*"):
                _reject_links(child)
                child.chmod(0o700 if child.is_dir() else 0o600)
    return path


def _reject_links(path):
    for part in (Path(path), *Path(path).parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise RuntimeError("Account profile path cannot traverse links.")


def isolated_environment(profile, inherited=None):
    """Keep OS/tool plumbing, discard global OpenCode configuration and secrets."""
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        upper = key.upper()
        if (upper.startswith("OPENCODE_") or
                re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|BEARER)", upper) or
                upper.startswith(("AWS_", "AZURE_", "GOOGLE_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_", "GITHUB_", "GH_"))):
            env.pop(key, None)
    profile = Path(profile)
    for kind in ("DATA", "STATE", "CACHE", "CONFIG"):
        env["XDG_" + kind + "_HOME"] = str(profile / kind.lower())
    env.update({"OPENCODE_CONFIG_DIR": str(profile / "config" / "opencode"),
                "OPENCODE_TEST_HOME": str(profile / "home"),
                "HOME": str(profile / "home"), "USERPROFILE": str(profile / "home"),
                "OPENCODE_CONFIG_PROJECT_DISABLE": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_MODELS_FETCH": "1", "PWD": str(profile / "home")})
    return env


def _profile_lock(profile):
    handle = (Path(profile) / "runtime.lock").open("a+b")
    try:
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
        return handle
    except (OSError, IOError):
        handle.close()
        raise ExecutionOwnershipError("Another Waraq process owns this account profile.") from None


class AccountRuntime:
    def __init__(self, manager, account_id, profile, executable, startup_timeout=30, start_guard=None):
        self.manager, self.account_id, self.profile = manager, account_id, profile
        self.executable = executable
        self.password = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(16)
        self.url = ""
        self.process = None
        self._lock = threading.RLock()
        self._stopping = False
        self._start_guard = start_guard
        self.sessions = set()
        self.env = isolated_environment(profile)
        self._profile_lease = _profile_lock(profile)
        try:
            owner = profile / "runtime-owner.json"
            if owner.exists():
                previous = json.loads(owner.read_text(encoding="utf-8"))
                if not isinstance(previous, dict) or _pid_alive(previous.get("pid")):
                    raise ExecutionOwnershipError("A previous account server still owns this profile.")
            self._start(startup_timeout)
        except Exception:
            if self.process and self.process.poll() is None:
                # Preserve the OS profile lock and an explicit shutdown handle
                # when initialisation cleanup cannot prove child termination.
                manager._runtimes[account_id] = self
                raise ExecutionOwnershipError("Account startup failed and native shutdown is unverified.") from None
            if self._profile_lease:
                self._profile_lease.close()
                self._profile_lease = None
            raise

    def _start(self, timeout):
        if self.manager._closed or (self._start_guard and not self._start_guard()):
            raise RuntimeError("Account runtime startup was cancelled.")
        env = dict(self.env, OPENCODE_PASSWORD=self.password)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(self.executable), "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"],
            cwd=self.profile / "home", env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags,
            start_new_session=os.name != "nt", text=True, encoding="utf-8", errors="replace")
        from .config import atomic_json
        ready = queue.Queue()
        def drain():
            try:
                for line in self.process.stdout:
                    # Never retain arbitrary native output, especially login URLs.
                    try:
                        value = json.loads(line)
                        url = value.get("url") if isinstance(value, dict) else None
                        if isinstance(url, str):
                            ready.put(url)
                    except ValueError:
                        pass
            finally:
                self.process.stdout.close()
                ready.put(None)
        try:
            atomic_json(self.profile / "runtime-owner.json", {"pid": self.process.pid, "instance_id": self.instance_id})
            threading.Thread(target=drain, daemon=True).start()
            deadline, url = time.monotonic() + timeout, None
            while time.monotonic() < deadline:
                if self.manager._closed or (self._start_guard and not self._start_guard()):
                    raise RuntimeError("Account runtime startup was cancelled.")
                try:
                    url = ready.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    break
                except queue.Empty:
                    continue
            parsed = urlsplit(url or "")
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                    not parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or
                    parsed.query or parsed.fragment):
                raise RuntimeError("OpenCode did not announce a private loopback endpoint.")
            self.url = url.rstrip("/")
            self.request("GET", "/api/info", timeout=5)
        except Exception as exc:
            self.stop()
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("OpenCode account server could not start within its deadline.") from None

    def request(self, method, path, data=None, directory=None, timeout=15):
        if not path.startswith("/api/") or "://" in path or "#" in path:
            raise RuntimeError("Invalid native API path.")
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("OpenCode account server is unavailable.")
        if directory is not None:
            path += ("&" if "?" in path else "?") + urlencode({"location[directory]": str(Path(directory).resolve())})
        payload = None if data is None else json.dumps(data).encode("utf-8")
        auth = base64.b64encode(("opencode:" + self.password).encode()).decode()
        req = Request(self.url + path, data=payload, method=method,
                      headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        # Never honour ambient HTTP proxies or redirects for a secret API request.
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=timeout) as response:
                body = response.read(16_000_001)
                if len(body) > 16_000_000:
                    raise RuntimeError("Native API response exceeds the size limit.")
                return json.loads(body) if body else None
        except HTTPError as exc:
            raise RuntimeError("OpenCode native API rejected the request (HTTP %d)." % exc.code) from None
        except (URLError, OSError, ValueError):
            raise RuntimeError("OpenCode native API connection failed.") from None

    def healthy(self):
        try:
            self.request("GET", "/api/info", timeout=2)
            return True
        except RuntimeError:
            return False

    def create_session(self, directory, env=None):
        with self._lock:
            if (self._stopping or self.manager._closed or
                    self.account_id in self.manager._usage_maintenance):
                raise RuntimeError("Account runtime is reserved or stopped.")
            result = self.request("POST", "/api/session", {"location": {"directory": str(Path(directory).resolve())}})
            ident = result["data"]["id"]
            self.sessions.add(ident)
            try:
                variables = dict(self.env)
                variables.update(env or {})
                variables.pop("OPENCODE_PASSWORD", None)
                variables.pop("OPENCODE_SERVER_PASSWORD", None)
                self.request("PUT", "/api/session/" + quote(ident, safe="") + "/environment", {"variables": variables})
            except Exception:
                self.sessions.discard(ident)
                raise
            return ident

    def inactive(self, session_id):
        result = self.request("GET", "/api/session/active", timeout=3)
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise RuntimeError("Native session activity response is invalid.")
        return session_id not in result["data"]

    def abort_and_wait(self, session_id, directory=None, timeout=15):
        with self._lock:
            return self._abort_and_wait_locked(session_id, directory, timeout)

    def _abort_and_wait_locked(self, session_id, directory=None, timeout=15):
        """Only a native acknowledgement plus absence from active permits replay."""
        try:
            self.request("POST", "/api/session/" + quote(session_id, safe="") + "/interrupt", timeout=min(timeout, 10))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.inactive(session_id):
                    self.release_session(session_id)
                    return True
                time.sleep(0.1)
        except Exception:
            pass
        # Do not stop a shared runtime: that would interrupt unrelated workers.
        return False

    def release_session(self, session_id):
        with self._lock:
            self.sessions.discard(session_id)

    def stop(self):
        with self._lock:
            self._stopping = True
            return self._stop_locked()

    def _stop_locked(self):
        proc = self.process
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError("Could not verify account server shutdown.") from None
        self.password = ""
        owner = self.profile / "runtime-owner.json"
        try:
            previous = json.loads(owner.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("instance_id") == self.instance_id:
                owner.unlink()
        except (OSError, ValueError):
            pass
        if self._profile_lease:
            self._profile_lease.close()
            self._profile_lease = None


def _safe_credential_metadata(entries):
    result = {"authenticated": False, "identity": "", "identity_verified": False, "email": "", "subscription": ""}
    candidates = [e for e in entries if isinstance(e, dict) and e.get("integrationID") == "openai" and
                  isinstance(e.get("value"), dict) and e["value"].get("type") == "oauth" and
                  "chatgpt" in str(e["value"].get("methodID", "")).lower()]
    active = next((e for e in candidates if e.get("active")), None)
    if not active:
        return result
    result["authenticated"] = True
    value = active["value"]
    metadata = value.get("metadata") or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    # Native OAuth validates the ID token. This only extracts labels from the
    # stored access token; its claims never authorise network access here.
    claims = {}
    token = value.get("access", "")
    if isinstance(token, str) and token.count(".") == 2 and len(token) < 100_000:
        try:
            encoded = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, TypeError):
            pass
    if not isinstance(claims, dict):
        claims = {}
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    auth = auth if isinstance(auth, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    subject = metadata.get("sub") or claims.get("sub")
    account = metadata.get("accountID") or metadata.get("account_id") or auth.get("chatgpt_account_id")
    if isinstance(subject, str) and subject:
        identity = "openai:" + subject + ":" + (account if isinstance(account, str) else "")
        result.update(identity=hashlib.sha256(identity.encode()).hexdigest(), identity_verified=True)
    for key, values in {"email": (metadata.get("email"), claims.get("email"), profile.get("email")),
                        "subscription": (metadata.get("subscription"), auth.get("chatgpt_plan_type"))}.items():
        result[key] = next((v[:200] for v in values if isinstance(v, str) and v), "")
    return result


def _usage_credential(entries):
    """Return a PRIVATE snapshot; callers must never expose this object."""
    if not isinstance(entries, list):
        return None
    candidates = [entry for entry in entries if isinstance(entry, dict) and
                  entry.get("active") is True and entry.get("integrationID") == "openai" and
                  isinstance(entry.get("value"), dict) and entry["value"].get("type") == "oauth"]
    if len(candidates) != 1:
        return None
    entry, value = candidates[0], candidates[0]["value"]
    ident, access, method = entry.get("id"), value.get("access"), value.get("methodID")
    if (not isinstance(ident, str) or not ident or len(ident) > 200 or
            not isinstance(method, str) or not isinstance(access, str) or
            not access or len(access) > 100000 or any(ord(c) < 33 or ord(c) > 126 for c in access)):
        return None
    metadata = _safe_credential_metadata([entry])
    if not metadata["identity_verified"]:
        return None
    native_metadata = value.get("metadata")
    account = native_metadata.get("accountID") if isinstance(native_metadata, dict) else None
    if not account and isinstance(native_metadata, dict):
        account = native_metadata.get("account_id")
    if not account and access.count(".") == 2:
        try:
            encoded = access.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            account = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        except (ValueError, TypeError, AttributeError):
            pass
    if not isinstance(account, str) or not account or len(account) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in account):
        return None
    expiry = finite_number(value.get("expires"))
    return {"id": ident, "method": method, "access": access, "account": account,
            "expires": expiry, "identity": hashlib.sha256(metadata["identity"].encode()).hexdigest()}


class AccountRuntimeManager:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.home).absolute() / "opencode-accounts"
        self._lock = threading.RLock()
        self._runtimes = {}
        self._login = None
        self._closed = False
        self._usage_epochs = {}
        self._usage_maintenance = set()
        self._account_locks = {}

    def profile_path(self, account_id):
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", account_id):
            raise RuntimeError("Invalid opaque account ID.")
        path = self.root / account_id
        if path.resolve().parent != self.root.resolve():
            raise RuntimeError("Account profile escaped its private root.")
        return path

    def _account_lock(self, account_id):
        with self._lock:
            return self._account_locks.setdefault(account_id, threading.RLock())

    def ensure(self, account_id, cfg=None, *, _usage_reserved=False, _start_guard=None):
        from .services import resolve_opencode, opencode_version
        # Serialize owners for this account. Cold startup never holds the
        # manager lock, so cancellation/close and other accounts stay responsive.
        with self._account_lock(account_id):
            epoch = self._usage_epochs.get(account_id, 0)
            if self._closed:
                raise RuntimeError("Account runtime manager is closed.")
            if account_id in self._usage_maintenance and not _usage_reserved:
                raise RuntimeError("Account runtime is reserved for maintenance.")
            runtime = self._runtimes.get(account_id)
            if runtime:
                with runtime._lock:
                    if not runtime._stopping and runtime.healthy():
                        return runtime
                    # Tracked sessions alone cannot prove native inactivity.
                    if runtime.sessions:
                        raise ExecutionOwnershipError("Account execution ownership is uncertain.")
                    try:
                        activity = runtime.request("GET", "/api/session/active", timeout=3)
                    except RuntimeError:
                        raise ExecutionOwnershipError("Account execution ownership is uncertain.") from None
                    if not isinstance(activity, dict) or not isinstance(activity.get("data"), dict) or activity["data"]:
                        raise ExecutionOwnershipError("Account execution ownership is uncertain.")
                    runtime.stop()
            _reject_links(self.root)
            _private_directory(self.root)
            profile = _private_directory(self.profile_path(account_id), recursive=True)
            owner = profile / "runtime-owner.json"
            if owner.exists():
                try:
                    previous = json.loads(owner.read_text(encoding="utf-8"))
                    if not isinstance(previous, dict) or _pid_alive(previous.get("pid")):
                        raise ExecutionOwnershipError("A previous account server still owns this profile.")
                except (OSError, ValueError):
                    raise ExecutionOwnershipError("Cannot establish previous account runtime ownership.") from None
            for name in ("home", "data", "state", "cache", "config", "config/opencode"):
                child = profile / name
                if child.exists() and child.resolve().is_relative_to(profile.resolve()) is False:
                    raise RuntimeError("Account profile contains an unsafe path.")
                child.mkdir(parents=True, exist_ok=True)
            settings = cfg or self.config.snapshot()
            executable = resolve_opencode(settings["opencode_executable"])
            native_version = opencode_version(executable)
            if not native_version.startswith("2."):
                raise RuntimeError("Managed ChatGPT accounts require OpenCode 2.")
            def can_start():
                return (not self._closed and self._usage_epochs.get(account_id, 0) == epoch and
                        (_start_guard is None or _start_guard()))
            runtime = AccountRuntime(self, account_id, profile, executable, start_guard=can_start)
            runtime.native_version = native_version
            if not can_start():
                runtime.stop()
                raise RuntimeError("Account runtime startup was cancelled.")
            self._runtimes[account_id] = runtime
            return runtime

    def metadata(self, account_id):
        runtime = self.ensure(account_id)
        result = _safe_credential_metadata(runtime.request("GET", "/api/credential")["data"])
        return dict(result, account_id=account_id)

    def _usage_capture(self, runtime, expected_binding):
        response = runtime.request("GET", "/api/credential", timeout=3)
        entries = response.get("data") if isinstance(response, dict) else None
        if not isinstance(entries, list):
            return None, usage_result("auth_unavailable", "credential_unavailable")
        active = [e for e in entries if isinstance(e, dict) and e.get("active") is True and
                  e.get("integrationID") == "openai" and isinstance(e.get("value"), dict)]
        if len(active) != 1 or active[0]["value"].get("type") != "oauth":
            return None, usage_result("auth_unavailable", "credential_unavailable")
        gate = classify_usage_capability(active[0]["value"].get("methodID"), getattr(runtime, "native_version", ""))
        if gate["status"] != "available":
            return None, gate
        credential = _usage_credential(entries)
        if (not credential or credential["identity"] != expected_binding["identity"] or
                expected_binding.get("credential_id") not in (None, credential["id"]) or
                expected_binding.get("instance_id") not in (None, runtime.instance_id)):
            return None, usage_result("auth_unavailable", "identity_unavailable")
        return credential, gate

    @staticmethod
    def _valid_usage_binding(account_id, binding):
        return (isinstance(binding, dict) and binding.get("account_id") == account_id and
                isinstance(binding.get("identity"), str) and
                re.fullmatch(r"[0-9a-f]{64}", binding["identity"]) is not None and
                isinstance(binding.get("auth_epoch"), int) and not isinstance(binding["auth_epoch"], bool) and
                binding["auth_epoch"] >= 0)

    def read_usage(self, account_id, expected_binding, cancel=None):
        """Capture privately, release all lifecycle locks for HTTPS, revalidate.

        The registry owns auth_epoch validation at commit; this manager also
        invalidates in-flight reads when login, deletion or shutdown starts.
        """
        if _cancelled(cancel):
            return usage_result("cancelled", "cancelled")
        if not self._valid_usage_binding(account_id, expected_binding):
            return usage_result("auth_unavailable", "binding_invalid")
        # Copy caller state so another thread cannot mutate the binding during IO.
        expected_binding = dict(expected_binding)
        try:
            with self._lock:
                if self._closed:
                    return usage_result("unavailable", "runtime_changed")
                runtime = self._runtimes.get(account_id)
            if runtime is None:
                return usage_result("deferred", "runtime_pending_idle")
            with runtime._lock:
                if self._closed or self._runtimes.get(account_id) is not runtime or runtime._stopping:
                    return usage_result("unavailable", "runtime_changed")
                if _cancelled(cancel):
                    return usage_result("cancelled", "cancelled")
                credential, gate = self._usage_capture(runtime, expected_binding)
                if not credential:
                    return gate
                if credential["expires"] is not None and credential["expires"] <= time.time() * 1000:
                    return usage_result("deferred", "expired_pending_idle")
                epoch = self._usage_epochs.get(account_id, 0)
                instance = runtime.instance_id
            result = _usage_https(credential["access"], credential["account"], cancel)
            for attempt in range(2):
                with runtime._lock:
                    if _cancelled(cancel):
                        return usage_result("cancelled", "cancelled")
                    if (self._closed or self._runtimes.get(account_id) is not runtime or runtime._stopping or
                            runtime.instance_id != instance or self._usage_epochs.get(account_id, 0) != epoch):
                        return usage_result("unavailable", "runtime_changed")
                    current, _ = self._usage_capture(runtime, expected_binding)
                    if not current or any(current[key] != credential[key] for key in ("id", "method", "identity", "account")):
                        return usage_result("auth_unavailable", "binding_changed")
                    retry = (attempt == 0 and result["reason_code"] == "unauthorized" and
                             current["access"] != credential["access"])
                    if retry and current["expires"] is not None and current["expires"] <= time.time() * 1000:
                        return usage_result("deferred", "expired_pending_idle")
                    if not retry:
                        return result
                    credential = current
                # Only native-owned credential rotation authorizes one retry.
                result = _usage_https(credential["access"], credential["account"], cancel)
        except Exception:
            # Native and transport exceptions may carry private response data.
            return usage_result("cancelled", "cancelled") if _cancelled(cancel) else usage_result("unavailable", "native_unavailable")
        return usage_result("unavailable", "native_unavailable")

    def restart_idle_for_usage(self, account_id, reservation, cancel=None):
        """Use only a scheduler maintenance lease with a lock-free validator.

        Native activity independently proves idle. A lease covers jobs whose
        sessions have not been created yet, and blocks new session creation.
        """
        valid = reservation.get("is_valid") if isinstance(reservation, dict) else None
        if not self._valid_usage_binding(account_id, reservation) or not callable(valid):
            return usage_result("deferred", "maintenance_unreserved")
        reserved = False
        try:
            with self._lock:
                if self._closed or _cancelled(cancel) or not valid():
                    return usage_result("cancelled", "cancelled")
                if account_id in self._usage_maintenance:
                    return usage_result("deferred", "account_busy")
                self._usage_maintenance.add(account_id)
                reserved = True
                runtime = self._runtimes.get(account_id)
            if runtime is not None:
                with runtime._lock:
                    if runtime.sessions or (self._login and self._login[0] == account_id):
                        return usage_result("deferred", "account_busy")
                    current, gate = self._usage_capture(runtime, reservation)
                    if not current:
                        return gate
                    activity = runtime.request("GET", "/api/session/active", timeout=3)
                    if not isinstance(activity, dict) or not isinstance(activity.get("data"), dict):
                        return usage_result("deferred", "activity_unverified")
                    if activity["data"]:
                        return usage_result("deferred", "account_busy")
                    if self._closed or _cancelled(cancel) or not valid():
                        return usage_result("cancelled", "cancelled")
                    runtime.stop()  # Failure preserves the old owner and lock.
                with self._lock:
                    if self._runtimes.get(account_id) is runtime:
                        self._runtimes.pop(account_id)
                    self._usage_epochs[account_id] = self._usage_epochs.get(account_id, 0) + 1
            with self._lock:
                if self._closed or _cancelled(cancel) or not valid():
                    return usage_result("cancelled", "cancelled")
            runtime = self.ensure(account_id, _usage_reserved=True,
                                  _start_guard=lambda: not _cancelled(cancel) and valid())
            # Location initialisation is the native refresh owner. This GET may
            # contact OAuth, so no manager/scheduler/store lock surrounds it.
            with runtime._lock:
                if self._closed or _cancelled(cancel) or not valid() or runtime._stopping:
                    return usage_result("cancelled", "cancelled")
                current, gate = self._usage_capture(runtime, reservation)
                if not current:
                    return gate
                runtime.request("GET", "/api/integration", timeout=10)
            return usage_result("partial", "native_refreshed")
        except Exception:
            return usage_result("cancelled", "cancelled") if _cancelled(cancel) else usage_result("deferred", "refresh_unavailable")
        finally:
            if reserved:
                with self._lock:
                    self._usage_maintenance.discard(account_id)

    def require_subscription(self, account_id):
        runtime = self.ensure(account_id)
        methods = {m["id"] for m in self.auth_methods(account_id)}
        entries = runtime.request("GET", "/api/credential")["data"]
        if not any(e.get("active") and e.get("integrationID") == "openai" and
                   e.get("value", {}).get("type") == "oauth" and
                   e.get("value", {}).get("methodID") in methods for e in entries):
            from .services import ServiceError
            raise ServiceError("سجّل الدخول إلى حساب ChatGPT قبل تشغيله.", "account_auth",
                               account_id=account_id, scope="account")

    def auth_methods(self, account_id):
        # Listing awaits location initialisation. A direct get immediately after
        # /api/info can race builtin integration registration on native startup.
        integrations = self.ensure(account_id).request("GET", "/api/integration")["data"]
        info = next((i for i in integrations if i.get("id") == "openai"), {})
        return [{"id": m["id"], "type": "oauth", "label": m.get("label", "ChatGPT")}
                for m in info.get("methods", []) if m.get("type") == "oauth" and
                "chatgpt" in (str(m.get("id", "")) + " " + str(m.get("label", ""))).lower()]

    def begin_login(self, account_id, method=None, inputs=None):
        with self._lock:
            if self._login:
                raise RuntimeError("Complete or cancel the current ChatGPT sign-in first.")
            self._usage_epochs[account_id] = self._usage_epochs.get(account_id, 0) + 1
            methods = self.auth_methods(account_id)
            selected = next((m for m in methods if method is None or m["id"] == method), None)
            if not selected:
                raise RuntimeError("This OpenCode build exposes no supported ChatGPT OAuth method.")
            runtime = self.ensure(account_id)
            data = {"methodID": selected["id"]}
            if inputs:
                data["answer"] = inputs
            attempt = runtime.request("POST", "/api/integration/openai/connect/oauth", data)["data"]
            attempt_id = str(attempt["attemptID"])
            self._login = (account_id, runtime, attempt_id, attempt.get("mode", "auto"))
            url = urlsplit(attempt["url"])
            if url.scheme != "https" or url.hostname != "auth.openai.com" or url.username or url.password or url.port not in (None, 443):
                self.cancel_login(account_id)
                raise RuntimeError("OpenCode returned an unsupported ChatGPT authorization URL.")
            return {"account_id": account_id, "attempt_id": attempt_id, "url": attempt["url"],
                    "mode": attempt["mode"], "instructions": attempt.get("instructions", ""),
                    "expires_at": attempt.get("time", {}).get("expires", 0) / 1000}

    def finish_login(self, account_id, code=None):
        with self._lock:
            if not self._login or self._login[0] != account_id:
                raise RuntimeError("There is no matching ChatGPT sign-in attempt.")
            _, runtime, attempt_id, mode = self._login
            path = "/api/integration/openai/connect/oauth/" + quote(attempt_id, safe="")
            if mode == "code" and code:
                runtime.request("POST", path + "/complete", {"code": str(code)})
            status = runtime.request("GET", path)["data"]["status"]
            if status == "pending":
                return {"account_id": account_id, "status": "pending", "authenticated": False}
            self._login = None
            if status != "complete":
                return {"account_id": account_id, "status": status, "authenticated": False}
            return dict(self.metadata(account_id), status="complete")

    def cancel_login(self, account_id):
        with self._lock:
            if self._login and self._login[0] == account_id:
                _, runtime, attempt_id, _ = self._login
                try:
                    runtime.request("DELETE", "/api/integration/openai/connect/oauth/" + quote(attempt_id, safe=""))
                finally:
                    self._login = None

    def confirm_stopped(self, account_id, session_id, instance_id="", workpath=""):
        """Reconcile a durable uncertain attempt without starting a new server."""
        with self._lock:
            runtime = self._runtimes.get(account_id)
            if runtime and (not instance_id or runtime.instance_id == instance_id):
                with runtime._lock:
                    if not session_id:
                        # Coordinate with session creation before deciding that
                        # an owner with no tracked attempt may be shut down.
                        if runtime.sessions:
                            return False
                        try:
                            activity = runtime.request("GET", "/api/session/active", timeout=3)
                            if not isinstance(activity, dict) or not isinstance(activity.get("data"), dict) or activity["data"]:
                                return False
                            runtime.stop()
                        except RuntimeError:
                            return False
                        self._runtimes.pop(account_id, None)
                        return True
                    return runtime.abort_and_wait(session_id, workpath)
            owner = self.profile_path(account_id) / "runtime-owner.json"
            if not owner.exists():
                return True
            try:
                previous = json.loads(owner.read_text(encoding="utf-8"))
                return isinstance(previous, dict) and not _pid_alive(previous.get("pid"))
            except (OSError, ValueError):
                return False

    def delete_auth(self, account_id):
        with self._lock:
            self._usage_epochs[account_id] = self._usage_epochs.get(account_id, 0) + 1
            self.cancel_login(account_id)
            runtime = self.ensure(account_id)
            with runtime._lock:
                if runtime.sessions:
                    raise RuntimeError("Cannot disconnect an account with running attempts.")
                entries = runtime.request("GET", "/api/credential")["data"]
                for entry in entries:
                    if entry.get("integrationID") == "openai":
                        runtime.request("DELETE", "/api/credential/" + quote(entry["id"], safe=""))

    def stop(self, account_id):
        with self._lock:
            self._usage_epochs[account_id] = self._usage_epochs.get(account_id, 0) + 1
            try:
                self.cancel_login(account_id)
            except RuntimeError:
                pass
        with self._account_lock(account_id):
            runtime = self._runtimes.get(account_id)
            if runtime:
                runtime.stop()
                self._runtimes.pop(account_id, None)

    def close(self):
        with self._lock:
            self._closed = True
            account_ids = set(self._runtimes) | set(self._account_locks)
        errors = []
        for account_id in account_ids:
            try:
                self.stop(account_id)
            except RuntimeError as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Some account servers could not be confirmed stopped.")
