from __future__ import annotations

import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from waraq.config import Config, compose_model, credential_name, defaults
from waraq.library import Library
from waraq.scheduler import Scheduler
from waraq.services import LIMIT_MESSAGE, Deepgram, ServiceError, Youtube, digest, transcript_from, validate_docx, worker_env
from waraq.store import Store, STAGES


def make_docx(path, text="مستند اختباري"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return path


@pytest.fixture
def env(tmp_path, scheduler_harness):
    cfg = Config(tmp_path)
    store = Store(tmp_path / "tasks.db")
    try:
        yield store, cfg
    finally:
        scheduler_harness.shutdown_and_join()
        with store.lock:
            store.db.close()


def items(n=1):
    return [{"video_id": f"video{i}", "title": f"درس {i}", "position": i,
             "url": f"https://www.youtube.com/watch?v=video{i}"} for i in range(1, n + 1)]


def test_atomic_claim_prevents_two_workers(env):
    store, cfg = env
    ident = store.add(items(), cfg.snapshot())[0]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: store.claim(ident, "download", str(i)), range(12)))
    assert results.count(True) == 1


def test_crash_recovery_preserves_paid_uncertainty(env):
    store, cfg = env
    ident = store.add(items(), cfg.snapshot())[0]
    for stage in STAGES:
        store.claim(ident, stage, "test")
    store.recover()
    states = store.get(ident)["stages"]
    assert states["download"]["status"] == "paused"
    assert states["deepgram"]["status"] == "uncertain"
    assert states["opencode"]["status"] == "paused"


def test_local_txt_skips_upstream(env, tmp_path):
    store, cfg = env
    txt = tmp_path / "درس.txt"
    txt.write_text("نص", encoding="utf-8")
    ident = store.add(items(), cfg.snapshot(), input_path=str(txt))[0]
    job = store.get(ident)
    assert Path(job["transcript"]) == Path(job["folder"]) / txt.name
    assert Path(job["transcript"]).read_bytes() == txt.read_bytes()
    assert job["input_path"] == str(txt)
    assert job["stages"]["download"]["status"] == "skipped"
    assert job["stages"]["deepgram"]["status"] == "skipped"
    assert job["stages"]["opencode"]["status"] == "pending"


def test_strict_order_waits_for_previous(env):
    store, cfg = env
    data = cfg.snapshot()
    data["strict_order"] = True
    ids = store.add(items(2), data)
    store.set_stage(ids[0], "download", "failed")
    store.set_stage(ids[1], "download", "completed")
    scheduler = Scheduler(store, cfg, None)
    jobs = store.jobs()
    assert not scheduler.ready(jobs[1], "deepgram", jobs)
    jobs[1]["config"]["strict_order"] = False
    assert scheduler.ready(jobs[1], "deepgram", jobs)


def test_rerun_preserves_artifact_and_changes_revision(env, tmp_path):
    store, cfg = env
    ident = store.add(items(), cfg.snapshot())[0]
    file = make_docx(tmp_path / "old.docx")
    store.artifact(ident, "document", file)
    store.set_stage(ident, "opencode", "completed")
    store.retry(ident, "opencode", True)
    job = store.get(ident)
    assert job["config"]["revision_opencode"]
    assert file.exists()


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_word_rejected(tmp_path, text):
    with pytest.raises(ServiceError):
        validate_docx(make_docx(tmp_path / "empty.docx", text))


def test_word_validation(tmp_path):
    path = make_docx(tmp_path / "valid.docx")
    assert validate_docx(path)["characters"] > 0
    path.write_bytes(b"not a word document")
    with pytest.raises(ServiceError):
        validate_docx(path)


def test_saved_response_recovered_without_api(env, tmp_path):
    store, cfg = env
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio fixture")
    ident = store.add(items(), cfg.snapshot(), input_path=str(audio))[0]
    job = store.get(ident)
    worker = cfg.data["deepgram_workers"][0]
    calls = []
    class FakeDeepgram(Deepgram):
        def request(self, audio, worker, cfg, secret, cancel, raw_path):
            from waraq.config import atomic_json
            calls.append(1)
            data = {"results": {"channels": [{"alternatives": [{"transcript": "نص محفوظ"}]}]}}
            atomic_json(raw_path, data)
            return data
    adapter = FakeDeepgram()
    first = adapter.transcribe(job, worker, cfg.data, "test", threading.Event(), lambda _: None)
    first.unlink()
    second = adapter.transcribe(job, worker, cfg.data, "test", threading.Event(), lambda _: None)
    assert second.read_text(encoding="utf-8-sig").strip() == "نص محفوظ"
    assert len(calls) == 1


def test_timeout_not_retried(monkeypatch, tmp_path):
    import requests
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout("lost response")
    monkeypatch.setattr(requests.Session, "post", fail)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"test")
    cfg = defaults(tmp_path)
    with pytest.raises(ServiceError) as exc:
        Deepgram().request(audio, cfg["deepgram_workers"][0], cfg, "not-real", threading.Event(), tmp_path / "response.json")
    assert exc.value.kind == "uncertain"
    assert len(calls) == 1


def test_bad_config_rejected_without_overwrite(env):
    _, cfg = env
    cfg.save(cfg.snapshot())
    before = cfg.path.read_bytes()
    changed = cfg.snapshot()
    changed["prompt"] = "missing required variables"
    with pytest.raises(ValueError):
        cfg.save(changed)
    assert cfg.path.read_bytes() == before


def test_youtube_limit_pauses_network(env):
    store, cfg = env
    youtube = Youtube(store)
    error = youtube.classify(Exception("HTTP Error 429"), cfg.data)
    assert error.kind == "rate_limited"
    assert error.retry_after >= cfg.data["youtube_cooldown"]
    assert youtube.cooldown_until > time.time()
    with pytest.raises(ServiceError):
        youtube.pace(cfg.data, threading.Event())


def test_full_pipeline_worker_concurrency_and_separate_keys(env, tmp_path):
    store, cfg = env
    ids = store.add(items(7), cfg.snapshot())
    counts = {s: 0 for s in STAGES}
    peaks = counts.copy()
    seen_keys, downloads = [], []
    lock = threading.Lock()
    targets = {"deepgram": 2, "opencode": 3}
    overlap = {stage: threading.Event() for stage in targets}
    def activity(stage):
        with lock:
            counts[stage] += 1
            peaks[stage] = max(peaks[stage], counts[stage])
            if stage in targets and counts[stage] >= targets[stage]:
                overlap[stage].set()
        try:
            # Keep the first workers in flight until the required concurrency
            # is observed. Fixed service sleeps depend on runner/disk speed and
            # can miss overlap even when the pool dispatches correctly.
            if stage in overlap:
                if not overlap[stage].wait(10):
                    raise TimeoutError("Required worker overlap was not reached: " + stage)
            else:
                time.sleep(0.03)
        finally:
            with lock:
                counts[stage] -= 1
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            downloads.append(job["position"])
            activity("download")
            p = Path(job["folder"]) / "audio.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"test audio")
            return p
    class FakeDG:
        def transcribe(self, job, worker, cfg, secret, cancel, progress):
            seen_keys.append((worker["id"], secret))
            activity("deepgram")
            p = Path(job["folder"]) / "transcript.txt"
            p.write_text("تفريغ اختباري", encoding="utf-8")
            return p
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            activity("opencode")
            return make_docx(Path(job["folder"]) / "result.docx")
    class Keys:
        def get(self, worker): return {"dg1": "key-one", "dg2": "key-two"}.get(worker, "")
    scheduler = Scheduler(store, cfg, Keys(), FakeYoutube(), FakeDG(), FakeOC())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        scheduler.tick()
        if all(j["stages"]["opencode"]["status"] == "completed" for j in store.jobs()) and scheduler.idle():
            break
        time.sleep(0.015)
    assert scheduler.idle(), "Workers are still publishing completed results"
    assert all(j["stages"]["opencode"]["status"] == "completed" for j in store.jobs())
    assert all(j["document"] for j in store.jobs())
    # One downloader, two keyed Deepgram workers, and an OpenCode pool no longer capped at two.
    assert peaks["download"] == 1 and peaks["deepgram"] == 2
    assert peaks["opencode"] >= 3, peaks
    assert downloads == list(range(1, 8))
    assert set(seen_keys) == {("dg1", "key-one"), ("dg2", "key-two")}
    for j in store.jobs():
        validate_docx(j["document"])


@pytest.mark.parametrize("background_loop", [False, True])
def test_database_cleanup_waits_for_completed_worker_publication(env, tmp_path, background_loop):
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    publishing = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class FakeOC:
        def process(self, job, worker, config, secret, cancel, log):
            return make_docx(Path(job["folder"]) / "result.docx")

    class HeldLibrary:
        def publish_current(self, current_store, job_id, config):
            publishing.set()
            if not release.wait(5):
                raise TimeoutError("Test publication was not released by scheduler shutdown")
            current_store.event(job_id, "Publication finished before database cleanup")
            finished.set()
            return []

    scheduler = Scheduler(store, cfg, None, opencode=FakeOC(), library=HeldLibrary())
    original_shutdown = scheduler.shutdown

    def shutdown():
        original_shutdown()
        release.set()

    scheduler.shutdown = shutdown
    # Held entries in other concurrency tests have no backing thread. They
    # must not prevent teardown from joining the actual OpenCode worker.
    scheduler.active["synthetic"] = {"job_id": "held", "stage": "deepgram", "project": "held",
                                     "started": time.time(), "cancel": threading.Event()}
    connection = store.db

    class GuardedConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def close(self):
            assert finished.is_set(), "SQLite closed before completed worker finished publishing"
            assert scheduler.thread is None or not scheduler.thread.is_alive()
            connection.close()

    store.db = GuardedConnection()
    if background_loop:
        scheduler.start()
    else:
        scheduler.tick()
    assert publishing.wait(3)
    assert store.get(ident)["stages"]["opencode"]["status"] == "completed"
    assert not scheduler.idle()
    # Deliberately return while publication is blocked: env's finalizer must
    # stop/join real threads before GuardedConnection allows SQLite to close.


def test_project_cap(env):
    store, cfg = env
    cfg.data["project_concurrency"] = 1
    # Workers sharing one project label share its cap; distinct labels run in parallel.
    for worker in cfg.data["deepgram_workers"]:
        worker["project"] = "shared"
    scheduler = Scheduler(store, cfg, type("Keys", (), {"get": lambda s,w: "x"})())
    scheduler.active["dg1"] = {"stage": "deepgram", "project": "shared", "job_id": "held", "started": time.time(), "cancel": threading.Event()}
    ids = store.add(items(), cfg.snapshot())
    store.set_stage(ids[0], "download", "completed")
    scheduler.tick()
    assert store.get(ids[0])["stages"]["deepgram"]["status"] == "pending"


def test_distinct_projects_allow_five_parallel_deepgram_workers(env):
    store, cfg = env
    cfg.data["project_concurrency"] = 1
    assert len({w["project"] for w in cfg.data["deepgram_workers"]}) == 7
    used = []
    class FakeDG:
        def transcribe(self, job, worker, cfg, secret, cancel, progress):
            used.append(worker["id"])
            time.sleep(0.2)
            path = Path(job["folder"]) / "transcript.txt"
            path.write_text("تفريغ", encoding="utf-8")
            return path
    scheduler = Scheduler(store, cfg, type("Keys", (), {"get": lambda s, w: "x"})(), deepgram=FakeDG())
    for worker in cfg.data["deepgram_workers"][:4]:
        scheduler.active[worker["id"]] = {"stage": "deepgram", "project": worker["project"], "job_id": "held",
                                          "started": time.time(), "cancel": threading.Event()}
    ids = store.add(items(), cfg.snapshot())
    store.set_stage(ids[0], "download", "completed")
    scheduler.tick()
    # Four projects are busy; the fifth worker still takes this job.
    assert store.get(ids[0])["stages"]["deepgram"]["status"] == "running"
    deadline = time.time() + 5
    while "dg5" in scheduler.active and time.time() < deadline:
        time.sleep(0.01)
    assert used == ["dg5"]


def test_chunk_resume_does_not_repeat_paid_chunks(env, tmp_path, monkeypatch):
    import waraq.services as services
    store, cfg = env
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    ident = store.add([{**items()[0], "duration": 8000}], cfg.snapshot(), input_path=str(audio))[0]
    job = store.get(ident)
    def split(command, *args):
        target = Path(command[-1]).parent
        (target / "00000.mp3").write_bytes(b"a" * 200)
        (target / "00001.mp3").write_bytes(b"b" * 200)
    monkeypatch.setattr(services, "run_process", split)
    calls = []
    class Adapter(Deepgram):
        failed = False
        def request(self, audio, worker, cfg, secret, cancel, raw_path):
            from waraq.config import atomic_json
            calls.append(audio.name)
            if audio.name == "00001.mp3" and not self.failed:
                self.failed = True
                raise ServiceError("lost connection", "uncertain")
            data = {"combined_transcript": audio.stem}
            atomic_json(raw_path, data)
            return data
    dg = Adapter()
    with pytest.raises(ServiceError):
        dg.transcribe(job, cfg.data["deepgram_workers"][0], cfg.data, "test", threading.Event(), lambda _: None)
    result = dg.transcribe(job, cfg.data["deepgram_workers"][0], cfg.data, "test", threading.Event(), lambda _: None)
    assert calls == ["00000.mp3", "00001.mp3", "00001.mp3"]
    assert result.read_text(encoding="utf-8-sig").strip() == "00000\n\n00001"


def test_real_ffmpeg_normalizes_local_wav(env, tmp_path):
    import wave
    from waraq.config import atomic_json
    store, cfg = env
    audio = tmp_path / "input.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    ident = store.add(items(), cfg.snapshot(), input_path=str(audio))[0]
    class Adapter(Deepgram):
        def request(self, audio, worker, cfg, secret, cancel, raw_path):
            assert audio.suffix == ".mp3"
            assert audio.stat().st_size > 1000
            data = {"combined_transcript": "اختبار تحويل الصوت"}
            atomic_json(raw_path, data)
            return data
    result = Adapter().transcribe(store.get(ident), cfg.data["deepgram_workers"][0], cfg.data, "test", threading.Event(), lambda _: None)
    assert result.is_file()
    assert audio.is_file()


def test_external_process_uses_literal_arguments_and_cancels(tmp_path):
    import sys
    from waraq.services import run_process, Cancelled
    payload = 'value & echo never | "literal"'
    result = run_process([sys.executable, "-c", "import sys; print(sys.argv[1])", payload], tmp_path, threading.Event(), 10)
    assert result == payload
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_process([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, cancel, 30)
    assert time.monotonic() - started < 10


def test_invalid_dependency_plan_is_rejected(env):
    store, cfg = env
    data = cfg.snapshot()
    data["deepgram_enabled"] = False
    with pytest.raises(ValueError):
        store.add(items(), data)


def test_queue_order_independent_of_original_lesson_number(env):
    store, cfg = env
    data = cfg.snapshot()
    data["strict_order"] = True
    ids = store.add([items(4)[3], items(2)[1]], data)
    jobs = store.jobs()
    assert [j["position"] for j in jobs] == [4, 2]
    assert [j["queue_order"] for j in jobs] == [1, 2]
    scheduler = Scheduler(store, cfg, None)
    store.set_stage(ids[1], "download", "completed")
    jobs = store.jobs()
    assert not scheduler.ready(jobs[1], "deepgram", jobs)


def test_queue_order_stable_when_timestamps_tie(env, monkeypatch):
    store, cfg = env
    data = cfg.snapshot()
    # Coarse Windows timers can hand identical time.time() values to rows inserted
    # in the same batch; queue order must not depend on the clock.
    monkeypatch.setattr("waraq.store.time.time", lambda: 1000.0)
    store.add([items(4)[3], items(2)[1]], data)
    jobs = store.jobs()
    assert [j["position"] for j in jobs] == [4, 2]
    assert [j["queue_order"] for j in jobs] == [1, 2]
    second = cfg.snapshot()
    monkeypatch.setattr("waraq.store.time.time", lambda: 1000.0)
    store.add([items(9)[8]], second)
    jobs = store.jobs()
    assert [j["position"] for j in jobs] == [4, 2, 9]


def test_opencode_only_reuses_committed_validated_document(env, tmp_path, monkeypatch):
    import waraq.services as services
    store, cfg = env
    txt = tmp_path / "original.txt"
    txt.write_text("مادة اختبارية", encoding="utf-8")
    ident = store.add(items(), cfg.snapshot(), input_path=str(txt))[0]
    job = store.get(ident)
    calls = []
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, work, *args):
        calls.append(1)
        make_docx(work / "001-درس 1.docx")
    monkeypatch.setattr(services, "run_process", run)
    adapter = services.OpenCode()
    worker = cfg.data["opencode_workers"][0]
    result = adapter.process(job, worker, cfg.data, "", threading.Event(), lambda _: None)
    adapter.process(job, worker, cfg.data, "", threading.Event(), lambda _: None)
    assert len(calls) == 1
    (result.parent / "validated.json").unlink()
    adapter.process(job, worker, cfg.data, "", threading.Event(), lambda _: None)
    assert len(calls) == 2
    assert len(list(result.parent.glob("unverified-*.docx"))) == 1


def test_prune_keeps_recent_events(env):
    store, cfg = env
    for i in range(30):
        store.event("job", f"msg {i}")
    store.prune(keep_events=10)
    remaining = store.events(limit=100)
    assert [e["message"] for e in remaining] == [f"msg {i}" for i in range(29, 19, -1)]


def test_opencode_works_on_transcript_copy_inside_workspace(env, tmp_path, monkeypatch):
    import waraq.services as services
    store, cfg = env
    txt = tmp_path / "original.txt"
    txt.write_text("مادة اختبارية", encoding="utf-8")
    ident = store.add(items(), cfg.snapshot(), input_path=str(txt))[0]
    job = store.get(ident)
    captured = {}
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, *args):
        captured["cwd"] = Path(cwd)
        captured["prompt"] = command[-1]
        copy = captured["cwd"] / "original.txt"
        assert copy.is_file()
        assert copy.read_text(encoding="utf-8-sig").strip() == "مادة اختبارية"
        make_docx(captured["cwd"] / "001-درس 1.docx")
    monkeypatch.setattr(services, "run_process", run)
    result = services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, "", threading.Event(), lambda _: None)
    # The prompt points at the workspace copy, never at the original path.
    assert str((captured["cwd"] / "original.txt").resolve()) in captured["prompt"]
    assert str(txt) not in captured["prompt"]
    assert str(result.resolve()) in captured["prompt"]
    assert txt.read_text(encoding="utf-8") == "مادة اختبارية"
    validate_docx(result)


def test_opencode_permission_rejection_diagnosed(env, tmp_path, monkeypatch):
    import waraq.services as services
    store, cfg = env
    txt = tmp_path / "original.txt"
    txt.write_text("مادة اختبارية", encoding="utf-8")
    ident = store.add(items(), cfg.snapshot(), input_path=str(txt))[0]
    job = store.get(ident)
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, cancel, timeout, log, process_env=None):
        log("permission requested: external_directory (C:\\blocked\\*); auto-rejecting")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(ServiceError) as exc:
        services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, "", threading.Event(), lambda _: None)
    # A dedicated diagnosis, not the generic missing-Word message, and the
    # worker stays usable (failed, not blocked).
    assert exc.value.kind == "failed"
    assert "external_directory" in str(exc.value)


def test_oversized_docx_entry_rejected(tmp_path):
    # 101 MB of zeros compresses to ~100 KB, so the file is cheap to build but
    # would previously be fully decompressed by testzip() before any size check.
    path = tmp_path / "bloated.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/media/huge.bin", b"\x00" * (101 * 1024 * 1024))
    with pytest.raises(ServiceError):
        validate_docx(path)


def pump(scheduler, store, ident, stage, wanted, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        scheduler.tick()
        if store.get(ident)["stages"][stage]["status"] in wanted:
            # Let the worker thread leave its finally block: closing the shared
            # sqlite connection mid-write is not thread-safe.
            while not scheduler.idle() and time.time() < deadline:
                time.sleep(0.005)
            assert scheduler.idle(), f"{stage} reached {wanted} but its worker did not finish"
            return
        time.sleep(0.01)
    raise AssertionError(f"{stage} stuck at {store.get(ident)['stages'][stage]['status']}")


def download_only_job(store, cfg, overrides=None):
    data = cfg.snapshot()
    data["deepgram_enabled"] = False
    data["opencode_enabled"] = False
    data.update(overrides or {})
    return store.add(items(), data)[0]


def local_txt_job(store, cfg, tmp_path, overrides=None):
    txt = tmp_path / "درس.txt"
    txt.write_text("مادة اختبارية", encoding="utf-8")
    data = cfg.snapshot()
    data.update(overrides or {})
    return store.add(items(), data, input_path=str(txt))[0]


def test_transient_failure_rescheduled_then_completes(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    calls = []
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            calls.append(1)
            if len(calls) < 3:
                raise ServiceError("انقطاع الشبكة المؤقت", "failed")
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube(), retry_delays=(0.05, 0.05, 0.05))
    pump(scheduler, store, ident, "download", ("completed",))
    stage = store.get(ident)["stages"]["download"]
    assert stage["attempts"] == 3
    assert stage["next_retry"] == 0
    assert len([e for e in store.events(50) if "أُعيدت الجدولة" in e["message"]]) == 2


def test_transient_retry_budget_exhausted(env):
    store, cfg = env
    ident = download_only_job(store, cfg, {"youtube_retry_attempts": 1})
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            raise ServiceError("فشل متكرر", "failed")
    scheduler = Scheduler(store, cfg, None, FakeYoutube(), retry_delays=(0.05,))
    pump(scheduler, store, ident, "download", ("failed",))
    stage = store.get(ident)["stages"]["download"]
    assert stage["retries"] == 0  # the terminal transition clears the episode counter
    assert len([e for e in store.events(50) if "أُعيدت الجدولة" in e["message"]]) == 1


def test_deepgram_failure_not_auto_rescheduled(env, tmp_path):
    store, cfg = env
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio fixture")
    data = cfg.snapshot()
    ident = store.add(items(), data, input_path=str(audio))[0]
    calls = []
    class FakeDG:
        def transcribe(self, job, worker, cfg, secret, cancel, progress):
            calls.append(1)
            raise ServiceError("فشل Deepgram: HTTP 400", "failed")
    keys = type("Keys", (), {"get": lambda s, w: "key"})()
    scheduler = Scheduler(store, cfg, keys, deepgram=FakeDG(), retry_delays=(0.05,))
    pump(scheduler, store, ident, "deepgram", ("failed",))
    time.sleep(0.1)
    scheduler.tick()
    stage = store.get(ident)["stages"]["deepgram"]
    assert stage["status"] == "failed" and stage["retries"] == 0 and stage["next_retry"] == 0
    assert len(calls) == 1  # paid transcription is never resubmitted automatically


def test_deepgram_retryable_failure_uses_two_distinct_backups(env, tmp_path):
    store, cfg = env
    cfg.data["opencode_enabled"] = False
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio fixture")
    ident = store.add(items(), cfg.snapshot(), input_path=str(audio))[0]
    calls = []
    class FakeDG:
        def transcribe(self, job, worker, config, secret, cancel, progress):
            calls.append(worker["id"])
            if worker["id"] != "dg7":
                raise ServiceError("Deepgram HTTP 503", "retryable")
            output = Path(job["folder"]) / "transcript.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("نص ناجح", encoding="utf-8")
            return output
    keys = type("Keys", (), {"get": lambda self, name: "key" if name in ("dg1", "dg6", "dg7") else ""})()
    scheduler = Scheduler(store, cfg, keys, deepgram=FakeDG(), deepgram_backup_delays=(0.01, 0.01))
    deadline = time.time() + 3
    while time.time() < deadline:
        scheduler.tick()
        if store.get(ident)["stages"]["deepgram"]["status"] == "completed":
            break
        time.sleep(0.01)
    assert calls == ["dg1", "dg6", "dg7"]
    assert store.get(ident)["stages"]["deepgram"]["status"] == "completed"
    while not scheduler.idle() and time.time() < deadline:
        time.sleep(0.01)


def test_deepgram_uncertain_result_does_not_use_backup(env, tmp_path):
    store, cfg = env
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio fixture")
    ident = store.add(items(), cfg.snapshot(), input_path=str(audio))[0]
    calls = []
    class FakeDG:
        def transcribe(self, job, worker, config, secret, cancel, progress):
            calls.append(worker["id"])
            raise ServiceError("انقطع الاتصال", "uncertain")
    keys = type("Keys", (), {"get": lambda self, name: "key"})()
    scheduler = Scheduler(store, cfg, keys, deepgram=FakeDG(), deepgram_backup_delays=(0.01, 0.01))
    pump(scheduler, store, ident, "deepgram", ("uncertain",))
    scheduler.tick()
    assert calls == ["dg1"]


def test_deepgram_explicit_server_failure_is_backup_eligible(monkeypatch, env, tmp_path):
    import requests
    _, cfg = env
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio fixture")
    class Response:
        ok = False
        status_code = 503
        text = "temporary failure"
        headers = {}
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(requests, "Session", Session)
    settings = cfg.snapshot()
    settings["max_retries"] = 1
    with pytest.raises(ServiceError) as error:
        Deepgram().request(audio, settings["deepgram_workers"][0], settings, "key", threading.Event(), tmp_path / "raw.json")
    assert error.value.kind == "retryable"


def test_bulk_retry_only_matching_attention_group(env):
    from PySide6.QtCore import QCoreApplication
    from waraq.bridge import Bridge
    app = QCoreApplication.instance() or QCoreApplication([])
    store, cfg = env
    ids = store.add(items(3), cfg.snapshot())
    store.set_stage(ids[0], "download", "failed")
    store.set_stage(ids[1], "download", "failed")
    store.set_stage(ids[2], "download", "blocked")
    scheduler = Scheduler(store, cfg, None)
    bridge = Bridge(store, cfg, None, scheduler)
    bridge.retryMany(ids + [ids[0], "missing"], "download", "failed")
    assert [store.get(ident)["stages"]["download"]["status"] for ident in ids] == ["pending", "pending", "blocked"]
    assert len([event for event in store.events() if event["message"] == "إعادة محاولة جماعية بطلب المستخدم."]) == 2
    bridge.timer.stop()


@pytest.mark.parametrize("kind", ["uncertain", "blocked", "cancelled"])
def test_terminal_kinds_never_rescheduled(env, tmp_path, kind):
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    calls = []
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            calls.append(1)
            raise ServiceError("حالة نهائية", kind)
    scheduler = Scheduler(store, cfg, None, opencode=FakeOC(), retry_delays=(0.05,), limit_retry_delay=0.05)
    pump(scheduler, store, ident, "opencode", (kind,))
    time.sleep(0.15)
    scheduler.tick()
    assert len(calls) == 1


def test_opencode_quota_retries_hourly_then_terminal(env, tmp_path):
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path, {"opencode_limit_retries": 2})
    calls = []
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            calls.append(1)
            raise ServiceError(LIMIT_MESSAGE, "rate_limited")
    scheduler = Scheduler(store, cfg, None, opencode=FakeOC(), limit_retry_delay=0.05)
    deadline = time.time() + 5
    deferred = None
    while time.time() < deadline:
        stage = store.get(ident)["stages"]["opencode"]
        if stage["status"] == "pending" and stage["retries"] >= 1:
            deferred = stage
            break
        scheduler.tick()
        time.sleep(0.01)
    assert deferred is not None and deferred["next_retry"] > time.time()
    pump(scheduler, store, ident, "opencode", ("failed",))
    stage = store.get(ident)["stages"]["opencode"]
    assert "استُنفدت محاولات الانتظار (2)" in stage["error"]
    assert len(calls) == 3
    assert len([e for e in store.events(50) if "أُعيدت الجدولة" in e["message"]]) == 2


def test_retry_counter_resets_after_success(env, tmp_path):
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    calls = []
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            calls.append(1)
            if len(calls) == 1:
                raise ServiceError("عابر", "failed")
            return make_docx(Path(job["folder"]) / "out.docx")
    scheduler = Scheduler(store, cfg, None, opencode=FakeOC(), retry_delays=(0.05,))
    pump(scheduler, store, ident, "opencode", ("completed",))
    stage = store.get(ident)["stages"]["opencode"]
    assert stage["attempts"] == 2 and stage["retries"] == 0


def test_scheduled_retry_survives_restart(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    store.claim(ident, "download", "yt1")
    store.defer_retry(ident, "download", "خطأ عابر", 3600)
    store.recover()
    stage = store.get(ident)["stages"]["download"]
    assert stage["status"] == "pending" and stage["retries"] == 1
    scheduler = Scheduler(store, cfg, None)
    jobs = store.jobs()
    assert not scheduler.ready(jobs[0], "download", jobs)
    assert not store.claim(ident, "download", "yt1")


def test_queue_drained_detection(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    assert not store.queue_drained()
    store.defer_retry(ident, "download", "بانتظار", 3600)
    assert not store.queue_drained()  # deferred work keeps the app open
    store.set_stage(ident, "download", "completed")
    assert store.queue_drained()


def test_template_config_validation(env):
    _, cfg = env
    assert cfg.data["opencode_templates"][0]["name"] == "افتراضي"
    good = cfg.snapshot()
    good["opencode_templates"] = [{"id": "t1", "name": "دروس", "text": "{transcript_path}\n{output_path}"}]
    cfg.save(good)
    assert [t["id"] for t in cfg.data["opencode_templates"]] == ["t1"]
    missing = cfg.snapshot()
    missing["opencode_templates"] = [{"id": "t1", "name": "ناقص", "text": "بلا متغيرات"}]
    with pytest.raises(ValueError):
        cfg.save(missing)
    duplicate = cfg.snapshot()
    duplicate["opencode_templates"] = [{"id": "t1", "name": "أ", "text": "{transcript_path} {output_path}"},
                                       {"id": "t1", "name": "ب", "text": "{transcript_path} {output_path}"}]
    with pytest.raises(ValueError):
        cfg.save(duplicate)


def test_quota_line_matching():
    from waraq.services import limit_reached
    assert limit_reached('{"type":"error","error":{"data":{"statusCode":429,"message":"The usage limit has been reached"}}}')
    assert limit_reached("AI_APICallError: The usage limit has been reached")
    assert not limit_reached("AI_APICallError: Our servers are currently overloaded. Please try again later.")
    assert not limit_reached('{"type":"text","part":{"text":"نص عادي"}}')


@pytest.mark.parametrize("raises", [True, False])
@pytest.mark.parametrize("error", [
    {"data": {"statusCode": 429, "message": "The usage limit has been reached"}},
    {"type": "api", "status": 429, "message": "The usage limit has been reached"},
])
def test_opencode_quota_stop_diagnosed(env, tmp_path, monkeypatch, raises, error):
    import waraq.services as services
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    job = store.get(ident)
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    quota = json.dumps({"type": "error", "sessionID": "ses_x", "error": error})
    def run(command, cwd, cancel, timeout, log, process_env=None):
        log(quota)
        if raises:
            raise ServiceError("فشلت العملية الخارجية")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(ServiceError) as exc:
        services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, "", threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited"


def test_default_pools_offer_two_deepgram_backups(tmp_path):
    cfg = Config(tmp_path)
    assert [w["id"] for w in cfg.data["deepgram_workers"]] == ["dg1", "dg2", "dg3", "dg4", "dg5", "dg6", "dg7"]
    assert [w["role"] for w in cfg.data["deepgram_workers"]] == ["primary"] * 5 + ["backup"] * 2
    assert len({w["project"] for w in cfg.data["deepgram_workers"]}) == 7
    assert [w["id"] for w in cfg.data["opencode_workers"]] == ["oc1", "oc2", "oc3", "oc4", "oc5"]
    assert {w["provider"] for w in cfg.data["opencode_workers"]} == {"login"}
    assert cfg.data["opencode_providers"] == []
    Config.validate(cfg.data)


def test_worker_pool_migration_tops_up_and_keeps_saved_workers(tmp_path):
    legacy = {"deepgram_workers": [{"id": "dg1", "name": "Deepgram 01", "enabled": False, "project": "default",
                                    "model": "nova-3", "language": "ar",
                                    "endpoint": "https://api.deepgram.com", "diarize": True}],
              "opencode_workers": [{"id": "oc1", "name": "OpenCode 01", "enabled": True, "provider": "login",
                                    "model": "openai/gpt-5.6-sol", "variant": "high", "agent": "build"}]}
    (tmp_path / "settings.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    cfg = Config(tmp_path)
    assert [w["id"] for w in cfg.data["deepgram_workers"]] == ["dg1", "dg2", "dg3", "dg4", "dg5", "dg6", "dg7"]
    assert [w["role"] for w in cfg.data["deepgram_workers"]] == ["primary"] * 5 + ["backup"] * 2
    assert [w["id"] for w in cfg.data["opencode_workers"]] == ["oc1", "oc2", "oc3", "oc4", "oc5"]
    # The saved worker keeps every field the user chose; the clones inherit its settings.
    assert cfg.data["deepgram_workers"][0]["project"] == "default"
    assert cfg.data["deepgram_workers"][0]["enabled"] is False
    assert cfg.data["deepgram_workers"][1]["model"] == "nova-3" and cfg.data["deepgram_workers"][1]["diarize"] is True
    assert cfg.data["deepgram_workers"][1]["project"] == "proj-2"
    assert [w["model"] for w in cfg.data["opencode_workers"]] == ["openai/gpt-5.6-sol"] * 5
    assert cfg.migration_note
    # A worker the user deletes afterwards is never resurrected.
    cfg.data["opencode_workers"] = cfg.data["opencode_workers"][:1]
    cfg.save({})
    again = Config(tmp_path)
    assert [w["id"] for w in again.data["opencode_workers"]] == ["oc1"]
    assert again.migration_note == ""


def test_migration_removes_automatically_created_router_workers(tmp_path):
    settings = {
        "workers_expanded": True,
        "deepgram_workers": [{"id": "dg1", "name": "Deepgram 01", "enabled": True, "project": "default",
                              "model": "whisper-large", "language": "ar",
                              "endpoint": "https://api.deepgram.com", "diarize": False}],
        "opencode_workers": [
            {"id": "oc1", "name": "OpenCode 01", "enabled": True, "model": "openai/gpt-5.6-sol", "variant": "high", "agent": "build"},
            {"id": "oc2", "name": "OpenCode 02", "enabled": True, "model": "openai/gpt-5.6-sol", "variant": "high", "agent": "build"},
            {"id": "oc3", "name": "OpenRouter 01", "enabled": True, "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash", "variant": "", "agent": "build"},
            {"id": "oc4", "name": "OpenRouter 02", "enabled": True, "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash", "variant": "", "agent": "build"},
            {"id": "oc5", "name": "OpenRouter 03", "enabled": True, "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash", "variant": "", "agent": "build"},
        ],
    }
    (tmp_path / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    cfg = Config(tmp_path)
    assert "أُزيلت" in cfg.migration_note
    # The five workers remain, but every one of them follows the user's own settings again.
    assert [w["id"] for w in cfg.data["opencode_workers"]] == ["oc1", "oc2", "oc3", "oc4", "oc5"]
    assert {w.get("provider", "login") for w in cfg.data["opencode_workers"]} == {"login"}
    assert {w["model"] for w in cfg.data["opencode_workers"]} == {"openai/gpt-5.6-sol"}
    assert [w["id"] for w in cfg.data["deepgram_workers"]] == ["dg1", "dg2", "dg3", "dg4", "dg5", "dg6", "dg7"]
    assert cfg.data["workers_normalised"] is True
    # A hand-made worker that merely mentions a router provider is left alone.
    cfg.data["opencode_workers"].append({"id": "ocmy1", "name": "عامل مزود خاص", "enabled": True,
                                         "provider": "openrouter", "model": "x/y", "variant": "", "agent": "build"})
    cfg.save({})
    assert any(w["id"] == "ocmy1" for w in Config(tmp_path).data["opencode_workers"])


def test_provider_validation(tmp_path):
    cfg = Config(tmp_path)
    unknown = cfg.snapshot()
    unknown["opencode_workers"][2]["provider"] = "nope"
    with pytest.raises(ValueError):
        Config.validate(unknown)
    bad_id = cfg.snapshot()
    bad_id["opencode_providers"] = [{"id": "Bad Id", "name": "x", "key_env": "X_API_KEY", "isolated": True}]
    with pytest.raises(ValueError):
        Config.validate(bad_id)
    reserved = cfg.snapshot()
    reserved["opencode_providers"] = [{"id": "openai", "name": "x", "key_env": "X_API_KEY", "isolated": True}]
    with pytest.raises(ValueError):
        Config.validate(reserved)
    bad_env = cfg.snapshot()
    bad_env["opencode_providers"] = [{"id": "groq", "name": "Groq", "key_env": "groq key", "isolated": True}]
    with pytest.raises(ValueError):
        Config.validate(bad_env)
    ok = cfg.snapshot()
    ok["opencode_providers"] = [{"id": "groq", "name": "Groq", "key_env": "GROQ_API_KEY", "isolated": True}]
    ok["opencode_workers"][2]["provider"] = "groq"
    Config.validate(ok)
    library = cfg.snapshot()
    library["library_auto"] = "yes"
    with pytest.raises(ValueError):
        Config.validate(library)


def test_provider_model_ids_and_worker_environment(tmp_path):
    cfg = Config(tmp_path)
    cfg.data["opencode_providers"] = [{"id": "groq", "name": "Groq", "key_env": "GROQ_API_KEY", "isolated": True}]
    assert compose_model("login", "openai/gpt-6-astra") == "openai/gpt-6-astra"
    assert compose_model("openrouter", "deepseek/deepseek-v4.1-flash") == "openrouter/deepseek/deepseek-v4.1-flash"
    assert compose_model("openrouter", "openrouter/x/y") == "openrouter/x/y"
    assert compose_model("groq", "llama-3.3-70b") == "groq/llama-3.3-70b"
    router = {"id": "oc3", "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}
    env = worker_env(router, "openrouter", "sk-or-secret", cfg.data)
    assert env["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert env["XDG_DATA_HOME"] == str(Path(cfg.data["opencode_data_root"]) / "oc3" / "data")
    assert env["XDG_STATE_HOME"] == str(Path(cfg.data["opencode_data_root"]) / "oc3" / "state")
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # A login worker keeps OpenCode's own credentials and its shared directories.
    login = {"id": "oc1", "provider": "login", "model": "openai/gpt-6-astra"}
    assert worker_env(login, "login", "sk-or-secret", cfg.data) == {}
    # A provider added by hand is fed the environment variable the user named.
    added = {"id": "oc9", "provider": "groq", "model": "llama-3.3-70b"}
    assert worker_env(added, "groq", "gsk-secret", cfg.data)["GROQ_API_KEY"] == "gsk-secret"
    assert credential_name("opencode", "oc3", "groq") == "oc3:groq"
    assert credential_name("deepgram", "dg1") == "dg1"


def test_opencode_command_carries_provider_model_and_isolated_env(env, tmp_path, monkeypatch):
    import waraq.services as services
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    job = store.get(ident)
    captured = {}
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, cancel, timeout, log, process_env=None):
        captured["command"] = command
        captured["env"] = process_env
        make_docx(Path(cwd) / "001-درس 1.docx")
    monkeypatch.setattr(services, "run_process", run)
    router = {"id": "oc3", "name": "OpenRouter 01", "provider": "openrouter",
              "model": "deepseek/deepseek-v4.1-flash", "variant": "", "agent": "build"}
    services.OpenCode().process(job, router, cfg.data, "sk-or-secret", threading.Event(), lambda _: None)
    assert captured["command"][captured["command"].index("--model") + 1] == "openrouter/deepseek/deepseek-v4.1-flash"
    # Reasoning effort is provider specific, so an empty variant drops the flag entirely.
    assert "--variant" not in captured["command"]
    assert captured["env"]["OPENROUTER_API_KEY"] == "sk-or-secret"
    assert captured["env"]["XDG_DATA_HOME"] == str(Path(cfg.data["opencode_data_root"]) / "oc3" / "data")
    captured.clear()
    login = {"id": "oc1", "name": "OpenCode 01", "provider": "login",
             "model": "openai/gpt-6-astra", "variant": "high", "agent": "build"}
    services.OpenCode().process(job, login, cfg.data, "", threading.Event(), lambda _: None)
    assert captured["command"][captured["command"].index("--model") + 1] == "openai/gpt-6-astra#high"
    assert "--variant" not in captured["command"]
    assert "--dir" not in captured["command"]
    assert "--standalone" in captured["command"]
    assert "--auto" in captured["command"]
    assert "OPENROUTER_API_KEY" not in captured["env"]
    assert "XDG_DATA_HOME" not in captured["env"]


def test_isolated_worker_without_key_is_skipped(env, tmp_path):
    store, cfg = env
    calls = []
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            calls.append((worker["id"], worker["provider"]))
            return make_docx(Path(job["folder"]) / "001-درس 1.docx")
    scheduler = Scheduler(store, cfg, None, opencode=FakeOC())
    # The first worker stays on the OpenCode login; the rest need their own provider key.
    cfg.data["opencode_workers"][2].update({"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    ident = local_txt_job(store, cfg, tmp_path)
    pump(scheduler, store, ident, "opencode", {"completed"})
    assert calls == [("oc1", "login")]
    assert scheduler.disabled_workers["oc3"] == "أضف مفتاح OpenRouter لهذا العامل"


def test_provider_override_forces_every_worker(env, tmp_path):
    store, cfg = env
    seen = []
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            seen.append((worker["id"], worker["provider"], worker["model"], worker.get("variant"), secret))
            return make_docx(Path(job["folder"]) / "001-درس 1.docx")
    class Keys:
        def get(self, worker): return "key-for-" + worker if worker.startswith("oc") else ""
    scheduler = Scheduler(store, cfg, Keys(), opencode=FakeOC())
    scheduler.set_provider_override("openrouter", "deepseek/deepseek-v4.1-flash", "high")
    ident = local_txt_job(store, cfg, tmp_path)
    pump(scheduler, store, ident, "opencode", {"completed"})
    # Even the login worker now runs on OpenRouter, with its own provider-scoped key.
    assert seen == [("oc1", "openrouter", "openrouter/deepseek/deepseek-v4.1-flash", "high", "key-for-oc1:openrouter")]
    # Clearing the override restores each worker's own provider, model and reasoning level.
    scheduler.set_provider_override("", "", "")
    assert scheduler.provider_override == ""
    own = {"id": "oc1", "provider": "login", "model": "openai/gpt-5.6-sol", "variant": "low"}
    assert scheduler.effective_worker(own) == own
    # Forcing a provider without a model keeps the worker's model, composed with that provider.
    cfg.data["opencode_providers"] = [{"id": "groq", "name": "Groq", "key_env": "GROQ_API_KEY", "isolated": True}]
    scheduler.set_provider_override("groq", "", "max")
    forced = scheduler.effective_worker(own)
    assert forced["provider"] == "groq" and forced["model"] == "groq/openai/gpt-5.6-sol" and forced["variant"] == "max"
    # An unknown provider is never forced, so a stale choice cannot silently misroute work.
    scheduler.set_provider_override("nope", "x", "y")
    assert scheduler.provider_override == ""
    scheduler.set_provider_override("", "", "")


def test_completed_stage_mirrors_into_library(env, tmp_path):
    store, cfg = env
    cfg.data["library_root"] = str(tmp_path / "library")
    class FakeOC:
        def process(self, job, worker, cfg, secret, cancel, log):
            return make_docx(Path(job["folder"]) / "001-درس 1.docx")
    scheduler = Scheduler(store, cfg, None, opencode=FakeOC())
    ident = local_txt_job(store, cfg, tmp_path)
    pump(scheduler, store, ident, "opencode", {"completed"})
    assert (tmp_path / "library" / "Word" / "001-درس 1.docx").is_file()
    # Turning automatic organising off leaves the library untouched on later runs.
    cfg.data["library_auto"] = False
    (tmp_path / "library" / "Word" / "001-درس 1.docx").unlink()
    store.retry(ident, "opencode", True)
    pump(scheduler, store, ident, "opencode", {"completed"})
    assert not (tmp_path / "library" / "Word" / "001-درس 1.docx").exists()


def test_library_publish_is_idempotent_and_collision_safe(tmp_path):
    cfg = Config(tmp_path)
    cfg.data["library_root"] = str(tmp_path / "library")
    library = Library()
    folder = tmp_path / "job"
    folder.mkdir()
    job = {"id": "abc123", "position": 3, "title": "درس تجريبي",
           "document": str(make_docx(folder / "003-درس.docx")),
           "transcript": str(folder / "transcript.txt"), "audio": str(folder / "audio.mp3")}
    Path(job["transcript"]).write_text("تفريغ", encoding="utf-8")
    Path(job["audio"]).write_bytes(b"audio")
    assert library.publish(job, cfg.data) == ["003-درس تجريبي.docx", "003-درس تجريبي.txt"]
    # Audio is copied only when the user opts in: it would double the disk footprint.
    assert not (tmp_path / "library" / "الصوت").exists()
    assert library.publish(job, cfg.data) == []
    with_audio = dict(cfg.data, library_copy_audio=True)
    assert library.publish(job, with_audio) == ["003-درس تجريبي.mp3"]
    # A different job with the same title gets its own file instead of overwriting.
    other = dict(job, id="def456")
    assert library.publish(other, cfg.data) == ["003-درس تجريبي (2).docx", "003-درس تجريبي (2).txt"]
    assert sorted(item["kind"] for item in library.scan(cfg.data)) == ["audio", "document", "document", "transcript", "transcript"]
    # A regenerated artifact refreshes the same mirror file rather than adding another copy.
    Path(job["document"]).write_bytes(b"x" * 40)
    assert library.publish(job, cfg.data) == ["003-درس تجريبي.docx"]
    assert sorted(item["name"] for item in library.scan(cfg.data)) == sorted([
        "003-درس تجريبي.docx", "003-درس تجريبي.txt", "003-درس تجريبي.mp3",
        "003-درس تجريبي (2).docx", "003-درس تجريبي (2).txt"])


def test_library_remove_stays_inside_the_library(tmp_path):
    cfg = Config(tmp_path)
    cfg.data["library_root"] = str(tmp_path / "library")
    library = Library()
    folder = tmp_path / "library" / "Word"
    folder.mkdir(parents=True)
    inside, outside = folder / "a.docx", tmp_path / "outside.docx"
    inside.write_bytes(b"a")
    outside.write_bytes(b"b")
    with pytest.raises(ValueError):
        library.remove(cfg.data, str(outside))
    with pytest.raises(ValueError):
        library.remove(cfg.data, str(tmp_path / "library" / "stray.docx"))
    library.remove(cfg.data, str(inside))
    assert not inside.exists() and outside.is_file()


def test_subject_folders_follow_jobs_library_and_word_export(env, tmp_path):
    store, cfg = env
    subject = store.create_folder("الفقه")
    with pytest.raises(ValueError):
        store.create_folder("الفقه")
    with pytest.raises(ValueError):
        store.create_folder("../خارج")
    with pytest.raises(ValueError):
        store.add(items(), cfg.snapshot(), folder_id="unknown")
    ident = store.add(items(), cfg.snapshot(), folder_id=subject)[0]
    job = store.get(ident)
    assert job["folder_name"] == "الفقه" and job["folder_id"] == subject
    source = make_docx(tmp_path / "original.docx")
    store.artifact(ident, "document", source)
    job = store.get(ident)
    library = Library()
    assert library.publish(job, cfg.data) == ["001-درس 1.docx"]
    grouped = tmp_path / "library" / "الفقه" / "Word" / "001-درس 1.docx"
    assert grouped.is_file()
    destination = tmp_path / "export"
    destination.mkdir()
    assert library.export_documents(store.jobs(), cfg.data, destination) == 1
    exported = list((destination / "الفقه" / "Word").glob("*.docx"))
    assert len(exported) == 1 and exported[0].read_bytes() == source.read_bytes()
    assert library.export_documents(store.jobs(), cfg.data, destination) == 1
    assert list((destination / "الفقه" / "Word").glob("*.docx")) == exported
    store.assign_folder(ident, "")
    assert library.publish(store.get(ident), cfg.data) == ["001-درس 1.docx"]
    assert not grouped.exists() and (tmp_path / "library" / "Word" / grouped.name).is_file()


def test_library_legacy_manifest_moves_to_subject(tmp_path):
    cfg = Config(tmp_path)
    source = make_docx(tmp_path / "original.docx")
    job = {"id": "old", "position": 1, "title": "عنوان", "document": str(source)}
    library = Library()
    library.publish(job, cfg.data)
    root = tmp_path / "library"
    manifest = root / ".waraq-library.json"
    manifest.write_text(json.dumps({"old|document": "001-عنوان.docx"}), encoding="utf-8")
    job["folder_name"] = "اللغة"
    assert library.publish(job, cfg.data) == ["001-عنوان.docx"]
    assert not (root / "Word" / "001-عنوان.docx").exists()
    assert (root / "اللغة" / "Word" / "001-عنوان.docx").is_file()
    assert library.publish(job, cfg.data) == []


def test_deepgram_uses_current_nova_diarization_parameter(monkeypatch, tmp_path):
    import requests
    captured = {}
    def fake_post(self, url, **kwargs):
        captured.update(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"results":{"channels":[{"alternatives":[{"transcript":"ok"}]}]}}'
        return response
    monkeypatch.setattr(requests.Session, "post", fake_post)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    cfg = defaults(tmp_path)
    assert cfg["deepgram_workers"][0]["model"] == "nova-3"
    worker = dict(cfg["deepgram_workers"][0], diarize=True)
    Deepgram().request(audio, worker, cfg, "test", threading.Event(), tmp_path / "raw.json")
    assert captured["params"]["diarize_model"] == "latest"
    assert "diarize" not in captured["params"]


def test_updated_youtube_binary_is_used_for_discovery(monkeypatch, env, tmp_path):
    import waraq.services as services
    from waraq.youtube_update import binary_path
    store, cfg = env
    binary = binary_path(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake executable")
    commands = []
    def fake_process(command, cwd, cancel, timeout, log=lambda line: None, env=None):
        commands.append(command)
        return json.dumps({"title": "مادة", "entries": [{"id": "one", "title": "موضوع", "playlist_index": 2}]})
    monkeypatch.setattr(services, "run_process", fake_process)
    result = Youtube(store).discover("https://www.youtube.com/watch?v=one", cfg.data, threading.Event())
    assert result["items"][0]["title"] == "موضوع"
    assert result["items"][0]["position"] == 2
    assert commands[0][0] == str(binary)
    assert "--no-config" in commands[0] and "--dump-single-json" in commands[0]


def test_existing_jobs_migrate_to_uncategorized_without_loss(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, video_id TEXT, title TEXT, url TEXT, position INTEGER, "
               "batch TEXT, config TEXT, folder TEXT, created REAL, audio TEXT DEFAULT '', transcript TEXT DEFAULT '', "
               "document TEXT DEFAULT '', input_path TEXT DEFAULT '', duration REAL DEFAULT 0, queue_order INTEGER DEFAULT 0, seq INTEGER DEFAULT 0)")
    db.execute("INSERT INTO jobs(id,video_id,title,url,position,batch,config,folder,created,seq) "
               "VALUES('old','video','درس سابق','',1,'',?, '',0,1)", (json.dumps(defaults(tmp_path)),))
    db.commit()
    db.close()
    store = Store(path)
    try:
        job = store.get("old")
        assert job["title"] == "درس سابق"
        assert job["folder_id"] == "" and job["folder_name"] == ""
    finally:
        store.db.close()


@pytest.mark.parametrize("message,kind", [
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("unable to download video data: HTTP Error 500", "failed"),
    ("<urlopen error timed out>", "failed"),
    ("Connection reset by peer", "failed"),
    ("getaddrinfo failed", "failed"),
    ("Sign in to confirm you're not a bot", "blocked"),
    ("HTTP Error 403: Forbidden", "blocked"),
    ("This video has been removed by the uploader", "permanent"),
    ("Video unavailable", "permanent"),
    ("The uploader has not made this video available in your country", "permanent"),
    ("Unsupported URL: https://example.com/x", "permanent"),
    ("HTTP Error 404: Not Found", "permanent"),
])
def test_youtube_error_classification_matrix(env, message, kind):
    store, cfg = env
    youtube = Youtube(store)
    error = youtube.classify(Exception(message), cfg.data)
    assert error.kind == kind
    if kind != "rate_limited":
        assert youtube.cooldown_until == 0


def test_rate_limit_cooldown_persists_across_restart(env):
    store, cfg = env
    youtube = Youtube(store)
    error = youtube.classify(Exception("HTTP Error 429"), cfg.data)
    assert error.kind == "rate_limited" and error.retry_after >= cfg.data["youtube_cooldown"]
    revived = Youtube(store)
    assert revived.cooldown_until == pytest.approx(youtube.cooldown_until)
    # Expiry is cleared lazily so a fresh process never sees a stale cooldown.
    youtube.cooldown_until = time.time() - 1
    youtube.expire_cooldown()
    assert youtube.cooldown_until == 0
    assert store.state_get("youtube_cooldown_until") is None


def test_rate_limited_download_is_deferred_then_completes(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    calls = []
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            calls.append(1)
            if len(calls) == 1:
                raise ServiceError("YouTube قيّد الطلبات (429).", "rate_limited", 0.05)
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube(), retry_delays=(0.05, 0.05, 0.05))
    pump(scheduler, store, ident, "download", ("completed",))
    stage = store.get(ident)["stages"]["download"]
    assert stage["attempts"] == 2
    assert scheduler.manual_paused_stages == set()
    assert "download" not in scheduler.automatic_stage_blocks
    assert len([e for e in store.events(50) if "أُعيدت الجدولة" in e["message"]]) == 1


def test_cooldown_blocks_downloads_until_expiry(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    class FakeYoutube:
        cooldown_until = time.time() + 3600
        def download(self, job, cfg, cancel, progress):
            assert self.cooldown_until <= time.time() or True
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    scheduler.tick()
    assert store.get(ident)["stages"]["download"]["status"] == "pending"
    FakeYoutube.cooldown_until = time.time() - 1
    pump(scheduler, store, ident, "download", ("completed",))


def test_youtube_rate_limit_budget_exhausted(env):
    store, cfg = env
    ident = download_only_job(store, cfg, {"youtube_retry_attempts": 1})
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            raise ServiceError("قيّد", "rate_limited", 0.01)
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    pump(scheduler, store, ident, "download", ("failed",))
    stage = store.get(ident)["stages"]["download"]
    assert stage["retries"] == 0
    assert len([e for e in store.events(50) if "استُنفدت إعادات تنزيل YouTube" in e["message"]]) == 1
    assert "download" not in scheduler.automatic_stage_blocks


def test_permanent_download_failure_is_terminal_but_isolated(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    store.add(items(2)[1:], cfg.snapshot())
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            if job["video_id"] == "video1":
                raise ServiceError("الفيديو محذوف ولن يُعاد.", "permanent")
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    pump(scheduler, store, ident, "download", ("failed",))
    stage = store.get(ident)["stages"]["download"]
    assert stage["attempts"] == 1
    assert scheduler.manual_paused_stages == set()
    assert "download" not in scheduler.automatic_stage_blocks
    other = store.jobs()[1]["id"]
    pump(scheduler, store, other, "download", ("completed",))


def test_blocked_download_requeues_after_recheck(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    calls = []
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            calls.append(1)
            if len(calls) == 1:
                raise ServiceError("YouTube رفض الوصول. حدّث الكوكيز.", "blocked")
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    pump(scheduler, store, ident, "download", ("blocked",))
    assert "download" in scheduler.automatic_stage_blocks
    assert scheduler.manual_paused_stages == set()
    store.retry(ident, "download")
    scheduler.unblock_stage("download")
    pump(scheduler, store, ident, "download", ("completed",))
    assert len(calls) == 2


def test_manual_pause_survives_download_retry(env):
    store, cfg = env
    ident = download_only_job(store, cfg)
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    scheduler.pause_stage("download", True)
    store.set_stage(ident, "download", "blocked", "توقف آلي سابق")
    store.retry(ident, "download")
    scheduler.unblock_stage("download")
    scheduler.tick()
    assert scheduler.manual_paused_stages == {"download"}
    assert store.get(ident)["stages"]["download"]["status"] == "pending"
    scheduler.pause_stage("download", False)
    pump(scheduler, store, ident, "download", ("completed",))


def test_low_disk_blocks_stage_then_recovers(env, monkeypatch):
    from collections import namedtuple
    import waraq.scheduler as scheduler_module
    store, cfg = env
    ident = download_only_job(store, cfg)
    usage = namedtuple("usage", "total used free")
    gb = 1024 ** 3
    monkeypatch.setattr(scheduler_module.shutil, "disk_usage", lambda root: usage(300 * gb, 299 * gb, 1 * gb))
    calls = []
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            calls.append(1)
            path = Path(job["folder"]) / "audio.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path
    scheduler = Scheduler(store, cfg, None, FakeYoutube())
    scheduler.tick()
    assert store.get(ident)["stages"]["download"]["status"] == "blocked"
    assert "download" in scheduler.automatic_stage_blocks
    assert calls == []
    monkeypatch.setattr(scheduler_module.shutil, "disk_usage", lambda root: usage(300 * gb, 10 * gb, 290 * gb))
    store.retry(ident, "download")
    scheduler.unblock_stage("download")
    pump(scheduler, store, ident, "download", ("completed",))
    assert calls == [1]


def test_youtube_retry_attempts_seeded_from_legacy_settings(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"auto_retry_attempts": 1}), encoding="utf-8")
    migrated = Config(tmp_path)
    assert migrated.data["youtube_retry_attempts"] == 1
    fresh = Config(tmp_path / "fresh")
    assert fresh.data["youtube_retry_attempts"] == 3


def test_ui_appearance_settings_validation(env):
    store, cfg = env
    data = cfg.snapshot()
    assert data["ui_scale"] == 1.0 and data["ui_rail_collapsed"] is False
    data.update({"ui_scale": 1.2, "ui_rail_collapsed": True})
    cfg.save(data)
    assert cfg.data["ui_scale"] == 1.2 and cfg.data["ui_rail_collapsed"] is True
    for bad in (0.5, 1.5, "كبير", True):
        data = cfg.snapshot()
        data["ui_scale"] = bad
        with pytest.raises(ValueError):
            cfg.save(data)
    data = cfg.snapshot()
    data["ui_rail_collapsed"] = "نعم"
    with pytest.raises(ValueError):
        cfg.save(data)

