"""Release test-owned Qt publishers while their application is still alive."""
from PySide6.QtCore import QCoreApplication, QEvent


def dispose_bridge(bridge):
    """Call after the fixture has drained its scheduler/account task senders."""
    bridge.timer.stop()
    bridge.closeAccounts()
    bridge.remote.stop()
    bridge.internet.stop()
    bridge.closeRemoteLibrary()
    bridge._remote_library._pool.shutdown(wait=True, cancel_futures=True)
    bridge.deleteLater()
    # processEvents alone need not deliver DeferredDelete without exec().
    QCoreApplication.sendPostedEvents(bridge, QEvent.Type.DeferredDelete)
