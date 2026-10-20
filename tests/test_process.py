from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import waraq.services as services


class HeldOutput:
    def __init__(self, entered, release, failure=None):
        self.entered, self.release, self.failure = entered, release, failure
        self.closed = False

    def __iter__(self):
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("test output was not released")
        if self.closed:
            raise ValueError("stream was closed before reader finished")
        if self.failure:
            raise self.failure
        yield "final quota and permission notice\n"

    def close(self):
        self.closed = True


class ExitedProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode, self.pid = stdout, returncode, 123

    def poll(self):
        return self.returncode


def test_process_exit_waits_for_readers_last_line(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    stdout = HeldOutput(entered, release)
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: ExitedProcess(stdout))
    logs = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(services.run_process, ["fake"], tmp_path, threading.Event(), 3, logs.append)
        try:
            assert entered.wait(2)
            assert not result.done(), "Process returned before reader drained stdout"
            assert not stdout.closed
        finally:
            release.set()
        assert result.result(timeout=3) == "final quota and permission notice"
    assert logs == ["final quota and permission notice"]
    assert stdout.closed


def test_reader_failure_is_reported_instead_of_silent_process_success(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    release.set()
    stdout = HeldOutput(entered, release, failure=OSError("reader failed"))
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: ExitedProcess(stdout))
    with pytest.raises(services.ServiceError, match="reader failed"):
        services.run_process(["fake"], tmp_path, threading.Event(), 3)
    assert stdout.closed


def test_nonzero_exit_keeps_last_diagnostic(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    release.set()
    stdout = HeldOutput(entered, release)
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: ExitedProcess(stdout, returncode=1))
    with pytest.raises(services.ServiceError, match="final quota and permission notice"):
        services.run_process(["fake"], tmp_path, threading.Event(), 3)


@pytest.mark.parametrize("cancelled", [False, True])
def test_inherited_stdout_does_not_block_cancel_or_timeout(monkeypatch, tmp_path, cancelled):
    entered, release = threading.Event(), threading.Event()
    stdout = HeldOutput(entered, release)
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: ExitedProcess(stdout))
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(services.run_process, ["fake"], tmp_path, cancel, 0.05)
        try:
            assert entered.wait(2)
            if cancelled:
                cancel.set()
            start = time.monotonic()
            with pytest.raises(services.Cancelled if cancelled else services.ServiceError):
                result.result(timeout=2)
            assert time.monotonic() - start < 2
            assert not stdout.closed, "Live inherited stdout was closed from the caller"
        finally:
            release.set()
    deadline = time.monotonic() + 2
    while not stdout.closed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert stdout.closed


def test_real_process_collects_final_unterminated_output(tmp_path):
    output = services.run_process([sys.executable, "-c", "import sys; sys.stdout.write('final without newline')"],
                                  tmp_path, threading.Event(), 3)
    assert output == "final without newline"


def test_cancel_before_start_does_not_launch_process(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: calls.append(True))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(services.Cancelled):
        services.run_process(["fake"], tmp_path, cancel, 3)
    assert not calls
