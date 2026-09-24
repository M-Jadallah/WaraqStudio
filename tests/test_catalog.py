"""The two pickers consume only supported Deepgram STT and local CLI models."""

from waraq import catalog


def test_deepgram_catalog_groups_languages_and_excludes_non_batch(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"stt": [
                {"canonical_name": "nova-3-general", "batch": True, "languages": ["ar", "en"]},
                {"canonical_name": "nova-3-general", "batch": True, "languages": ["ar-JO"]},
                {"canonical_name": "flux-general", "batch": False, "languages": ["en"]},
            ], "tts": [{"canonical_name": "aura-2", "batch": True}]}

    monkeypatch.setattr(catalog.requests, "get", lambda url, timeout: Response())
    assert catalog.deepgram_models() == [
        {"id": "nova-3-general", "languages": ["ar", "ar-JO", "en"]}
    ]


def test_opencode_catalog_reads_cli_output_without_shell(monkeypatch, tmp_path):
    class Result:
        stdout = "openai/gpt-6-sol\nother text\nopenai/gpt-6-sol\nopencode/big-pickle\n"

    seen = {}
    monkeypatch.setattr(catalog, "resolve_opencode", lambda path: tmp_path / "opencode.exe")

    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return Result()

    monkeypatch.setattr(catalog.subprocess, "run", run)
    assert catalog.opencode_models("opencode", str(tmp_path)) == ["openai/gpt-6-sol", "opencode/big-pickle"]
    assert seen["command"] == [str(tmp_path / "opencode.exe"), "models"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert seen["kwargs"]["timeout"] == 20
