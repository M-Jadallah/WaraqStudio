"""Pure, allowlisted interpretation of legacy Codex usage telemetry.

This module has no credential, filesystem or network access. Missing meters are
unknown; windows are identified by their actual duration, never their position.
"""
from __future__ import annotations

import math


SOURCE = "legacy_codex_usage"
LEGACY_METHODS = frozenset(("chatgpt-browser", "chatgpt-headless"))
CHATGPT_OAUTH_METHODS = frozenset((*LEGACY_METHODS, "chatgpt-token-sharing"))
VERIFIED_NATIVE_VERSIONS = frozenset(("2.0.27", "2.0.32"))
USAGE_REASON_CODES = frozenset((
    "token_sharing_unsupported", "method_unsupported", "native_version_unverified", "legacy_supported",
    "invalid_response", "windows_missing", "usage_available", "usage_partial", "cancelled",
    "timeout", "response_too_large", "unauthorized", "endpoint_unavailable", "throttled",
    "http_error", "connection_failed", "binding_invalid", "runtime_changed", "identity_unavailable",
    "expired_pending_idle", "binding_changed", "native_unavailable", "maintenance_unreserved",
    "runtime_unavailable", "account_busy", "activity_unverified", "native_refreshed", "refresh_unavailable",
    "credential_unavailable",
    "runtime_pending_idle",
))


def usage_result(status, reason_code, observed_at=0, **fields):
    return {"status": status, "reason_code": reason_code, "source": SOURCE,
            "observed_at": observed_at, "five_hour": None, "weekly": None, **fields}


def classify_usage_capability(method_id, native_version):
    if method_id == "chatgpt-token-sharing":
        return usage_result("unsupported", "token_sharing_unsupported")
    if not isinstance(method_id, str) or method_id not in LEGACY_METHODS:
        return usage_result("unsupported", "method_unsupported")
    if not isinstance(native_version, str) or native_version not in VERIFIED_NATIVE_VERSIONS:
        return usage_result("unsupported", "native_version_unverified")
    return usage_result("available", "legacy_supported")


def oauth_method_metadata(method_id, native_version):
    """Credential-free method catalog. Labels and source never come from native.

    The usage status is a capability classification, not a successful quota
    observation. A compatible legacy method still needs a real usage read.
    """
    if not isinstance(method_id, str) or method_id not in CHATGPT_OAUTH_METHODS:
        return None
    labels = {"chatgpt-browser": "Codex browser (legacy)",
              "chatgpt-headless": "Codex device code (legacy)",
              "chatgpt-token-sharing": "Sign in with ChatGPT"}
    legacy = method_id in LEGACY_METHODS
    capability = classify_usage_capability(method_id, native_version)
    return {"id": method_id, "type": "oauth", "label": labels[method_id],
            "kind": "legacy_codex" if legacy else "siwc",
            "usage": {"status": capability["status"], "reason_code": capability["reason_code"],
                      "source": SOURCE if legacy else ""}}


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def _window(value, observed_at):
    if not isinstance(value, dict):
        return None
    seconds = finite_number(value.get("limit_window_seconds"))
    used = finite_number(value.get("used_percent"))
    reset = finite_number(value.get("reset_at"))
    # Epoch seconds only, with a bounded future horizon; a past reset is stale
    # and cannot describe the current quota window.
    if (seconds is None or not 0 < seconds <= 366 * 86400 or used is None or used < 0 or
            reset is None or not 946684800 <= reset <= observed_at + 366 * 86400 or reset <= observed_at):
        return None
    return {"used_percent": used, "remaining_percent": max(0, min(100, 100 - used)),
            "window_minutes": seconds / 60, "resets_at": reset}


def _meter(value, observed_at):
    output = {"five_hour": None, "weekly": None}
    others, seen, invalid = [], set(), False
    if not isinstance(value, dict):
        return output, True
    for name in ("primary_window", "secondary_window"):
        raw = value.get(name)
        if raw is None:
            continue
        window = _window(raw, observed_at)
        if window is None:
            invalid = True
            continue
        duration = window["window_minutes"]
        # Duplicate durations are ambiguous and must not overwrite one another.
        if duration in seen:
            invalid = True
            key = {300: "five_hour", 10080: "weekly"}.get(duration)
            if key:
                output[key] = None
            continue
        seen.add(duration)
        key = {300: "five_hour", 10080: "weekly"}.get(duration)
        if key:
            output[key] = window
        else:
            others.append(window)
    if others:
        output["other_windows"] = others
    return output, invalid


def parse_usage_response(payload, observed_at):
    now = finite_number(observed_at)
    if now is None or now < 946684800 or not isinstance(payload, dict):
        return usage_result("unavailable", "invalid_response")
    meter, invalid = _meter(payload.get("rate_limit"), now)
    # Other meters (e.g. code review) never become the account's main quota.
    buckets = []
    for index, raw in enumerate((payload.get("additional_rate_limits") or [])[:8]
                                if isinstance(payload.get("additional_rate_limits"), list) else []):
        if not isinstance(raw, dict):
            invalid = True
            continue
        extra, bad = _meter(raw.get("rate_limit"), now)
        invalid |= bad
        if any(extra.values()):
            buckets.append({"index": index, **extra})
    present = sum(meter[key] is not None for key in ("five_hour", "weekly"))
    if not present and not meter.get("other_windows") and not buckets:
        return usage_result("unavailable", "invalid_response" if invalid else "windows_missing", now)
    result = usage_result("available" if present == 2 and not invalid else "partial",
                          "usage_available" if present == 2 and not invalid else "usage_partial", now, **meter)
    if buckets:
        result["buckets"] = buckets
    return result
