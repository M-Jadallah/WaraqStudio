"""Feasibility diagnostics must never use or expose real account credentials."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import probe_account_rotation_protocol as probe


def write_schema(root, *, external=True, restricted=True):
    root.mkdir(parents=True, exist_ok=True)
    documents = {
        "ClientRequest.json": {"oneOf": [{"properties": {"method": {"enum": ["account/rateLimits/read"]}}}]},
        "LoginAccountParams.json": {"oneOf": []},
        "GetAccountRateLimitsResponse.json": {
            "properties": {"rateLimits": {}, "rateLimitsByLimitId": {}},
            "definitions": {"RateLimitWindow": {"properties": {"usedPercent": {}, "resetsAt": {}}}},
        },
    }
    if external:
        documents["LoginAccountParams.json"]["oneOf"].append({
            "description": "[UNSTABLE] FOR OPENAI INTERNAL USE ONLY - DO NOT USE." if restricted else "Experimental",
            "properties": {"type": {"enum": ["chatgptAuthTokens"]}},
        })
    for name, document in documents.items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")


def test_child_environment_excludes_credentials_proxies_and_real_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    monkeypatch.setenv("WARAQ_ACCOUNTS_CAPABILITY", "secret-capability")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret-proxy.example")
    monkeypatch.setenv("CODEX_HOME", "real-private-profile")
    profile = tmp_path / "isolated"
    env = probe.isolated_environment(profile, tmp_path)
    assert not {"OPENAI_API_KEY", "WARAQ_ACCOUNTS_CAPABILITY", "HTTPS_PROXY", "PATH"} & env.keys()
    assert "secret" not in json.dumps(env)
    assert env["CODEX_HOME"] == env["HOME"] == env["USERPROFILE"] == str(profile)


def test_schema_presence_does_not_erase_internal_only_restriction(tmp_path):
    write_schema(tmp_path)
    report = probe.schema_summary(tmp_path)
    assert report["rate_limits_read_present"]
    assert report["external_tokens_login_present"]
    assert report["external_tokens_internal_only"]
    assert report["external_tokens_unstable"]
    assert len(report["schema_bundle_sha256"]) == 64


def test_missing_external_login_remains_absent(tmp_path):
    write_schema(tmp_path, external=False)
    assert not probe.schema_summary(tmp_path)["external_tokens_login_present"]


def test_probe_never_starts_server_or_authenticates(tmp_path, monkeypatch):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"synthetic executable")
    commands = []

    def run(binary, arguments, temporary, environment):
        commands.append(arguments)
        assert Path(environment["CODEX_HOME"]).is_relative_to(temporary)
        if arguments == ["--version"]:
            return b"codex-cli 1.0.0-test\n"
        assert "generate-json-schema" in arguments
        assert 'cli_auth_credentials_store="ephemeral"' in arguments
        assert not any(item in arguments for item in ("login", "turn/start", "--listen"))
        target = Path(arguments[arguments.index("--out") + 1])
        write_schema(target, external="--experimental" in arguments)
        return b"untrusted output must never be forwarded"

    monkeypatch.setattr(probe, "run_cli", run)
    report = probe.probe(binary, Path(__file__).resolve().parents[1])
    assert len(commands) == 3
    assert not report["protocol"]["default"]["external_tokens_login_present"]
    assert report["protocol"]["experimental"]["external_tokens_internal_only"]
    assert report["release_gate"]["status"] == "not_passed"
    assert not report["release_gate"]["next_phase_permitted"]
    assert not report["authenticated_quota_read_attempted"]
    assert "untrusted" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_child_errors_never_print_raw_output_or_exception(tmp_path, monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("secret-command", 45, output=b"secret-output", stderr=b"secret-error")
        return SimpleNamespace(returncode=1, stdout=b"secret-output", stderr=b"secret-error")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(probe.ProbeError) as error:
        probe.run_cli(tmp_path / "binary", [], tmp_path, {})
    assert str(error.value) == ("cli_timeout" if failure == "timeout" else "cli_command_failed")
    assert "secret" not in str(error.value)


def test_relative_binary_is_rejected_before_execution(monkeypatch):
    monkeypatch.setattr(probe, "run_cli", lambda *args: pytest.fail("relative executable was run"))
    with pytest.raises(probe.ProbeError, match="explicit_absolute_binary_required"):
        probe.probe(Path("codex.exe"), Path.cwd())


def test_cli_successful_inspection_still_exits_with_unpassed_gate(tmp_path, monkeypatch, capsys):
    report = {"release_gate": {"status": "not_passed", "next_phase_permitted": False}}
    monkeypatch.setattr(probe, "probe", lambda *args: report)
    output = tmp_path / "evidence.json"
    assert probe.main(["--codex", str(tmp_path / "codex.exe"), "--output", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert json.loads(capsys.readouterr().out) == report


def test_accidental_auth_file_aborts_inspection(tmp_path, monkeypatch):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"synthetic executable")

    def run(binary, arguments, temporary, environment):
        if arguments == ["--version"]:
            return b"codex-cli 1.0.0-test"
        (Path(environment["CODEX_HOME"]) / "auth.json").write_text("synthetic-private-data")
        write_schema(Path(arguments[arguments.index("--out") + 1]))
        return b""

    monkeypatch.setattr(probe, "run_cli", run)
    with pytest.raises(probe.ProbeError, match="unexpected_auth_file"):
        probe.probe(binary, Path(__file__).resolve().parents[1])


def test_changed_binary_cannot_produce_valid_provenance(tmp_path, monkeypatch):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"original")

    def run(binary, arguments, temporary, environment):
        if arguments == ["--version"]:
            return b"codex-cli 1.0.0-test"
        write_schema(Path(arguments[arguments.index("--out") + 1]))
        binary.write_bytes(b"replacement")
        return b""

    monkeypatch.setattr(probe, "run_cli", run)
    with pytest.raises(probe.ProbeError, match="binary_changed_during_probe"):
        probe.probe(binary, Path(__file__).resolve().parents[1])


def test_malformed_schema_diagnostics_do_not_echo_content(tmp_path):
    write_schema(tmp_path)
    (tmp_path / "ClientRequest.json").write_text("not-json synthetic-private-data")
    with pytest.raises(probe.ProbeError) as error:
        probe.schema_summary(tmp_path)
    assert str(error.value) == "schema_json_invalid"


def test_invalid_future_schema_shape_fails_closed_without_traceback(tmp_path, monkeypatch, capsys):
    def invalid(*args):
        raise AttributeError("synthetic-private-data")
    monkeypatch.setattr(probe, "probe", invalid)
    assert probe.main(["--codex", str(tmp_path / "codex.exe")]) == 1
    printed = capsys.readouterr()
    assert not printed.err
    assert "synthetic-private-data" not in printed.out
    assert json.loads(printed.out)["release_gate"]["status"] == "not_passed"
