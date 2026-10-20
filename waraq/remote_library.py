"""Device-local library index and constrained file capabilities for remote clients."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from .library import FOLDERS
from .remote_common import RemoteError

MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_PREVIEW = 1024 * 1024
CAPABILITIES = dict(library=True, library_preview=True, library_delete=True, library_organize=True)


def _stat_key(value):
    return [value.st_dev, value.st_ino, getattr(value, 'st_birthtime_ns', value.st_ctime_ns)]


def _version(value):
    return hashlib.sha256(json.dumps([_stat_key(value), value.st_size, value.st_mtime_ns,
                                     value.st_ctime_ns]).encode()).hexdigest()


def _metadata(path):
    # Windows path stat reports creation time in st_ctime on some Python/NTFS
    # combinations; fstat consistently supplies the actual change time. Index
    # and compare-and-swap must use the same handle metadata as file streaming.
    if os.name == 'nt':
        with path.open('rb') as stream:
            return os.fstat(stream.fileno())
    return path.stat(follow_symlinks=False)


def _no_link(path):
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise RemoteError('روابط الملفات والمجلدات غير مسموحة في المكتبة.', 409)
    return value


def _validate_path(root, relative):
    parts = Path(relative).parts
    if (Path(relative).is_absolute() or any(part in ('.', '..') for part in parts) or
            not ((len(parts) == 2 and parts[0] in FOLDERS.values()) or
                 (len(parts) == 3 and parts[1] in FOLDERS.values())) or
            parts[-1].startswith('.') or parts[-1].lower().endswith('.part')):
        raise RemoteError('الملف خارج نطاق المكتبة.', 404)
    # Validate the configured root and every ancestor too, including junctions.
    chain = list(reversed(root.parents)) + [root]
    for path in chain:
        _no_link(path)
    target = root
    for part in parts:
        target /= part
        _no_link(target)
    if not target.resolve().is_relative_to(root.resolve()):
        raise RemoteError('الملف خارج نطاق المكتبة.', 404)
    return target


def _windows_final_path(stream):
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
    if not count or count >= len(buffer):
        raise RemoteError('تعذر التحقق من الملف المفتوح.', 409)
    value = buffer.value
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(value)


def _canonical_windows_path(path):
    # absolute() preserves DOS 8.3 names (for example RUNNER~1 in TEMP) and
    # SUBST drive aliases. The native opened handle reports the normalized
    # long path, so compare against the equally canonical expected pathname.
    # Callers still reject every reparse ancestor before reaching this helper.
    return os.path.normcase(str(Path(path).resolve(strict=True)))


def _guard_internal_path(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root):
        raise RemoteError('مسار المكتبة غير صالح.',409)
    for ancestor in list(reversed(path.parents)) + [path]:
        try:_no_link(ancestor)
        except FileNotFoundError:pass


def _read_manifest_checked(root):
    from .library import MANIFEST
    path = root / MANIFEST
    _guard_internal_path(root,path)
    try:
        expected = _no_link(path)
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(expected.st_mode):
        raise RemoteError('سجل المكتبة غير صالح؛ لم يُحذف الملف.',409)
    descriptor = os.open(path,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as stream:
        _guard_internal_path(root,path)
        if os.name == 'nt' and os.path.normcase(str(_windows_final_path(stream))) != _canonical_windows_path(path):
            raise RemoteError('تغير سجل المكتبة؛ لم يُحذف الملف.',409)
        opened = os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino) != (expected.st_dev,expected.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise RemoteError('تغير سجل المكتبة؛ لم يُحذف الملف.',409)
        raw = stream.read(16*1024*1024+1)
        if len(raw)>16*1024*1024:
            raise RemoteError('سجل المكتبة أكبر من الحد الآمن؛ لم يُحذف الملف.',409)
    try:result=json.loads(raw.decode('utf-8'))
    except (ValueError,UnicodeError):
        raise RemoteError('تعذر قراءة سجل المكتبة؛ لم يُحذف الملف.',409) from None
    if not isinstance(result,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in result.items()):
        raise RemoteError('سجل المكتبة غير صالح؛ لم يُحذف الملف.',409)
    return result


class RemoteLibraryFile:
    """A capability, never a filesystem path exposed to the phone."""
    def __init__(self, registry, row, context=None):
        self._registry, self._row = registry, dict(row)
        self._context = context
        self.name = row['name']

    @contextmanager
    def open(self):
        registry, row = self._registry, self._row
        try:
            registry._check(self._context)
            with registry.library._lock:
                registry._check(self._context)
                registry._current_root(row['root'])
                target = _validate_path(Path(row['root']), row['relative'])
                descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
                stream = os.fdopen(descriptor, 'rb')
                try:
                    _validate_path(Path(row['root']), row['relative'])
                    if os.name == 'nt' and os.path.normcase(str(_windows_final_path(stream))) != _canonical_windows_path(target):
                        raise RemoteError('تغير موقع الملف. حدّث المكتبة.', 409)
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or _version(info) != row['version']:
                        raise RemoteError('تغير الملف. حدّث المكتبة قبل المتابعة.', 409)
                except BaseException:
                    stream.close()
                    raise
            try:
                registry._check(self._context)
                yield stream
            finally:
                stream.close()
        except RemoteError:
            raise
        except (OSError, ValueError):
            raise RemoteError('الملف غير متاح أو تغير موقعه. حدّث المكتبة.', 409) from None


class RemoteLibrary:
    """One registry per Bridge, shared by LAN and Internet; disk work is off Qt."""
    def __init__(self, bridge):
        self.bridge = bridge
        self.library = bridge.scheduler.library
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='remote-library')
        self._cancel = threading.Event()
        self._rows, self._groups = [], []
        self._revision, self._root, self._secret = '', '', ''
        self._busy = 0
        self._scanning = self._organizing = False
        self._error = ''
        self._loaded = False
        self._index = Path(bridge.config.home) / 'remote-library.sqlite3'

    @property
    def busy(self):
        with self._lock:
            return self._busy > 0

    def close(self):
        self._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    stop = close

    def _check(self, context=None):
        if self._cancel.is_set() or (context and (context["cancel"].is_set() or time.time() >= context["deadline"])):
            raise RemoteError("انتهت مهلة طلب المكتبة أو أُلغي؛ لم ينفذ التعديل.",503)

    def _current_root(self, expected):
        if str(Path(self.bridge.config.snapshot()['library_root']).absolute()) != expected:
            raise RemoteError('تغيرت المكتبة. حدّث القائمة.', 409)

    def _submit(self, operation, context=None):
        with self._lock:
            if self._cancel.is_set():
                raise RemoteError('المكتبة تتوقف الآن.', 503)
            self._busy += 1
        def work():
            try:
                self._check(context)
                return operation()
            finally:
                with self._lock:
                    self._busy -= 1
        try:
            future = self._pool.submit(work)
            def cancelled(done):
                if done.cancelled():
                    with self._lock:self._busy -= 1
            future.add_done_callback(cancelled)
            return future
        except BaseException:
            with self._lock: self._busy -= 1
            raise

    def _connect(self):
        database = sqlite3.connect(self._index)
        try:
            database.row_factory = sqlite3.Row
            database.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,root TEXT NOT NULL,relative TEXT NOT NULL,
                identity TEXT NOT NULL,version TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,
                group_id TEXT NOT NULL,group_name TEXT NOT NULL,size INTEGER NOT NULL,modified REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,root TEXT NOT NULL,name TEXT NOT NULL,
                UNIQUE(root,name));
            ''')
        except BaseException:
            database.close()
            raise
        return database

    def _scan(self, cfg, context=None):
        root = Path(cfg['library_root']).absolute()
        try:
            with self.library._lock:
                self._check(context)
                found = []
                origins = set()
                for job in self.bridge.store.jobs():
                    for key in ('input_path','audio','transcript','document'):
                        if job.get(key):
                            try:
                                info=Path(job[key]).stat()
                                origins.add((info.st_dev,info.st_ino))
                            except OSError: pass
                if root.exists():
                    for ancestor in list(reversed(root.parents)) + [root]: _no_link(ancestor)
                    folders = [(root / folder, kind, '') for kind,folder in FOLDERS.items()]
                    for group in root.iterdir():
                        if self._cancel.is_set(): return
                        try: info = _no_link(group)
                        except (OSError,RemoteError): continue
                        if stat.S_ISDIR(info.st_mode) and group.name not in FOLDERS.values() and not group.name.startswith('.'):
                            folders.extend((group / folder, kind, group.name) for kind,folder in FOLDERS.items())
                    for folder,kind,group in folders:
                        if self._cancel.is_set(): return
                        try:
                            if not stat.S_ISDIR(_no_link(folder).st_mode): continue
                        except (OSError,RemoteError): continue
                        for path in folder.iterdir():
                            if self._cancel.is_set(): return
                            try:
                                if path.name.startswith('.') or path.name.lower().endswith('.part'): continue
                                info = _no_link(path)
                                if not stat.S_ISREG(info.st_mode): continue
                                relative = str(path.relative_to(root))
                                _validate_path(root, relative)
                                info = _metadata(path)
                                found.append(dict(root=str(root),relative=relative,identity=json.dumps(_stat_key(info)),
                                                  version=_version(info),name=path.name,kind=kind,group_name=group,
                                                  size=info.st_size,modified=info.st_mtime,protected=(info.st_dev,info.st_ino) in origins))
                            except (OSError,RemoteError): continue
                # sqlite3's context manager commits/rolls back; it does not
                # close the handle. Release it even when a scan fails, rather
                # than leaving Windows file locks until a later GC cycle.
                with closing(self._connect()) as db, db:
                    meta = dict(db.execute('SELECT key,value FROM meta').fetchall())
                    secret = meta.get('secret') or secrets.token_hex(32)
                    previous = {r['relative']:dict(r) for r in db.execute('SELECT * FROM files WHERE root=?',(str(root),))}
                    by_identity = {r['identity']:r for r in previous.values()}
                    groups = {r['name']:r['id'] for r in db.execute('SELECT * FROM groups WHERE root=?',(str(root),))}
                    found_relatives = {item['relative'] for item in found}
                    used_ids = set()
                    for item in found:
                        group = item['group_name']
                        if group not in groups:
                            groups[group]=secrets.token_urlsafe(24)
                            db.execute('INSERT INTO groups VALUES(?,?,?)',(groups[group],str(root),group))
                        item['group_id']=groups[group]
                        old=previous.get(item['relative'])
                        if not old or old['identity'] != item['identity']:
                            old=by_identity.get(item['identity'])
                            if old and old['relative'] in found_relatives:old=None
                        if old and old['id'] in used_ids:old=None
                        item['id']=old['id'] if old else secrets.token_urlsafe(24)
                        used_ids.add(item['id'])
                    found.sort(key=lambda row:(row['group_name'].casefold(),row['name'].casefold(),row['id']))
                    before = sorted((r['id'],r['relative'],r['version']) for r in previous.values())
                    after = sorted((r['id'],r['relative'],r['version']) for r in found)
                    revision = meta.get('revision','') if before==after and meta.get('root')==str(root) else secrets.token_hex(24)
                    revision = revision or secrets.token_hex(24)
                    self._check(context)
                    db.execute('DELETE FROM files')
                    for r in found:
                        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?)',tuple(r[k] for k in
                                   ('id','root','relative','identity','version','name','kind','group_id','group_name','size','modified')))
                    for key,value in dict(secret=secret,revision=revision,root=str(root)).items():
                        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,value))
                with self._lock:
                    self._rows,self._revision,self._root,self._secret = found,revision,str(root),secret
                    self._groups=[{'id':groups[name],'name':name or 'غير مصنف'} for name in sorted({r['group_name'] for r in found},key=str.casefold)]
                    self._loaded,self._error=True,''
        except Exception:
            with self._lock: self._error='تعذر قراءة المكتبة. تحقق من إعداداتها وصلاحيات المجلد.'
        finally:
            with self._lock:self._scanning=False

    def refresh(self, cfg, context=None):
        with self._lock:
            if self._scanning:return
            self._scanning=True
        future=self._submit(lambda:self._scan(cfg,context),context)
        def finished(done):
            with self._lock:self._scanning=False
        future.add_done_callback(finished)

    def list(self, data, cfg, context=None):
        kind,group,query=data.get('kind','all'),data.get('group_id','all'),data.get('query','')
        limit=data.get('limit',50)
        cursor=data.get('cursor','')
        if (kind not in ('all',*FOLDERS) or not isinstance(group,str) or not isinstance(query,str) or
                len(query)>300 or type(limit) is not int or not 1<=limit<=100 or
                not isinstance(cursor,str) or len(cursor)>2048 or type(data.get('refresh',False)) is not bool):
            raise RemoteError('مرشحات مكتبة غير صالحة.')
        query=query.strip().casefold()
        root=str(Path(cfg['library_root']).absolute())
        with self._lock:
            if not self._loaded or root != self._root or data.get('refresh'):
                if root != self._root:self._rows,self._groups,self._revision=[],[],''
                self.refresh(cfg,context)
            offset=0
            if cursor:
                try:
                    encoded,signature=cursor.split('.')
                    expected=hmac.new(self._secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(signature,expected):raise ValueError()
                    payload=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
                    if payload[:4]!=[self._revision,kind,group,query] or type(payload[4]) is not int or payload[4]<0:raise ValueError()
                    offset=payload[4]
                except (ValueError,TypeError,IndexError):
                    raise RemoteError('تغيرت المكتبة أو المرشحات. ابدأ من الصفحة الأولى.',409) from None
            if group!='all' and group not in {g['id'] for g in self._groups}:
                raise RemoteError('المجموعة غير متاحة. حدّث المكتبة.',404)
            filtered=[r for r in self._rows if (kind=='all' or r['kind']==kind) and
                      (group=='all' or r['group_id']==group) and (not query or query in r['name'].casefold())]
            selected=filtered[offset:offset+limit]
            files=[dict(file_id=r['id'],name=r['name'],kind=r['kind'],group_id=r['group_id'],group_name=r['group_name'] or 'غير مصنف',
                        size=r['size'],modified=r['modified'],version=r['version'],can_download=r['size']<=MAX_DOWNLOAD,
                        can_preview=r['kind']=='transcript',can_delete=not r.get('protected',False)) for r in selected]
            next_cursor=''
            if offset+limit<len(filtered):
                encoded=base64.urlsafe_b64encode(json.dumps([self._revision,kind,group,query,offset+limit],ensure_ascii=False).encode()).decode().rstrip('=')
                next_cursor=encoded+'.'+hmac.new(self._secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
            return dict(files=files,groups=list(self._groups),total=len(filtered),revision=self._revision,next_cursor=next_cursor,
                        busy=self._scanning,error=self._error,organizing=self._organizing)

    def file(self, data, cfg, context=None):
        ident=data.get('file_id')
        with self._lock:
            row=next((dict(r) for r in self._rows if r['id']==ident and r['root']==str(Path(cfg['library_root']).absolute())),None)
        if not row:raise RemoteError('الملف غير موجود في المكتبة. حدّث القائمة.',404)
        if data.get('expected_version') and data['expected_version']!=row['version']:
            raise RemoteError('تغير الملف. حدّث القائمة.',409)
        return RemoteLibraryFile(self,row,context)

    def preview(self, data, cfg, context=None):
        capability=self.file(data,cfg,context)
        if capability._row['kind']!='transcript':raise RemoteError('المعاينة متاحة للتفريغات النصية فقط.')
        def work():
            self._check(context)
            with capability.open() as stream:
                self._check(context)
                raw=stream.read(MAX_PREVIEW+4)
                size=os.fstat(stream.fileno()).st_size
            truncated=size>MAX_PREVIEW
            raw=raw[:MAX_PREVIEW]
            import codecs
            try:
                text=codecs.getincrementaldecoder('utf-8-sig')().decode(raw,final=not truncated)
                if '\x00' in text:raise ValueError()
            except (UnicodeError,ValueError):raise RemoteError('الملف ليس تفريغ UTF-8 صالحًا.') from None
            return dict(name=capability.name,text=text,truncated=truncated,size=size)
        return self._submit(work,context)

    def delete(self,data,cfg,context=None):
        if data.get('confirm') is not True or not isinstance(data.get('expected_version'),str) or not data['expected_version']:
            raise RemoteError('يلزم تأكيد الحذف ومراجعة الملف الحالية.')
        capability=self.file(data,cfg,context)
        def work():
            row=capability._row
            try:
                with self.library._lock:
                    self._check(context)
                    self._current_root(row['root'])
                    target=_validate_path(Path(row['root']),row['relative'])
                    if target.with_name(target.name+'.part').exists():raise RemoteError('يجري نشر الملف. انتظر ثم حدّث المكتبة.',409)
                    _no_link(target)
                    info=_metadata(target)
                    if _version(info)!=row['version']:raise RemoteError('تغير الملف. حدّث القائمة قبل حذفه.',409)
                    # Validate and parse all manifest inputs before the destructive
                    # CAS step. atomic_json uses random O_EXCL mkstemp names;
                    # pre-existing .tmp* links cannot be followed or overwritten.
                    from .library import MANIFEST
                    from .config import atomic_json
                    root=Path(row['root'])
                    manifest=_read_manifest_checked(root)
                    manifest={k:v for k,v in manifest.items() if k.rsplit('|',1)[-1] not in FOLDERS or
                              self.library._old_path(root,k.rsplit('|',1)[-1],v)!=target.resolve(strict=True)}
                    with self.bridge.store.lock:
                        for job in self.bridge.store.jobs():
                            for key in ('input_path','audio','transcript','document'):
                                source=job.get(key)
                                if source:
                                    try:
                                        if os.path.samefile(target,source):raise RemoteError('هذا الملف أصل مهمة؛ لا يمكن حذفه من الهاتف.',409)
                                    except FileNotFoundError:pass
                        self._check(context)
                        _guard_internal_path(root,root/MANIFEST)
                        self._unlink_checked(target,row)
                    _guard_internal_path(root,root/MANIFEST)
                    atomic_json(root/MANIFEST,manifest)
                self._scan(cfg)
                return {'message':'حُذفت نسخة الملف من المكتبة.','revision':self._revision}
            except RemoteError:raise
            except (OSError,ValueError):raise RemoteError('تعذر حذف الملف. قد يكون مفتوحًا أو تغير موقعه.',409) from None
        return self._submit(work,context)

    @staticmethod
    def _unlink_checked(target,row):
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
            kernel.CreateFileW.restype=wintypes.HANDLE
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            kernel.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            handle=kernel.CreateFileW(str(target),0x10000|0x80000000,1,None,3,0x00200000,None)
            if handle==wintypes.HANDLE(-1).value:raise OSError('open')
            import msvcrt
            descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
            with os.fdopen(descriptor,'rb') as stream:
                _validate_path(Path(row['root']),row['relative'])
                if os.path.normcase(str(_windows_final_path(stream)))!=_canonical_windows_path(target) or _version(os.fstat(stream.fileno()))!=row['version']:
                    raise RemoteError('تغير الملف قبل الحذف.',409)
                disposition=wintypes.BOOL(True)
                if not kernel.SetFileInformationByHandle(handle,4,ctypes.byref(disposition),ctypes.sizeof(disposition)):raise OSError('delete')
        else:
            # Walk each ancestor using held no-follow descriptors. Unlink is
            # relative to the verified parent, never to a re-resolved path.
            descriptor=os.open(target.anchor,os.O_RDONLY|os.O_DIRECTORY)
            try:
                for part in target.parts[1:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                    os.close(descriptor);descriptor=child
                current=os.stat(target.name,dir_fd=descriptor,follow_symlinks=False)
                if _version(current)!=row['version']:raise RemoteError('تغير الملف قبل الحذف.',409)
                os.unlink(target.name,dir_fd=descriptor)
            finally:os.close(descriptor)

    def organize(self,cfg,context=None):
        with self._lock:
            if self._organizing:return {'message':'يجري تنظيم المكتبة.','busy':True}
            self._organizing=True
        def work():
            try:
                self._check(context)
                # Each publisher takes the same library lock. Cancellation is
                # rechecked before each individual copy, not only before queuing.
                root=Path(cfg['library_root']).absolute()
                def check(path=None):
                    self._check(context)
                    self._current_root(str(root))
                    path=Path(path).absolute() if path is not None else root
                    if not path.is_relative_to(root):raise RemoteError('مسار النشر خارج المكتبة.',409)
                    for ancestor in list(reversed(path.parents))+[path]:
                        try:_no_link(ancestor)
                        except FileNotFoundError:pass
                self.library.organize_current(self.bridge.store,cfg,check=check)
                self._scan(cfg)
            except Exception:
                with self._lock:self._error='تعذر تنظيم المكتبة. تحقق من الإعدادات وصلاحيات الملفات.'
            finally:
                with self._lock:self._organizing=False
        future=self._submit(work,context)
        def finished(done):
            with self._lock:self._organizing=False
        future.add_done_callback(finished)
        return {'message':'بدأ تنظيم المكتبة.','busy':True}
