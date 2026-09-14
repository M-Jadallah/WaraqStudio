"""Reproducible Windows folder build. No API keys or runtime databases are included."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
work = root.parents[1] / "work" / "pyinstaller"
work.mkdir(parents=True, exist_ok=True)
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "WaraqStudio",
           "--distpath", str(root / "Windows"), "--workpath", str(work / "build"),
           "--specpath", str(work), "--add-data", str(root / "waraq/qml") + ";waraq/qml",
           "--add-data", str(root / "waraq/icon.svg") + ";waraq", "--icon", str(root / "waraq/icon.ico"),
           "--collect-all", "yt_dlp_ejs", "--collect-all", "imageio_ffmpeg",
           "--collect-submodules", "yt_dlp_plugins", "--hidden-import", "keyring.backends.Windows",
           "--hidden-import", "PySide6.QtQuick", "--hidden-import", "PySide6.QtQuickControls2"]
for module in ["numpy", "pandas", "scipy", "matplotlib", "IPython", "pytest", "tkinter", "PySide6.QtWebEngineCore",
               "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets", "PySide6.QtPdf", "PySide6.Qt3DCore"]:
    command += ["--exclude-module", module]
command.append(str(root / "main.py"))
raise SystemExit(subprocess.call(command, cwd=root))
