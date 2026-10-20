from __future__ import annotations

from concurrent.futures import Future
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import threading
import time

import pytest
from qt_test_support import dispose_bridge

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.remote_common import RemoteError, REQUEST_CONTEXT
from waraq.remote_library import MAX_DOWNLOAD, MAX_PREVIEW, RemoteLibraryFile
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def env(tmp_path, qt_app):
    app=qt_app
    config=Config(tmp_path)
    store=Store(tmp_path/'jobs.sqlite3')
    scheduler=Scheduler(store,config,None)
    bridge=Bridge(store,config,None,scheduler)
    bridge.timer.stop()
    registry=bridge.internet_commands.library
    root=Path(config.data['library_root'])
    (root/'التفريغات').mkdir(parents=True)
    yield bridge,registry,root,config,store,app
    bridge.closeAccounts()
    registry.close()
    deadline=time.monotonic()+10
    while registry.busy and time.monotonic()<deadline:time.sleep(.01)
    assert not registry.busy
    bridge.remote.stop()
    bridge.internet.stop()
    scheduler.shutdown()
    dispose_bridge(bridge)
    store.db.close()


def ready(registry,config,data=None,timeout=15):
    request=dict(data or {})
    result=registry.list(request,config.snapshot())
    deadline=time.monotonic()+timeout
    while result['busy'] and time.monotonic()<deadline:
        time.sleep(.01)
        request.pop('refresh',None)
        result=registry.list(request,config.snapshot())
    assert not result['busy'],result
    assert not result['error'],result
    return result


def refresh(registry,config,timeout=15):
    return ready(registry,config,{'refresh':True},timeout=timeout)


@pytest.mark.parametrize('fail_write', [False, True])
def test_scan_closes_index_connection_after_commit_or_failure(env, monkeypatch, fail_write):
    bridge,registry,root,config,store,app=env
    (root/'التفريغات'/'retained.txt').write_text('retained',encoding='utf-8')
    original=registry._connect
    retained=[]
    def connect():
        database=original()
        retained.append(database)  # Prevent GC from hiding a leaked connection.
        if fail_write:
            database.set_authorizer(lambda action,table,*args:
                sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and table=='meta' else sqlite3.SQLITE_OK)
        return database
    monkeypatch.setattr(registry,'_connect',connect)
    registry._scan(config.snapshot())
    assert registry._loaded is not fail_write
    assert bool(registry._error) is fail_write
    assert len(retained)==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        retained[0].execute('SELECT 1')
    if not fail_write:
        with closing(sqlite3.connect(registry._index)) as database:
            assert database.execute('SELECT name FROM files').fetchone()[0]=='retained.txt'


def test_shared_registry_capabilities_and_transport_stop(env):
    bridge,registry,root,config,store,app=env
    assert bridge.remote.commands.library is registry
    assert bridge.internet_commands.execute('state',{})['capabilities']==dict(library=True,library_preview=True,library_delete=True,library_organize=True)
    with registry.library._lock:
        registry.refresh(config.snapshot())
        assert bridge.remote.busy and bridge.internet_commands.busy
        bridge.remote.commands.stop()
        assert not registry._cancel.is_set()
    ready(registry,config)


def test_large_library_cursor_groups_filters_and_restart_stability(env):
    bridge,registry,root,config,store,app=env
    for i in range(3021):(root/'التفريغات'/f'{i:04d}-عربي.txt').write_text(str(i),encoding='utf-8')
    group=root/'مادة خاصة'/'Word';group.mkdir(parents=True)
    (group/'شرح.docx').write_bytes(b'docx')
    # This deliberately scans thousands of real Windows files; allow slower
    # filesystem/security inspection while retaining a bounded completion check.
    result=ready(registry,config,{'limit':100},timeout=60)
    assert result['total']==3022
    assert len(result['groups'])==2
    seen=[]
    while True:
        seen.extend(f['file_id'] for f in result['files'])
        if not result['next_cursor']:break
        result=registry.list({'limit':100,'cursor':result['next_cursor']},config.snapshot())
    assert len(set(seen))==3022
    first=registry.list({'limit':1},config.snapshot())
    cursor=first['next_cursor']
    for changed in ({'kind':'transcript'},{'query':'عربي'},{'group_id':first['groups'][0]['id']}):
        with pytest.raises(RemoteError) as exc:
            registry.list({'cursor':cursor,**changed},config.snapshot())
        assert exc.value.status==409
    revision=first['revision']
    registry._loaded=False
    restarted=ready(registry,config,{'limit':1},timeout=60)
    assert restarted['revision']==revision
    assert restarted['files'][0]['file_id']==first['files'][0]['file_id']
    assert str(root) not in json.dumps(first,ensure_ascii=False)
    (root/'التفريغات'/'new.txt').write_text('new')
    refresh(registry,config,timeout=60)
    with pytest.raises(RemoteError) as exc:registry.list({'cursor':cursor},config.snapshot())
    assert exc.value.status==409


def test_preview_bom_utf8_boundary_and_download_limit(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'نص.txt'
    content=b'\xef\xbb\xbf'+('أ'*MAX_PREVIEW).encode()
    path.write_bytes(content)
    row=ready(registry,config)['files'][0]
    preview=registry.preview({'file_id':row['file_id'],'expected_version':row['version']},config.snapshot()).result(5)
    assert preview['truncated'] and preview['size']==len(content)
    assert preview['text'].startswith('أ') and '\ufeff' not in preview['text']
    cap=registry.file({'file_id':row['file_id']},config.snapshot())
    assert isinstance(cap,RemoteLibraryFile)
    with cap.open() as stream:assert stream.read()==content
    large=root/'التفريغات'/'large.txt'
    with large.open('wb') as stream:stream.truncate(MAX_DOWNLOAD+1)
    result=refresh(registry,config)
    item=next(f for f in result['files'] if f['name']=='large.txt')
    assert not item['can_download']


def test_delete_compare_and_swap_preserves_origin_and_ids_never_reused(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'نسخة.txt';path.write_text('one')
    first=ready(registry,config)['files'][0]
    path.write_text('changed')
    with pytest.raises(RemoteError):registry.delete({'file_id':first['file_id'],'expected_version':first['version'],'confirm':True},config.snapshot()).result(5)
    assert path.exists()
    current=refresh(registry,config)['files'][0]
    assert registry.delete({'file_id':current['file_id'],'expected_version':current['version'],'confirm':True},config.snapshot()).result(5)['revision']
    assert not path.exists()
    path.write_text('replacement')
    new=refresh(registry,config)['files'][0]
    assert new['file_id']!=current['file_id']
    with pytest.raises(RemoteError):registry.file({'file_id':current['file_id']},config.snapshot())
    job=store.add([{'video_id':'local','title':'local','position':1}],config.snapshot())[0]
    with store.lock:store.db.execute('UPDATE jobs SET transcript=? WHERE id=?',(str(path),job))
    protected=refresh(registry,config)['files'][0]
    assert not protected['can_delete']
    with pytest.raises(RemoteError):registry.delete({'file_id':protected['file_id'],'expected_version':protected['version'],'confirm':True},config.snapshot()).result(5)
    assert path.exists()


def test_removed_job_keeps_mirrored_library_file_and_organize_async(env,monkeypatch):
    bridge,registry,root,config,store,app=env
    source=config.home/'original.txt';source.write_text('محتوى',encoding='utf-8')
    job=store.add([{'video_id':'local','title':'local','position':1,'input_path':str(source)}],config.snapshot())[0]
    registry.organize(config.snapshot())
    result=ready(registry,config)
    deadline=time.monotonic()+5
    while registry.busy and time.monotonic()<deadline:time.sleep(.01)
    result=refresh(registry,config)
    assert len(result['files'])==1
    store.delete_jobs([job])
    assert refresh(registry,config)['total']==1
    threads=[]
    original=registry.library.organize_current
    def organize(*args,**kwargs):threads.append(threading.current_thread());return original(*args,**kwargs)
    monkeypatch.setattr(registry.library,'organize_current',organize)
    assert bridge.internet_commands.execute('library_organize',{})['busy']
    deadline=time.monotonic()+5
    while registry.busy and time.monotonic()<deadline:time.sleep(.01)
    assert threads and threads[0] is not threading.main_thread()


def test_renamed_replaced_and_root_changed_capabilities_fail_safely(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'old.txt';path.write_text('one')
    row=ready(registry,config)['files'][0]
    cap=registry.file({'file_id':row['file_id']},config.snapshot())
    path.rename(path.with_name('renamed.txt'))
    with pytest.raises(RemoteError):
        with cap.open():pass
    refresh(registry,config)
    renamed=registry.list({},config.snapshot())['files'][0]
    cap=registry.file({'file_id':renamed['file_id']},config.snapshot())
    config.data['library_root']=str(config.home/'new-root')
    with pytest.raises(RemoteError):
        with cap.open():pass
    assert ready(registry,config)['total']==0


def test_cancel_deadline_after_library_lock_prevents_delete(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'safe.txt';path.write_text('safe')
    row=ready(registry,config)['files'][0]
    cancelled=threading.Event()
    context={'cancel':cancelled,'deadline':time.time()+5}
    with registry.library._lock:
        token=REQUEST_CONTEXT.set(context)
        try:future=bridge.internet_commands.execute('library_delete',{'file_id':row['file_id'],'expected_version':row['version'],'confirm':True})
        finally:REQUEST_CONTEXT.reset(token)
        assert isinstance(future,Future)
        cancelled.set()
    with pytest.raises(RemoteError) as exc:future.result(5)
    assert exc.value.status==503 and path.exists()
    token=REQUEST_CONTEXT.set({'cancel':threading.Event(),'deadline':time.time()-.1})
    try:future=bridge.internet_commands.execute('library_delete',{'file_id':row['file_id'],'expected_version':row['version'],'confirm':True})
    finally:REQUEST_CONTEXT.reset(token)
    with pytest.raises(RemoteError):future.result(5)
    assert path.exists()


def test_links_junctions_part_files_and_unapproved_directories_excluded(env):
    bridge,registry,root,config,store,app=env
    outside=config.home/'outside';outside.mkdir();(outside/'secret.txt').write_text('secret')
    (root/'التفريغات'/'valid.txt').write_text('valid')
    (root/'التفريغات'/'hidden.part').write_text('partial')
    (root/'التفريغات'/'.hidden.txt').write_text('hidden')
    (root/'other').mkdir();(root/'other'/'no.txt').write_text('no')
    junction=root/'linked'
    if os.name=='nt':
        completed=subprocess.run(['cmd','/c','mklink','/J',str(junction),str(outside)],capture_output=True,text=True)
        assert completed.returncode==0,completed.stderr
    else:junction.symlink_to(outside,target_is_directory=True)
    try:
        result=ready(registry,config)
        assert [r['name'] for r in result['files']]==['valid.txt']
        row=result['files'][0]
        capability=registry.file({'file_id':row['file_id']},config.snapshot())
        target=root/'التفريغات'/'valid.txt'
        target.unlink()
        if os.name=='nt':
            # Replace the containing directory by a junction after indexing.
            (root/'التفريغات').rename(root/'old-transcripts')
            completed=subprocess.run(['cmd','/c','mklink','/J',str(root/'التفريغات'),str(outside)],capture_output=True)
            assert completed.returncode==0
        else:target.symlink_to(outside/'secret.txt')
        with pytest.raises(RemoteError):
            with capability.open():pass
    finally:
        if junction.exists():junction.rmdir() if os.name=='nt' else junction.unlink()
        if os.name=='nt' and (root/'التفريغات').exists():(root/'التفريغات').rmdir()


def test_pending_publish_part_prevents_delete(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'valid.txt';path.write_text('valid')
    row=ready(registry,config)['files'][0]
    path.with_name(path.name+'.part').write_text('publishing')
    with pytest.raises(RemoteError):registry.delete({'file_id':row['file_id'],'expected_version':row['version'],'confirm':True},config.snapshot()).result(5)
    assert path.exists()


def test_real_registry_restart_and_hardlinks_keep_distinct_ids(env):
    from waraq.remote_library import RemoteLibrary
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'first.txt';path.write_text('same')
    os.link(path,root/'التفريغات'/'second.txt')
    result=ready(registry,config)
    assert len({f['file_id'] for f in result['files']})==2
    expected={f['name']:f['file_id'] for f in result['files']}
    restarted=RemoteLibrary(bridge)
    try:
        fresh=ready(restarted,config)
        assert fresh['revision']==result['revision']
        assert {f['name']:f['file_id'] for f in fresh['files']}==expected
    finally:
        restarted.close()
        until=time.monotonic()+5
        while restarted.busy and time.monotonic()<until:time.sleep(.01)


def test_initial_scan_is_async_and_cancelled_organize_never_copies(env,monkeypatch):
    import waraq.remote_library as module
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'test.txt';path.write_text('test')
    seen=[]
    original=module._validate_path
    def validate(*args):
        seen.append(threading.current_thread())
        return original(*args)
    monkeypatch.setattr(module,'_validate_path',validate)
    first=registry.list({},config.snapshot())
    assert first['busy']
    ready(registry,config)
    assert seen and all(t is not threading.main_thread() for t in seen)
    source=config.home/'original.txt';source.write_text('origin')
    store.add([{'video_id':'local','title':'local','position':1,'input_path':str(source)}],config.snapshot())
    context={'cancel':threading.Event(),'deadline':time.time()+5}
    with registry.library._lock:
        registry.organize(config.snapshot(),context)
        context['cancel'].set()
    until=time.monotonic()+5
    while registry.busy and time.monotonic()<until:time.sleep(.01)
    assert [p.name for p in (root/'التفريغات').iterdir()]==['test.txt']
    assert not registry.busy


def test_expected_version_and_cancelled_file_preview_reads(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'private.txt';path.write_text('private')
    row=ready(registry,config)['files'][0]
    for action in ('library_file','library_preview'):
        with pytest.raises(RemoteError) as exc:
            bridge.internet_commands.execute(action,{'file_id':row['file_id'],'expected_version':'stale'})
        assert exc.value.status==409
    cancelled=threading.Event()
    context={'cancel':cancelled,'deadline':time.time()+5}
    token=REQUEST_CONTEXT.set(context)
    try:capability=bridge.internet_commands.execute('library_file',{'file_id':row['file_id'],'expected_version':row['version']})
    finally:REQUEST_CONTEXT.reset(token)
    cancelled.set()
    with pytest.raises(RemoteError) as exc:
        with capability.open():pytest.fail('cancelled read opened')
    assert exc.value.status==503
    context={'cancel':threading.Event(),'deadline':time.time()+5}
    with registry.library._lock:
        token=REQUEST_CONTEXT.set(context)
        try:future=bridge.internet_commands.execute('library_preview',{'file_id':row['file_id'],'expected_version':row['version']})
        finally:REQUEST_CONTEXT.reset(token)
        context['cancel'].set()
    with pytest.raises(RemoteError) as exc:future.result(5)
    assert exc.value.status==503
    context={'cancel':threading.Event(),'deadline':time.time()-.1}
    capability=registry.file({'file_id':row['file_id']},config.snapshot(),context)
    with pytest.raises(RemoteError) as exc:
        with capability.open():pytest.fail('expired read opened')
    assert exc.value.status==503


def test_bridge_shutdown_cancels_shared_library_and_clears_busy(env):
    bridge,registry,root,config,store,app=env
    with registry.library._lock:
        registry.refresh(config.snapshot())
        assert registry.busy
        bridge.closeRemoteLibrary()
        bridge.closeRemoteLibrary()  # prepareClose plus aboutToQuit is idempotent.
        assert registry._cancel.is_set()
    until=time.monotonic()+5
    while registry.busy and time.monotonic()<until:time.sleep(.01)
    assert not registry.busy
    with pytest.raises(RemoteError):registry.preview({'file_id':'missing'},config.snapshot())


def test_remote_organize_refuses_reparse_destination_before_any_write(env):
    bridge,registry,root,config,store,app=env
    source=config.home/'original.txt';source.write_text('origin')
    store.add([{'video_id':'local','title':'local','position':1,'input_path':str(source)}],config.snapshot())
    outside=config.home/'outside-destination';outside.mkdir()
    existing=root/'التفريغات';existing.rmdir()
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(existing),str(outside)],capture_output=True)
        assert result.returncode==0
    else:existing.symlink_to(outside,target_is_directory=True)
    try:
        registry.organize(config.snapshot())
        until=time.monotonic()+5
        while registry.busy and time.monotonic()<until:time.sleep(.01)
        assert list(outside.iterdir())==[]
        assert registry._error
    finally:
        existing.rmdir() if os.name=='nt' else existing.unlink()


def test_cas_detects_same_size_edit_even_if_modified_time_restored(env):
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'edit.txt';path.write_text('one')
    row=ready(registry,config)['files'][0]
    original=path.stat()
    time.sleep(.01)
    path.write_text('two')
    os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
    with pytest.raises(RemoteError):
        registry.delete({'file_id':row['file_id'],'expected_version':row['version'],'confirm':True},config.snapshot()).result(5)
    assert path.read_text()=='two'


def test_delete_rejects_manifest_link_before_unlink_and_ignores_tmp_link(env,monkeypatch):
    from waraq.library import MANIFEST
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'safe.txt';path.write_text('safe')
    row=ready(registry,config)['files'][0]
    outside=config.home/'outside-metadata';outside.mkdir()
    secret=outside/'secret.txt';secret.write_text('unchanged')
    manifest=root/MANIFEST
    def junction_or_link(link,target,directory):
        if os.name=='nt':
            result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True)
            assert result.returncode==0
        else:link.symlink_to(target,target_is_directory=directory)
    junction_or_link(manifest,outside,True)
    try:
        with pytest.raises(RemoteError):
            registry.delete({'file_id':row['file_id'],'expected_version':row['version'],'confirm':True},config.snapshot()).result(5)
        assert path.read_text()=='safe'
        assert secret.read_text()=='unchanged'
    finally:
        manifest.rmdir() if os.name=='nt' else manifest.unlink()
    temporary=root/'.tmp-evil'
    junction_or_link(temporary,outside,True)
    try:
        import tempfile
        # Force the random allocator's first name to collide with the hostile
        # junction. O_EXCL must skip it and use the second fresh candidate.
        with monkeypatch.context() as patcher:
            patcher.setattr(tempfile,'_get_candidate_names',lambda:iter(('evil','fresh-safe')))
            assert registry.delete({'file_id':row['file_id'],'expected_version':row['version'],'confirm':True},config.snapshot()).result(5)['revision']
        assert not path.exists()
        assert secret.read_text()=='unchanged'
        assert sorted(p.name for p in outside.iterdir())==['secret.txt']
    finally:
        temporary.rmdir() if os.name=='nt' else temporary.unlink()


def test_malformed_manifest_fails_before_deleting_library_file(env):
    from waraq.library import MANIFEST
    bridge,registry,root,config,store,app=env
    path=root/'التفريغات'/'safe.txt';path.write_text('safe')
    row=ready(registry,config)['files'][0]
    for text in ('broken json','{"job|transcript": {"path":"bad"}}'):
        (root/MANIFEST).write_text(text,encoding='utf-8')
        with pytest.raises(RemoteError):
            registry.delete({'file_id':row['file_id'],'expected_version':row['version'],'confirm':True},config.snapshot()).result(5)
        assert path.read_text()=='safe'


@pytest.mark.skipif(os.name!='nt',reason='Windows native DOS alias semantics')
def test_windows_short_path_alias_preview_download_manifest_and_cas_delete(env):
    import ctypes
    from ctypes import wintypes
    from waraq.library import MANIFEST
    from waraq.remote_library import _windows_final_path, _canonical_windows_path
    bridge,registry,root,config,store,app=env
    long_root=root/'Long Library Directory For Alias Test'
    transcripts=long_root/'التفريغات'
    transcripts.mkdir(parents=True)
    path=transcripts/'Long Lesson Filename For Alias Test.txt'
    content=b'\xef\xbb\xbf'+ 'نص محفوظ بلا تغيير'.encode()
    path.write_bytes(content)
    (long_root/MANIFEST).write_text(json.dumps({'old-job|transcript':str(Path('التفريغات')/path.name)}),encoding='utf-8')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetShortPathNameW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]
    kernel.GetShortPathNameW.restype=wintypes.DWORD
    buffer=ctypes.create_unicode_buffer(32768)
    size=kernel.GetShortPathNameW(str(long_root),buffer,len(buffer))
    assert 0<size<len(buffer),'GetShortPathNameW failed'
    short_root=Path(buffer.value)
    if os.path.normcase(str(short_root))==os.path.normcase(str(long_root)):
        pytest.skip('This NTFS volume has 8.3 alias creation disabled')
    assert short_root.is_dir()
    config.data['library_root']=str(short_root)
    item=ready(registry,config)['files'][0]
    capability=registry.file({'file_id':item['file_id'],'expected_version':item['version']},config.snapshot())
    with capability.open() as stream:
        assert stream.read()==content
        assert os.path.normcase(str(_windows_final_path(stream)))==_canonical_windows_path(path)
    preview=registry.preview({'file_id':item['file_id'],'expected_version':item['version']},config.snapshot()).result(5)
    assert preview['text']=='نص محفوظ بلا تغيير'
    # Reading and replacing the manifest, and the native CAS delete handle,
    # exercise the other two canonical final-path comparisons through the alias.
    result=registry.delete({'file_id':item['file_id'],'expected_version':item['version'],'confirm':True},config.snapshot()).result(5)
    assert result['revision'] and not path.exists()
    assert json.loads((long_root/MANIFEST).read_text(encoding='utf-8'))=={}
