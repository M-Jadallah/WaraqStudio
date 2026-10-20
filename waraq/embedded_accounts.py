"""Sanitized Python facade for the packaged account engine.

This module never opens credential storage, decodes tokens, refreshes OAuth, or
contacts provider endpoints. All public reads are cached; RPC methods run in a
worker/background thread supplied by the caller or ``request_refresh``.
"""
from __future__ import annotations

import copy
from functools import wraps
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from .embedded_runtime import EmbeddedRuntimeError, EmbeddedRuntimeSupervisor, private_directory, _atomic_json


class AccountEngineError(EmbeddedRuntimeError):
    def __init__(self, reason="engine_unavailable", retry_at=0):
        self.retry_at = _timestamp(retry_at)
        self.kind = {"NO_ELIGIBLE_ACCOUNT": "no_available_account", "CONCURRENCY_LIMIT": "accounts_busy",
                     "LOCATION_BUSY": "accounts_busy", "LOGIN_REQUIRED": "login_required"}.get(reason, "runtime_unavailable")
        super().__init__(reason)


class AccountUnavailableError(AccountEngineError):
    pass


_ERRORS = frozenset(("NO_ELIGIBLE_ACCOUNT", "CONCURRENCY_LIMIT", "LOCATION_BUSY", "ACCOUNT_NOT_FOUND",
                     "ACCOUNT_BUSY", "INVALID_INPUT", "AUTH_FAILED", "AUTH_CANCELLED", "AUTH_PENDING",
                     "AUTH_EXPIRED", "LOGIN_REQUIRED", "DUPLICATE_ACCOUNT", "STALE_BINDING", "STORAGE_FAILED",
                     "UNAUTHORIZED", "RATE_LIMITED", "UNSUPPORTED", "ENGINE_CLOSED", "UNKNOWN_METHOD",
                     "ATTEMPT_NOT_FOUND", "METHOD_UNAVAILABLE", "BINDING_CHANGED", "USAGE_UNAVAILABLE",
                     "AUTH_BUSY", "AUTH_BEGIN_FAILED", "AUTH_ATTEMPT_NOT_FOUND", "ACCOUNT_NOT_AUTHENTICATED",
                     "STORAGE_UNAVAILABLE", "ENGINE_UNAVAILABLE", "LOCATION_MISMATCH", "ATTEMPT_EXISTS"))
_IDEMPOTENT = frozenset(("health", "accounts.list", "auth.status", "attempt.resolve"))
# Only transport loss is safe to repeat immediately. Validation, permission,
# ownership and protocol errors need a deliberate repair/recheck instead.
_TRANSPORT_ERRORS = frozenset(("engine_connection_failed", "engine_start_timeout", "engine_unavailable"))
_FIXED_ERRORS = frozenset(("private_path_is_link", "private_permissions_failed", "private_owner_unknown",
                           "engine_manifest_invalid", "engine_version_mismatch", "engine_integrity_failed",
                           "engine_payload_missing", "engine_protocol_mismatch", "engine_api_invalid",
                           "engine_response_too_large", "engine_session_invalid", "engine_already_owned",
                           "engine_owner_invalid", "engine_owner_unverified", "engine_shutdown_unverified"))
_HEALTH_ERRORS = _FIXED_ERRORS | _TRANSPORT_ERRORS | frozenset(("engine_http_error", "engine_request_failed",
                        "ENGINE_UNAVAILABLE", "STORAGE_UNAVAILABLE", "STORAGE_FAILED", "UNAUTHORIZED"))


def _number(value, default=0):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _timestamp(value):
    value = _number(value)
    return value / 1000 if value > 100_000_000_000 else value


def _tracked(method):
    """Prevent new automatic work while a synchronous operation is active."""
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            self._operations += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._lock:
                self._operations -= 1
    return run


def _serialized(method):
    """Serialize metadata/auth updates, without blocking execution control."""
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._refresh_lock:
            return method(self, *args, **kwargs)
    return _tracked(run)


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else ""


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise AccountEngineError("invalid_account_reference")
    return value


def _window(raw):
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("usedPercent", raw.get("used_percent")), -1)
    remaining = _number(raw.get("remainingPercent", raw.get("remaining_percent")), -1)
    if remaining < 0 and 0 <= used <= 100:
        remaining = 100 - used
    if not 0 <= remaining <= 100:
        return None
    reset = _timestamp(raw.get("resetsAt", raw.get("resets_at", raw.get("resetAtMs", raw.get("resetAt")))))
    minutes = _number(raw.get("windowMinutes", raw.get("window_minutes")))
    if not minutes:
        minutes = _number(raw.get("windowSeconds", raw.get("window_seconds"))) / 60
    return {"status": "available", "remainingPercent": remaining, "resetsAt": reset, "windowMinutes": minutes}


def _usage(raw, previous=None):
    raw = raw if isinstance(raw, dict) else {}
    five = _window(raw.get("fiveHour", raw.get("five_hour")))
    weekly = _window(raw.get("weekly"))
    # Provider positions change on single-window plans. Label a window only
    # when its stated duration identifies it; absent data stays unknown.
    for name in ("primary", "secondary"):
        window = _window(raw.get(name))
        if window and window["windowMinutes"] == 300:
            five = window
        elif window and window["windowMinutes"] == 10080:
            weekly = window
    status = raw.get("status")
    if status not in ("available", "partial", "unavailable", "auth_unavailable", "unsupported", "stale", "unknown"):
        status = "available" if five and weekly else "partial" if five or weekly else "unknown"
    success = _timestamp(raw.get("lastSuccessAt", raw.get("observedAt", raw.get("observed_at"))))
    retained = previous if isinstance(previous, dict) else {}
    stale = bool(raw.get("stale", False))
    # A transient engine/endpoint failure never erases a successful quota read.
    if not five and not weekly and (retained.get("fiveHour") or retained.get("weekly")) and status not in ("auth_unavailable",):
        five, weekly = copy.deepcopy(retained.get("fiveHour")), copy.deepcopy(retained.get("weekly"))
        success, stale = _number(retained.get("lastSuccessAt")), True
    reason = raw.get("messageCode", raw.get("reason", "usage_" + status))
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", reason):
        reason = "usage_unavailable"
    wait = max(0, _number(raw.get("refreshWaitSeconds")))
    return {"status": status, "stale": stale, "lastSuccessAt": success,
            "lastAttemptAt": _timestamp(raw.get("lastAttemptAt", raw.get("attemptedAt"))),
            "messageCode": reason, "source": "embedded", "canRefresh": bool(raw.get("canRefresh", wait == 0)),
            "refreshWaitSeconds": wait, "refreshBlockReason": "cooldown" if wait else "",
            "refreshing": bool(raw.get("refreshing", False)), "fiveHour": five, "weekly": weekly}


def _account(raw, previous=None):
    if not isinstance(raw, dict):
        raise AccountEngineError("engine_protocol_mismatch")
    ident = _identifier(raw.get("accountRef", raw.get("id")))
    login = raw.get("loginState", raw.get("login_state", "staged"))
    login = {"ready": "authenticated", "reauth": "needsverify", "staged": "signed_out"}.get(login, login)
    if login not in ("authenticated", "needsverify", "signed_out", "authorizing", "checking", "failed"):
        login = "needsverify"
    health = raw.get("health", "unknown")
    if health not in ("ready", "cooldown", "disabled", "unknown", "unavailable", "failed"):
        health = "unknown"
    # Identity labels are never merged across changes to the opaque binding.
    binding = _text(raw.get("bindingVersion", raw.get("binding_version")), 160)
    old = previous if previous and previous.get("id") == ident and binding and previous.get("binding_version") == binding else {}
    return {"id": ident, "alias": _text(raw.get("alias"), 80), "priority": int(_number(raw.get("priority"), 100)),
            "enabled": bool(raw.get("enabled", True)), "preferred": bool(raw.get("preferred", False)),
            "email": _text(raw.get("email"), 254), "subscription": _text(raw.get("plan", raw.get("subscription")), 80),
            "login_state": login, "health": health, "in_flight": max(0, int(_number(raw.get("inFlight", raw.get("in_flight"))))),
            "cooldown_until": max(0, _timestamp(raw.get("cooldownUntil", raw.get("cooldown_until")))),
            "usage": _usage(raw.get("quota", raw.get("usage")), old.get("usage")),
            "binding_version": binding,
            "created_at": _number(raw.get("createdAt", raw.get("created_at")))}


class EmbeddedAccountService:
    def __init__(self, config, supervisor=None, clock=None):
        self.config = config
        self.supervisor = supervisor or EmbeddedRuntimeSupervisor(config)
        self.runtime_manager = self.supervisor
        self.clock = clock or time.time
        self.root = Path(config.home) / "accounts-v2"
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._usage_lock = threading.Lock()
        self._operations = 0
        self._accounts, self._auth, self._attempts = {}, {}, {}
        self._closed, self._refresh_thread = False, None
        self._failures, self._next_refresh, self._next_usage = 0, 0, 0
        self._health = {"state": "starting", "reason": "", "next_retry_at": 0, "migration_required": True}
        self._health["requires_manual_recovery"] = False
        self._persisted_public = None
        # These files contain only allowlisted UI metadata, never plugin state.
        try:
            state = json.loads((self.root / "ui-state.json").read_text(encoding="utf-8"))
            self._health["migration_required"] = state.get("migration_required") is not False
        except (OSError, ValueError, AttributeError):
            pass
        try:
            cached = json.loads((self.root / "public-cache.json").read_text(encoding="utf-8"))
            for raw in cached if isinstance(cached, list) else []:
                row = _account(raw)
                row["usage"]["stale"] = True
                self._accounts[row["id"]] = row
        except (OSError, ValueError, TypeError, EmbeddedRuntimeError):
            self._accounts = {}

    @property
    def status(self):
        with self._lock:
            return copy.deepcopy(self._health)

    def list(self):
        with self._lock:
            rows = copy.deepcopy(sorted(self._accounts.values(), key=lambda item: (not item["preferred"], -item["priority"], item["id"])))
            cfg = self.config.snapshot()
            freshness = max(60, int(cfg.get("accounts_refresh_seconds", 300)))
            for row in rows:
                success = row["usage"]["lastSuccessAt"]
                if success and self.clock() - success >= freshness:
                    row["usage"]["stale"] = True
            return rows

    def readiness(self, model=""):
        with self._lock:
            if self._closed:
                return {"ready": False, "reason": "engine_closed", "next_retry_at": 0}
            if self._health["state"] != "ready":
                return {"ready": False, "reason": self._health["reason"] or "engine_starting", "next_retry_at": self._health["next_retry_at"]}
            now = self.clock()
            eligible = [row for row in self._accounts.values() if row["enabled"] and
                        row["login_state"] == "authenticated" and row["health"] in ("ready", "cooldown") and
                        row["cooldown_until"] <= now]
            return {"ready": bool(eligible), "reason": "" if eligible else "no_eligible_account", "next_retry_at": self.next_available_at(model)}

    def available(self, model=""):
        return self.readiness(model)["ready"]

    def models(self):
        """Read the private host's catalog from a background caller."""
        runtime = self.supervisor.ensure()
        result = runtime.request("GET", "/api/model", directory=runtime.profile / "home")
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise AccountEngineError("engine_protocol_mismatch")
        models = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            provider, ident = row.get("providerID"), row.get("id")
            if isinstance(provider, str) and isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9._-]+", provider) and re.fullmatch(r"[^\s/#]+", ident):
                models.add(provider + "/" + ident)
        return sorted(models)

    def next_available_at(self, model=""):
        with self._lock:
            now = self.clock()
            if any(row["enabled"] and row["login_state"] == "authenticated" and row["health"] in ("ready", "cooldown")
                   and row["cooldown_until"] <= now for row in self._accounts.values()):
                return now
            candidates = [row["cooldown_until"] for row in self._accounts.values() if row["enabled"] and row["cooldown_until"] > now]
            return min(candidates, default=self._health["next_retry_at"])

    @_tracked
    def _call(self, method, params=None, directory=None):
        with self._lock:
            if self._closed:
                raise AccountEngineError("engine_closed")
        tries = 2 if method in _IDEMPOTENT else 1
        for attempt in range(tries):
            try:
                runtime = self.supervisor.ensure()
                response = runtime.rpc(method, params or {}, directory=directory)
                if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                    raise AccountEngineError("engine_protocol_mismatch")
                if not response["ok"]:
                    error = response.get("error") or {}
                    code = error.get("code")
                    code = code if code in _ERRORS else "engine_request_failed"
                    retry = _timestamp(error.get("retryAt"))
                    if code in ("NO_ELIGIBLE_ACCOUNT", "CONCURRENCY_LIMIT", "LOCATION_BUSY"):
                        with self._lock:
                            self._next_refresh = min(self._next_refresh, self.clock() + 2)
                        raise AccountUnavailableError(code, retry)
                    raise AccountEngineError(code, retry)
                result = response.get("data")
                if not isinstance(result, dict):
                    raise AccountEngineError("engine_protocol_mismatch")
                with self._lock:
                    if self._closed:
                        raise AccountEngineError("engine_closed")
                    # An unrelated successful RPC must not clear a latched
                    # profile/ownership failure; only explicit recovery does.
                    if not self._health["requires_manual_recovery"]:
                        self._failures = 0
                        self._health.update(state="ready", reason="", next_retry_at=0)
                return result
            except AccountEngineError as exc:
                if exc.reason in _HEALTH_ERRORS:
                    self._record_failure(exc.reason)
                raise
            except EmbeddedRuntimeError as exc:
                if exc.reason in _TRANSPORT_ERRORS and attempt + 1 < tries and not self._closed:
                    time.sleep(0.1)
                    continue
                retry = self._record_failure(exc.reason)
                raise AccountEngineError(exc.reason, retry) from None

    def _record_failure(self, reason):
        with self._lock:
            if self._closed:
                return 0
            self._failures += 1
            manual = reason in _FIXED_ERRORS or reason == "UNAUTHORIZED"
            # Preserve a fixed error even if an already-running RPC later fails.
            if self._health["requires_manual_recovery"]:
                return self._health["next_retry_at"]
            retry = 0 if manual else self.clock() + min(60, 2 ** min(self._failures, 6))
            self._health.update(state="unavailable", reason=reason, next_retry_at=retry,
                                requires_manual_recovery=manual)
            for row in self._accounts.values():
                row["usage"]["stale"] = True
            return retry

    def _replace(self, rows):
        if not isinstance(rows, list):
            raise AccountEngineError("engine_protocol_mismatch")
        with self._lock:
            if self._closed:
                return self.list()
            replacement = {}
            for raw in rows:
                ident = raw.get("accountRef", raw.get("id")) if isinstance(raw, dict) else None
                row = _account(raw, self._accounts.get(ident))
                replacement[row["id"]] = row
            self._accounts = replacement
            public = self.list()
        # Persist only the sanitized projection. Failure of this optional cache
        # cannot invalidate the engine's successful authoritative mutation.
        with self._lock:
            if public != self._persisted_public:
                try:
                    private_directory(self.root)
                    _atomic_json(self.root / "public-cache.json", public)
                    self._persisted_public = copy.deepcopy(public)
                except (OSError, EmbeddedRuntimeError):
                    pass
        return public

    @_serialized
    def refresh(self):
        with self._refresh_lock:
            result = self._call("accounts.list")
            try:
                rows = self._replace(result.get("accounts"))
            except AccountEngineError:
                self._record_failure("engine_protocol_mismatch")
                raise
            with self._lock:
                self._next_refresh = self.clock() + 5
            return rows

    def request_refresh(self, force=False):
        """Shared automatic gate for scheduler and GUI; force skips freshness only."""
        with self._lock:
            cfg = self.config.snapshot()
            if (self._closed or not cfg.get("accounts_enabled", True)
                    or self._health["requires_manual_recovery"] or self.clock() < self._health["next_retry_at"]
                    or (not force and self.clock() < self._next_refresh)
                    or self.refresh_in_flight):
                return False
            self._refresh_thread = threading.Thread(target=self._background_refresh, daemon=True, name="embedded-account-refresh")
            self._refresh_thread.start()
            return True

    @_tracked
    def _background_refresh(self):
        try:
            with self._lock:
                # Settings/health may change between reservation and execution.
                if (self._closed or not self.config.snapshot().get("accounts_enabled", True)
                        or self._health["requires_manual_recovery"] or self.clock() < self._health["next_retry_at"]):
                    return
            self.refresh()
            with self._lock:
                cfg = self.config.snapshot()
                usage_due = (not self._closed and cfg.get("accounts_enabled", True)
                             and bool(cfg.get("accounts_auto_refresh", True)) and self.clock() >= self._next_usage)
                if usage_due:
                    self._next_usage = self.clock() + max(60, int(cfg.get("accounts_refresh_seconds", 300)))
            if usage_due:
                self.refresh_usage()
        except EmbeddedRuntimeError:
            pass

    @property
    def refresh_in_flight(self):
        with self._lock:
            return bool(self._operations or (self._refresh_thread and self._refresh_thread.is_alive()))

    @_serialized
    def _mutate(self, method, params):
        result = self._call(method, params)
        # A subsequent failed list must not turn an acknowledged mutation into
        # an apparent failure that encourages the user to repeat it.
        with self._lock:
            ident = params.get("accountRef")
            if method == "account.remove":
                self._accounts.pop(ident, None)
            else:
                raw = result.get("account", result)
                row = _account(raw, self._accounts.get(raw.get("accountRef")))
                self._accounts[row["id"]] = row
                if method == "account.prefer":
                    for account in self._accounts.values():
                        account["preferred"] = account["id"] == ident
        try:
            self.refresh()
        except EmbeddedRuntimeError:
            pass
        return result

    def create(self, alias):
        result = self._mutate("account.create", {"alias": _text(alias.strip() if isinstance(alias, str) else "", 80)})
        raw = result.get("account", result)
        return _account(raw)

    def update(self, ident, alias=None, priority=None, enabled=None):
        params = {"accountRef": _identifier(ident)}
        if alias is not None:
            params["alias"] = _text(alias.strip(), 80)
        if priority is not None:
            params["priority"] = int(_number(priority, 100))
        if enabled is not None:
            params["enabled"] = bool(enabled)
        self._mutate("account.update", params)
        return next((row for row in self.list() if row["id"] == ident), {})

    def prefer(self, ident):
        self._mutate("account.prefer", {"accountRef": _identifier(ident)})
        return next((row for row in self.list() if row["id"] == ident), {})

    def remove(self, ident):
        self._mutate("account.remove", {"accountRef": _identifier(ident)})
        with self._lock:
            self._auth.pop(ident, None)
        return {"removed": True}

    def auth_methods(self, ident=None):
        if ident is not None:
            _identifier(ident)
        result = self._call("health")
        return [{"id": row["id"], "label": _text(row.get("label"), 80), "type": "oauth"}
                for row in result.get("authMethods", []) if isinstance(row, dict) and row.get("id") in ("browser", "device")]

    @_serialized
    def begin_login(self, ident, method=None):
        ident = _identifier(ident)
        method = method or "browser"
        if method not in ("browser", "device"):
            raise AccountEngineError("invalid_auth_method")
        result = self._call("auth.begin", {"accountRef": ident, "method": method})
        challenge = _identifier(result.get("attemptId"))
        if result.get("accountRef") != ident:
            raise AccountEngineError("stale_auth_binding")
        url = _text(result.get("url"), 4096)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise AccountEngineError("invalid_auth_url")
        with self._lock:
            self._auth[ident] = challenge
        return {"status": "pending", "mode": "device" if method == "device" else "auto", "url": url,
                "userCode": _text(result.get("userCode"), 40)}

    @_serialized
    def finish_login(self, ident, code=None):
        ident = _identifier(ident)
        if code:
            raise AccountEngineError("manual_auth_not_supported")
        with self._lock:
            challenge = self._auth.get(ident)
        if not challenge:
            raise AccountEngineError("auth_not_started")
        result = self._call("auth.status", {"attemptId": challenge})
        with self._lock:
            if self._auth.get(ident) != challenge or result.get("accountRef") != ident:
                raise AccountEngineError("stale_auth_binding")
        state = result.get("state")
        if state not in ("pending", "success", "failed", "cancelled"):
            raise AccountEngineError("engine_protocol_mismatch")
        if state != "pending":
            with self._lock:
                self._auth.pop(ident, None)
            self.refresh()
        return {"status": state, "authenticated": state == "success"}

    @_serialized
    def cancel_login(self, ident):
        ident = _identifier(ident)
        with self._lock:
            challenge = self._auth.get(ident)
        if challenge:
            self._call("auth.cancel", {"attemptId": challenge})
            with self._lock:
                if self._auth.get(ident) == challenge:
                    self._auth.pop(ident, None)
        return {"status": "cancelled"}

    @_serialized
    def recheck(self, ident=""):
        if ident:
            _identifier(ident)
        with self._lock:
            if self._closed:
                raise AccountEngineError("engine_closed")
            self._health.update(requires_manual_recovery=False, next_retry_at=0)
            self._failures = 0
        if ident:
            self._call("usage.refresh", {"accountRef": _identifier(ident), "manual": True})
        rows = self.refresh()
        if not ident:
            return rows
        _identifier(ident)
        return next((row for row in rows if row["id"] == ident), {})

    @_tracked
    def refresh_usage(self, account_id=None, manual=False):
        # A provider quota request can take minutes. Its own gate never holds
        # the metadata/auth lock or blocks attempt begin/resolve/close RPCs.
        with self._usage_lock:
            return self._refresh_usage(account_id, manual)

    def _refresh_usage(self, account_id=None, manual=False):
        params = {"manual": bool(manual)}
        if account_id:
            params["accountRef"] = _identifier(account_id)
        with self._lock:
            targets = [account_id] if account_id else list(self._accounts)
            for ident in targets:
                if ident in self._accounts:
                    self._accounts[ident]["usage"].update(refreshing=True, lastAttemptAt=self.clock())
        try:
            self._call("usage.refresh", params)
        except EmbeddedRuntimeError:
            with self._lock:
                for ident in targets:
                    if ident in self._accounts:
                        self._accounts[ident]["usage"].update(refreshing=False, stale=True, status="unavailable", messageCode="usage_unavailable")
            raise
        with self._lock:
            for ident in targets:
                if ident in self._accounts:
                    self._accounts[ident]["usage"]["refreshing"] = False
        return self.refresh()

    def request_usage(self, account_id=None, manual=False, all=False):
        # The GUI invokes this synchronous method from its bounded executor.
        return self.refresh_usage(None if all else account_id, manual)

    @_tracked
    def begin_attempt(self, job_id, worker_id, model, directory=None, max_concurrency=None):
        if directory is None:
            raise AccountEngineError("attempt_directory_required")
        directory = str(Path(directory).resolve())
        cfg = self.config.snapshot()
        maximum = max_concurrency if max_concurrency is not None else cfg.get("accounts_max_concurrency", 5)
        attempt_id = uuid.uuid4().hex
        result = self._call("attempt.begin", {"attemptId": attempt_id, "jobId": str(job_id), "workerId": str(worker_id),
                            "model": str(model), "directory": directory, "maxConcurrency": max(1, min(16, int(maximum)))}, directory=directory)
        if result.get("attemptId") != attempt_id:
            raise AccountEngineError("stale_attempt_binding")
        account = _identifier(result.get("accountRef"))
        runtime = self.supervisor.ensure()
        lease = {"id": attempt_id, "attempt_id": attempt_id, "account_id": account, "alias": _text(result.get("alias"), 80),
                 "email": _text(result.get("email"), 254), "instance_id": runtime.instance_id,
                 "runtime_instance_id": runtime.instance_id, "workpath": directory, "state": "active"}
        with self._lock:
            self._attempts[attempt_id] = lease
            if account in self._accounts:
                self._accounts[account]["in_flight"] += 1
        return copy.deepcopy(lease)

    def resolve_attempt(self, attempt_id):
        attempt_id = _identifier(attempt_id)
        with self._lock:
            lease = self._attempts.get(attempt_id, {})
        result = self._call("attempt.resolve", {"attemptId": attempt_id}, directory=lease.get("workpath"))
        return self._attempt_projection(result, attempt_id)

    @staticmethod
    def _attempt_projection(result, attempt_id):
        if result.get("attemptId") != attempt_id:
            raise AccountEngineError("stale_attempt_binding")
        events = []
        for event in result.get("events", [])[:100]:
            if isinstance(event, dict) and event.get("type") in ("selected", "response", "failure", "closed"):
                status = int(_number(event.get("status")))
                events.append({"type": event["type"], "at": _timestamp(event.get("at")),
                               "account_id": _text(event.get("accountRef"), 160), "status": status if 100 <= status <= 599 else None})
        failure = result.get("failure")
        safe_failure = None
        if isinstance(failure, dict):
            kind = failure.get("kind")
            kind = kind if kind in ("quota", "rate_limited", "reauth", "workspace_blocked", "provider_error") else "provider_error"
            safe_failure = {"kind": kind, "confirmed": failure.get("confirmed") is True,
                            "retry_at": _timestamp(failure.get("retryAtMs"))}
        return {"id": attempt_id, "attempt_id": attempt_id, "account_id": _text(result.get("accountRef"), 160),
                "state": _text(result.get("state"), 40), "events": events, "failure": safe_failure}

    def close_attempt(self, attempt_id, outcome="success", reason="", retry_after=0):
        projection = self._close_attempt(attempt_id, outcome, reason, retry_after)
        self.request_refresh(force=True)
        return projection

    @_tracked
    def _close_attempt(self, attempt_id, outcome="success", reason="", retry_after=0):
        attempt_id = _identifier(attempt_id)
        if outcome not in ("success", "failed", "cancelled", "limit", "error", "completed"):
            raise AccountEngineError("invalid_attempt_outcome")
        with self._lock:
            lease = self._attempts.get(attempt_id, {})
        result = self._call("attempt.close", {"attemptId": attempt_id, "outcome": outcome}, directory=lease.get("workpath"))
        projection = self._attempt_projection(result, attempt_id)
        with self._lock:
            self._attempts.pop(attempt_id, None)
            account = lease.get("account_id")
            if account in self._accounts:
                self._accounts[account]["in_flight"] = max(0, self._accounts[account]["in_flight"] - 1)
        return projection

    def acknowledge_migration(self):
        private_directory(self.root)
        _atomic_json(self.root / "ui-state.json", {"migration_required": False})
        with self._lock:
            self._health["migration_required"] = False
        return self.status

    def close(self):
        with self._lock:
            self._closed = True
            self._health.update(state="closed", reason="engine_closed")
        self.supervisor.close()
