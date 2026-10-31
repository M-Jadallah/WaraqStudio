"""Native embedded account panel acceptance with a fake service; no login/network."""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QLocale, QMetaObject, QObject, QPointF, QUrl, Qt, qInstallMessageHandler
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
            row['can_export'] = True
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
            assert not find('accountAutoSwitchEnabled').property('enabled')
            assert find('accountRotationSupportMessage').property('text')
            threshold = find('accountSwitchRemainingPercent')
            for value in (0, 20.5, 100):
                threshold.setProperty('text', QLocale().toString(float(value), 'g', 15))
                assert threshold.property('acceptableInput'), value
                assert QMetaObject.invokeMethod(threshold, 'editingFinished')
                QTest.qWait(20)
                assert config.snapshot()['accounts_switch_remaining_percent'] == float(value)
            assert config.snapshot()['accounts_auto_switch_enabled'] is False
            assert find('accountRotationHundredHint').property('visible')
            threshold.setProperty('text', QLocale().toString(20.0, 'g', 15))
            assert QMetaObject.invokeMethod(threshold, 'editingFinished')
            QTest.qWait(20)
            assert not find('accountRotationHundredHint').property('visible')
            assert find('accountMigrationNotice').property('visible')
            bridge.acknowledgeAccountMigration()
            pump(app, lambda: not bridge.accountBusy)
            assert not find('accountMigrationNotice').property('visible')
            for scale in (1.0, 1.35):
                config.save({'ui_scale': scale})
                bridge.settingsChanged.emit()
                QTest.qWait(100)
                assert abs(root.property('uiScale') - scale) < 0.01
                listing = find('accountList')
                listing.setProperty('contentY', min(100, max(0, listing.property('contentHeight') - listing.property('height'))))
                before = listing.property('contentY')
                resets = QSignalSpy(bridge.accountsModel.modelReset)
                accounts.rows[0]['usage']['fiveHour']['remainingPercent'] = 37
                bridge._publish_accounts()
                QTest.qWait(80)
                assert resets.count() == 0
                assert abs(listing.property('contentY') - before) < 2
            if len(sys.argv) > 1:
                evidence = Path(sys.argv[1]).resolve()
                evidence.mkdir(parents=True, exist_ok=True)
                root.setWidth(1060)
                root.setHeight(720)
                root.setProperty('toastText', '')
                for theme in ('dark', 'light'):
                    for scale in (1.0, 1.4):
                        config.save({'ui_scale': scale, 'theme': theme})
                        bridge.settingsChanged.emit()
                        QTest.qWait(150)
                        image = root.grabWindow()
                        assert not image.isNull()
                        assert image.save(str(evidence / f'accounts-{theme}-{scale}.png'))
                config.save({'ui_scale': 1.0, 'theme': 'dark'})
                bridge.settingsChanged.emit()
                QTest.qWait(100)
            ident = accounts.rows[0]['id']
            with patch('waraq.bridge.QDesktopServices.openUrl', return_value=True):
                bridge.loginAccount(ident)
                pump(app, lambda: not bridge.accountLoginChoice.get('loading'))
                assert find('accountLoginMethodDialog').property('visible')
                bridge.chooseAccountLoginMethod(ident, 'browser')
                pump(app, lambda: not bridge.accountBusy)
            assert accounts.row(ident)['login_state'] == 'authenticated'
            dialog = find('accountTransferDialog')
            assert find('accountExportButton').property('enabled')
            assert QMetaObject.invokeMethod(dialog, 'openExport')
            QTest.qWait(80)
            assert dialog.property('visible') and dialog.property('selectedCount') == 0
            select_all = find('accountTransferSelectAll')
            select_all.setProperty('checked', True)
            assert QMetaObject.invokeMethod(select_all, 'clicked')
            assert dialog.property('selectedCount') == 12
            password, confirmation = find('accountTransferPassword'), find('accountTransferConfirmation')
            password.setProperty('text', '  كلمة مرور الملف  ')
            confirmation.setProperty('text', 'different-password')
            assert not find('accountTransferSubmitButton').property('enabled')
            confirmation.setProperty('text', '  كلمة مرور الملف  ')
            assert find('accountTransferSubmitButton').property('enabled')
            visibility = find('accountTransferShowPassword')
            visibility.setProperty('checked', True)
            visibility.setProperty('checked', False)
            import threading
            accounts.gate = threading.Event()
            with patch('waraq.bridge.QFileDialog.getSaveFileName', return_value=(str(home/'backup.waraq-accounts'), '')):
                assert QMetaObject.invokeMethod(dialog, 'submit')
            assert not password.property('text') and not confirmation.property('text')
            assert bridge.accountTransfer['status'] == 'working'
            assert not find('accountTransferCloseButton').property('enabled')
            QTest.keyClick(root, Qt.Key.Key_Escape)
            assert dialog.property('visible')
            accounts.gate.set()
            pump(app, lambda: not bridge.accountBusy)
            assert bridge.accountTransfer['exported'] == 12
            assert find('accountTransferMessage').property('text')
            dialog.close()
            accounts.gate = None
            assert QMetaObject.invokeMethod(dialog, 'openExport')
            if len(sys.argv) > 1:
                root.setWidth(1060); root.setHeight(720)
                for theme in ('dark', 'light'):
                    for scale in (1.0, 1.4):
                        config.save({'ui_scale': scale, 'theme': theme})
                        bridge.settingsChanged.emit()
                        QTest.qWait(150)
                        assert dialog.property('width') <= root.width()
                        assert dialog.property('height') <= root.height()
                        assert root.grabWindow().save(str(evidence / f'account-transfer-export-{theme}-{scale}.png'))
                assert QMetaObject.invokeMethod(password, 'forceActiveFocus')
                QTest.keyClick(root, Qt.Key.Key_Return)
                QTest.qWait(80)
                assert confirmation.property('activeFocus')
                field_top = confirmation.mapToScene(QPointF()).y()
                body_top = find('accountTransferBody').mapToScene(QPointF()).y()
                footer_top = find('accountTransferCloseButton').mapToScene(QPointF()).y()
                assert field_top >= body_top and field_top + confirmation.height() < footer_top
                assert root.grabWindow().save(str(evidence / 'account-transfer-keyboard-1.4.png'))
            password.setProperty('text', 'temporary-password')
            confirmation.setProperty('text', 'temporary-password')
            QTest.keyClick(root, Qt.Key.Key_Escape)
            QTest.qWait(100)
            assert not dialog.property('visible') and not password.property('text') and not confirmation.property('text')
            with patch('waraq.bridge.QFileDialog.getOpenFileName', return_value=('', '')):
                assert QMetaObject.invokeMethod(dialog, 'openImport')
                assert not dialog.property('visible')
            with patch('waraq.bridge.QFileDialog.getOpenFileName', return_value=(str(home/'backup.waraq-accounts'), '')):
                assert QMetaObject.invokeMethod(dialog, 'openImport')
            QTest.qWait(80)
            assert dialog.property('visible') and not confirmation.property('visible')
            from waraq.embedded_accounts import AccountEngineError
            accounts.transfer_error = AccountEngineError('TRANSFER_DECRYPT_FAILED')
            password.setProperty('text', 'wrong-password-123')
            assert QMetaObject.invokeMethod(dialog, 'submit')
            pump(app, lambda: not bridge.accountBusy)
            assert find('accountTransferMessage').property('text') == 'كلمة المرور غير صحيحة أو الملف تالف'
            assert not password.property('text')
            if len(sys.argv) > 1:
                for theme in ('dark', 'light'):
                    for scale in (1.0, 1.4):
                        config.save({'ui_scale': scale, 'theme': theme})
                        bridge.settingsChanged.emit(); QTest.qWait(150)
                        assert root.grabWindow().save(str(evidence / f'account-transfer-import-{theme}-{scale}.png'))
            del accounts.transfer_error
            password.setProperty('text', 'password-12345')
            assert QMetaObject.invokeMethod(dialog, 'submit')
            pump(app, lambda: not bridge.accountBusy)
            assert bridge.accountTransfer['imported'] == 1 and bridge.accountTransfer['skipped'] == 1
            dialog.close()
            accounts.status['state'] = 'unavailable'
            bridge._publish_accounts()
            assert find('accountEngineRecoverButton').property('visible')
            bad = [message for message in messages if any(text in message for text in ('ReferenceError', 'TypeError', 'Cannot assign', 'Binding loop'))]
            assert not bad, bad
            print('Embedded account UI, encrypted transfer, password cleanup, health, migration, OAuth and scroll checks passed')
        finally:
            root.close()
            engine.deleteLater()
            dispose_bridge(bridge)
            app.processEvents()
            store.db.close()


if __name__ == '__main__':
    main()
