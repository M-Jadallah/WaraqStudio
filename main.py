from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer, QUrl, qInstallMessageHandler
from PySide6.QtGui import QFont, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMessageBox

from waraq.bridge import Bridge
from waraq import __version__
from waraq.config import Config, Secrets
from waraq.scheduler import Scheduler
from waraq.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--screenshot")
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--check-environment")
    args = parser.parse_args()
    QQuickStyle.setStyle("Basic")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Waraq Studio")
    app.setOrganizationName("Waraq")
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "waraq" / "icon.svg")))
    home = Path(args.data_dir) if args.data_dir else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WaraqStudio"
    home.mkdir(parents=True, exist_ok=True)
    def qt_message(kind, context, message):
        with (home / "interface.log").open("a", encoding="utf-8") as f:
            f.write(message + "\n")
        if sys.stderr:
            print(message, file=sys.stderr)
    qInstallMessageHandler(qt_message)
    lock = QLockFile(str(home / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "Waraq Studio", "توجد نسخة تعمل بالفعل.")
        return 1
    try:
        config = Config(home)
    except Exception:
        QMessageBox.critical(None, "تعذر فتح ورق",
                             "تعذرت قراءة إعدادات التطبيق. احتفظ بنسخة من الملف ثم راجع صيغته:\n" + str(home / "settings.json"))
        lock.unlock()
        return 3
    if args.check_environment:
        import json
        import shutil
        import imageio_ffmpeg
        import yt_dlp
        import yt_dlp_ejs
        import keyring
        import openpyxl
        import requests
        from waraq.services import resolve_opencode
        report = {"version": __version__, "ffmpeg": str(imageio_ffmpeg.get_ffmpeg_exe()), "yt_dlp": yt_dlp.version.__version__,
                  "keyring": type(keyring.get_keyring()).__name__, "node": shutil.which("node"),
                  "requests": requests.__version__, "openpyxl": openpyxl.__version__, "ejs": True}
        report["ffmpeg_exists"] = Path(report["ffmpeg"]).is_file()
        try:
            report["opencode"] = str(resolve_opencode(config.data["opencode_executable"]))
        except Exception as exc:
            report["opencode_error"] = str(exc)
        Path(args.check_environment).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lock.unlock()
        return 0
    try:
        store = Store(home / "tasks.sqlite3")
        store.recover()
    except Exception:
        QMessageBox.critical(None, "تعذر فتح ورق",
                             "تعذر فتح قاعدة المهام. تحقق من المساحة وصلاحيات مجلد البيانات، واحتفظ بنسخة من القاعدة قبل إصلاحها:\n" + str(home / "tasks.sqlite3"))
        lock.unlock()
        return 4
    secrets = Secrets()
    scheduler = Scheduler(store, config, secrets)
    bridge = Bridge(store, config, secrets, scheduler)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", bridge)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "waraq" / "qml" / "Main.qml")))
    if not engine.rootObjects():
        return 2
    root = engine.rootObjects()[0]
    root.setProperty("page", args.page)
    app.aboutToQuit.connect(scheduler.shutdown)
    if args.screenshot:
        def capture():
            try:
                root.grabWindow().save(str(Path(args.screenshot).resolve()))
            finally:
                app.quit()
        QTimer.singleShot(1800, capture)
    elif args.smoke:
        QTimer.singleShot(1200, app.quit)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
