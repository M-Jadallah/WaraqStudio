"""Cold-launch the built Windows app with unique test credentials and temp data."""
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waraq.config import Config
from waraq.store import Store


def main():
    root = Path(__file__).resolve().parents[1]
    executable = root / "Windows/WaraqStudio/WaraqStudio.exe"
    evidence = root / "output/ui-redesign/bundle"
    evidence.mkdir(parents=True, exist_ok=True)
    bundled_qml = executable.parent / "_internal/waraq/qml"
    checked = 0
    for source in (root / "waraq/qml").rglob("*"):
        if source.is_file():
            target = bundled_qml / source.relative_to(root / "waraq/qml")
            assert target.is_file(), target
            assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest(), f"Stale bundle resource: {target}"
            checked += 1
    with tempfile.TemporaryDirectory(prefix="waraq-bundle-", ignore_cleanup_errors=True) as temp:
        home = Path(temp)
        config = Config(home)
        # Unique worker ids ensure no credential target belonging to a user is read.
        credential_prefix = "bundle" + uuid.uuid4().hex[:12]
        for kind in ("deepgram_workers", "opencode_workers"):
            for index, worker in enumerate(config.data[kind]):
                worker["id"] = f"{credential_prefix}{'dg' if kind == 'deepgram_workers' else 'oc'}{index}"
        config.data["workers_normalised"] = True
        config.save(config.data)
        store = Store(home / "tasks.sqlite3")
        store.add([{"title": f"درس اختباري {i} — معاينة واجهة ورق", "video_id": f"bundle{i}", "position": i, "url": "https://example.invalid/demo"} for i in range(1, 5)], config.snapshot())
        store.db.close()

        def run(*arguments, timeout=60):
            result = subprocess.run([str(executable), "--data-dir", str(home), *arguments], capture_output=True, text=True, timeout=timeout)
            assert result.returncode == 0, (arguments, result.returncode, result.stderr)

        # A cold frozen bundle validates every embedded-runtime file before
        # starting its private engine; permit that bounded integrity pass.
        run("--check-environment", str(evidence / "environment.json"), timeout=180)
        environment = json.loads((evidence / "environment.json").read_text(encoding="utf-8"))
        assert environment.get("embedded_integrity") is True and environment.get("embedded_engine_ready") is True, environment
        run("--smoke")
        for theme in ("dark", "light"):
            config.save({**config.snapshot(), "theme": theme})
            for page in range(6):
                screenshot = evidence / f"{theme}-page-{page}.png"
                run("--page", str(page), "--screenshot", str(screenshot))
                assert screenshot.is_file() and screenshot.stat().st_size > 10000, screenshot
        log = (home / "interface.log").read_text(encoding="utf-8") if (home / "interface.log").exists() else ""
        (evidence / "interface.log").write_text(log, encoding="utf-8")
        failures = [line for line in log.splitlines() if any(term in line.lower() for term in ("referenceerror", "typeerror", "cannot", "unable", "binding loop", "failed to load", "recursive rearrange"))]
        assert not failures, failures
    report = {"matching_qml_resources": checked, "environment_check": True, "smoke": True, "page_launches": 12, "qml_errors": 0, "user_data_used": False}
    (evidence / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Bundled Windows app passed:", report)


if __name__ == "__main__":
    main()
