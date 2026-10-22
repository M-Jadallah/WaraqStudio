"""Desktop source drafts publish a batch only after explicit confirmation."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QUrl

from qt_test_support import dispose_bridge
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def source_draft(tmp_path, scheduler_harness, qt_app):
    config = Config(tmp_path)
    config.data["accounts_enabled"] = False
    store = Store(tmp_path / "tasks.db")
    scheduler = Scheduler(store, config, None)
    bridge = Bridge(store, config, None, scheduler)
    folder = store.create_folder("الدروس")
    output = tmp_path / "batch results"
    output.mkdir()
    env = SimpleNamespace(bridge=bridge, draft=bridge.sourceDraft, config=config,
                          store=store, scheduler=scheduler, folder=folder, output=output)
    try:
        yield env
    finally:
        bridge.sourceDraft.cancelDiscovery()
        bridge.timer.stop()
        bridge.closeAccounts()
        scheduler_harness.shutdown_and_join()
        dispose_bridge(bridge)
        with store.lock:
            store.db.close()


def transcript(root, name="درس.txt", content=None):
    path = root / name
    path.write_bytes(content if content is not None else "نص عربي أصلي\r\n".encode("utf-8-sig"))
    return path


def ready(env, kind="transcript"):
    draft = env.draft
    draft.setKind(kind)
    draft.setTemplateId("default")
    draft.setFolderId(env.folder)
    draft.setOutputRoot(str(env.output))
    return draft


def youtube_data(title="قائمة الدروس", prefix="video"):
    return {"title": title, "items": [
        {"video_id": f"{prefix}-{i}", "title": f"درس {i}", "position": i,
         "url": f"https://www.youtube.com/watch?v={prefix}-{i}",
         "available": True, "selected": True}
        for i in (1, 2, 3)
    ]}


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    QCoreApplication.processEvents()
    assert predicate(), "Expected Qt draft update did not arrive"


def test_draft_begins_empty_despite_saved_defaults(source_draft):
    env = source_draft
    draft = env.draft
    assert env.config.data["output_root"]
    assert env.config.data["opencode_templates"]
    assert draft.kind == "youtube"
    assert (draft.templateId, draft.folderId, draft.outputRoot, draft.preset) == ("", "", "", "")
    assert draft.items == []
    assert draft.itemCount == draft.selectedCount == 0
    assert not draft.canEnqueue
    assert draft.validationMessage
    assert draft.fieldErrors == {}  # Empty required controls are neutral until the user confirms.
    assert env.bridge.metaObject().indexOfProperty("sourceDraft") >= 0


def test_batch_resolves_output_alias_without_changing_general_default(source_draft, tmp_path):
    # Native Windows runners may expose TEMP under an 8.3 alias. Compare the
    # actual directory, not the spelling used by the file picker/draft.
    env = source_draft
    draft = env.draft
    original = env.config.data["output_root"]
    chosen = str(env.output.parent) + "/./" + env.output.name
    draft.setKind("transcript")
    draft.setTemplateId("default")
    draft.setFolderId(env.folder)
    draft.setOutputRoot(chosen)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    assert draft.outputRoot == chosen
    assert draft.enqueue()["ok"]
    job = env.store.jobs()[0]
    assert Path(job["folder"]).parent == env.output.resolve()
    assert Path(job["config"]["output_root"]) == env.output.resolve()
    assert env.config.data["output_root"] == original


@pytest.mark.parametrize("missing", ["templateId", "folderId", "outputRoot"])
def test_each_results_field_is_mandatory_before_any_batch(source_draft, tmp_path, missing):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    getattr(draft, "set" + missing[0].upper() + missing[1:])("")
    assert not draft.canEnqueue
    result = draft.enqueue()
    assert not result["ok"] and result["count"] == 0
    assert draft.fieldErrors[missing]
    assert not env.store.jobs()
    assert not list(env.output.iterdir())
    assert draft.itemCount == draft.selectedCount == 1


@pytest.mark.parametrize("invalid", ["template", "folder"])
def test_deleted_required_selection_is_revalidated_at_submit(source_draft, tmp_path, invalid):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    assert draft.canEnqueue
    if invalid == "template":
        env.config.data["opencode_templates"] = []
        env.bridge.settingsChanged.emit()
    else:
        env.store.db.execute("DELETE FROM folders WHERE id=?", (env.folder,))
        env.bridge.foldersChanged.emit()
    assert not draft.canEnqueue
    error_field = "templateId" if invalid == "template" else "folderId"
    assert draft.fieldErrors[error_field]
    draft.setOutputRoot(str(env.output))
    assert draft.fieldErrors[error_field]
    result = draft.enqueue()
    assert not result["ok"]
    assert draft.fieldErrors[error_field]
    assert not env.store.jobs()
    assert draft.itemCount == 1


def test_staging_only_reviews_exact_files_and_deduplicates_canonical_paths(source_draft, tmp_path):
    env = source_draft
    source = transcript(tmp_path, "درس # 1.TXT")
    equivalent = source.parent / "unused" / ".." / source.name
    (source.parent / "unused").mkdir()
    draft = ready(env)
    assert draft.stagePaths([str(source), str(equivalent)], "transcript") == 1
    assert draft.itemCount == draft.selectedCount == 1
    assert draft.items[0]["input_path"] == str(source.resolve())
    assert source.read_bytes().startswith(b"\xef\xbb\xbf")
    assert not env.store.jobs()
    assert not list(env.output.iterdir())
    assert not list(Path(env.config.data["output_root"]).glob("*"))
    assert draft.stagePaths([str(source)], "transcript") == 0
    assert draft.itemCount == 1


def test_folder_staging_uses_natural_order_and_ignores_nested_files(source_draft, tmp_path, monkeypatch):
    env = source_draft
    folder = tmp_path / "transcripts"
    folder.mkdir()
    for name in ("درس10.txt", "درس2.TXT", "درس02.txt", "درس1.txt", "ignore.mp3"):
        transcript(folder, name)
    nested = folder / "nested"
    nested.mkdir()
    transcript(nested)
    draft = ready(env)
    monkeypatch.setattr("waraq.source_draft.QFileDialog.getExistingDirectory", lambda *a: str(folder))
    draft.chooseFolder()
    assert [item["title"] for item in draft.items] == ["درس1", "درس02", "درس2", "درس10"]
    assert draft.selectedCount == 4
    assert not env.store.jobs()
    assert not list(env.output.iterdir())


def test_staging_rejects_invalid_files_but_keeps_valid_review_items(source_draft, tmp_path):
    draft = ready(source_draft)
    valid = transcript(tmp_path)
    empty = transcript(tmp_path, "empty.txt", b"\xef\xbb\xbf \r\n")
    non_utf8 = transcript(tmp_path, "invalid.txt", b"\xff\xfe\x00")
    unsupported = transcript(tmp_path, "unsupported.pdf")
    missing = tmp_path / "missing.txt"
    assert draft.stagePaths([str(p) for p in (empty, valid, non_utf8, unsupported, missing, tmp_path)], "transcript") == 1
    assert [item["input_path"] for item in draft.items] == [str(valid.resolve())]
    assert not source_draft.store.jobs()


def test_drop_stages_only_local_transcripts_in_transcript_mode(source_draft, tmp_path):
    env = source_draft
    source = transcript(tmp_path, "درس # 1.TXT")
    unsupported = transcript(tmp_path, "unsupported.pdf")
    urls = [QUrl.fromLocalFile(str(source)).toString(), QUrl.fromLocalFile(str(source)),
            "https://example.com/lesson.txt", QUrl.fromLocalFile(str(unsupported)).toString()]
    assert env.draft.stageDroppedUrls(urls) == 1
    assert env.draft.kind == "transcript"
    assert env.draft.items[0]["input_path"] == str(source.resolve())
    assert not env.draft.canEnqueue
    assert not env.store.jobs()


def test_modes_preserve_their_items_selection_and_order_independently(source_draft, tmp_path):
    env = source_draft
    draft = ready(env)
    files = [transcript(tmp_path, name) for name in ("first.txt", "second.txt", "third.txt")]
    draft.stagePaths([str(p) for p in files], "transcript")
    draft.selectItem(1, False)
    draft.moveItem(2, -1)
    transcript_items = draft.items
    audio = transcript(tmp_path, "recording.mp3", b"fake audio")
    draft.setKind("audio")
    assert not draft.items
    draft.stagePaths([str(audio)], "audio")
    assert draft.itemCount == draft.selectedCount == 1
    draft.setKind("youtube")
    assert not draft.items
    draft.setKind("transcript")
    assert draft.items == transcript_items
    assert [item["title"] for item in draft.items] == ["first", "third", "second"]
    assert [item["selected"] for item in draft.items] == [True, True, False]
    assert (draft.templateId, draft.folderId, draft.outputRoot) == ("default", env.folder, str(env.output))
    assert not env.store.jobs()


def test_transcript_batch_owns_bytes_and_captures_output_without_changing_global_settings(source_draft, tmp_path):
    env = source_draft
    selected_prompt = "القالب المختار: {transcript_path} إلى {output_path}"
    env.config.data["opencode_templates"].append({"id": "chosen", "name": "قالب الدروس", "text": selected_prompt})
    original_config = env.config.snapshot()
    draft = ready(env)
    draft.setTemplateId("chosen")
    sources = [transcript(tmp_path, name) for name in ("first.txt", "second.txt", "third.txt")]
    contents = {p.stem: p.read_bytes() for p in sources}
    draft.stagePaths([str(p) for p in sources], "transcript")
    draft.selectItem(1, False)
    draft.moveItem(2, -1)
    result = draft.enqueue()
    assert result["ok"] and result["count"] == 2
    jobs = env.store.jobs()
    assert [job["title"] for job in jobs] == ["first", "third"]
    assert len({job["batch"] for job in jobs}) == 1
    assert [job["queue_order"] for job in jobs] == [1, 2]
    for job in jobs:
        assert job["folder_id"] == env.folder
        assert job["config"]["output_root"] == str(env.output)
        assert job["config"]["template_name"] == "قالب الدروس"
        assert job["config"]["prompt"] == selected_prompt
        assert Path(job["folder"]).parent == env.output
        assert Path(job["transcript"]).parent == Path(job["folder"])
        assert Path(job["transcript"]).read_bytes() == contents[job["title"]]
        assert [job["stages"][s]["status"] for s in ("download", "deepgram", "opencode")] == ["skipped", "skipped", "pending"]
    assert env.config.snapshot() == original_config
    for path in sources:
        path.unlink()
    assert Path(jobs[0]["transcript"]).read_bytes() == contents["first"]
    env.config.data["output_root"] = str(tmp_path / "later global output")
    env.config.data["opencode_templates"][1]["text"] = "later template"
    stored = env.store.get(jobs[0]["id"])
    assert stored["config"]["output_root"] == str(env.output)
    assert stored["config"]["prompt"] == selected_prompt


def test_audio_uses_transcription_and_word_and_never_downloads(source_draft, tmp_path):
    env = source_draft
    draft = ready(env, "audio")
    audio = transcript(tmp_path, "lesson.m4a", b"fake audio")
    draft.stagePaths([str(audio)], "audio")
    assert draft.canEnqueue
    assert draft.enqueue()["ok"]
    job = env.store.jobs()[0]
    assert job["audio"] == str(audio.resolve())
    assert job["transcript"] == ""
    assert [job["stages"][s]["status"] for s in ("download", "deepgram", "opencode")] == ["skipped", "pending", "pending"]


def test_success_resets_all_modes_and_required_choices_and_blocks_second_submit(source_draft, tmp_path):
    env = source_draft
    draft = ready(env, "audio")
    draft.stagePaths([str(transcript(tmp_path, "sound.mp3", b"audio"))], "audio")
    draft.setKind("transcript")
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    assert draft.enqueue()["ok"]
    assert len(env.store.jobs()) == 1
    assert (draft.templateId, draft.folderId, draft.outputRoot, draft.preset) == ("", "", "", "")
    for kind in ("youtube", "transcript", "audio"):
        draft.setKind(kind)
        assert draft.items == []
        assert not draft.canEnqueue
    assert not draft.enqueue()["ok"]
    assert len(env.store.jobs()) == 1


def test_file_removed_after_review_refuses_whole_batch_and_preserves_draft(source_draft, tmp_path):
    env = source_draft
    draft = ready(env)
    first = transcript(tmp_path, "first.txt")
    second = transcript(tmp_path, "second.txt")
    draft.stagePaths([str(first), str(second)], "transcript")
    before = draft.items
    second.unlink()
    result = draft.enqueue()
    assert not result["ok"] and result["count"] == 0
    assert not env.store.jobs()
    assert not list(env.output.iterdir())
    assert draft.items == before
    assert draft.templateId == "default" and draft.folderId == env.folder


@pytest.mark.parametrize("failure", ["copy", "database"])
def test_failed_batch_rolls_back_every_job_and_managed_copy_preserving_review(source_draft, tmp_path, monkeypatch, failure):
    env = source_draft
    draft = ready(env)
    first = transcript(tmp_path, "first.txt")
    second = transcript(tmp_path, "second.txt")
    draft.stagePaths([str(first), str(second)], "transcript")
    before = draft.items
    if failure == "database":
        env.store.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON jobs WHEN NEW.title='second' BEGIN SELECT RAISE(ABORT, 'controlled DB failure'); END")
    else:
        original_write = Path.write_bytes
        def fail_second_copy(path, data):
            if path.name == "second.txt" and env.output in path.parents:
                raise OSError("controlled copy failure")
            return original_write(path, data)
        monkeypatch.setattr(Path, "write_bytes", fail_second_copy)
    result = draft.enqueue()
    assert not result["ok"] and result["count"] == 0
    assert not env.store.jobs()
    assert env.store.db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    assert not list(env.output.iterdir())
    assert first.exists() and second.exists()
    assert draft.items == before
    assert not draft.enqueueBusy


def test_output_path_that_became_file_is_rejected_without_losing_review(source_draft, tmp_path):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    env.output.rmdir()
    env.output.write_bytes(b"not a directory")
    result = draft.enqueue()
    assert not result["ok"]
    assert draft.fieldErrors["outputRoot"]
    assert not env.store.jobs()
    assert draft.itemCount == 1


def test_cancelled_file_folder_and_output_dialogs_keep_existing_draft(source_draft, tmp_path, monkeypatch):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    before = draft.items
    monkeypatch.setattr("waraq.source_draft.QFileDialog.getOpenFileNames", lambda *a: ([], ""))
    monkeypatch.setattr("waraq.source_draft.QFileDialog.getExistingDirectory", lambda *a: "")
    draft.chooseFiles()
    draft.chooseFolder()
    draft.chooseOutputFolder()
    assert draft.items == before
    assert (draft.templateId, draft.folderId, draft.outputRoot) == ("default", env.folder, str(env.output))
    assert not env.store.jobs()


@pytest.fixture
def discoveries(source_draft, monkeypatch):
    """Controllable workers exercise the real queued-signal acceptance boundary."""
    calls = {}
    guard = threading.Lock()
    def discover(url, cfg, cancel, refresh):
        call = SimpleNamespace(cancel=cancel, refresh=refresh, release=threading.Event(),
                               thread=threading.current_thread())
        with guard:
            calls[url] = call
        if not call.release.wait(5):
            raise RuntimeError("Controlled discovery was not released")
        if url.endswith("failed"):
            raise ValueError("controlled discovery failure")
        return youtube_data(url.rsplit("/", 1)[-1], url.rsplit("/", 1)[-1])
    monkeypatch.setattr(source_draft.scheduler.youtube, "discover", discover)
    try:
        yield calls
    finally:
        with guard:
            active = list(calls.values())
        for call in active:
            call.release.set()
        for call in active:
            call.thread.join(5)
            assert not call.thread.is_alive(), "Draft discovery thread did not stop"
        QCoreApplication.processEvents()


def load_youtube(env, discoveries, name="lessons"):
    url = "https://www.youtube.com/" + name
    env.draft.discover(url, False)
    wait_until(lambda: url in discoveries)
    discoveries[url].release.set()
    wait_until(lambda: not env.draft.busy)
    return url


@pytest.mark.parametrize("preset,states", [
    ("download", ["pending", "skipped", "skipped"]),
    ("transcript", ["pending", "pending", "skipped"]),
    ("full", ["pending", "pending", "pending"]),
])
def test_youtube_requires_explicit_preset_and_keeps_required_results_settings(source_draft, discoveries, preset, states):
    env = source_draft
    draft = ready(env, "youtube")
    load_youtube(env, discoveries)
    assert draft.selectedCount == 3
    assert draft.preset == "" and not draft.canEnqueue
    draft.setPreset(preset)
    assert draft.canEnqueue
    draft.setTemplateId("")
    assert not draft.canEnqueue  # Even download-only must receive all required selections.
    draft.setTemplateId("default")
    draft.selectItem(1, False)
    draft.moveItem(2, -1)
    assert draft.enqueue()["ok"]
    jobs = env.store.jobs()
    assert [job["video_id"] for job in jobs] == ["lessons-1", "lessons-3"]
    assert [job["position"] for job in jobs] == [1, 3]
    assert [job["queue_order"] for job in jobs] == [1, 2]
    for job in jobs:
        assert [job["stages"][s]["status"] for s in ("download", "deepgram", "opencode")] == states
        assert job["config"]["output_root"] == str(env.output)
        assert job["folder_id"] == env.folder


def test_new_discovery_cancels_old_and_ignores_its_late_success(source_draft, discoveries):
    draft = source_draft.draft
    first = "https://www.youtube.com/first"
    second = "https://www.youtube.com/second"
    draft.discover(first, False)
    wait_until(lambda: first in discoveries)
    draft.discover(second, True)
    wait_until(lambda: second in discoveries)
    assert discoveries[first].cancel.is_set()
    assert discoveries[second].refresh
    discoveries[second].release.set()
    wait_until(lambda: not draft.busy)
    assert draft.title == "second"
    second_items = draft.items
    discoveries[first].release.set()
    discoveries[first].thread.join(3)
    QCoreApplication.processEvents()
    assert draft.title == "second" and draft.items == second_items


def test_cancel_invalidates_result_immediately_and_late_success_cannot_restore_it(source_draft, discoveries):
    draft = source_draft.draft
    url = "https://www.youtube.com/cancelled"
    draft.discover(url, False)
    wait_until(lambda: url in discoveries)
    assert draft.busy
    draft.cancelDiscovery()
    assert discoveries[url].cancel.is_set()
    assert not draft.busy
    discoveries[url].release.set()
    discoveries[url].thread.join(3)
    QCoreApplication.processEvents()
    assert not draft.items and not draft.title and not draft.busy
    assert not source_draft.store.jobs()


def test_late_failed_discovery_cannot_clear_new_success(source_draft, discoveries):
    draft = source_draft.draft
    first = "https://www.youtube.com/failed"
    second = "https://www.youtube.com/current"
    draft.discover(first, False)
    wait_until(lambda: first in discoveries)
    draft.discover(second, False)
    wait_until(lambda: second in discoveries)
    discoveries[second].release.set()
    wait_until(lambda: not draft.busy)
    current = draft.items
    discoveries[first].release.set()
    discoveries[first].thread.join(3)
    QCoreApplication.processEvents()
    assert draft.items == current and draft.title == "current"
    assert not draft.busy


def test_discovery_inflight_prevents_submit_until_explicit_cancel(source_draft, discoveries, tmp_path):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    assert draft.canEnqueue
    url = "https://www.youtube.com/background"
    draft.setKind("youtube")
    draft.discover(url, False)
    wait_until(lambda: url in discoveries)
    draft.setKind("transcript")
    assert draft.busy and not draft.canEnqueue
    assert not draft.enqueue()["ok"]
    assert not env.store.jobs()
    draft.cancelDiscovery()
    assert draft.canEnqueue
    assert draft.enqueue()["ok"]
    discoveries[url].release.set()
    discoveries[url].thread.join(3)
    QCoreApplication.processEvents()
    draft.setKind("youtube")
    assert draft.items == [] and not draft.title
    assert len(env.store.jobs()) == 1


def test_explicit_reset_invalidates_inflight_result_and_empties_every_choice(source_draft, discoveries):
    env = source_draft
    draft = ready(env, "youtube")
    draft.setPreset("full")
    url = "https://www.youtube.com/reset"
    draft.discover(url, False)
    wait_until(lambda: url in discoveries)
    draft.reset()
    assert discoveries[url].cancel.is_set()
    assert not draft.busy
    assert (draft.templateId, draft.folderId, draft.outputRoot, draft.preset) == ("", "", "", "")
    discoveries[url].release.set()
    discoveries[url].thread.join(3)
    QCoreApplication.processEvents()
    assert draft.items == [] and not draft.title


def test_already_queued_old_result_cannot_overwrite_newer_read(source_draft, discoveries):
    draft = source_draft.draft
    first = "https://www.youtube.com/queued-old"
    second = "https://www.youtube.com/newer"
    draft.discover(first, False)
    wait_until(lambda: first in discoveries)
    discoveries[first].release.set()
    discoveries[first].thread.join(3)  # Result is queued, but Qt has not delivered it yet.
    assert not discoveries[first].thread.is_alive()
    draft.discover(second, False)
    wait_until(lambda: second in discoveries)
    QCoreApplication.processEvents()
    assert draft.busy and draft.items == [] and draft.title == ""
    discoveries[second].release.set()
    wait_until(lambda: not draft.busy)
    assert draft.title == "newer"


def test_current_discovery_failure_preserves_previous_review_and_reports_error(source_draft, discoveries):
    env = source_draft
    draft = ready(env, "youtube")
    load_youtube(env, discoveries)
    previous = draft.items
    failed = "https://www.youtube.com/failed"
    draft.discover(failed, True)
    wait_until(lambda: failed in discoveries)
    discoveries[failed].release.set()
    wait_until(lambda: not draft.busy)
    assert draft.items == previous
    assert "controlled discovery failure" in draft.fieldErrors["items"]
    assert not env.store.jobs()


def test_no_selected_items_cannot_create_empty_batch(source_draft, tmp_path):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    draft.selectRange("none")
    assert draft.selectedCount == 0 and not draft.canEnqueue
    assert not draft.enqueue()["ok"]
    assert draft.fieldErrors["items"]
    assert not env.store.jobs()
    assert draft.itemCount == 1


def test_actual_output_write_failure_preserves_draft_without_creating_jobs(source_draft, tmp_path, monkeypatch):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    def denied(*args, **kwargs):
        raise PermissionError("controlled write denial")
    monkeypatch.setattr("waraq.source_draft.tempfile.TemporaryFile", denied)
    result = draft.enqueue()
    assert not result["ok"] and result["count"] == 0
    assert "controlled write denial" in result["message"]
    assert not env.store.jobs()
    assert not list(env.output.iterdir())
    assert draft.itemCount == draft.selectedCount == 1
    assert draft.outputRoot == str(env.output)


def test_editing_youtube_url_cannot_submit_stale_items_until_matching_read(source_draft, discoveries):
    env = source_draft
    draft = ready(env, "youtube")
    loaded_url = load_youtube(env, discoveries)
    draft.setPreset("full")
    assert draft.canEnqueue
    before = draft.items
    draft.setUrl("https://www.youtube.com/different")
    assert draft.items == before  # Preserve the review while preventing a mismatched submission.
    assert not draft.canEnqueue
    assert not draft.enqueue()["ok"]
    assert not env.store.jobs()
    draft.setUrl(loaded_url)
    assert draft.canEnqueue
    assert draft.enqueue()["ok"]
    assert len(env.store.jobs()) == 3


def test_failed_new_url_remains_unsubmittable_after_unrelated_edits(source_draft, discoveries):
    env = source_draft
    draft = ready(env, "youtube")
    load_youtube(env, discoveries)
    draft.setPreset("full")
    failed = "https://www.youtube.com/failed"
    draft.discover(failed, False)
    wait_until(lambda: failed in discoveries)
    discoveries[failed].release.set()
    wait_until(lambda: not draft.busy)
    draft.setTemplateId("default")  # Clearing an error banner must not clear source validity.
    draft.selectRange("all")
    assert draft.itemCount == 3
    assert not draft.canEnqueue
    assert not draft.enqueue()["ok"]
    assert not env.store.jobs()


def test_reentrant_second_confirm_cannot_insert_duplicate_batch(source_draft, tmp_path, monkeypatch):
    env = source_draft
    draft = ready(env)
    draft.stagePaths([str(transcript(tmp_path))], "transcript")
    add = env.store.add
    second_confirmations = []
    def confirm_again(*args, **kwargs):
        assert draft.enqueueBusy
        second_confirmations.append(draft.enqueue())
        draft.setKind("audio")
        draft.reset()
        assert draft.kind == "transcript"  # Busy mutations cannot corrupt the in-flight batch.
        return add(*args, **kwargs)
    monkeypatch.setattr(env.store, "add", confirm_again)
    assert draft.enqueue()["ok"]
    assert len(second_confirmations) == 1
    assert not second_confirmations[0]["ok"]
    assert len(env.store.jobs()) == 1
    assert not draft.enqueueBusy


def test_exposed_review_items_are_copies_with_stable_ids_across_moves(source_draft, tmp_path):
    draft = ready(source_draft)
    draft.stagePaths([str(transcript(tmp_path, name)) for name in ("one.txt", "two.txt")], "transcript")
    initial = draft.items
    original_ids = [row["id"] for row in initial]
    initial[0]["selected"] = False
    initial[0]["input_path"] = "untrusted replacement"
    assert draft.selectedCount == 2
    assert draft.items[0]["input_path"] != "untrusted replacement"
    draft.moveItem(1, -1)
    assert [row["id"] for row in draft.items] == list(reversed(original_ids))


@pytest.mark.parametrize("download,transcription,word", [(False, True, True), (True, False, True)])
def test_invalid_custom_youtube_pipeline_is_blocked_before_submit(source_draft, discoveries, download, transcription, word):
    env = source_draft
    draft = ready(env, "youtube")
    load_youtube(env, discoveries)
    env.config.data.update(download_enabled=download, deepgram_enabled=transcription, opencode_enabled=word)
    draft.setPreset("custom")
    assert not draft.canEnqueue
    result = draft.enqueue()
    assert not result["ok"] and result["count"] == 0
    assert draft.fieldErrors["preset"]
    assert not env.store.jobs()
    assert draft.itemCount == 3


def test_custom_youtube_pipeline_captures_selected_global_stages(source_draft, discoveries):
    env = source_draft
    draft = ready(env, "youtube")
    load_youtube(env, discoveries)
    env.config.data.update(download_enabled=True, deepgram_enabled=True, opencode_enabled=False)
    draft.setPreset("custom")
    assert draft.canEnqueue
    assert draft.enqueue()["ok"]
    for job in env.store.jobs():
        assert [job["stages"][s]["status"] for s in ("download", "deepgram", "opencode")] == ["pending", "pending", "skipped"]


def test_close_cancels_workers_and_ignores_late_callbacks_and_new_work(source_draft, discoveries):
    env = source_draft
    draft = env.draft
    url = "https://www.youtube.com/closing"
    draft.discover(url, False)
    wait_until(lambda: url in discoveries)
    draft.close()
    assert discoveries[url].cancel.is_set()
    assert not draft.busy
    discoveries[url].release.set()
    discoveries[url].thread.join(3)
    QCoreApplication.processEvents()
    assert draft.items == []
    draft.discover("https://www.youtube.com/after-close", False)
    assert not draft.busy
    assert "https://www.youtube.com/after-close" not in discoveries
    assert not draft.enqueue()["ok"]
    assert not env.store.jobs()


def test_output_choices_keep_saved_root_explicit_and_reuse_equivalent_directory_ids(source_draft, tmp_path):
    env = source_draft
    draft = env.draft
    saved = env.config.data["output_root"]
    Path(saved).mkdir(exist_ok=True)
    assert draft.outputChoices[0]["id"] == saved
    assert draft.outputRoot == ""  # Offering the saved choice does not select it.
    equivalent = Path(saved) / ".." / Path(saved).name
    draft.setOutputRoot(str(equivalent))
    assert draft.outputRoot == saved
    assert [row["id"] for row in draft.outputChoices].count(saved) == 1
    draft.setOutputRoot(str(env.output))
    assert {row["id"] for row in draft.outputChoices} == {saved, str(env.output)}
    draft.reset()
    assert draft.outputRoot == ""
    assert {row["id"] for row in draft.outputChoices} == {saved, str(env.output)}
