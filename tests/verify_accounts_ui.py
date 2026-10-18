"""Native embedded account panel acceptance with a fake service; no login/network."""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QUrl, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from test_account_bridge import EmbeddedAccountsDouble, pump, phone_usage_fixtures
from qt_test_support import dispose_bridge
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.store import Store


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    with tempfile.TemporaryDirectory(prefix="waraq-embedded-ui-") as temporary:
        home = Path(temporary)
        config, store = Config(home), Store(home / 'ui.db')
        accounts = EmbeddedAccountsDouble()
        for index in range(12):
            row = accounts.create('حساب ' + str(index + 1))
            row['usage'] = phone_usage_fixtures()[index % 4]['usage']
        scheduler = SimpleNamespace(accounts=accounts, running=False, workers=lambda: [], idle=lambda: True,
                                    disabled_workers={}, forget_keys=lambda: None, shutdown=lambda: None, library=SimpleNamespace())
        scheduler.recheck_account = lambda ident: accounts.recheck(ident) if ident else accounts.refresh()
        bridge = Bridge(store, config, None, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty('backend', bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'waraq/qml/Main.qml')))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        root.setProperty('page', 2)
        QTest.qWait(150)
        def find(name):
            item = root.findChild(QObject, name)
            assert item is not None, name
            return item
        try:
            assert find('accountEngineHealth').property('text')
            assert find('accountMigrationNotice').property('visible')
            bridge.acknowledgeAccountMigration()
            pump(app, lambda: not bridge.accountBusy)
            assert not find('accountMigrationNotice').property('visible')
            for scale in (1.0, 1.35):
                root.setProperty('uiScale', scale)
                QTest.qWait(100)
                listing = find('accountList')
                listing.setProperty('contentY', min(100, max(0, listing.property('contentHeight') - listing.property('height'))))
                before = listing.property('contentY')
                resets = QSignalSpy(bridge.accountsModel.modelReset)
                accounts.rows[0]['usage']['fiveHour']['remainingPercent'] = 37
                bridge._publish_accounts()
                QTest.qWait(80)
                assert resets.count() == 0
                assert abs(listing.property('contentY') - before) < 2
            ident = accounts.rows[0]['id']
            with patch('waraq.bridge.QDesktopServices.openUrl', return_value=True):
                bridge.loginAccount(ident)
                pump(app, lambda: not bridge.accountLoginChoice.get('loading'))
                assert find('accountLoginMethodDialog').property('visible')
                bridge.chooseAccountLoginMethod(ident, 'browser')
                pump(app, lambda: not bridge.accountBusy)
            assert accounts.row(ident)['login_state'] == 'authenticated'
            accounts.status['state'] = 'unavailable'
            bridge._publish_accounts()
            assert find('accountEngineRecoverButton').property('visible')
            bad = [message for message in messages if any(text in message for text in ('ReferenceError', 'TypeError', 'Cannot assign', 'Binding loop'))]
            assert not bad, bad
            print('Embedded account UI, health, migration, OAuth and scroll checks passed')
        finally:
            root.close()
            engine.deleteLater()
            dispose_bridge(bridge)
            app.processEvents()
            store.db.close()


if __name__ == '__main__':
    main()
