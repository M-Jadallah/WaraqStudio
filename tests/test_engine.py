from __future__ import annotations

import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from waraq.config import Config, defaults
from waraq.scheduler import Scheduler
from waraq.services import LIMIT_MESSAGE, Deepgram, ServiceError, Youtube, digest, transcript_from, validate_docx
from waraq.store import Store, STAGES


def make_docx(path, text="مستند اختباري"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return path


@pytest.fixture
def env(tmp_path):
    cfg = Config(tmp_path)
    store = Store(tmp_path / "tasks.db")
    yield store, cfg
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
    assert job["transcript"] == str(txt)
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
    assert error.kind == "blocked"
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
    def activity(stage, delay):
        with lock:
            counts[stage] += 1
            peaks[stage] = max(peaks[stage], counts[stage])
        time.sleep(delay)
        with lock:
            counts[stage] -= 1
    class FakeYoutube:
        cooldown_until = 0
        def download(self, job, cfg, cancel, progress):
            downloads.append(job["position"])
            activity("download", 0.03)
            p = Path(job["folder"]) / "audio.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"test audio")
            return p
    class FakeDG:
        def transcribe(self, job, worker, cfg, secret, cancel, progress):
            seen_keys.append((worker["id"], secret))
            activity("deepgram", 0.4)
            p = Path(job["folder"]) / "transcript.txt"
            p.write_text("تفريغ اختباري", encoding="utf-8")
            return p
    class FakeOC:
        def process(self, job, worker, cfg, cancel, log):
            activity("opencode", 0.5)
            return make_docx(Path(job["folder"]) / "result.docx")
    class Keys:
        def get(self, worker): return {"dg1": "key-one", "dg2": "key-two"}[worker]
    scheduler = Scheduler(store, cfg, Keys(), FakeYoutube(), FakeDG(), FakeOC())
    deadline = time.time() + 8
    while time.time() < deadline:
        scheduler.tick()
        if all(j["stages"]["opencode"]["status"] == "completed" for j in store.jobs()):
            break
        time.sleep(0.015)
    assert all(j["document"] for j in store.jobs())
    assert peaks == {"download": 1, "deepgram": 2, "opencode": 2}
    assert downloads == list(range(1, 8))
    assert set(seen_keys) == {("dg1", "key-one"), ("dg2", "key-two")}
    for j in store.jobs():
        validate_docx(j["document"])


def test_project_cap(env):
    store, cfg = env
    cfg.data["project_concurrency"] = 1
    assert cfg.data["deepgram_workers"][0]["project"] == cfg.data["deepgram_workers"][1]["project"]
    # Held active request consumes the entire project slot.
    scheduler = Scheduler(store, cfg, type("Keys", (), {"get": lambda s,w: "x"})())
    scheduler.active["dg1"] = {"stage": "deepgram", "project": "default", "job_id": "held", "started": time.time(), "cancel": threading.Event()}
    ids = store.add(items(), cfg.snapshot())
    store.set_stage(ids[0], "download", "completed")
    scheduler.tick()
    assert store.get(ids[0])["stages"]["deepgram"]["status"] == "pending"


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
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, work, *args):
        calls.append(1)
        make_docx(work / "001-درس 1.docx")
    monkeypatch.setattr(services, "run_process", run)
    adapter = services.OpenCode()
    worker = cfg.data["opencode_workers"][0]
    result = adapter.process(job, worker, cfg.data, threading.Event(), lambda _: None)
    adapter.process(job, worker, cfg.data, threading.Event(), lambda _: None)
    assert len(calls) == 1
    (result.parent / "validated.json").unlink()
    adapter.process(job, worker, cfg.data, threading.Event(), lambda _: None)
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
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, *args):
        captured["cwd"] = Path(cwd)
        captured["prompt"] = command[-1]
        copy = captured["cwd"] / "original.txt"
        assert copy.is_file()
        assert copy.read_text(encoding="utf-8-sig").strip() == "مادة اختبارية"
        make_docx(captured["cwd"] / "001-درس 1.docx")
    monkeypatch.setattr(services, "run_process", run)
    result = services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, threading.Event(), lambda _: None)
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
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, cancel, timeout, log, process_env=None):
        log("permission requested: external_directory (C:\\blocked\\*); auto-rejecting")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(ServiceError) as exc:
        services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, threading.Event(), lambda _: None)
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
    ident = download_only_job(store, cfg, {"auto_retry_attempts": 1})
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


@pytest.mark.parametrize("kind", ["uncertain", "blocked", "cancelled"])
def test_terminal_kinds_never_rescheduled(env, tmp_path, kind):
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    calls = []
    class FakeOC:
        def process(self, job, worker, cfg, cancel, log):
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
        def process(self, job, worker, cfg, cancel, log):
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
        def process(self, job, worker, cfg, cancel, log):
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
def test_opencode_quota_stop_diagnosed(env, tmp_path, monkeypatch, raises):
    import waraq.services as services
    store, cfg = env
    ident = local_txt_job(store, cfg, tmp_path)
    job = store.get(ident)
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    quota = '{"type":"error","sessionID":"ses_x","error":{"data":{"statusCode":429,"message":"The usage limit has been reached"}}}'
    def run(command, cwd, cancel, timeout, log, process_env=None):
        log(quota)
        if raises:
            raise ServiceError("فشلت العملية الخارجية")
    monkeypatch.setattr(services, "run_process", run)
    with pytest.raises(ServiceError) as exc:
        services.OpenCode().process(job, cfg.data["opencode_workers"][0], cfg.data, threading.Event(), lambda _: None)
    assert exc.value.kind == "rate_limited"
