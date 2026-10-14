"""Opt-in native V2 acceptance check with a local fake model; no paid requests.

Run: python tests/verify_opencode_v2.py
Exercises real discovery, private servers, model listing, worker credentials,
tool execution, Arabic paths, and the DOCX commit contract.
"""

import base64
import json
import os
import shlex
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from waraq.catalog import opencode_models
from waraq.config import defaults
from waraq.services import OpenCode, ServiceError, opencode_version, resolve_opencode, validate_docx


CONTENT_TYPES = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
RELATIONSHIPS = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'


def main():
    executable = resolve_opencode("opencode")
    version = opencode_version(executable)
    assert version.startswith("2."), f"Expected OpenCode 2, got {version}"
    calls = []
    tools_seen = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            credential = self.headers.get("Authorization")
            assert credential in ("Bearer local-smoke-key-oc1", "Bearer local-smoke-key-oc2")
            calls.append((self.path, credential))
            tools = {t["function"]["name"]: t["function"] for t in body.get("tools", [])}
            tools_seen.append(sorted(tools))
            tool_reply = any(m.get("role") == "tool" for m in body.get("messages", []))
            if any(n in tools for n in ("shell", "bash")) and not tool_reply:
                # The fake model asks the real OpenCode shell tool to create a
                # tiny editable DOCX in cwd; the provider never writes the file.
                source = (
                    "from pathlib import Path; import zipfile; "
                    "p=Path('001-اختبار.docx'); "
                    "z=zipfile.ZipFile(p,'w'); "
                    f"z.writestr('[Content_Types].xml',{CONTENT_TYPES!r}); "
                    f"z.writestr('_rels/.rels',{RELATIONSHIPS!r}); "
                    "z.writestr('word/document.xml','<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>اختبار OpenCode 2</w:t></w:r></w:p></w:body></w:document>'); "
                    "z.close()"
                )
                encoded = base64.b64encode(source.encode()).decode()
                expression = f"import base64;exec(base64.b64decode('{encoded}'))"
                if os.name == "nt":
                    command = "& '" + sys.executable.replace("'", "''") + "' -c \"" + expression + '\"'
                else:
                    command = shlex.quote(sys.executable) + " -c " + shlex.quote(expression)
                name = next(n for n in ("shell", "bash") if n in tools)
                arguments = {"command": command, "description": "Create acceptance-test DOCX"}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_local_smoke",
                         "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "تم الاختبار."}
                finish = "stop"
            chunks = [
                {"id": "chatcmpl-local", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "mock", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-local", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "mock", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            payload = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
            data = (payload + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="waraq-v2-") as temp:
            root = Path(temp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "opencode.json").write_text(json.dumps({
                "providers": {"localtest": {
                    "env": ["WARAQ_LOCAL_SMOKE_KEY"],
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                    "models": {"mock": {"limit": {"context": 16000, "output": 2000}, "variants": [{"id": "high"}]}},
                }},
                "model": "localtest/mock", "snapshots": False,
            }), encoding="utf-8")
            env = {"XDG_CONFIG_HOME": str(root / "config-home"), "OPENCODE_CONFIG_DIR": str(config_dir),
                   "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CACHE_HOME": str(root / "cache"), "OPENCODE_TEST_HOME": str(root / "home"),
                   "PWD": str(root / "wrong inherited directory"), "WARAQ_LOCAL_SMOKE_KEY": "local-smoke-key"}
            with patch.dict(os.environ, env):
                models = opencode_models(str(executable), str(root))
                assert "localtest/mock" in models, [m for m in models if m.startswith("localtest/")]
                source = root / "تفريغ أصلي.txt"
                source.write_text("مادة عربية للاختبار المحلي.", encoding="utf-8")
                cfg = defaults(root)
                cfg["opencode_executable"] = str(executable)
                cfg["opencode_timeout"] = 60
                cfg["prompt"] = "أنشئ مستند الاختبار في {output_path} من {transcript_path}."
                cfg["opencode_providers"] = [{"id": "localtest", "name": "Local smoke model",
                                              "key_env": "WARAQ_LOCAL_SMOKE_KEY", "isolated": True}]
                # The check tests the CLI contract, independent of installed
                # lesson-formatting skills or the user's own providers.
                def process(wid):
                    worker = {"id": wid, "provider": "localtest", "model": "mock", "variant": "high", "agent": "build"}
                    job = {"transcript": str(source), "folder": str(root / ("مجلد مهمة مع مسافات " + wid)),
                           "title": "اختبار", "position": 1}
                    logs = []
                    try:
                        output = OpenCode().process(job, worker, cfg, "local-smoke-key-" + wid, threading.Event(), logs.append)
                    except Exception:
                        for line in logs:
                            if line.startswith("{"):
                                event = json.loads(line)
                                state = event.get("part", {}).get("state", {})
                                if state.get("output"):
                                    print(state["output"][:1000])
                            else:
                                print(line)
                        raise
                    report = validate_docx(output)
                    assert (output.parent / "validated.json").is_file()
                    assert (root / "opencode-workers" / wid / "data" / "opencode" / "opencode.db").is_file()
                    return report
                with patch.object(OpenCode, "prepare_context", return_value=[]), ThreadPoolExecutor(max_workers=2) as pool:
                    reports = list(pool.map(process, ("oc1", "oc2")))
                assert source.read_text(encoding="utf-8") == "مادة عربية للاختبار المحلي."
                for wid in ("oc1", "oc2"):
                    assert sum(key == "Bearer local-smoke-key-" + wid for _, key in calls) >= 2, (calls, tools_seen)
                # --auto must retain explicit denies and a failed tool must
                # never be committed as a successful Word task.
                config_path = config_dir / "opencode.json"
                restricted = json.loads(config_path.read_text(encoding="utf-8"))
                restricted["permissions"] = [{"action": "shell", "resource": "*", "effect": "deny"}]
                config_path.write_text(json.dumps(restricted), encoding="utf-8")
                denied_job = {"transcript": str(source), "folder": str(root / "denied job"), "title": "اختبار", "position": 1}
                denied_worker = {"id": "oc1", "provider": "localtest", "model": "mock", "variant": "high", "agent": "build"}
                with patch.object(OpenCode, "prepare_context", return_value=[]):
                    try:
                        OpenCode().process(denied_job, denied_worker, cfg, "local-smoke-key-oc1", threading.Event(), lambda _: None)
                    except ServiceError as error:
                        assert "لم ينشئ OpenCode" in str(error), str(error)
                    else:
                        raise AssertionError("Explicit shell deny was bypassed")
                assert not list((root / "denied job").rglob("validated.json"))
                assert not list((root / "denied job").rglob("*.docx"))
                assert any("execute" in tools and "shell" not in tools and "bash" not in tools for tools in tools_seen)
                print(json.dumps({"version": version, "local_model_calls": len(calls),
                                  "parallel_workers": 2, "explicit_deny_preserved": True,
                                  "docx_characters": [r["characters"] for r in reports], "status": "passed"}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
