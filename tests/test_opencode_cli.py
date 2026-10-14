"""OpenCode install discovery and the V1/V2 CLI boundary, without paid calls."""

import json
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from waraq import services
from waraq.config import defaults


@pytest.fixture
def install(monkeypatch, tmp_path):
    prefix = tmp_path / "profile with spaces" / "npm"
    prefix.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(prefix.parent))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(services.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(services.shutil, "which", lambda _: None)
    return prefix


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"native executable stub")
    return path


@pytest.mark.parametrize("extension", [".cmd", ".ps1", ".bat"])
@pytest.mark.parametrize("package", ["@opencode/cli", "opencode-ai"])
def test_resolves_npm_shim_without_running_it(install, monkeypatch, extension, package):
    shim = touch(install / ("opencode" + extension))
    binary = touch(install / "node_modules" / package / "bin" / "opencode.exe")
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: pytest.fail("shim was executed"))
    assert services.resolve_opencode(str(shim)) == binary.resolve()


def test_resolves_local_npm_bin_and_hoisted_platform_dependency(install):
    shim = touch(install / "node_modules" / ".bin" / "opencode.cmd")
    binary = touch(install / "node_modules" / "@opencode" / "cli-windows-x64-baseline" / "bin" / "opencode.exe")
    assert services.resolve_opencode(str(shim)) == binary.resolve()


def test_shim_respects_package_when_both_generations_are_present(install):
    shim = touch(install / "opencode.cmd")
    shim.write_text(r'"%dp0%\node_modules\opencode-ai\bin\opencode" %*', encoding="utf-8")
    binary = touch(install / "node_modules" / "opencode-ai" / "bin" / "opencode.exe")
    touch(install / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    assert services.resolve_opencode(str(shim)) == binary.resolve()


def test_arm64_platform_fallback_does_not_select_x64(install, monkeypatch):
    shim = touch(install / "opencode.cmd")
    monkeypatch.setattr(services.platform, "machine", lambda: "ARM64")
    touch(install / "node_modules" / "@opencode" / "cli-windows-x64" / "bin" / "opencode.exe")
    binary = touch(install / "node_modules" / "@opencode" / "cli-windows-arm64" / "bin" / "opencode.exe")
    assert services.resolve_opencode(str(shim)) == binary.resolve()


def test_automatic_discovery_survives_stale_gui_path(install):
    binary = touch(install / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    assert services.resolve_opencode("opencode") == binary.resolve()
    assert services.resolve_opencode("opencode.exe") == binary.resolve()
    assert services.resolve_opencode("") == binary.resolve()


def test_automatic_discovery_uses_path_shim_first(install, monkeypatch):
    other = install / "custom prefix"
    shim = touch(other / "opencode.cmd")
    binary = touch(other / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    touch(install / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    monkeypatch.setattr(services.shutil, "which", lambda _: str(shim))
    assert services.resolve_opencode("opencode") == binary.resolve()


def test_explicit_missing_or_broken_path_never_switches_install(install):
    touch(install / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    broken = touch(install / "old installation" / "opencode.cmd")
    for path in (broken, install / "missing.exe"):
        with pytest.raises(services.ServiceError) as exc:
            services.resolve_opencode(str(path))
        assert exc.value.kind == "blocked"


def test_manual_path_expands_environment_and_quotes(install, monkeypatch):
    binary = touch(install / "standalone" / "opencode.exe")
    monkeypatch.setenv("WARAQ_TEST_CLI", str(binary.parent))
    assert services.resolve_opencode('"${WARAQ_TEST_CLI}/opencode.exe"') == binary.resolve()


def test_missing_install_has_actionable_message(install):
    with pytest.raises(services.ServiceError, match="@opencode/cli") as exc:
        services.resolve_opencode("opencode")
    assert exc.value.kind == "blocked"


def test_version_probe_is_shared_and_invalidated_on_upgrade(install, monkeypatch):
    binary = touch(install / "opencode.exe")
    calls = []
    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="opencode v2.0.27\n")
    monkeypatch.setattr(services.subprocess, "run", probe)
    services._opencode_version_cached.cache_clear()
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(lambda _: services.opencode_version(binary), range(5))) == ["2.0.27"] * 5
    assert len(calls) == 1
    assert calls[0][0] == [str(binary.resolve()), "--version"]
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert not calls[0][1].get("shell")
    binary.write_bytes(b"upgraded executable with a different size")
    services.opencode_version(binary)
    assert len(calls) == 2


@pytest.mark.parametrize("output", ["not OpenCode", "opencode v3.0.0"])
def test_unknown_version_is_blocked(install, monkeypatch, output):
    binary = touch(install / "opencode.exe")
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(services.ServiceError) as exc:
        services.opencode_version(binary)
    assert exc.value.kind == "blocked"


@pytest.mark.parametrize("error", [OSError("cannot execute"), subprocess.TimeoutExpired("opencode", 10)])
def test_probe_failures_are_actionable(install, monkeypatch, error):
    binary = touch(install / "opencode.exe")
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(services.subprocess, "run", fail)
    with pytest.raises(services.ServiceError) as exc:
        services.opencode_version(binary)
    assert exc.value.kind == "blocked"
    assert "إصداره" in str(exc.value)


@pytest.mark.parametrize("version", ["1.19.0", "2.0.27"])
@pytest.mark.parametrize("variant", ["", "high"])
def test_version_specific_run_contract(version, variant, tmp_path):
    worker = {"agent": "build", "variant": variant}
    prompt = "--هذا نص عربي وليس خيار تشغيل\nسطر آخر"
    command = services.opencode_run_command(Path("opencode.exe"), version, worker,
                                            "openai/model", tmp_path, prompt)
    assert command[-2:] == ["--", prompt]
    model = command[command.index("--model") + 1]
    if version.startswith("2."):
        assert "--standalone" in command
        assert "--auto" in command
        assert "--dir" not in command
        assert "--variant" not in command
        assert model == "openai/model" + ("#high" if variant else "")
    else:
        assert "--standalone" not in command
        assert "--auto" not in command
        assert command[command.index("--dir") + 1] == str(tmp_path)
        assert ("--variant" in command) == bool(variant)
        assert model == "openai/model"


def test_existing_model_variant_is_preserved_or_replaced(tmp_path):
    for variant, expected in (("", "openai/model#low"), ("high", "openai/model#high")):
        command = services.opencode_run_command("opencode.exe", "2.0.27", {"agent": "build", "variant": variant},
                                                "openai/model#low", tmp_path, "prompt")
        assert command[command.index("--model") + 1] == expected


@pytest.mark.parametrize("provider", ["login", "openrouter"])
def test_v2_process_uses_task_directory_and_worker_environment(tmp_path, monkeypatch, provider):
    source = tmp_path / "source.txt"
    source.write_text("تفريغ عربي", encoding="utf-8")
    original = source.read_bytes()
    job = {"transcript": str(source), "folder": str(tmp_path / "job"), "position": 1, "title": "درس"}
    cfg = defaults(tmp_path)
    worker = {"id": "oc1", "provider": provider, "model": "openai/model" if provider == "login" else "model",
              "variant": "high", "agent": "build"}
    monkeypatch.setenv("PWD", str(tmp_path / "wrong directory"))
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "shared.db"))
    monkeypatch.setattr(services, "resolve_opencode", lambda _: Path("opencode.exe"))
    monkeypatch.setattr(services, "opencode_version", lambda _: "2.0.27")
    monkeypatch.setattr(services.OpenCode, "prepare_context", lambda *a: [])
    def run(command, cwd, cancel, timeout, log, env):
        assert env["PWD"] == str(cwd.resolve())
        assert "--standalone" in command
        assert "--dir" not in command
        if provider == "openrouter":
            assert "OPENCODE_DB" not in env
            assert env["OPENROUTER_API_KEY"] == "test-worker-key"
            assert env["XDG_DATA_HOME"] == str(tmp_path / "opencode-workers" / "oc1" / "data")
        else:
            assert "XDG_DATA_HOME" not in env or env["XDG_DATA_HOME"] == services.os.environ.get("XDG_DATA_HOME")
            assert env["OPENCODE_DB"] == str(tmp_path / "shared.db")
        assert (cwd / "source.txt").read_bytes() == original
        with zipfile.ZipFile(cwd / "001-درس.docx", "w") as document:
            document.writestr("[Content_Types].xml", "<Types/>")
            document.writestr("_rels/.rels", "<Relationships/>")
            document.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>مستند عربي</w:t></w:r></w:p></w:body></w:document>')
    monkeypatch.setattr(services, "run_process", run)
    output = services.OpenCode().process(job, worker, cfg, "test-worker-key", threading.Event(), lambda _: None)
    assert output.is_file()
    assert source.read_bytes() == original
    assert (output.parent / "validated.json").is_file()


@pytest.mark.parametrize("error,limited", [
    ({"type": "api", "message": "Too many requests", "status": 429}, True),
    ({"type": "api", "message": "Service unavailable", "status": 503}, False),
    ({"type": "api", "message": "Unauthorized", "status": 401}, False),
    ({"data": {"statusCode": 429}}, True),
])
def test_quota_classification_accepts_both_error_formats(error, limited):
    assert services.limit_reached(json.dumps({"type": "error", "error": error})) is limited
