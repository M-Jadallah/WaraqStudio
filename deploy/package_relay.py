"""Create the gateway deployment archive from an explicit, secret-free allowlist."""
from pathlib import Path
import hashlib
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    files = [root / "compose.relay.yaml", root / ".dockerignore",
             root / "deploy/relay/Dockerfile", root / "deploy/relay/.env.example",
             root / "deploy/relay/README_AR.md", root / "deploy/relay/COOLIFY_SETUP_AR.md", root / "relay/requirements.txt",
             root / "waraq/icon.svg"]
    files.extend(sorted((root / "relay").glob("*.py")))
    files.extend(sorted((root / "deploy/relay").glob("*.py")))
    files.extend(root / "waraq/mobile" / name for name in ("index.html", "app.js", "style.css"))
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(path)
    target = root / "dist/WaraqGateway-Coolify.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"{target} ({len(files)} files, {target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
