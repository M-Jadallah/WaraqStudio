"""Inspect an explicit Codex binary without opening accounts or starting a server.

This is a development feasibility probe, not an application quota reader. Schema
availability cannot establish authenticated access, quota scope, or production
support. A completed probe therefore exits 2 (release gate not passed); operational
errors exit 1. No success path enables rotation or changes Waraq's runtime data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class ProbeError(Exception):
    """Fixed, non-sensitive diagnostic code; never forward child output."""


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def isolated_environment(profile, temporary):
    # Do not inherit tokens, proxies, provider overrides, or the user's CLI home.
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
           if key in os.environ}
    env.update(CODEX_HOME=str(profile), HOME=str(profile), USERPROFILE=str(profile),
               APPDATA=str(profile / "appdata"), LOCALAPPDATA=str(profile / "local"),
               XDG_CONFIG_HOME=str(profile / "config"), XDG_DATA_HOME=str(profile / "data"),
               XDG_STATE_HOME=str(profile / "state"), XDG_CACHE_HOME=str(profile / "cache"),
               TEMP=str(temporary), TMP=str(temporary))
    return env


def run_cli(binary, arguments, temporary, environment):
    try:
        result = subprocess.run(
            [str(binary), *arguments], cwd=temporary, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("cli_timeout") from None
    except OSError:
        raise ProbeError("cli_unavailable") from None
    if result.returncode:
        raise ProbeError("cli_command_failed")
    return result.stdout


def schema_summary(directory):
    root = directory.resolve()
    files = sorted(directory.rglob("*.json"))
    if not files or len(files) > 5000:
        raise ProbeError("schema_bundle_invalid")
    documents, fingerprints, total = {}, {}, 0
    for path in files:
        if not path.resolve().is_relative_to(root):
            raise ProbeError("schema_path_invalid")
        size = path.stat().st_size
        total += size
        if size > 16_000_000 or total > 128_000_000:
            raise ProbeError("schema_bundle_too_large")
        relative = path.relative_to(directory).as_posix()
        fingerprints[relative] = digest(path)
        if path.name in ("ClientRequest.json", "LoginAccountParams.json", "GetAccountRateLimitsResponse.json"):
            if path.name in documents:
                raise ProbeError("schema_target_ambiguous")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise ProbeError("schema_json_invalid") from None
            if not isinstance(document, dict):
                raise ProbeError("schema_json_invalid")
            documents[path.name] = document
    requests = documents.get("ClientRequest.json", {})
    login = documents.get("LoginAccountParams.json", {})
    response = documents.get("GetAccountRateLimitsResponse.json", {})
    external = next((branch for branch in login.get("oneOf", [])
                     if "chatgptAuthTokens" in branch.get("properties", {}).get("type", {}).get("enum", [])), None)
    description = str((external or {}).get("description", "")).upper()
    request_methods = set()

    def collect(value):
        if isinstance(value, dict):
            method = value.get("properties", {}).get("method", {})
            request_methods.update(item for item in method.get("enum", []) if isinstance(item, str))
            if isinstance(method.get("const"), str):
                request_methods.add(method["const"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(requests)
    window = response.get("definitions", {}).get("RateLimitWindow", {}).get("properties", {})
    return {
        "schema_file_count": len(files),
        "schema_bundle_sha256": hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest(),
        "rate_limits_read_present": "account/rateLimits/read" in request_methods,
        "external_tokens_login_present": external is not None,
        "external_tokens_internal_only": "INTERNAL USE ONLY" in description or "DO NOT USE" in description,
        "external_tokens_unstable": "UNSTABLE" in description,
        "quota_response_fields": sorted(response.get("properties", {})),
        "quota_window_fields": sorted(window),
    }


def repository_summary(root):
    names = (
        "engine/vendor/lib/constants.ts", "engine/vendor/lib/auth/auth.ts",
        "engine/vendor/lib/auth/scopes.ts", "engine/src/rpc.js",
        "scripts/prepare_embedded_engine.py",
        "engine/vendor/lib/codex-usage.ts", "engine/vendor/lib/request/fetch-helpers.ts",
    )
    sources = {name: (root / name).read_text(encoding="utf-8") for name in names}
    base = re.search(r'export const CODEX_BASE_URL\s*=\s*"([^"]+)"', sources[names[0]])
    scopes = re.search(r"REQUIRED_OAUTH_SCOPES\s*=\s*\[(.*?)\]", sources[names[2]], re.S)
    usage = re.search(r"fetch\(`\$\{CODEX_BASE_URL\}([^`]+)`", sources[names[5]])
    return {
        "source_sha256": {name: digest(root / name) for name in names},
        "codex_backend_base_url": base.group(1) if base else None,
        "codex_usage_path": usage.group(1) if usage else None,
        "responses_route_rewritten_for_codex": "URL_PATHS.CODEX_RESPONSES" in sources[names[6]],
        "fixed_oauth_client_id": bool(re.search(r'export const CLIENT_ID\s*=\s*"', sources[names[1]])),
        "requested_oauth_scopes": re.findall(r'"([A-Za-z0-9_.]+)"', scopes.group(1)) if scopes else [],
        "packager_contains_codex_app_server_pin": bool(re.search(r"^CODEX_(?:CLI_)?VERSION\s*=", sources[names[4]], re.M)),
        "credential_records_inspected": False,
    }


def probe(binary, root):
    if not binary.is_absolute() or not binary.is_file():
        raise ProbeError("explicit_absolute_binary_required")
    binary = binary.resolve()
    binary_hash = digest(binary)
    with tempfile.TemporaryDirectory(prefix="waraq-quota-protocol-") as temporary:
        work = Path(temporary).resolve()
        # TemporaryDirectory recursively cleans only this generated directory.
        if work.parent != Path(tempfile.gettempdir()).resolve():
            raise ProbeError("temporary_path_invalid")
        profile = work / "profile"
        profile.mkdir()
        environment = isolated_environment(profile, work)
        version_bytes = run_cli(binary, ["--version"], work, environment)
        version = version_bytes.decode("utf-8", errors="replace").strip()
        if not re.fullmatch(r"codex-cli [0-9][0-9A-Za-z.+-]{0,100}", version):
            raise ProbeError("cli_version_invalid")
        summaries = {}
        for experimental in (False, True):
            # Omitting --experimental does not establish API stability/support.
            target = work / ("experimental" if experimental else "default")
            args = ["-c", 'cli_auth_credentials_store="ephemeral"', "app-server", "generate-json-schema",
                    "--out", str(target)]
            if experimental:
                args.append("--experimental")
            run_cli(binary, args, work, environment)
            summaries["experimental" if experimental else "default"] = schema_summary(target)
        if list(profile.rglob("auth.json")):
            raise ProbeError("unexpected_auth_file")
        if digest(binary) != binary_hash:
            raise ProbeError("binary_changed_during_probe")
    return {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "offline_schema_inspection_only",
        "binary": {"version": version, "sha256": binary_hash},
        "protocol": summaries,
        "repository": repository_summary(root),
        "account_login_attempted": False,
        "authenticated_quota_read_attempted": False,
        "inference_attempted": False,
        "auth_file_created": False,
        "release_gate": {
            "status": "not_passed",
            "runtime_feature_implemented": False,
            "next_phase_permitted": False,
            "not_verified": ["production_support", "authenticated_account_binding", "inference_quota_scope",
                             "renewal_and_credential_isolation", "permitted_rotation_integration"],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True, help="Explicit absolute executable; never searches PATH")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file (contains no account data)")
    args = parser.parse_args(argv)
    try:
        report = probe(args.codex, Path(__file__).resolve().parents[1])
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 2
    except ProbeError as exc:
        print(json.dumps({"error": str(exc), "release_gate": {"status": "not_passed"}}))
        return 1
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        print(json.dumps({"error": "probe_io_or_schema_failed", "release_gate": {"status": "not_passed"}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
