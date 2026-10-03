import pytest

from scheduler_test_support import SchedulerHarness


@pytest.fixture
def scheduler_harness():
    with SchedulerHarness() as harness:
        yield harness
