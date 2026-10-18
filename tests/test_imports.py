from __future__ import annotations

import json
import time
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QUrl
from qt_test_support import dispose_bridge

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def imports(tmp_path, scheduler_harness, qt_app):
    app = qt_app
    config = Config(tmp_path)
    store = Store(tmp_path / "tasks.db")
    scheduler = Scheduler(store, config, None)
    bridge = Bridge(store, config, None, scheduler)
    notices = []
    bridge.notice.connect(lambda text, error: notices.append((text, error)))
    try:
        yield bridge, store, config, scheduler, notices
    finally:
        bridge.timer.stop()
        bridge.closeAccounts()
        scheduler_harness.shutdown_and_join()
        dispose_bridge(bridge)
        with store.lock:
            store.db.close()


def transcript(tmp_path, name="درس.txt", content=None):
    path = tmp_path / name
    path.write_bytes(content if content is not None else "نص عربي أصلي\r\n".encode("utf-8-sig"))
    return path


def test_import_owns_exact_bytes_and_captures_template_folder(imports, tmp_path):
    bridge, store, config, _, _ = imports
    source = transcript(tmp_path)
    original = source.read_bytes()
    folder = store.create_folder("الدروس")
    config.data["opencode_templates"].append({"id": "chosen", "name": "القالب المختار", "text": "نص {transcript_path}"})
    config.data.update(download_enabled=False, deepgram_enabled=False, opencode_enabled=False)
    assert bridge.importTranscriptPaths([source], "chosen", folder) == 1
    job = store.jobs()[0]
    assert job["input_path"] == str(source.resolve())
    assert Path(job["transcript"]).parent == Path(job["folder"])
    assert Path(job["transcript"]).name == source.name
    assert Path(job["transcript"]).read_bytes() == original
    assert job["folder_id"] == folder
    assert job["config"]["prompt"] == "نص {transcript_path}"
    assert job["config"]["template_name"] == "القالب المختار"
    assert job["config"]["opencode_enabled"]
    assert [job["stages"][stage]["status"] for stage in ("download", "deepgram", "opencode")] == ["skipped", "skipped", "pending"]
    source.unlink()
    assert Path(job["transcript"]).read_bytes() == original


@pytest.mark.parametrize("method", ["files", "folder", "drop"])
def test_txt_import_methods_capture_saved_output_root_template_and_subject(imports, tmp_path, monkeypatch, method):
    bridge, store, config, _, _ = imports
    sources = tmp_path / "source transcripts"
    sources.mkdir()
    source = transcript(sources, "درس مختار.txt")
    output_root = tmp_path / "chosen output"
    library_root = tmp_path / "separate library"
    subject = store.create_folder("المادة المختارة")
    prompt = "القالب المختار: {transcript_path} إلى {output_path}"
    settings = config.snapshot()
    settings.update(output_root=str(output_root), library_root=str(library_root))
    settings["opencode_templates"].append({"id": "chosen", "name": "قالب المادة", "text": prompt})
    assert bridge.saveSettings(json.dumps(settings, ensure_ascii=False))
    if method == "files":
        monkeypatch.setattr("waraq.bridge.QFileDialog.getOpenFileNames", lambda *a: ([str(source)], ""))
        count = bridge.importTranscripts("chosen", subject)
    elif method == "folder":
        monkeypatch.setattr("waraq.bridge.QFileDialog.getExistingDirectory", lambda *a: str(sources))
        count = bridge.importTranscriptFolder("chosen", subject)
    else:
        count = bridge.importDroppedTranscripts([QUrl.fromLocalFile(str(source)).toString()], "chosen", subject)
    assert count == 1
    job = store.jobs()[0]
    assert job["config"]["output_root"] == str(output_root)
    assert Path(job["folder"]).parent == output_root
    assert Path(job["transcript"]).parent == Path(job["folder"])
    assert Path(job["transcript"]).read_bytes() == source.read_bytes()
    assert job["input_path"] == str(source.resolve())
    assert job["config"]["prompt"] == prompt
    assert job["config"]["template_name"] == "قالب المادة"
    assert job["folder_id"] == subject
    assert job["folder_name"] == "المادة المختارة"
    assert output_root != library_root
    changed = config.snapshot()
    changed.update(output_root=str(tmp_path / "different output"), prompt="قالب آخر {transcript_path} {output_path}")
    changed["opencode_templates"][1]["text"] = "نص قالب جديد {transcript_path} {output_path}"
    changed["opencode_templates"][1]["name"] = "اسم جديد"
    assert bridge.saveSettings(json.dumps(changed, ensure_ascii=False))
    captured = store.get(job["id"])
    assert captured["config"]["output_root"] == str(output_root)
    assert captured["config"]["prompt"] == prompt
    assert captured["config"]["template_name"] == "قالب المادة"
    assert captured["folder"] == job["folder"]
    assert captured["transcript"] == job["transcript"]


def test_multiple_paths_share_batch_and_strict_queue_order(imports, tmp_path):
    bridge, store, config, scheduler, _ = imports
    sources = [transcript(tmp_path, name) for name in ("درس3.txt", "درس1.txt", "درس2.txt")]
    config.data["strict_order"] = True
    assert bridge.importTranscriptPaths(sources + [sources[0]]) == 3
    jobs = store.jobs()
    assert [job["title"] for job in jobs] == [source.stem for source in sources]
    assert len({job["batch"] for job in jobs}) == 1
    assert len({job["folder"] for job in jobs}) == 3
    assert [job["position"] for job in jobs] == [1, 2, 3]
    assert [job["queue_order"] for job in jobs] == [1, 2, 3]
    assert [job["seq"] for job in jobs] == [1, 2, 3]
    assert scheduler.ready(jobs[0], "opencode", jobs)
    assert not scheduler.ready(jobs[1], "opencode", jobs)
    store.set_stage(jobs[0]["id"], "opencode", "completed")
    jobs = store.jobs()
    assert scheduler.ready(jobs[1], "opencode", jobs)


def test_folder_import_natural_order_immediate_txt_only(imports, tmp_path, monkeypatch):
    bridge, store, _, _, _ = imports
    source_folder = tmp_path / "source"
    source_folder.mkdir()
    for name in ("درس10.txt", "درس2.TXT", "درس02.txt", "درس1.txt", "ignore.mp3"):
        transcript(source_folder, name)
    nested = source_folder / "nested"
    nested.mkdir()
    transcript(nested)
    monkeypatch.setattr("waraq.bridge.QFileDialog.getExistingDirectory", lambda *a: str(source_folder))
    assert bridge.importTranscriptFolder() == 4
    assert [job["title"] for job in store.jobs()] == ["درس1", "درس02", "درس2", "درس10"]


def test_file_selector_and_audio_import_compatibility(imports, tmp_path, monkeypatch):
    bridge, store, _, _, _ = imports
    source = transcript(tmp_path)
    monkeypatch.setattr("waraq.bridge.QFileDialog.getOpenFileNames", lambda *a: ([str(source)], ""))
    assert bridge.importTranscripts("default") == 1
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    folder = store.create_folder("صوت")
    monkeypatch.setattr("waraq.bridge.QFileDialog.getOpenFileNames", lambda *a: ([str(audio)], ""))
    assert bridge.importLocal(folder, "default") == 1
    job = store.jobs()[1]
    assert job["audio"] == str(audio)
    assert job["folder_id"] == folder
    assert job["config"]["template_name"] == "افتراضي"
    assert job["stages"]["download"]["status"] == "skipped"
    assert job["stages"]["deepgram"]["status"] == "pending"


def test_drop_handles_encoded_urls_deduplication_and_invalid_items(imports, tmp_path):
    bridge, store, _, _, notices = imports
    source = transcript(tmp_path, "درس # 1.TXT")
    unsupported = transcript(tmp_path, "bad.pdf")
    url = QUrl.fromLocalFile(str(source)).toString()
    assert bridge.importDroppedTranscripts([url, QUrl(url), "https://example.com/lesson.txt", QUrl.fromLocalFile(str(unsupported)).toString()]) == 1
    assert len(store.jobs()) == 1
    assert Path(store.jobs()[0]["transcript"]).read_bytes() == source.read_bytes()
    assert notices[-1][1]
    assert "أضيفت 1" in notices[-1][0]
    assert "bad.pdf" in notices[-1][0]


def test_invalid_paths_are_skipped_with_aggregated_notice(imports, tmp_path):
    bridge, store, _, _, notices = imports
    valid = transcript(tmp_path)
    empty = transcript(tmp_path, "empty.txt", b"\xef\xbb\xbf \r\n")
    non_utf8 = transcript(tmp_path, "invalid.txt", b"\xff\xfe\x00")
    missing = tmp_path / "missing.txt"
    assert bridge.importTranscriptPaths([empty, valid, non_utf8, missing, tmp_path]) == 1
    assert store.jobs()[0]["position"] == 1
    assert len(notices) == 1
    assert notices[0][1]
    assert all(name in notices[0][0] for name in ("empty.txt", "invalid.txt", "missing.txt"))


@pytest.mark.parametrize("kind", ["template", "folder"])
def test_invalid_selection_does_not_create_jobs_or_copies(imports, tmp_path, kind):
    bridge, store, config, _, notices = imports
    source = transcript(tmp_path)
    args = ("missing", "") if kind == "template" else ("", "missing")
    assert bridge.importTranscriptPaths([source], *args) == 0
    assert not store.jobs()
    assert not list(Path(config.data["output_root"]).glob("*"))
    assert notices[-1][1]


@pytest.mark.parametrize("failure", ["copy", "database"])
def test_batch_failure_rolls_back_jobs_stages_and_copies(imports, tmp_path, monkeypatch, failure):
    bridge, store, config, _, notices = imports
    first, second = transcript(tmp_path, "first.txt"), transcript(tmp_path, "second.txt")
    root = Path(config.data["output_root"])
    if failure == "copy":
        original_write = Path.write_bytes
        def failing_write(path, content):
            if path.name == "second.txt" and root in path.parents:
                raise OSError("copy failure")
            return original_write(path, content)
        monkeypatch.setattr(Path, "write_bytes", failing_write)
    else:
        store.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON jobs WHEN NEW.title='second' BEGIN SELECT RAISE(ABORT, 'DB failure'); END")
    assert bridge.importTranscriptPaths([first, second]) == 0
    assert not store.jobs()
    assert store.db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    assert not list(root.glob("*"))
    assert first.exists() and second.exists()
    assert notices[-1][1]


def test_store_direct_txt_import_rejects_invalid_without_phantom_job(imports, tmp_path):
    _, store, config, _, _ = imports
    source = transcript(tmp_path, content=b"\xff")
    with pytest.raises(UnicodeDecodeError):
        store.add([{"video_id": "local", "title": "درس", "position": 1}], config.snapshot(), input_path=str(source))
    assert not store.jobs()
    assert not list(Path(config.data["output_root"]).glob("*"))


def test_failed_cleanup_preserves_database_error_and_reports_leftover(imports, tmp_path, monkeypatch):
    bridge, store, _, _, notices = imports
    source = transcript(tmp_path)
    store.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'DB failure'); END")
    def cannot_clean(path):
        raise PermissionError("cleanup failure")
    monkeypatch.setattr("waraq.store.shutil.rmtree", cannot_clean)
    with pytest.raises(sqlite3.IntegrityError, match="DB failure"):
        store.add([{"video_id": "local", "title": "درس", "position": 1}], bridge.config.snapshot(), input_path=str(source))
    assert bridge.importTranscriptPaths([source]) == 0
    assert not store.jobs()
    assert "DB failure" in notices[-1][0]
    if hasattr(Exception(), "add_note"):
        assert "cleanup failure" in notices[-1][0]


def test_import_slots_are_registered_for_qml(imports):
    bridge, *_ = imports
    meta = bridge.metaObject()
    for signature in ("importTranscripts(QString,QString)", "importTranscriptFolder(QString,QString)",
                      "importDroppedTranscripts(QVariantList,QString,QString)", "importLocal(QString,QString)"):
        assert meta.indexOfMethod(signature) >= 0


def test_deleted_original_still_produces_word_through_scheduler(imports, tmp_path, monkeypatch):
    import waraq.services as services
    bridge, store, config, scheduler, _ = imports
    source = transcript(tmp_path)
    original = source.read_bytes()
    config.data["opencode_templates"].append({"id": "custom", "name": "خاص", "text": "اقرأ {transcript_path}"})
    assert bridge.importTranscriptPaths([source], "custom") == 1
    job = store.jobs()[0]
    source.unlink()
    upstream = []
    def forbidden(*args, **kwargs):
        upstream.append(True)
        raise AssertionError("upstream called")
    monkeypatch.setattr(scheduler.youtube, "download", forbidden)
    monkeypatch.setattr(scheduler.deepgram, "transcribe", forbidden)
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    runtime = Mock(executable="opencode.exe", url="http://127.0.0.1:1234", password="synthetic", env={}, instance_id="instance")
    runtime.create_session.return_value = "ses_test"
    runtime.inactive.return_value = True
    accounts = Mock(supervisor=SimpleNamespace(ensure=lambda *a: runtime))
    accounts.readiness.return_value = {"ready": True}
    accounts.resolve_attempt.return_value = {"failure": None}
    accounts.begin_attempt.return_value = {"attempt_id": "attempt", "account_id": "opaque", "alias": "Account"}
    scheduler.accounts = accounts
    scheduler.opencode = services.OpenCode(accounts=accounts)
    calls = []
    def run(command, work, *args):
        calls.append(command)
        copied = Path(work) / source.name
        assert copied.read_bytes() == original
        assert str(copied.resolve()) in command[-1]
        assert command[-1].startswith("اقرأ ")
        output = Path(work) / f"001-{source.stem}.docx"
        with zipfile.ZipFile(output, "w") as document:
            document.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            document.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            document.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>مستند مكتمل</w:t></w:r></w:p></w:body></w:document>')
    monkeypatch.setattr(services, "run_process", run)
    deadline = time.time() + 5
    while time.time() < deadline:
        scheduler.tick()
        current = store.get(job["id"])
        if current["stages"]["opencode"]["status"] == "completed" and scheduler.idle():
            break
        time.sleep(0.01)
    assert current["stages"]["opencode"]["status"] == "completed"
    assert len(calls) == 1
    assert not upstream
    assert Path(current["document"]).is_file()
    assert Path(current["transcript"]).read_bytes() == original
    assert store.db.execute("SELECT outcome FROM execution_attempts").fetchone()[0] == "completed"
