"""Reproducible Windows folder build. No API keys or runtime databases are included."""
import subprocess
import shutil
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
status = subprocess.call(command, cwd=root)
if status == 0:
    # PyInstaller may put Python's older VC runtime in _internal first. Qt's
    # PySide6 wheel needs the newer copies shipped beside its DLLs.
    internal = root / "Windows" / "WaraqStudio" / "_internal"
    for name in ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
        source = internal / "PySide6" / name
        if source.is_file():
            shutil.copy2(source, internal / name)
    # QtCore links to Windows' unversioned ICU exports. A third-party ICU 78
    # DLL sometimes collected at the bundle root shadows System32/icuuc.dll
    # but only exports version-suffixed names, making QtCore fail to import.
    (internal / "icuuc.dll").unlink(missing_ok=True)
raise SystemExit(status)
