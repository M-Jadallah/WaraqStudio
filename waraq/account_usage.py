"""Bounded, credential-free account telemetry, independent of job routing.

Workers emit sanitized results. A GUI owner applies them through ``apply``;
without a callback the service never starts network work.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .opencode_usage import USAGE_REASON_CODES


STATUSES = {"available", "partial", "unsupported", "auth_unavailable", "deferred", "unavailable", "cancelled"}
BACKOFF = (60, 120, 300, 600, 900)
REASONS = USAGE_REASON_CODES | frozenset((
    "usage_unavailable", "not_checked", "usage_available", "usage_partial", "usage_windows_unavailable",
    "usage_read_failed", "usage_cancelled", "usage_refresh_deferred", "usage_runtime_unsupported",
    "account_not_ready"))


def _number(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return float(value) if math.isfinite(value) else default
    except (OverflowError, ValueError):
        return default


def _window(value, duration):
    if not isinstance(value, dict):
        return None
    used = _number(value.get("used_percent"))
    remaining = _number(value.get("remaining_percent"))
    minutes = _number(value.get("window_minutes"))
    reset = _number(value.get("resets_at"))
    if (minutes is None or minutes <= 0 or minutes > 366 * 1440
            or (duration is not None and minutes != duration) or reset is None or reset <= 0):
        return None
    if used is None and remaining is not None:
        used = 100 - remaining
    if used is None or used < 0:
        return None
    return {"used_percent": used, "remaining_percent": max(0, min(100, 100 - used)),
            "window_minutes": minutes, "resets_at": reset}


def safe_usage_result(value):
    """Closed whitelist: raw HTTP/native fields and exception text cannot escape."""
    value = value if isinstance(value, dict) else {}
    status = value.get("status")
    status = status if isinstance(status, str) and status in STATUSES else "unavailable"
    reason = value.get("reason_code", "usage_unavailable")
    reason = reason if isinstance(reason, str) and reason in REASONS else "usage_unavailable"
    # Sources are identifiers, never a URL or provider/native response string.
    source = value.get("source", "")
    source = source if source in ("legacy_codex_usage", "chatgpt_usage", "") else ""
    result = {"status": status, "reason_code": reason, "source": source,
              "observed_at": max(0, _number(value.get("observed_at"), 0)),
              "five_hour": _window(value.get("five_hour"), 300),
              "weekly": _window(value.get("weekly"), 10080),
              "retry_after": max(0, _number(value.get("retry_after"), 0))}
    others = value.get("other_windows")
    if isinstance(others, list):
        windows = [window for raw in others[:2] if (window := _window(raw, None))]
        if windows:
            result["other_windows"] = windows
    buckets = value.get("buckets")
    if isinstance(buckets, list):
        safe_buckets = []
        for index, raw in enumerate(buckets[:8]):
            if not isinstance(raw, dict):
                continue
            bucket = {"index": index, "five_hour": _window(raw.get("five_hour"), 300),
                      "weekly": _window(raw.get("weekly"), 10080)}
            if isinstance(raw.get("other_windows"), list):
                bucket["other_windows"] = [window for item in raw["other_windows"][:2]
                                           if (window := _window(item, None))]
            if any(bucket.get(key) for key in ("five_hour", "weekly", "other_windows")):
                safe_buckets.append(bucket)
        if safe_buckets:
            result["buckets"] = safe_buckets
    if status in ("available", "partial"):
        present = sum(result[key] is not None for key in ("five_hour", "weekly"))
        any_window = present or result.get("other_windows") or result.get("buckets")
        result["status"] = "available" if present == 2 and status == "available" else "partial" if any_window else "unavailable"
        if not any_window:
            result["reason_code"] = "usage_windows_unavailable"
    return result


class AccountUsageMonitor:
    def __init__(self, scheduler, clock=None, executor=None, monotonic=None):
        self.scheduler = scheduler
        self.accounts = scheduler.accounts
        self.clock = clock or time.time
        self.monotonic = monotonic or (time.monotonic if clock is None else clock)
        self.lock = threading.RLock()
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-usage")
        self.callback = None
        self.closed = False
        self.pending = {}
        self.last_attempt = {}
        self.next_attempt = {}
        self.failures = {}
        # Durable snapshots are stale until this process validates their binding.
        self.validated = set()
        self.unsupported = {}
        self._floor_deadlines = {}
        self._due_deadlines = {}
        self._retry_deadlines = {}

    def interval(self):
        value = _number(self.scheduler.config.snapshot().get("chatgpt_usage_refresh_seconds", 300), 300)
        return max(60, min(1800, value))

    def set_callback(self, callback):
        with self.lock:
            if not self.closed:
                self.callback = callback

    def state(self, ident):
        with self.lock:
            return {"refreshing": ident in self.pending, "lastAttemptAt": self.last_attempt.get(ident, 0),
                    "nextAttemptAt": self.next_attempt.get(ident, 0),
                    "maintenance": self.accounts.usage_is_maintenance(ident)}

    def request(self, ident, manual=False):
        with self.lock:
            if self.closed or self.callback is None or ident in self.pending:
                return False
            now = self.clock()
            monotonic_now = self.monotonic()
            config = self.scheduler.config.snapshot()
            if not config.get("chatgpt_accounts_enabled", False):
                return False
            if not manual and not config.get("chatgpt_usage_auto_refresh", True):
                return False
            if not self.accounts.usage_eligible(ident):
                return False
            binding = self.accounts.usage_binding(ident)
            if not manual and self.unsupported.get(ident) == binding:
                return False
            record = self.accounts.usage_record(ident)
            last = max(self.last_attempt.get(ident, 0), record["last_attempt_at"] if record else 0)
            due = max(self.next_attempt.get(ident, 0), record["next_attempt_at"] if record else 0)
            snapshot = record["snapshot"] if record else {}
            if ident not in self._floor_deadlines:
                # Import persisted wall-clock due times once; live cadence then
                # uses monotonic deadlines, so clock corrections cannot stall
                # polling. A future persisted attempt signals a clock rollback;
                # retain its full delay rather than waiting until the old date.
                rollback = bool(last and last > now)
                remaining = max(0, due - (last if rollback else now))
                floor = 30 if rollback else max(0, last + 30 - now) if last else 0
                retry_wait = (snapshot.get("retry_after", 0) if rollback else
                              max(0, snapshot.get("observed_at", 0) + snapshot.get("retry_after", 0) - now))
                self._floor_deadlines[ident] = monotonic_now + floor
                self._due_deadlines[ident] = monotonic_now + remaining
                self._retry_deadlines[ident] = monotonic_now + retry_wait
            # Manual refresh may bypass the ordinary interval, never Retry-After
            # or the per-account 30-second request floor.
            if monotonic_now < self._floor_deadlines[ident]:
                return False
            failed = self.failures.get(ident, 0) or (record and record["status"] not in ("available", "partial", "not_checked"))
            if (monotonic_now < self._retry_deadlines[ident]
                    or (monotonic_now < self._due_deadlines[ident] and (not manual or failed))):
                return False
            request_id = uuid.uuid4().hex
            if not self.accounts.begin_usage_request(ident, binding, request_id, now):
                return False
            cancel = threading.Event()
            self.pending[ident] = {"request_id": request_id, "binding": binding, "cancel": cancel}
            self.last_attempt[ident] = now
            self._floor_deadlines[ident] = monotonic_now + 30
            try:
                self.executor.submit(self._work, ident, request_id, binding, cancel)
            except Exception:
                self.pending.pop(ident, None)
                self.next_attempt[ident] = now + BACKOFF[0]
                self._due_deadlines[ident] = monotonic_now + BACKOFF[0]
                return False
            return True

    def request_all(self):
        return sum(self.request(row["id"], manual=True) for row in self.accounts.list())

    def schedule_due(self):
        with self.lock:
            if self.closed or self.callback is None:
                return 0
        return sum(self.request(row["id"]) for row in self.accounts.list())

    def _work(self, ident, request_id, binding, cancel):
        try:
            result = self.scheduler.refresh_account_usage(ident, binding, cancel)
        except Exception:
            # Runtime exceptions may contain credentials or native response bodies.
            result = {"status": "unavailable", "reason_code": "usage_read_failed"}
        result = safe_usage_result(result)
        if not result["observed_at"]:
            result["observed_at"] = self.clock()
        payload = dict(binding, request_id=request_id, result=result)
        with self.lock:
            current = self.pending.get(ident)
            if (self.closed or cancel.is_set() or not current or current["request_id"] != request_id):
                return
            callback = self.callback
        if callback is not None:
            try:
                callback(payload)
            except Exception:
                # A failed receiver must not leave this account permanently busy.
                with self.lock:
                    if self.pending.get(ident) is current:
                        self.pending.pop(ident, None)
                        self.next_attempt[ident] = self.clock() + BACKOFF[0]
                        self._due_deadlines[ident] = self.monotonic() + BACKOFF[0]

    def apply(self, payload):
        if not isinstance(payload, dict):
            return False
        ident, request_id = payload.get("account_id"), payload.get("request_id")
        if not isinstance(ident, str) or not isinstance(request_id, str):
            return False
        with self.lock:
            current = self.pending.get(ident)
            if (self.closed or not current or current["request_id"] != request_id
                    or current["cancel"].is_set()):
                return False
            binding = current["binding"]
            if any(payload.get(key) != binding[key] for key in ("account_id", "auth_epoch", "identity")):
                return False
            result = safe_usage_result(payload.get("result"))
            success = result["status"] in ("available", "partial")
            count = 0 if success else self.failures.get(ident, 0) + 1
            delay = self.interval() if success else BACKOFF[min(count - 1, len(BACKOFF) - 1)]
            delay = max(delay, result["retry_after"])
            due = self.clock() + delay
            accepted = self.accounts.record_usage(ident, binding, request_id, result, due)
            # Only this request can clear its spinner, including an invalidated
            # auth binding that the GUI has not removed from its model yet.
            self.pending.pop(ident, None)
            if accepted:
                self.failures[ident] = count
                self.next_attempt[ident] = due
                self._due_deadlines[ident] = self.monotonic() + delay
                self._retry_deadlines[ident] = self.monotonic() + result["retry_after"]
                if success:
                    self.validated.add(ident)
                if result["status"] == "unsupported":
                    self.unsupported[ident] = binding
            return accepted

    def invalidate(self, ident):
        with self.lock:
            current = self.pending.pop(ident, None)
            if current:
                current["cancel"].set()
            self.validated.discard(ident)
            self.unsupported.pop(ident, None)
            self.failures.pop(ident, None)
            self.next_attempt.pop(ident, None)
            self.last_attempt.pop(ident, None)
            self._floor_deadlines.pop(ident, None)
            self._due_deadlines.pop(ident, None)
            self._retry_deadlines.pop(ident, None)
            self.accounts.invalidate_usage(ident)

    def stop(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.callback = None
            for current in self.pending.values():
                current["cancel"].set()
            self.pending.clear()
            self.validated.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)

    close = stop

    def public_snapshot(self, ident, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            record = self.accounts.usage_record(ident)
            try:
                binding = self.accounts.usage_binding(ident)
                if record and record["auth_epoch"] != binding["auth_epoch"]:
                    record = None
            except ValueError:
                record = None
            snapshot = record["snapshot"] if record else {}
            success = record["last_success_at"] if record else 0
            stale = bool(success and (ident not in self.validated or not self.accounts.usage_eligible(ident)
                         or record["status"] not in ("available", "partial")
                         or now >= success + 2 * self.interval()
                         or any(snapshot.get(key) and now >= snapshot[key]["resets_at"]
                                for key in ("five_hour", "weekly"))))
            result = {"status": record["status"] if record else "unavailable", "stale": stale,
                      "lastSuccessAt": success, "lastAttemptAt": record["last_attempt_at"] if record else 0,
                      "messageCode": record["reason_code"] if record else "not_checked",
                      "source": snapshot.get("source", ""), "refreshing": ident in self.pending}
            for source, target in (("five_hour", "fiveHour"), ("weekly", "weekly")):
                window = snapshot.get(source)
                result[target] = {"status": "stale" if stale else "available" if window else "unavailable",
                                  "remainingPercent": window["remaining_percent"] if window else None,
                                  "resetsAt": window["resets_at"] if window else None,
                                  "windowMinutes": window["window_minutes"] if window else None}
                if not window:
                    result[target]["status"] = "unavailable"
            return result
