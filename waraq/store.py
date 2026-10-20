from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

STAGES = ("download", "deepgram", "opencode")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, video_id TEXT NOT NULL, title TEXT NOT NULL,
              url TEXT NOT NULL, position INTEGER NOT NULL, batch TEXT NOT NULL,
              config TEXT NOT NULL, folder TEXT NOT NULL, created REAL NOT NULL,
              audio TEXT DEFAULT '', transcript TEXT DEFAULT '', document TEXT DEFAULT '',
              input_path TEXT DEFAULT '', duration REAL DEFAULT 0, queue_order INTEGER DEFAULT 0,
              seq INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS stages (
              job_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
              worker TEXT DEFAULT '', progress REAL DEFAULT -1, error TEXT DEFAULT '',
              attempts INTEGER DEFAULT 0, updated REAL NOT NULL, next_retry REAL DEFAULT 0,
              retries INTEGER DEFAULT 0,
              PRIMARY KEY(job_id,name)
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, ts REAL NOT NULL, job_id TEXT, level TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
              created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_attempts (
              id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL,
              worker_id TEXT NOT NULL, model TEXT NOT NULL, session_id TEXT DEFAULT '',
              instance_id TEXT DEFAULT '', workpath TEXT DEFAULT '',
              started REAL NOT NULL, finished REAL DEFAULT 0, outcome TEXT DEFAULT 'running');
        ''')
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "queue_order" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN queue_order INTEGER DEFAULT 0")
            self.db.execute("UPDATE jobs SET queue_order=position")
        if "seq" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN seq INTEGER DEFAULT 0")
            # rowid equals insertion order; timestamps can tie on coarse Windows timers.
            self.db.execute("UPDATE jobs SET seq=rowid")
        if "folder_id" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN folder_id TEXT DEFAULT ''")
        stage_columns = {r[1] for r in self.db.execute("PRAGMA table_info(stages)")}
        if "retries" not in stage_columns:
            self.db.execute("ALTER TABLE stages ADD COLUMN retries INTEGER DEFAULT 0")
        self._migrate_accounts()

    def _migrate_accounts(self):
        """Archive the retired registry and preserve interrupted execution ownership.

        Authentication is deliberately not imported: the new engine owns refresh
        tokens and requires one initial login. SQLite's backup API includes WAL
        contents, unlike copying the database file while it is open.
        """
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        legacy = tables.intersection({"chatgpt_accounts", "chatgpt_usage", "chatgpt_attempts"})
        if not legacy:
            return
        backup = self.path.with_name(self.path.name + ".before-embedded-accounts.sqlite3")
        if not backup.exists():
            with sqlite3.connect(backup) as archive:
                self.db.backup(archive)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            holds = self.state_get("execution_holds", {})
            for job_id, held in self.state_get("chatgpt_session_holds", {}).items():
                holds.setdefault(job_id, {**held, "legacy": True})
            if "chatgpt_attempts" in legacy:
                for row in self.db.execute("SELECT * FROM chatgpt_attempts").fetchall():
                    attempt = dict(row)
                    self.db.execute("INSERT OR IGNORE INTO execution_attempts "
                                    "(id,account_id,job_id,worker_id,model,session_id,instance_id,workpath,started,finished,outcome) "
                                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(attempt.get(key, "") for key in
                                    ("id", "account_id", "job_id", "worker_id", "model", "session_id", "instance_id", "workpath", "started", "finished", "outcome")))
                    if attempt["outcome"] == "running":
                        holds.setdefault(attempt["job_id"], {**{key: attempt.get(key, "") for key in
                                         ("account_id", "session_id", "instance_id", "workpath")},
                                         "attempt_id": attempt["id"], "legacy": True})
            aliases = []
            if "chatgpt_accounts" in legacy:
                for row in self.db.execute("SELECT metadata FROM chatgpt_accounts"):
                    metadata = json.loads(row[0])
                    aliases.append(str(metadata.get("alias", ""))[:80])
            if holds:
                self.state_put("execution_holds", holds)
            self.state_put("embedded_accounts_migration", {"version": 1, "aliases": aliases,
                           "requires_login": bool(aliases), "archive": str(backup), "migrated_at": time.time()})
            self.db.execute("DELETE FROM runtime_state WHERE key GLOB 'chatgpt_*'")
            for table in sorted(legacy):
                self.db.execute(f"DROP TABLE {table}")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def begin_execution(self, attempt, job_id, worker_id, model, workpath):
        """Persist ownership before allowing a model request to execute."""
        with self.lock:
            self.db.execute("INSERT INTO execution_attempts "
                            "(id,account_id,job_id,worker_id,model,session_id,instance_id,workpath,started) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (attempt["attempt_id"], attempt.get("account_id", ""), job_id, worker_id, model,
                             attempt.get("session_id", ""), attempt.get("runtime_instance_id", ""), str(workpath), time.time()))

    def execution_session(self, attempt_id, session_id, instance_id):
        with self.lock:
            self.db.execute("UPDATE execution_attempts SET session_id=?,instance_id=? WHERE id=?",
                            (session_id, instance_id, attempt_id))

    def finish_execution(self, attempt_id, outcome):
        with self.lock:
            self.db.execute("UPDATE execution_attempts SET outcome=?,finished=? WHERE id=?",
                            (outcome, time.time(), attempt_id))

    def hold_execution(self, attempt_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM execution_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise ValueError("سجل المحاولة غير موجود؛ لا يمكن إغفال حالة التنفيذ.")
            holds = self.state_get("execution_holds", {})
            holds[row["job_id"]] = {**{key: row[key] for key in ("account_id", "session_id", "instance_id", "workpath")},
                                     "attempt_id": attempt_id}
            self.state_put("execution_holds", holds)
            self.finish_execution(attempt_id, "uncertain")

    def clear_execution_hold(self, job_id, expected):
        with self.lock:
            holds = self.state_get("execution_holds", {})
            if holds.get(job_id) != expected:
                raise ValueError("تغيّرت الجلسة أثناء الفحص؛ أعد الفحص.")
            holds.pop(job_id)
            self.state_put("execution_holds", holds)

    def event(self, job_id, message, level="info"):
        with self.lock:
            self.db.execute("INSERT INTO events(ts,job_id,level,message) VALUES(?,?,?,?)",
                            (time.time(), job_id, level, str(message)[:4000]))

    def recover(self):
        with self.lock:
            # Native account sessions may outlive Waraq. Preserve a replay hold
            # before changing their interrupted attempts or stage statuses.
            holds = self.state_get("execution_holds", {})
            for attempt in self.db.execute("SELECT * FROM execution_attempts WHERE outcome='running'").fetchall():
                holds.setdefault(attempt["job_id"], {**{key: attempt[key] for key in
                                            ("account_id", "session_id", "instance_id", "workpath")},
                                            "attempt_id": attempt["id"]})
            if holds:
                self.state_put("execution_holds", holds)
            self.db.execute("UPDATE execution_attempts SET outcome='interrupted',finished=? WHERE outcome='running'", (time.time(),))
            # An interrupted paid request is not automatically submitted again.
            self.db.execute("UPDATE stages SET status=CASE WHEN name='deepgram' THEN 'uncertain' ELSE 'paused' END, "
                            "worker='', error='توقف التشغيل السابق؛ راجع النتيجة قبل الاستئناف' WHERE status='running'")
            self.prune()

    def prune(self, keep_events=5000):
        """Bound table growth: keep the most recent events, drop day-old cache rows."""
        with self.lock:
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                            (keep_events,))
            self.db.execute("DELETE FROM cache WHERE created < ?", (time.time() - 86400,))
            self.db.execute("DELETE FROM execution_attempts WHERE outcome NOT IN ('running','uncertain','interrupted') AND id NOT IN "
                            "(SELECT id FROM execution_attempts ORDER BY started DESC LIMIT ?)", (keep_events,))

    def folders(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,name FROM folders ORDER BY name COLLATE NOCASE")]

    def create_folder(self, name):
        name = str(name).strip()
        if not 1 <= len(name) <= 80 or name in (".", "..") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
            raise ValueError("اسم المجلد يجب أن يكون بين 1 و80 محرفًا دون رموز المسارات.")
        if name.casefold() in ("word", "التفريغات", "الصوت") or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("هذا الاسم محجوز في Windows.")
        ident = uuid.uuid4().hex[:12]
        with self.lock:
            try:
                self.db.execute("INSERT INTO folders VALUES(?,?,?)", (ident, name, time.time()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("يوجد مجلد بهذا الاسم.") from exc
        return ident

    def folder_name(self, ident):
        if not ident:
            return ""
        with self.lock:
            row = self.db.execute("SELECT name FROM folders WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("المجلد المحدد غير موجود.")
            return row["name"]

    def assign_folder(self, job_id, folder_id):
        with self.lock:
            self.folder_name(folder_id)
            if self.db.execute("UPDATE jobs SET folder_id=? WHERE id=?", (folder_id, job_id)).rowcount != 1:
                raise ValueError("المهمة غير موجودة.")

    def add(self, items, config, batch="", input_path="", folder_id=""):
        items = [item for item in items if item.get("selected", True) and item.get("available", True)]
        if not any(config[s + "_enabled"] for s in STAGES):
            raise ValueError("فعّل مرحلة واحدة على الأقل.")
        if any(not item.get("input_path", input_path) for item in items):
            if not config["download_enabled"]:
                raise ValueError("مصدر YouTube يحتاج التنزيل؛ استورد الملفات المحلية لتجاوزه.")
            if config["opencode_enabled"] and not config["deepgram_enabled"]:
                raise ValueError("إنشاء Word يحتاج تفريغًا. فعّل Deepgram أو استورد ملف TXT.")
        added = []
        copied_folders = []
        batch = batch or uuid.uuid4().hex[:8]
        with self.lock:
            self.folder_name(folder_id)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                base = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM jobs").fetchone()[0]
                for queue_order, item in enumerate(items, 1):
                    ident = uuid.uuid4().hex[:12]
                    folder = str(Path(config["output_root"]) / ident)
                    source_path = str(item.get("input_path", input_path))
                    local_txt = source_path.lower().endswith(".txt")
                    transcript = ""
                    if local_txt:
                        # Own the bytes before publishing a runnable job. The source
                        # path is kept only for provenance, never for Word input.
                        content = Path(source_path).read_bytes()
                        if not content.decode("utf-8-sig").strip():
                            raise ValueError("ملف التفريغ فارغ.")
                        managed_folder = Path(folder)
                        managed_folder.mkdir(parents=True, exist_ok=False)
                        copied_folders.append(managed_folder)
                        transcript = str(managed_folder / Path(source_path).name)
                        Path(transcript).write_bytes(content)
                    self.db.execute("INSERT INTO jobs(id,video_id,title,url,position,batch,config,folder,created,input_path,duration,queue_order,seq,folder_id) "
                                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (ident, item["video_id"], item["title"], item.get("url", ""), item["position"],
                                     batch, json.dumps(config, ensure_ascii=False), folder, time.time(), source_path,
                                     item.get("duration") or 0, queue_order, base + queue_order, folder_id))
                    for stage in STAGES:
                        skip = not config[stage + "_enabled"] or (source_path and stage == "download") or (local_txt and stage == "deepgram")
                        self.db.execute("INSERT INTO stages(job_id,name,status,updated) VALUES(?,?,?,?)",
                                        (ident, stage, "skipped" if skip else "pending", time.time()))
                    if source_path:
                        field = "transcript" if local_txt else "audio"
                        self.db.execute(f"UPDATE jobs SET {field}=? WHERE id=?", (transcript if local_txt else source_path, ident))
                    added.append(ident)
                self.db.execute("COMMIT")
            except BaseException as exc:
                self.db.execute("ROLLBACK")
                output_root = Path(config["output_root"]).resolve()
                for copied_folder in copied_folders:
                    try:
                        # Remove only new job directories directly under this
                        # output root; never follow a replaced directory outside it.
                        if copied_folder.resolve().parent != output_root:
                            raise OSError("مسار تنظيف نسخة التفريغ خارج مجلد العمل.")
                        shutil.rmtree(copied_folder)
                    except OSError as cleanup_error:
                        if hasattr(exc, "add_note"):
                            exc.add_note("تعذر تنظيف نسخة التفريغ: " + str(cleanup_error))
                raise
        return added

    def revision(self):
        """Cheap invalidation token for writes on this or another connection.

        data_version alone excludes this connection's writes; total_changes
        alone excludes external commits. Read both under the Store lock.
        """
        with self.lock:
            return self.db.total_changes, self.db.execute("PRAGMA data_version").fetchone()[0]

    def jobs(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT jobs.*,COALESCE(folders.name,'') AS folder_name FROM jobs LEFT JOIN folders ON folders.id=jobs.folder_id ORDER BY jobs.seq")]
            # One pass over all stages instead of a query per job; the queue is
            # rescanned several times per second by the UI timer and scheduler.
            stages = {}
            for s in self.db.execute("SELECT * FROM stages"):
                stages.setdefault(s["job_id"], {})[s["name"]] = dict(s)
            for row in rows:
                row["config"] = json.loads(row["config"])
                row["stages"] = stages.get(row["id"], {})
            return rows

    def get(self, ident):
        with self.lock:
            row = self.db.execute("SELECT jobs.*,COALESCE(folders.name,'') AS folder_name FROM jobs LEFT JOIN folders ON folders.id=jobs.folder_id WHERE jobs.id=?", (ident,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            job["config"] = json.loads(job["config"])
            job["stages"] = {s["name"]: dict(s) for s in self.db.execute("SELECT * FROM stages WHERE job_id=?", (ident,))}
            return job

    def claim(self, ident, stage, worker):
        with self.lock:
            if stage == "opencode" and ident in self.state_get("execution_holds", {}):
                # The durable replay guard also applies after changing provider
                # or disabling managed mode, independently of account routing.
                return False
            cur = self.db.execute("UPDATE stages SET status='running',worker=?,progress=-1,error='',attempts=attempts+1,updated=? "
                                  "WHERE job_id=? AND name=? AND status='pending' AND next_retry<=?",
                                  (worker, time.time(), ident, stage, time.time()))
            return cur.rowcount == 1

    def delete_jobs(self, idents, busy_ids=()):
        """Remove queue records only, atomically with claims.

        Scheduler.delete_jobs is the public entry point for a live queue: it
        supplies active jobs, including completed stages still publishing.
        The running-stage check also protects direct Store callers.
        """
        idents = list(dict.fromkeys(str(ident) for ident in idents))
        busy_ids = set(busy_ids)
        result = {"deleted": [], "busy": [], "missing": []}
        if not idents:
            return result
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                busy_ids.update(self.state_get("execution_holds", {}))
                for ident in idents:
                    if not self.db.execute("SELECT 1 FROM jobs WHERE id=?", (ident,)).fetchone():
                        result["missing"].append(ident)
                    elif ident in busy_ids or self.db.execute(
                            "SELECT 1 FROM stages WHERE job_id=? AND status='running' LIMIT 1", (ident,)).fetchone():
                        result["busy"].append(ident)
                    else:
                        self.db.execute("DELETE FROM stages WHERE job_id=?", (ident,))
                        self.db.execute("DELETE FROM jobs WHERE id=?", (ident,))
                        result["deleted"].append(ident)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return result

    def set_stage(self, ident, stage, status, error="", progress=-1, next_retry=0):
        with self.lock:
            # Any explicit transition ends the consecutive-retry episode.
            self.db.execute("UPDATE stages SET status=?,error=?,progress=?,updated=?,next_retry=?,retries=0,worker='' WHERE job_id=? AND name=?",
                            (status, str(error)[:2000], progress, time.time(), next_retry, ident, stage))

    def defer_retry(self, ident, stage, error, delay):
        """Reschedule a transient failure; returns the consecutive retry count used."""
        with self.lock:
            row = self.db.execute("SELECT retries FROM stages WHERE job_id=? AND name=?", (ident, stage)).fetchone()
            used = (row["retries"] if row else 0) + 1
            self.db.execute("UPDATE stages SET status='pending',error=?,progress=-1,updated=?,next_retry=?,retries=?,worker='' "
                            "WHERE job_id=? AND name=?",
                            (str(error)[:2000], time.time(), time.time() + delay, used, ident, stage))
            return used

    def queue_drained(self):
        """True when nothing is runnable: no stage pending (even deferred) or running."""
        with self.lock:
            row = self.db.execute("SELECT COUNT(*) FROM stages WHERE status IN ('pending','running')").fetchone()
            return row[0] == 0

    def progress(self, ident, stage, value):
        with self.lock:
            self.db.execute("UPDATE stages SET progress=?,updated=? WHERE job_id=? AND name=?",
                            (value, time.time(), ident, stage))

    def artifact(self, ident, kind, path):
        if kind not in ("audio", "transcript", "document"):
            raise ValueError(kind)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {kind}=? WHERE id=?", (str(path), ident))

    def retry(self, ident, stage, rerun=False):
        with self.lock:
            if (stage == "opencode" or rerun) and ident in self.state_get("execution_holds", {}):
                raise ValueError("تحقق من توقف جلسة ChatGPT السابقة من إدارة الحسابات قبل إعادة التنفيذ.")
            job = self.get(ident)
            if not job or any(s["status"] == "running" for s in job["stages"].values()):
                raise ValueError("انتظر انتهاء المرحلة النشطة أو ألغِها أولًا.")
            if rerun:
                for name in STAGES[STAGES.index(stage):]:
                    if job["stages"][name]["status"] != "skipped":
                        self.set_stage(ident, name, "pending")
                # Explicit new revision preserves old outputs.
                cfg = job["config"]
                cfg["revision_" + stage] = uuid.uuid4().hex[:8]
                self.db.execute("UPDATE jobs SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), ident))
            else:
                self.set_stage(ident, stage, "pending")

    def events(self, limit=200):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def cache_get(self, key, ttl=21600):
        with self.lock:
            row = self.db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row and time.time() - row["created"] < ttl else None

    def cache_put(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, json.dumps(value, ensure_ascii=False), time.time()))

    def state_get(self, key, default=None):
        """Operational state that must survive a restart, e.g. the YouTube cooldown."""
        with self.lock:
            row = self.db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def state_put(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                            (key, json.dumps(value, ensure_ascii=False), time.time()))

    def state_clear(self, key):
        with self.lock:
            self.db.execute("DELETE FROM runtime_state WHERE key=?", (key,))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
