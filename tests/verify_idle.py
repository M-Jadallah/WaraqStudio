"""Bounded native idle/reopen acceptance. Temporary data; no OAuth or paid work.

The default run verifies deterministic startup/hash/SQL counts. --performance
also samples this process and its descendants with Windows process counters.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import faulthandler
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ProcessTreeMeter:
    """Keep handles so CPU spent by an exiting owned descendant is counted."""

    def __init__(self):
        self.handles = {}
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.psapi = ctypes.WinDLL('psapi', use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4

    def discover(self):
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        self.kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        self.kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        relations = {}
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            more = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                relations[entry.th32ProcessID] = entry.th32ParentProcessID
                more = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        owned = {os.getpid()}
        for _ in range(len(relations)):
            new = {pid for pid, parent in relations.items() if parent in owned}
            if new <= owned:
                break
            owned.update(new)
        for pid in owned - self.handles.keys():
            handle = self.kernel.OpenProcess(0x1000 | 0x10, False, pid)
            if handle:
                self.handles[pid] = handle

    def snapshot(self):
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('ReadOperations', 'WriteOperations', 'OtherOperations', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Memory(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                        ('peakWorking', 'working', 'peakPaged', 'paged', 'peakNonpaged', 'nonpaged', 'pagefile', 'peakPagefile', 'private')]
        self.kernel.GetProcessIoCounters.argtypes = (wintypes.HANDLE, ctypes.POINTER(IO))
        self.psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD)
        self.discover()
        result = dict(cpu_seconds=0.0, read_bytes=0, write_bytes=0, working_bytes=0, private_bytes=0,
                      app_cpu_seconds=0.0, app_working_bytes=0, app_private_bytes=0)
        for pid, handle in self.handles.items():
            clocks = [wintypes.FILETIME() for _ in range(4)]
            if self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in clocks)):
                cpu = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in clocks[2:]) / 10_000_000
                result['cpu_seconds'] += cpu
                if pid == os.getpid():
                    result['app_cpu_seconds'] = cpu
            io, memory = IO(), Memory()
            memory.cb = ctypes.sizeof(memory)
            if self.kernel.GetProcessIoCounters(handle, ctypes.byref(io)):
                result['read_bytes'] += io.ReadBytes
                result['write_bytes'] += io.WriteBytes
            if self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
                result['working_bytes'] += memory.working
                result['private_bytes'] += memory.private
                if pid == os.getpid():
                    result['app_working_bytes'] = memory.working
                    result['app_private_bytes'] = memory.private
        return result

    def close(self):
        for handle in self.handles.values():
            self.kernel.CloseHandle(handle)


def run_case(args):
    import _winapi
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QFont
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtWidgets import QApplication
    from waraq.bridge import Bridge
    from waraq.config import Config
    from waraq.embedded_accounts import EmbeddedAccountService
    import waraq.embedded_runtime as native
    from waraq.scheduler import Scheduler
    from waraq.store import Store

    if os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
        os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'))
    faulthandler.enable()
    faulthandler.dump_traceback_later(max(100, args.warmup + args.measure + 90), repeat=False)
    QQuickStyle.setStyle('Basic')
    app = QApplication([])
    font = QFont('Segoe UI', 10)
    font.setBold(True)
    app.setFont(font)
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))

    def pump_until(predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
        assert predicate(), 'Native idle acceptance did not settle before its watchdog'

    def wait(seconds):
        deadline = time.monotonic() + seconds
        pump_until(lambda: time.monotonic() >= deadline, seconds + 5)

    hashes = []
    actual_payload = native.PackagedPayload
    def payload(*a, **kw):
        hashes.append(time.monotonic())
        return actual_payload(*a, **kw)

    with tempfile.TemporaryDirectory(prefix='waraq-idle-acceptance-', ignore_cleanup_errors=True) as temporary:
        home = Path(temporary)
        config = Config(home)
        # Empty isolated accounts cannot make any provider/quota request.
        config.data['accounts_auto_refresh'] = False
        profile = home / 'accounts-v2'
        alias = profile / 'home/AppData/Local/Microsoft/Windows/INetCache/Content.IE5'
        alias.parent.mkdir(parents=True)
        target = alias.with_name('IE') if args.case != 'blocked' else home / 'outside-profile'
        target.mkdir()
        _winapi.CreateJunction(str(target), str(alias))
        def deny_alias_listing():
            # Match Windows' real compatibility-junction DACL, not just an
            # unrestricted junction created by a development test fixture.
            advapi = ctypes.WinDLL('advapi32', use_last_error=True)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
            advapi.SetKernelObjectSecurity.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)
            kernel.LocalFree.argtypes = (ctypes.c_void_p,)
            descriptor = ctypes.c_void_p()
            node = native._WindowsPath(alias, writable=True)
            try:
                assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    'D:P(D;;0x1;;;WD)(A;;FA;;;SY)(A;;FA;;;' + native._owner_sid() + ')',
                    1, ctypes.byref(descriptor), None)
                assert advapi.SetKernelObjectSecurity(node.handle, 0x80000004, descriptor)
            finally:
                if descriptor:
                    kernel.LocalFree(descriptor)
                node.close()
        deny_alias_listing()
        alias_acl = subprocess.run(['icacls', str(alias), '/L'], capture_output=True, check=True).stdout
        sentinel = target / 'keep.txt'
        sentinel.write_text('preserve cache and never traverse foreign aliases', encoding='utf-8')
        initial_acl = subprocess.run(['icacls', str(target)], capture_output=True, check=True).stdout if args.case == 'blocked' else None
        supervisor = native.EmbeddedRuntimeSupervisor(config, payload_root=args.payload_root)
        accounts = EmbeddedAccountService(config, supervisor=supervisor)
        store = Store(home / 'queue.db')
        if args.jobs:
            store.add([dict(video_id=f'idle-{index}', position=index+1, title=f'درس اختباري {index}', url='')
                       for index in range(args.jobs)], config.snapshot())
        reads = []
        actual_jobs = store.jobs
        def jobs():
            reads.append(time.monotonic())
            return actual_jobs()
        store.jobs = jobs
        scheduler = Scheduler(store, config, None, accounts=accounts)
        bridge, engine, window = None, None, None
        result = {}
        with patch.object(native, 'PackagedPayload', side_effect=payload):
            try:
                bridge = Bridge(store, config, None, scheduler)
                engine = QQmlApplicationEngine()
                engine.rootContext().setContextProperty('backend', bridge)
                engine.load(QUrl.fromLocalFile(str(ROOT / 'waraq/qml/Main.qml')))
                assert engine.rootObjects(), messages
                window = engine.rootObjects()[0]
                if args.case == 'blocked':
                    pump_until(lambda: accounts.status['state'] == 'unavailable')
                    assert accounts.status['reason'] == 'private_path_is_link', accounts.status
                    wait(6)
                    assert not hashes, 'Invalid profile must fail before any payload validation'
                    assert supervisor._runtime is None
                    assert sentinel.read_text(encoding='utf-8').startswith('preserve')
                    assert subprocess.run(['icacls', str(target)], capture_output=True, check=True).stdout == initial_acl
                    # Remove only this test-owned junction, not its target.
                    alias.rmdir()
                    real_target = alias.with_name('IE')
                    real_target.mkdir()
                    _winapi.CreateJunction(str(real_target), str(alias))
                    deny_alias_listing()
                    bridge.recoverAccountEngine()
                pump_until(lambda: accounts.status['state'] == 'ready' and accounts.supervisor._runtime is not None
                           and not (accounts._refresh_thread and accounts._refresh_thread.is_alive()))
                assert len(hashes) == 1, hashes
                assert subprocess.run(['icacls', str(alias), '/L'], capture_output=True, check=True).stdout == alias_acl
                assert not scheduler.running and scheduler.thread is None
                cache = profile / 'public-cache.json'
                pump_until(cache.exists)
                cache_stamp = cache.stat().st_mtime_ns
                wait(args.warmup)
                baseline_reads, baseline_hashes = len(reads), len(hashes)
                meter = ProcessTreeMeter()
                before = meter.snapshot()
                start = time.monotonic()
                samples = []
                deadline = start + args.measure
                while time.monotonic() < deadline:
                    wait(min(1, max(0, deadline-time.monotonic())))
                    samples.append(meter.snapshot())
                after = samples[-1]
                elapsed = time.monotonic()-start
                meter.close()
                cpu = after['cpu_seconds']-before['cpu_seconds']
                result = dict(case=args.case, jobs=args.jobs, seconds=round(elapsed, 3),
                              cpu_seconds=round(cpu, 3), cpu_cores=round(cpu/elapsed, 4),
                              cpu_percent_machine=round(cpu/elapsed/os.cpu_count()*100, 3),
                              read_mb=round((after['read_bytes']-before['read_bytes'])/1024**2, 3),
                              working_mb=round(after['working_bytes']/1024**2, 2),
                              private_mb=round(after['private_bytes']/1024**2, 2),
                              app_cpu_percent_machine=round((after['app_cpu_seconds']-before['app_cpu_seconds'])/elapsed/os.cpu_count()*100, 3),
                              app_working_mb=round(after['app_working_bytes']/1024**2, 2),
                              app_private_mb=round(after['app_private_bytes']/1024**2, 2),
                              working_growth_mb=round((after['working_bytes']-before['working_bytes'])/1024**2, 2),
                              idle_job_scans=len(reads)-baseline_reads, idle_payload_validations=len(hashes)-baseline_hashes,
                              cache_rewritten=cache.stat().st_mtime_ns != cache_stamp,
                              renderer=os.environ.get('QT_QUICK_BACKEND', 'default'), qml_messages=messages,
                              process_ids_count=len(meter.handles), logical_processors=os.cpu_count())
                assert result['idle_job_scans'] == 0, result
                assert result['idle_payload_validations'] == 0, result
                assert not result['cache_rewritten'], result
                assert not messages, messages
                if args.performance:
                    assert result['cpu_cores'] <= 0.1, result
                    if args.measure >= 300:
                        assert result['working_growth_mb'] <= 20, result
                if args.case == 'reopen':
                    accounts.close()
                    # Reopen exactly the same profile twice without resetting it.
                    for _ in range(2):
                        reopened = native.EmbeddedRuntimeSupervisor(config, payload_root=args.payload_root)
                        client = EmbeddedAccountService(config, supervisor=reopened)
                        try:
                            assert client.refresh() == []
                            assert client.status['state'] == 'ready'
                            assert sentinel.read_text(encoding='utf-8').startswith('preserve')
                        finally:
                            client.close()
                    result['successful_profile_opens'] = 3
                    assert len(hashes) == 3
            finally:
                if bridge:
                    bridge.timer.stop()
                    bridge.closeAccounts()
                scheduler.shutdown()
                thread = accounts._refresh_thread
                if thread:
                    pump_until(lambda: not thread.is_alive(), 20)
                if window:
                    window.close()
                if engine:
                    engine.deleteLater()
                    QCoreApplication.sendPostedEvents(engine, QEvent.DeferredDelete)
                if bridge:
                    pump_until(lambda: not bridge._account_pending, 20)
                    bridge.closeRemoteLibrary()
                    bridge._remote_library._pool.shutdown(wait=True, cancel_futures=True)
                    bridge.deleteLater()
                    QCoreApplication.sendPostedEvents(bridge, QEvent.DeferredDelete)
                store.db.close()
        print(json.dumps(result), flush=True)
        if args.evidence:
            args.evidence.mkdir(parents=True, exist_ok=True)
            (args.evidence / f'idle-{args.case}-{args.jobs}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    faulthandler.cancel_dump_traceback_later()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('healthy', 'blocked', 'reopen'))
    parser.add_argument('--jobs', type=int, default=18)
    parser.add_argument('--warmup', type=float, default=2)
    parser.add_argument('--measure', type=float, default=6)
    parser.add_argument('--performance', action='store_true')
    parser.add_argument('--payload-root', type=Path)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    if args.case:
        run_case(args)
        return
    for case in ('healthy', 'blocked', 'reopen'):
        command = [sys.executable, str(Path(__file__).resolve()), '--case', case, '--jobs', str(args.jobs),
                   '--warmup', str(args.warmup), '--measure', str(args.measure)]
        if args.payload_root:
            command += ['--payload-root', str(args.payload_root)]
        if args.evidence:
            command += ['--evidence', str(args.evidence)]
        if args.performance:
            command += ['--performance']
        completed = subprocess.run(command, cwd=ROOT, text=True, encoding='utf-8', capture_output=True,
                                   timeout=max(150, args.warmup+args.measure+100))
        print(completed.stdout, end='', flush=True)
        assert completed.returncode == 0, completed.stderr
    print('Native idle, blocked profile recovery and three-open acceptance passed; no user accounts or paid requests.')


if __name__ == '__main__':
    main()
