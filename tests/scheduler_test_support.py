"""Drain real scheduler threads before a test releases its database/files."""
from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from unittest.mock import patch

import waraq.scheduler as scheduler_module
from waraq.scheduler import Scheduler


class SchedulerHarness:
    def __init__(self):
        self.schedulers = []
        self.threads = []
        self.patches = ExitStack()

    def __enter__(self):
        original_init = Scheduler.__init__
        original_threading = scheduler_module.threading
        harness = self

        def initialize(scheduler, *args, **kwargs):
            original_init(scheduler, *args, **kwargs)
            harness.schedulers.append(scheduler)

        class TrackedThreading:
            def Thread(self, *args, **kwargs):
                thread = original_threading.Thread(*args, **kwargs)
                harness.threads.append(thread)
                return thread

            def __getattr__(self, name):
                return getattr(original_threading, name)

        self.patches.enter_context(patch.object(Scheduler, "__init__", initialize))
        self.patches.enter_context(patch.object(scheduler_module, "threading", TrackedThreading()))
        return self

    def shutdown_and_join(self, timeout=10):
        for scheduler in self.schedulers:
            scheduler.shutdown()
        deadline = time.monotonic() + timeout

        def join(thread):
            if thread is None or thread.ident is None:
                return
            thread.join(max(0, deadline - time.monotonic()))
            assert not thread.is_alive(), f"Scheduler thread {thread.name!r} did not stop before database cleanup"

        # Finish dispatch first: a tick already in progress may still start a
        # worker after shutdown is signalled. Then all worker handles are known.
        for scheduler in self.schedulers:
            join(scheduler.thread)
        for thread in self.threads:
            join(thread)
        # Some concurrency tests inject synthetic active entries. Only real
        # threads own database work, so those entries must not block cleanup.

    def __exit__(self, *exc):
        try:
            self.shutdown_and_join()
        finally:
            self.patches.close()
