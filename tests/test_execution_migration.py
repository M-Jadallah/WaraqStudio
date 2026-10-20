"""Migration preserves WAL audit and replay holds without importing credentials."""
import json
import sqlite3

import pytest

from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


def test_legacy_registry_is_archived_once_and_removed(tmp_path):
    path = tmp_path / "queue.sqlite3"
    old = sqlite3.connect(path, isolation_level=None)
    old.execute("PRAGMA journal_mode=WAL")
    old.executescript("""
        CREATE TABLE chatgpt_accounts(id TEXT PRIMARY KEY,metadata TEXT,updated REAL);
        CREATE TABLE chatgpt_usage(account_id TEXT PRIMARY KEY,snapshot TEXT);
        CREATE TABLE chatgpt_attempts(id TEXT PRIMARY KEY,account_id TEXT,job_id TEXT,worker_id TEXT,
          model TEXT,session_id TEXT,generation INTEGER,started REAL,finished REAL,outcome TEXT,
          instance_id TEXT,workpath TEXT);
    """)
    old.execute("INSERT INTO chatgpt_accounts VALUES(?,?,?)", ("old-account", json.dumps({"alias": "الحساب الأول"}), 10))
    old.execute("INSERT INTO chatgpt_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("attempt", "old-account", "job", "oc1", "openai/mock", "ses_old", 1, 10, 0, "running", "old-instance", "C:/work"))
    store = Store(path)
    archive = path.with_name(path.name + ".before-embedded-accounts.sqlite3")
    assert archive.is_file()
    with sqlite3.connect(archive) as backup:
        assert backup.execute("SELECT session_id FROM chatgpt_attempts").fetchone()[0] == "ses_old"
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"chatgpt_accounts", "chatgpt_usage", "chatgpt_attempts"}.intersection(tables)
    migrated = store.state_get("embedded_accounts_migration")
    assert migrated["aliases"] == ["الحساب الأول"] and migrated["requires_login"]
    store.recover()
    assert store.state_get("execution_holds")["job"] == {
        "account_id": "old-account", "session_id": "ses_old", "instance_id": "old-instance", "workpath": "C:/work",
        "attempt_id": "attempt", "legacy": True}
    assert store.db.execute("SELECT outcome FROM execution_attempts").fetchone()[0] == "interrupted"
    store.db.close()
    old.close()
    stamp = archive.stat().st_mtime_ns
    restored = Store(path)
    assert archive.stat().st_mtime_ns == stamp
    assert restored.state_get("execution_holds")["job"]["legacy"]
    restored.db.close()


def test_fresh_database_has_no_retired_registry(tmp_path):
    store = Store(tmp_path / "queue.sqlite3")
    assert not any(row[0].startswith("chatgpt_") for row in store.db.execute("SELECT name FROM sqlite_master"))
    store.db.close()


def test_recovered_execution_cannot_be_retried_or_claimed(tmp_path):
    cfg = Config(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("source", encoding="utf-8")
    store = Store(tmp_path / "queue.sqlite3")
    ident = store.add([{"video_id": "local", "position": 1, "title": "test", "input_path": str(source)}], cfg.snapshot())[0]
    store.begin_execution({"attempt_id": "attempt", "account_id": "opaque", "runtime_instance_id": "instance"},
                          ident, "oc1", "openai/mock", "C:/work")
    store.execution_session("attempt", "ses_running", "instance")
    store.recover()
    with pytest.raises(ValueError):
        store.retry(ident, "opencode")
    assert not store.claim(ident, "opencode", "oc1")
    assert store.delete_jobs([ident])["busy"] == [ident]
    held = store.state_get("execution_holds")[ident]
    with pytest.raises(ValueError):
        store.clear_execution_hold(ident, {**held, "session_id": "wrong"})
    store.clear_execution_hold(ident, held)
    store.retry(ident, "opencode")
    assert store.claim(ident, "opencode", "oc1")
    store.db.close()
