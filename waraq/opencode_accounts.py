"""Durable account metadata and routing. OAuth credentials never enter this store.

Callers take the scheduler lock before these methods when claiming jobs. The
registry uses Store's reentrant lock so a lease and job claim can share that lock.
The only in-memory state is live leases: interrupted jobs remain paused on restart.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import threading
import uuid
from contextlib import contextmanager


def _finite_number(value, default=0):
    try:
        number = float(value) if isinstance(value, (int, float)) else default
        return number if math.isfinite(number) else default
    except (OverflowError, ValueError, TypeError):
        return default


class DuplicateAccountError(ValueError):
    """A verified native identity already belongs to another registry entry."""


class OpenCodeAccounts:
    def __init__(self, store, config, clock=None):
        self.store, self.config = store, config
        self.clock = clock or time.time
        self.leases = {}
        self.usage_maintenance = {}
        with self.store.lock:
            for row in self._rows():
                if "auth_epoch" not in row:
                    row["auth_epoch"] = 0
                    self._put(row)

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise

    def _rows(self):
        return [json.loads(row[0]) for row in self.store.db.execute("SELECT metadata FROM chatgpt_accounts")]

    def _get(self, ident):
        row = self.store.db.execute("SELECT metadata FROM chatgpt_accounts WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("حساب ChatGPT غير موجود.")
        result = json.loads(row[0])
        result.setdefault("auth_epoch", 0)
        return result

    def _put(self, row):
        self.store.db.execute("INSERT OR REPLACE INTO chatgpt_accounts VALUES(?,?,?)",
                              (row["id"], json.dumps(row, ensure_ascii=False), self.clock()))

    def _safe(self, row):
        now = self.clock()
        reset = max(_finite_number(row.get("reset_at", 0)), _finite_number(row.get("throttle_until", 0)))
        exclusions = [{"model": model, "reset_at": _finite_number(info.get("until", 0))}
                      for model, info in row.get("models", {}).items()
                      if _finite_number(info.get("until", 0)) > now]
        health = row.get("health", "login_required")
        if health == "cooldown" and reset <= now:
            health = "probe"
        if exclusions and reset <= now:
            reset = min(info["reset_at"] for info in exclusions)
            if health == "healthy":
                health = "model_limited"
        return {key: row.get(key, "") for key in
                ("id", "alias", "priority", "enabled", "login_state", "email", "subscription", "error")} | {
            "preferred": self.store.state_get("chatgpt_preferred", "") == row["id"],
            "health": health, "reset_at": reset, "cooldown_seconds": max(0, int(reset - now)),
            "model_exclusions": exclusions,
            "in_flight": sum(v["account_id"] == row["id"] for v in self.leases.values())}

    def list(self):
        with self.store.lock:
            return [self._safe(row) for row in sorted(self._rows(), key=lambda r: (r["priority"], r["id"]))]

    def create(self, alias=""):
        with self.store.lock:
            rows = self._rows()
            row = {"id": uuid.uuid4().hex, "alias": str(alias).strip()[:80] or f"ChatGPT {len(rows) + 1}",
                   "priority": max((r["priority"] for r in rows), default=0) + 1,
                   "enabled": True, "login_state": "logged_out", "health": "login_required",
                   "identity": "", "email": "", "subscription": "", "error": "", "reset_at": 0,
                   "throttle_until": 0, "probe_count": 0, "generation": 0, "auth_epoch": 0, "models": {}}
            self._put(row)
            return self._safe(row)

    def update(self, ident, alias=None, priority=None, enabled=None):
        with self.store.lock:
            row = self._get(ident)
            if alias is not None:
                row["alias"] = str(alias).strip()[:80] or row["alias"]
            if priority is not None:
                row["priority"] = max(0, int(priority))
            if enabled is not None:
                row["enabled"] = bool(enabled)
            self._put(row)
            self._wake_waiters()
            return self._safe(row)

    def prefer(self, ident):
        with self.store.lock:
            self._get(ident)
            self.store.state_put("chatgpt_preferred", ident)
            self.store.state_put("chatgpt_current", ident)
            self._wake_waiters()

    def remove(self, ident):
        with self.store.lock:
            self.assert_idle(ident)
            row = self._get(ident)
            if row["login_state"] in ("authorizing", "pending"):
                raise ValueError("أكمل تسجيل الدخول أو ألغِه أولًا.")
            with self._transaction():
                self.invalidate_usage(ident)
                self.store.db.execute("DELETE FROM chatgpt_usage WHERE account_id=?", (ident,))
                self.store.db.execute("DELETE FROM chatgpt_accounts WHERE id=?", (ident,))
                for key in ("chatgpt_preferred", "chatgpt_current"):
                    if self.store.state_get(key, "") == ident:
                        self.store.state_clear(key)

    def assert_idle(self, ident):
        with self.store.lock:
            if ident in self.usage_maintenance:
                raise ValueError("انتظر انتهاء تحديث استخدام هذا الحساب أولًا.")
            if any(v["account_id"] == ident for v in self.leases.values()):
                raise ValueError("انتظر انتهاء مهام هذا الحساب أولًا.")
            if self._get(ident).get("health") == "execution_uncertain":
                raise ValueError("جلسة الحساب السابقة لم يُتحقق من توقفها؛ أعد فحص الحساب أولًا.")

    def set_login_state(self, ident, state, error=""):
        with self.store.lock:
            if state in ("authorizing", "removing", "pending", "checking", "logged_out", "signed_out"):
                self.assert_idle(ident)
            row = self._get(ident)
            if state in ("authorizing", "pending", "removing", "logged_out", "signed_out"):
                self._invalidate_auth(row)
            row.update(login_state=str(state)[:40], error=str(error)[:300])
            if state not in ("authenticated", "checking"):
                row["health"] = "login_required"
            self._put(row)

    def record_login_result(self, ident, result, reset_cooldown=True):
        with self.store.lock:
            self.assert_idle(ident)
            row = self._get(ident)
            if not result.get("authenticated"):
                self._invalidate_auth(row)
                row.update(login_state="logged_out", health="login_required", error="يلزم تسجيل الدخول.")
                self._put(row)
                return self._safe(row)
            # Accept only an explicit stable identity. Hash it again so the DB
            # cannot accidentally retain a subject/token supplied by a runtime.
            identity = str(result.get("identity", "") or "")
            if not identity or result.get("identity_verified") is False:
                self._invalidate_auth(row)
                row.update(login_state="needsverify", health="login_required", error="تعذر التحقق من هوية حساب مستقلة.")
                self._put(row)
                raise ValueError("لم يُرجع تسجيل الدخول هوية حساب قابلة للتحقق.")
            digest = hashlib.sha256(identity.encode()).hexdigest()
            if not reset_cooldown and row.get("identity") and row["identity"] != digest:
                self._invalidate_auth(row)
                row.update(login_state="needsverify", health="login_required", error="تغيّرت هوية الحساب؛ أعد تسجيل الدخول للتحقق.")
                self._put(row)
                raise ValueError(row["error"])
            if any(r["id"] != ident and r.get("identity") == digest for r in self._rows()):
                self._invalidate_auth(row)
                row.update(login_state="duplicate", health="login_required", error="هذا الحساب مسجل مسبقًا.")
                self._put(row)
                raise DuplicateAccountError("هذا الحساب مسجل مسبقًا؛ استخدم الحساب الموجود.")
            if row.get("identity") != digest or reset_cooldown:
                self._invalidate_auth(row)
            row.update(identity=digest, email=str(result.get("email", ""))[:160],
                       subscription=str(result.get("subscription", ""))[:80], login_state="authenticated")
            if reset_cooldown:
                row.update(health="healthy", error="", reset_at=0, throttle_until=0, probe_count=0,
                           models={}, generation=row["generation"] + 1)
            elif row["health"] in ("auth_error", "login_required"):
                row.update(health="cooldown" if row.get("reset_at", 0) else "healthy", error="")
            self._put(row)
            self._wake_waiters()
            return self._safe(row)

    def recheck(self, ident):
        """Explicit user recheck; auth verification is performed by the runtime."""
        with self.store.lock:
            self.assert_idle(ident)
            row = self._get(ident)
            row.update(reset_at=0, throttle_until=0, models={}, error="", probe_count=0,
                       generation=row["generation"] + 1)
            row["health"] = "healthy" if row["login_state"] == "authenticated" else "login_required"
            self._put(row)
            self._wake_waiters()
            return self._safe(row)

    def _wake_waiters(self):
        self.store.db.execute("UPDATE stages SET next_retry=0 WHERE name='opencode' AND status='pending' "
                              "AND error LIKE 'حسابات ChatGPT:%'")

    def _invalidate_auth(self, row):
        row["auth_epoch"] = row.get("auth_epoch", 0) + 1
        self.invalidate_usage(row["id"])

    def usage_binding(self, ident):
        """Credential-free identity, independent of job-routing generation."""
        with self.store.lock:
            row = self._get(ident)
            return {"account_id": ident, "auth_epoch": row.get("auth_epoch", 0),
                    "identity": row.get("identity", "")}

    def usage_eligible(self, ident):
        with self.store.lock:
            try:
                row = self._get(ident)
            except ValueError:
                return False
            return bool(row.get("identity") and row["login_state"] == "authenticated"
                        and row.get("health") != "execution_uncertain"
                        and not any(h.get("account_id") == ident for h in
                                    self.store.state_get("chatgpt_session_holds", {}).values()))

    def _matches_usage_binding(self, ident, binding):
        try:
            return self.usage_binding(ident) == {key: binding.get(key) for key in
                                               ("account_id", "auth_epoch", "identity")}
        except ValueError:
            return False

    def begin_usage_request(self, ident, binding, request_id, now=None):
        with self.store.lock:
            if not self._matches_usage_binding(ident, binding) or not self.usage_eligible(ident):
                return False
            self.store.db.execute(
                "INSERT INTO chatgpt_usage(account_id,auth_epoch,request_id,last_attempt_at) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET request_id=excluded.request_id,last_attempt_at=excluded.last_attempt_at",
                (ident, binding["auth_epoch"], str(request_id), self.clock() if now is None else now))
            return True

    def usage_record(self, ident):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM chatgpt_usage WHERE account_id=?", (ident,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["snapshot"] = json.loads(result["snapshot"])
            return result

    def record_usage(self, ident, binding, request_id, result, next_attempt_at=0):
        """Update only an existing, latest request with the same auth identity."""
        from .account_usage import safe_usage_result
        result = safe_usage_result(result)
        with self.store.lock:
            if not self._matches_usage_binding(ident, binding):
                return False
            row = self.usage_record(ident)
            if not row or row["auth_epoch"] != binding["auth_epoch"] or row["request_id"] != str(request_id):
                return False
            success = result["status"] in ("available", "partial") and any(
                result.get(key) for key in ("five_hour", "weekly", "other_windows", "buckets"))
            snapshot = result if success else row["snapshot"]
            self.store.db.execute(
                "UPDATE chatgpt_usage SET snapshot=?,last_success_at=?,next_attempt_at=?,status=?,reason_code=? "
                "WHERE account_id=? AND auth_epoch=? AND request_id=?",
                (json.dumps(snapshot, ensure_ascii=False), result["observed_at"] if success else row["last_success_at"],
                 _finite_number(next_attempt_at), result["status"], result["reason_code"], ident,
                 binding["auth_epoch"], str(request_id)))
            return True

    def invalidate_usage(self, ident):
        with self.store.lock:
            reservation = self.usage_maintenance.get(ident)
            if reservation:
                reservation["cancel"].set()
            self.store.db.execute("DELETE FROM chatgpt_usage WHERE account_id=?", (ident,))

    def reserve_usage_maintenance(self, ident, binding):
        """Caller holds scheduler lock first. Reservation excludes queued leases."""
        with self.store.lock:
            if (not self._matches_usage_binding(ident, binding) or not self.usage_eligible(ident)
                    or ident in self.usage_maintenance
                    or any(lease["account_id"] == ident for lease in self.leases.values())):
                return None
            reservation = dict(binding, token=uuid.uuid4().hex, cancel=threading.Event())
            self.usage_maintenance[ident] = reservation
            return reservation

    def release_usage_maintenance(self, reservation):
        with self.store.lock:
            reservation["cancel"].set()
            ident = reservation["account_id"]
            if self.usage_maintenance.get(ident) is reservation:
                self.usage_maintenance.pop(ident)
                self._wake_waiters()

    def usage_is_maintenance(self, ident):
        with self.store.lock:
            return ident in self.usage_maintenance

    def _candidate(self, row, model, now):
        if row["id"] in self.usage_maintenance:
            return False, now + 1
        if not row["enabled"] or row["login_state"] != "authenticated" or row["health"] in ("auth_error", "execution_uncertain"):
            return False, 0
        exclusion = row.get("models", {}).get(model, {})
        until = max(row.get("reset_at", 0), row.get("throttle_until", 0), exclusion.get("until", 0))
        if until > now:
            return False, until
        return True, 0

    def acquire(self, job_id, worker_id, model):
        """Return (lease, next_wakeup). Saturation never spills to a new account."""
        with self.store.lock:
            model = str(model).split("#", 1)[0].strip()
            now = self.clock()
            rows = sorted(self._rows(), key=lambda r: (r["priority"], r["id"]))
            preferred = self.store.state_get("chatgpt_preferred", "")
            current = self.store.state_get("chatgpt_current", "")
            rows.sort(key=lambda r: 0 if r["id"] == current else (1 if r["id"] == preferred else 2))
            wakeups = []
            for row in rows:
                eligible, wakeup = self._candidate(row, model, now)
                if not eligible:
                    if row["id"] in self.usage_maintenance and row["id"] in (current, preferred):
                        return None, now + 1
                    # A short transport throttle is not an exhausted account;
                    # keep its identity sticky and wait rather than rotating.
                    if (row["enabled"] and row["login_state"] == "authenticated"
                            and row["id"] in (current, preferred)
                            and row.get("throttle_until", 0) > now
                            and row.get("reset_at", 0) <= now
                            and row.get("models", {}).get(model, {}).get("until", 0) <= now):
                        return None, row["throttle_until"]
                    if wakeup:
                        wakeups.append(wakeup)
                    continue
                active = [v for v in self.leases.values() if v["account_id"] == row["id"]]
                probe = row["health"] == "cooldown" or bool(row.get("models", {}).get(model))
                limit = max(1, int(self.config.snapshot().get("chatgpt_account_concurrency", 5)))
                if len(active) >= limit or (probe and active):
                    return None, now + 1
                lease = {"id": uuid.uuid4().hex, "account_id": row["id"], "alias": row["alias"],
                         "generation": row["generation"], "model": model, "probe": probe}
                with self._transaction():
                    self.store.db.execute("INSERT INTO chatgpt_attempts(id,account_id,job_id,worker_id,model,generation,started) VALUES(?,?,?,?,?,?,?)",
                                          (lease["id"], row["id"], job_id, worker_id, model, row["generation"], now))
                    self.store.state_put("chatgpt_current", row["id"])
                # A durable audit row must exist before exposing a live lease.
                self.leases[lease["id"]] = lease
                return lease, 0
            return None, min(wakeups) if wakeups else now + 30

    def next_retry(self, model):
        with self.store.lock:
            model = str(model).split("#", 1)[0].strip()
            now = self.clock()
            waits = []
            for row in self._rows():
                eligible, until = self._candidate(row, model, now)
                if eligible:
                    return now
                if until:
                    waits.append(until)
            return min(waits) if waits else now + 30

    def success(self, lease):
        with self.store.lock:
            row = self._get(lease["account_id"])
            if row["generation"] == lease["generation"] and lease.get("probe"):
                row.update(health="healthy", reset_at=0, throttle_until=0, probe_count=0, error="")
                row["models"].pop(lease["model"], None)
                self._put(row)

    def failure(self, lease, error):
        """Apply a confirmed event only to the account generation that sent it."""
        with self.store.lock:
            row = self._get(lease["account_id"])
            supported = error.kind in ("account_auth", "model_unavailable", "throttled") or (
                error.kind == "rate_limited" and bool(getattr(error, "confirmed", False)))
            if not supported:
                return False
            if lease["id"] not in self.leases or row["generation"] != lease["generation"]:
                return True
            if getattr(error, "account_id", row["id"]) not in (None, "", row["id"]):
                return False
            kind, now = error.kind, self.clock()
            row["error"] = str(error)[:300]
            reset = getattr(error, "reset_at", None)
            reset = _finite_number(reset)
            reset = reset if now < reset < now + 366 * 86400 else 0
            confirmed = bool(getattr(error, "confirmed", False))
            scope = getattr(error, "scope", "unknown")
            if kind == "rate_limited" and confirmed:
                row["probe_count"] += 1
                until = reset or now + min(3600, 300 * 2 ** min(row["probe_count"] - 1, 4))
                if scope == "model":
                    row["models"][lease["model"]] = {"until": until}
                else:
                    row.update(health="cooldown", reset_at=until)
            elif kind == "account_auth":
                row.update(health="auth_error", login_state="expired")
            elif kind == "model_unavailable":
                row["models"][lease["model"]] = {"until": reset or now + 900}
            elif kind == "throttled":
                retry_after = getattr(error, "retry_after", 0)
                delay = _finite_number(retry_after, 30)
                row["throttle_until"] = reset or now + max(5, min(300, delay or 30))
            else:
                return False
            row["generation"] += 1
            self._put(row)
            return True

    def release(self, lease, outcome="finished", session_id=""):
        with self.store.lock:
            self.leases.pop(lease["id"], None)
            self.store.db.execute("UPDATE chatgpt_attempts SET finished=?,outcome=?,session_id=CASE WHEN ?='' THEN session_id ELSE ? END "
                                  "WHERE id=? AND outcome='running'",
                                  (self.clock(), str(outcome)[:40], str(session_id)[:160], str(session_id)[:160], lease["id"]))

    def session_started(self, lease, session_id, instance_id="", workpath=""):
        with self.store.lock:
            lease.update(session_id=str(session_id), instance_id=str(instance_id), workpath=str(workpath))
            self.store.db.execute("UPDATE chatgpt_attempts SET session_id=?,instance_id=?,workpath=? WHERE id=? AND outcome='running'",
                                  (str(session_id)[:160], str(instance_id)[:160], str(workpath)[:2000], lease["id"]))

    def hold(self, lease, job_id):
        with self._transaction():
            row = self._get(lease["account_id"])
            row.update(health="execution_uncertain", error="لم يُتحقق من توقف الجلسة السابقة.", generation=row["generation"] + 1)
            self._put(row)
            holds = self.store.state_get("chatgpt_session_holds", {})
            holds[job_id] = {key: lease.get(key, "") for key in ("account_id", "session_id", "instance_id", "workpath")}
            self.store.state_put("chatgpt_session_holds", holds)

    def clear_hold(self, job_id):
        self.clear_holds([job_id])

    def clear_holds(self, job_ids):
        with self._transaction():
            holds = self.store.state_get("chatgpt_session_holds", {})
            accounts = {held["account_id"] for job_id in job_ids if (held := holds.pop(job_id, None))}
            self.store.state_put("chatgpt_session_holds", holds)
            for ident in accounts:
                if not any(v["account_id"] == ident for v in holds.values()):
                    row = self._get(ident)
                    row.update(health="cooldown" if row.get("reset_at", 0) else "healthy", error="")
                    self._put(row)
            self._wake_waiters()
