"""Exercise the real release shell script with isolated Git/GitHub commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILT = "a" * 40
NEWER = "b" * 40
TAG = "v1.0.0.56"

DRIVER = r'''
import json, os, sys
from pathlib import Path
path = Path(os.environ["PUBLISH_MOCK_STATE"])
state = json.loads(path.read_text())
tool, args = sys.argv[1], sys.argv[2:]
state["calls"].append([tool] + args)
code = 0
if tool == "git":
    assert args[:4] == ["ls-remote", "--exit-code", "--tags", "origin"]
    if state.get("git_error"):
        code = 128
    elif state.get("tag"):
        if state.get("annotated"):
            print("c" * 40, args[4], sep="\t")
            print(state["tag"], args[5], sep="\t")
        else:
            print(state["tag"], args[4], sep="\t")
    else:
        code = 2
elif args[0] == "api":
    if "/git/ref/heads/main" in args[1]:
        state["head_reads"] = state.get("head_reads", 0) + 1
        if state.get("advance_on_read") == state["head_reads"]:
            state["main"] = "b" * 40
        if state.get("head_error"):
            print("gh: HTTP 503", file=sys.stderr)
            code = 1
        else:
            print(state["main"])
    else:
        assert args[:3] == ["api", "--method", "POST"]
        assert args[3] == "repos/example/project/git/refs"
        assert args[4:] == ["-f", "ref=refs/tags/" + os.environ["TAG"], "-f", "sha=" + os.environ["GITHUB_SHA"]]
        if state.get("advance_on_create"):
            state["main"] = "b" * 40
        if state.get("create_error"):
            print("gh: Resource not accessible by integration (HTTP 403)", file=sys.stderr)
            code = 1
        else:
            state["tag"] = os.environ["GITHUB_SHA"]
elif args[:2] == ["release", "view"]:
    code = 0 if state.get("release") else 1
elif args[:2] in (["release", "create"], ["release", "edit"]):
    if state.get("release_error"):
        code = 1
    else:
        state["release"] = True
else:
    assert args[:2] == ["release", "upload"]
path.write_text(json.dumps(state))
sys.exit(code)
'''


def bash_executable():
    if os.name == "nt" and shutil.which("git"):
        for parent in list(Path(shutil.which("git")).resolve().parents)[:3]:
            candidate = parent / "bin/bash.exe"
            if candidate.is_file():
                return str(candidate)
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("Release shell acceptance requires Bash")
    return executable


@pytest.fixture
def publish(tmp_path):
    state_path = tmp_path / "state.json"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    functions = tmp_path / "commands.sh"
    functions.write_text('git() { "$PUBLISH_MOCK_PYTHON" "$PUBLISH_MOCK_DRIVER" git "$@"; }\n'
                         'gh() { "$PUBLISH_MOCK_PYTHON" "$PUBLISH_MOCK_DRIVER" gh "$@"; }\n', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("portable.zip", "app-setup.exe", "gateway.sha256", "SHA256SUMS", f"WaraqStudio-{TAG}-provenance.json"):
        (dist / name).write_text("synthetic release asset", encoding="utf-8")
    (tmp_path / "RELEASE_NOTES.md").write_text("Synthetic release", encoding="utf-8")

    def run(overrides=None, ref="refs/heads/main"):
        state_path.write_text(json.dumps({"main": BUILT, "tag": None, "release": False, "calls": [], **(overrides or {})}))
        env = {**os.environ, "BASH_ENV": functions.as_posix(), "PUBLISH_MOCK_DRIVER": driver.as_posix(),
               "PUBLISH_MOCK_PYTHON": Path(sys.executable).as_posix(), "PUBLISH_MOCK_STATE": state_path.as_posix(),
               "GITHUB_REPOSITORY": "example/project", "GITHUB_SHA": BUILT, "GITHUB_REF": ref, "TAG": TAG,
               "GITHUB_STEP_SUMMARY": (tmp_path / "summary.md").as_posix(), "GH_TOKEN": "test-only"}
        result = subprocess.run([bash_executable(), (ROOT / "scripts/publish_release.sh").as_posix()],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        return result, json.loads(state_path.read_text())

    return run


def writes(state):
    return [call for call in state["calls"] if call[:4] == ["gh", "api", "--method", "POST"]
            or call[:3] in (["gh", "release", "create"], ["gh", "release", "edit"], ["gh", "release", "upload"])]


def test_superseded_main_skips_without_remote_writes(publish):
    result, state = publish({"main": NEWER})
    assert result.returncode == 0, result.stderr
    assert "::notice::Skipping superseded main release" in result.stdout
    assert not writes(state)
    assert state["tag"] is None and not state["release"]


def test_current_main_creates_exact_tag_and_uploads_provenance(publish):
    result, state = publish()
    assert result.returncode == 0, result.stderr
    assert state["tag"] == BUILT and state["release"]
    create = next(call for call in state["calls"] if call[:3] == ["gh", "release", "create"])
    assert create[3:7] == [TAG, "--verify-tag", "--target", BUILT]
    upload = next(call for call in state["calls"] if call[:3] == ["gh", "release", "upload"])
    assert set(upload[4:-1]) == {"dist/portable.zip", "dist/app-setup.exe", "dist/gateway.sha256", "dist/SHA256SUMS", f"dist/WaraqStudio-{TAG}-provenance.json"}
    assert upload[-1] == "--clobber"


def test_existing_release_edits_without_recreating_tag(publish):
    result, state = publish({"tag": BUILT, "release": True})
    assert result.returncode == 0, result.stderr
    assert not any(call[:4] == ["gh", "api", "--method", "POST"] for call in state["calls"])
    assert any(call[:3] == ["gh", "release", "edit"] for call in state["calls"])


@pytest.mark.parametrize("annotated", [False, True])
def test_conflicting_existing_tag_is_never_moved(publish, annotated):
    result, state = publish({"tag": NEWER, "annotated": annotated})
    assert result.returncode != 0
    assert "Existing tags will not be changed" in result.stdout
    assert state["tag"] == NEWER and not writes(state)


def test_matching_annotated_tag_resolves_to_pinned_commit(publish):
    result, state = publish({"tag": BUILT, "annotated": True})
    assert result.returncode == 0, result.stderr
    assert state["tag"] == BUILT and state["release"]


@pytest.mark.parametrize("error", ["head_error", "git_error"])
def test_read_failure_is_not_missing_tag_or_successful_skip(publish, error):
    result, state = publish({error: True})
    assert result.returncode != 0
    assert not writes(state) and state["tag"] is None


def test_invalid_main_response_fails_closed(publish):
    result, state = publish({"main": "null"})
    assert result.returncode != 0
    assert "invalid main commit" in result.stdout
    assert not writes(state)


def test_main_advance_during_ref_creation_skips_observed_403(publish):
    result, state = publish({"advance_on_create": True, "create_error": True})
    assert result.returncode == 0, result.stderr
    assert "HTTP 403" in result.stderr
    assert "Skipping superseded main release" in result.stdout
    assert not any(call[:2] == ["gh", "release"] for call in state["calls"])
    assert state["tag"] is None


def test_real_ref_permission_failure_still_fails(publish):
    result, state = publish({"create_error": True})
    assert result.returncode != 0
    assert "check the GitHub token permissions" in result.stdout
    assert not state["release"]


def test_main_advance_after_tag_creation_does_not_publish_release(publish):
    result, state = publish({"advance_on_read": 2})
    assert result.returncode == 0, result.stderr
    assert state["tag"] == BUILT
    assert not state["release"]
    assert not any(call[:2] == ["gh", "release"] for call in state["calls"])


def test_explicit_tag_release_keeps_its_original_commit(publish):
    result, state = publish({"main": NEWER, "tag": BUILT}, ref="refs/tags/" + TAG)
    assert result.returncode == 0, result.stderr
    assert not state.get("head_reads")
    create = next(call for call in state["calls"] if call[:3] == ["gh", "release", "create"])
    assert create[3:7] == [TAG, "--verify-tag", "--target", BUILT]


def test_release_creation_failure_does_not_upload_assets(publish):
    result, state = publish({"release_error": True})
    assert result.returncode != 0
    assert not any(call[:3] == ["gh", "release", "upload"] for call in state["calls"])


def test_release_workflow_uses_guarded_publisher_with_builtin_token():
    source = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    step = source.split("      - name: Publish GitHub Release\n", 1)[1].split("      - name:", 1)[0]
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "run: bash scripts/publish_release.sh" in step
