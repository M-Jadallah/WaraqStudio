import pytest

from scheduler_test_support import SchedulerHarness


@pytest.fixture(scope="session")
def qt_app():
    from PySide6.QtCore import QCoreApplication
    # Keep the native application alive across every QObject-based fixture.
    # Per-test references can otherwise disappear while queued events survive.
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def scheduler_harness():
    with SchedulerHarness() as harness:
        yield harness
