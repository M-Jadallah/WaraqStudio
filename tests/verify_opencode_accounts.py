"""Opt-in real OpenCode 2 account-process acceptance; localhost fake model only.

Run: python tests/verify_opencode_accounts.py
Does not open OAuth, read existing credentials, change user settings, or make a
paid model call. The subscription gate alone is stubbed because the fake local
provider deliberately has no real ChatGPT account. Gate rejection is unit tested.
"""
import base64
import hashlib
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waraq.config import Config, defaults
from waraq.opencode_runtime import AccountRuntimeManager
from waraq.opencode_usage import VERIFIED_NATIVE_VERSIONS, parse_usage_response
from waraq.services import OpenCode, ServiceError, digest, opencode_version, resolve_opencode, validate_docx
from waraq.scheduler import Scheduler
from waraq.store import Store
from verify_opencode_v2 import CONTENT_TYPES, RELATIONSHIPS


def verify_usage_quota(executable, version):
    """Real native serialization in a new disposable profile; zero inference.

    The private outbound transport is replaced before any synthetic credential
    is installed. Future expiry prevents native OAuth refresh. This verifies
    the tested native contract; it does not claim a live ChatGPT quota check.
    """
    assert version in VERIFIED_NATIVE_VERSIONS, "Usage acceptance requires an explicitly verified native version"
    with tempfile.TemporaryDirectory(prefix="waraq-usage-native-acceptance-") as temporary:
        root = Path(temporary)
        cfg = dict(defaults(root), opencode_executable=str(executable))
        manager = AccountRuntimeManager(SimpleNamespace(home=root, snapshot=lambda: dict(cfg)))
        outbound = []
        now = time.time()
        synthetic = {"rate_limit": {
            "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_at": now + 3600},
            "secondary_window": {"used_percent": 17, "limit_window_seconds": 604800, "reset_at": now + 86400}}}
        def transport(access, account, cancel):
            assert access.startswith("x.") and account == "usage-synthetic-account"
            outbound.append("mocked_usage_get")
            return parse_usage_response(synthetic, now)
        try:
            with patch("waraq.opencode_runtime._usage_https", side_effect=transport):
                account_id = "account_usage_synthetic"
                runtime = manager.ensure(account_id)
                # Initialise while the disposable native account has no OAuth.
                methods = manager.auth_methods(account_id)
                assert {method["id"] for method in methods} == {
                    "chatgpt-token-sharing", "chatgpt-browser", "chatgpt-headless"}
                assert next(method for method in methods if method["id"] == "chatgpt-browser")["usage"]["status"] == "available"
                assert next(method for method in methods if method["id"] == "chatgpt-token-sharing")["usage"]["status"] == "unsupported"
                claims = {"sub": "usage-synthetic-subject", "https://api.openai.com/auth": {
                    "chatgpt_account_id": "usage-synthetic-account"}}
                encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                value = {"type": "oauth", "methodID": "chatgpt-browser", "access": "x." + encoded + ".x",
                         "refresh": "usage-synthetic-refresh", "expires": int((now + 86400) * 1000),
                         "metadata": {"sub": "usage-synthetic-subject", "accountID": "usage-synthetic-account"}}
                runtime.request("POST", "/api/credential", {"integrationID": "openai", "value": value})
                metadata = manager.metadata(account_id)
                assert metadata["identity_verified"] and metadata["authenticated"]
                binding = {"account_id": account_id, "auth_epoch": 1,
                           "identity": hashlib.sha256(metadata["identity"].encode()).hexdigest()}
                usage = manager.read_usage(account_id, binding, threading.Event())
                assert usage["status"] == "available", usage["reason_code"]
                assert usage["five_hour"]["remaining_percent"] == 58
                assert usage["weekly"]["remaining_percent"] == 83
                assert len(outbound) == 1
                assert "synthetic-refresh" not in json.dumps(usage)
                manager.delete_auth(account_id)
                value = dict(value, methodID="chatgpt-token-sharing")
                runtime.request("POST", "/api/credential", {"integrationID": "openai", "value": value})
                usage = manager.read_usage(account_id, binding, threading.Event())
                assert usage["status"] == "unsupported" and usage["reason_code"] == "token_sharing_unsupported"
                assert len(outbound) == 1, "SIWC must not call the legacy quota backend"
                manager.delete_auth(account_id)
        finally:
            manager.close()
        assert runtime.process.poll() is not None
        return {"version": version, "native_usage_contract": "passed", "live_usage_backend": "unverified",
                "synthetic_usage_calls": len(outbound), "token_sharing_usage_calls": 0, "model_calls": 0}


def main():
    executable = resolve_opencode("opencode")
    version = opencode_version(executable)
    assert version.startswith("2."), version
    calls = []
    quota_calls = []
    slow_started, release_slow = threading.Event(), threading.Event()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path.startswith("/v1/"), self.path
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(self.path)
            if self.headers.get("Authorization") == "Bearer local-quota-account":
                quota_calls.append(time.monotonic())
                payload = json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded",
                    "type": "usage_limit_reached", "message": "The usage limit has been reached.",
                    "reset_at": int(time.time()) + 3600}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "3600")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            messages = body.get("messages", [])
            if any("SLOW_ACCEPTANCE" in str(m.get("content", "")) for m in messages):
                slow_started.set()
                release_slow.wait(30)
            tools = {t["function"]["name"]: t["function"] for t in body.get("tools", [])}
            if any(n in tools for n in ("shell", "bash")) and not any(m.get("role") == "tool" for m in messages):
                source = (
                    "from pathlib import Path; import os,json,zipfile; "
                    "assert Path(os.environ['PWD']).resolve()==Path.cwd().resolve(); "
                    "assert 'OPENCODE_PASSWORD' not in os.environ; "
                    "assert 'OPENAI_API_KEY' not in os.environ; "
                    "assert 'OPENCODE_DB' not in os.environ; "
                    "assert os.environ.get('NODE_PATH','').endswith('skill_modules'); "
                    "Path('environment.json').write_text(json.dumps({'cwd':str(Path.cwd()),'node_path':os.environ.get('NODE_PATH')})); "
                    "z=zipfile.ZipFile('001-اختبار.docx','w'); "
                    f"z.writestr('[Content_Types].xml',{CONTENT_TYPES!r}); "
                    f"z.writestr('_rels/.rels',{RELATIONSHIPS!r}); "
                    "z.writestr('word/document.xml','<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>اختبار الحساب المعزول</w:t></w:r></w:p></w:body></w:document>'); z.close()"
                )
                encoded = base64.b64encode(source.encode()).decode()
                expression = f"import base64;exec(base64.b64decode('{encoded}'))"
                command = ("& '" + sys.executable.replace("'", "''") + "' -c \"" + expression + '\"') if os.name == "nt" else shlex.quote(sys.executable) + " -c " + shlex.quote(expression)
                name = next(n for n in ("shell", "bash") if n in tools)
                args = {"command": command, "description": "Create isolated account acceptance document"}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_account_acceptance", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "تم الاختبار المحلي."}, "stop"
            chunks = [{"id": "local-test", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "local-test", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            payload = ("".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="waraq-account-acceptance-") as temporary:
            root = Path(temporary)
            cfg = defaults(root)
            cfg.update(opencode_executable=str(executable), opencode_timeout=60,
                       prompt="أنشئ مستند الاختبار: {output_path} من {transcript_path}.")
            config = SimpleNamespace(home=root, snapshot=lambda: dict(cfg))
            manager = AccountRuntimeManager(config)
            source = root / "تفريغ أصلي.txt"
            source.write_text("اختبار حسابات محلي فقط", encoding="utf-8")
            source_hash = digest(source)
            workers = [{"id": "oc1", "model": "openai/mock", "variant": "high", "agent": "build"},
                       {"id": "oc2", "model": "openai/mock", "variant": "high", "agent": "build"}]
            # Place a fake provider only in newly-created temporary profiles.
            # Never inherit a global provider config or modify the real install.
            for account in ("account_local_A", "account_local_B"):
                profile_config = manager.profile_path(account) / "config" / "opencode"
                profile_config.mkdir(parents=True)
                (profile_config / "opencode.json").write_text(json.dumps({
                    "providers": {"openai": {"env": ["WARAQ_LOCAL_MODEL"],
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                        "models": {"mock": {"limit": {"context": 16000, "output": 2000}, "variants": [{"id": "high"}]}}}},
                    "model": "openai/mock", "snapshots": False}), encoding="utf-8")
            try:
                inherited = {"OPENAI_API_KEY": "must-never-reach-native", "OPENCODE_DB": str(root / "must-not-exist.db"),
                             "OPENCODE_CONFIG_CONTENT": '{"model":"paid/model"}', "WARAQ_LOCAL_MODEL": "local-only"}
                with patch.dict(os.environ, inherited), patch.object(manager, "require_subscription", return_value=None), \
                     patch.object(OpenCode, "prepare_context", return_value=[str(root / "skill_modules")]):
                    service = OpenCode(manager)
                    starts = []
                    def process(index, account="account_local_A", slow=False):
                        run_cfg = dict(cfg, _chatgpt_account_id=account,
                                       _chatgpt_session_started=lambda *args: starts.append((index, args)))
                        if slow:
                            run_cfg["prompt"] += " SLOW_ACCEPTANCE"
                        job = {"transcript": str(source), "folder": str(root / ("مهمة " + str(index))), "title": "اختبار", "position": 1}
                        logs = []
                        return service.process(job, workers[index % 2], run_cfg, "must-be-ignored", threading.Event(), logs.append)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        outputs = list(pool.map(process, (0, 1)))
                    first = manager.ensure("account_local_A")
                    for headers in ({}, {"Authorization": "Basic " + base64.b64encode(b"opencode:wrong-password").decode()}):
                        try:
                            build_opener(ProxyHandler({})).open(Request(first.url + "/api/info", headers=headers), timeout=5)
                        except HTTPError as error:
                            assert error.code == 401, error.code
                        else:
                            raise AssertionError("Native account API accepted an unauthenticated caller")
                    # Exercise the real gate through the real native API despite
                    # the per-instance substitute used by the fake model calls.
                    try:
                        AccountRuntimeManager.require_subscription(manager, "account_local_A")
                    except ServiceError as error:
                        assert error.kind == "account_auth", error.kind
                    else:
                        raise AssertionError("A profile without ChatGPT OAuth passed the subscription gate")
                    assert len({args[1] for _, args in starts}) == 1, "Workers did not share one account process"
                    assert len({args[0] for _, args in starts}) == 2
                    for output in outputs:
                        validate_docx(output)
                        assert (output.parent / "validated.json").exists()
                    second = manager.ensure("account_local_B")
                    assert first.process.pid != second.process.pid and first.url != second.url
                    assert first.profile != second.profile
                    assert not (root / "must-not-exist.db").exists()
                    assert manager.auth_methods("account_local_A"), "Native ChatGPT method discovery failed"
                    assert not manager.metadata("account_local_A")["authenticated"]
                    # Native credential serialization, safe identity extraction,
                    # and account isolation use only a made-up local credential.
                    token_payload = base64.urlsafe_b64encode(json.dumps({"sub": "acceptance-subject"}).encode()).decode().rstrip("=")
                    first.request("POST", "/api/credential", {"integrationID": "openai", "value": {
                        "type": "oauth", "methodID": "chatgpt-local-test", "access": "x." + token_payload + ".x",
                        "refresh": "fake-local-refresh", "expires": int(time.time() * 1000) + 3600000}})
                    metadata = manager.metadata("account_local_A")
                    assert metadata["authenticated"] and metadata["identity_verified"]
                    assert not manager.metadata("account_local_B")["authenticated"]
                    assert "fake-local-refresh" not in json.dumps(metadata)
                    manager.delete_auth("account_local_A")
                    # Cancellation must abort native execution, not just its CLI.
                    cancel = threading.Event()
                    cancelled_starts = []
                    run_cfg = dict(cfg, _chatgpt_account_id="account_local_B",
                                   _chatgpt_session_started=lambda *args: cancelled_starts.append(args))
                    run_cfg["prompt"] += " SLOW_ACCEPTANCE"
                    job = {"transcript": str(source), "folder": str(root / "cancelled"), "title": "اختبار", "position": 1}
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(service.process, job, workers[0], run_cfg, "", cancel, lambda _: None)
                        assert slow_started.wait(15), "Local model did not receive the cancellation test"
                        cancel.set()
                        try:
                            pending.result(timeout=20)
                        except ServiceError as error:
                            assert error.kind == "cancelled", error.kind
                        else:
                            raise AssertionError("Cancellation unexpectedly committed a result")
                    assert second.inactive(cancelled_starts[0][0])
                    assert not list((root / "cancelled").rglob("validated.json"))
                    assert digest(source) == source_hash
                    assert all("/v1/" in path for path in calls)
                    old_pid = first.process.pid
                manager.close()
                assert first.process.poll() is not None and second.process.poll() is not None
                assert not (first.profile / "runtime-owner.json").exists()
                # Real scheduler + real native provider error + real second
                # account execution. A one-hour Retry-After must not delay the
                # explicit subscription-quota failover.
                scheduler_config = Config(root / "scheduler")
                scheduler_config.data.update(cfg, chatgpt_accounts_enabled=True, library_auto=False,
                    opencode_workers=[dict(workers[0], name="Native acceptance", enabled=True, provider="login")])
                scheduler_store = Store(root / "scheduler" / "queue.db")
                scheduler_manager = AccountRuntimeManager(scheduler_config)
                scheduler = Scheduler(scheduler_store, scheduler_config, None, account_runtime=scheduler_manager)
                account_ids = []
                for alias, envname in (("Quota", "WARAQ_LOCAL_MODEL_A"), ("Ready", "WARAQ_LOCAL_MODEL_B")):
                    account = scheduler.accounts.create(alias)
                    account_ids.append(account["id"])
                    scheduler.accounts.record_login_result(account["id"], {"authenticated": True,
                        "identity": "fake-local-" + alias, "identity_verified": True})
                    directory = scheduler_manager.profile_path(account["id"]) / "config" / "opencode"
                    directory.mkdir(parents=True)
                    (directory / "opencode.json").write_text(json.dumps({
                        "providers": {"openai": {"env": [envname],
                            "package": "@opencode/ai/providers/openai-compatible",
                            "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                            "models": {"mock": {"limit": {"context": 16000, "output": 2000}, "variants": [{"id": "high"}]}}}},
                        "model": "openai/mock", "snapshots": False}), encoding="utf-8")
                scheduler_started = time.monotonic()
                try:
                    with patch.dict(os.environ, {"WARAQ_LOCAL_MODEL_A": "local-quota-account", "WARAQ_LOCAL_MODEL_B": "local-ready-account"}), \
                         patch.object(scheduler_manager, "require_subscription", return_value=None), \
                         patch.object(OpenCode, "prepare_context", return_value=[str(root / "skill_modules")]):
                        job_id = scheduler_store.add([{"video_id": "native-failover", "title": "اختبار", "position": 1}],
                            scheduler_config.snapshot(), input_path=str(source))[0]
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            scheduler.tick()
                            stage = next(j for j in scheduler_store.jobs() if j["id"] == job_id)["stages"]["opencode"]
                            # Stage completion precedes publication/finally.
                            # Audit assertions require the lease to be released.
                            if stage["status"] == "completed" and scheduler.idle():
                                break
                            if stage["status"] in ("failed", "blocked", "uncertain"):
                                raise AssertionError("Native failover stage stopped: " + json.dumps(stage, ensure_ascii=False))
                            time.sleep(0.1)
                        assert stage["status"] == "completed" and scheduler.idle(), stage
                        with scheduler_store.lock:
                            attempts = [dict(r) for r in scheduler_store.db.execute(
                                "SELECT account_id,outcome,session_id FROM chatgpt_attempts WHERE job_id=? ORDER BY started", (job_id,))]
                        assert [a["account_id"] for a in attempts] == account_ids, attempts
                        assert [a["outcome"] for a in attempts] == ["rate_limited", "completed"], attempts
                        assert all(a["session_id"] for a in attempts)
                        # OpenCode can issue a title request beside the main
                        # turn. Both must stop promptly despite Retry-After.
                        assert quota_calls and max(quota_calls) - min(quota_calls) < 5, quota_calls
                        job = next(j for j in scheduler_store.jobs() if j["id"] == job_id)
                        validate_docx(job["document"])
                        assert len(list(Path(job["folder"]).rglob("validated.json"))) == 1
                finally:
                    scheduler.shutdown()
                    deadline = time.monotonic() + 20
                    while scheduler.active and time.monotonic() < deadline:
                        time.sleep(0.1)
                    scheduler_manager.close()
                    scheduler_store.db.close()
                print(json.dumps({"status": "passed", "version": version, "local_model_calls": len(calls),
                    "shared_workers": 2, "isolated_accounts": 2, "native_cancel_verified": True,
                    "native_api_auth_verified": True,
                    "native_credential_metadata_verified": True, "native_process_stopped": old_pid,
                    "native_scheduler_quota_failover": True, "quota_retry_after": 3600,
                    "quota_requests_including_title": len(quota_calls),
                    "quota_failover_seconds": round(time.monotonic() - scheduler_started, 2)}))
            finally:
                release_slow.set()
                manager.close()
    finally:
        release_slow.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-only", action="store_true")
    parser.add_argument("--executable", default="opencode", help="Native executable for usage-only acceptance")
    args = parser.parse_args()
    if args.usage_only:
        executable = resolve_opencode(args.executable)
        print(json.dumps(verify_usage_quota(executable, opencode_version(executable))))
    else:
        assert args.executable == "opencode", "Executable option requires --usage-only"
        main()
