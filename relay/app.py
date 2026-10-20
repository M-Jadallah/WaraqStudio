"""Device-scoped, single-process WSS gateway. Run one Uvicorn worker.

The VPS is a trusted transport endpoint, not an end-to-end encrypted store.
No job content, files, or plaintext credentials are persisted here.
"""
from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import time
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from starlette.requests import ClientDisconnect
from waraq.mobile_assets import ASSET_TYPES, PWA_PAGES, MOBILE_CSP, library_query

from .config import Settings
from .db import Claim, Database, Device, Enrollment, Grant, Phone, Ticket, digest, token

COOKIE = '__Host-waraq_session'
PENDING_COOKIE = '__Host-waraq_pending'
CHUNK_SIZE = 65536
MAX_MESSAGE = 15 * 1024 * 1024
MAX_CONTROL = 256 * 1024
ASSETS = Path(__file__).resolve().parent.parent / 'waraq' / 'mobile'
ACTIONS = {'queue', 'cancel', 'retry', 'stage', 'discover', 'enqueue', 'upload', 'library_delete', 'library_organize'}
PHONE_EXISTS = 'هذا الجهاز مرتبط بهاتف بالفعل. ألغِ ربط الهاتف الحالي من ورق ثم أنشئ QR جديدًا.'
PAIR_RESERVED = 'يوجد اقتران تمت الموافقة عليه ولم يكتمل بعد. انتظر اكتماله أو انتهاء صلاحيته.'


class BodyLimit(HTTPException):
    def __init__(self):
        super().__init__(413, 'حجم الطلب أكبر من المسموح.')


class SecurityMiddleware:
    """Validate the actual Host, never untrusted forwarded host headers."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.host = urlsplit(settings.public_base_url).netloc.lower()
        self.uploads = defaultdict(int)
        self.total_uploads = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        host = headers.get(b'host', b'').decode('latin1').lower()
        origin = headers.get(b'origin', b'').decode('latin1')
        bad = host != self.host or bool(origin and origin != self.settings.public_base_url)
        # QR scanners and external links can initiate a cross-site top-level
        # navigation. These routes serve only the public HTML shell; actual
        # pairing, device state, commands and files remain separately protected.
        path = scope.get('path', '')
        public_page = path in ('/', '/internet', '/pair') or (
            path.startswith('/devices/') and len(path.split('/')) == 3 and bool(path.split('/')[2]))
        public_navigation = (scope['type'] == 'http' and scope.get('method') == 'GET' and public_page
                             and headers.get(b'sec-fetch-mode') == b'navigate'
                             and headers.get(b'sec-fetch-dest') == b'document')
        if headers.get(b'sec-fetch-site') == b'cross-site' and not public_navigation:
            bad = True
        if scope['type'] == 'http' and scope['method'] not in ('GET', 'HEAD'):
            if scope['path'] != '/api/enroll':
                bad |= origin != self.settings.public_base_url or headers.get(b'x-waraq-client') != b'1'
            bad |= not headers.get(b'content-type', b'').startswith(b'application/json')
        if bad:
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 4403})
            return await JSONResponse({'error': 'مصدر الطلب غير مسموح.'}, 403)(scope, receive, send)
        size = 0
        parts = scope.get('path', '').split('/')
        upload_device = (parts[3] if scope['type'] == 'http' and scope['method'] == 'POST' and
                         len(parts) == 6 and parts[1:3] == ['api', 'devices'] and
                         parts[4:] == ['action', 'upload'] else None)
        body_limit = self.settings.max_body_bytes if upload_device else min(self.settings.max_body_bytes, 256*1024)

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > body_limit:
                raise BodyLimit()
            return message

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'), (b'x-frame-options', b'DENY'),
                    (b'cross-origin-resource-policy', b'same-origin'),
                    (b'strict-transport-security', b'max-age=31536000'),
                    (b'content-security-policy', MOBILE_CSP.encode('ascii'))])
            await send(message)
        if upload_device:
            # Reject immediately before reading any upload bytes; retain the
            # permit until its command completes so parsed JSON also counts.
            if self.total_uploads >= 4 or self.uploads[upload_device] >= 2:
                return await JSONResponse({'error': 'توجد عمليات رفع قيد التنفيذ. انتظر اكتمالها.'}, 503)(scope, receive, secure_send)
            self.total_uploads += 1
            self.uploads[upload_device] += 1
        try:
            try:
                await self.app(scope, limited_receive, secure_send)
            except BodyLimit:
                await JSONResponse({'error': 'حجم الطلب أكبر من المسموح.'}, 413)(scope, receive, secure_send)
        finally:
            if upload_device:
                self.total_uploads -= 1
                self.uploads[upload_device] -= 1
                if not self.uploads[upload_device]:
                    del self.uploads[upload_device]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class EnrollBody(StrictModel):
    code: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=120)


class PairBody(StrictModel):
    ticket: str = Field(min_length=20, max_length=200)
    phone_name: str = Field(default='هاتفي', min_length=1, max_length=80)


@dataclass
class Pending:
    id: str
    device_id: str
    phone_id: str
    file: bool
    ready: asyncio.Future
    chunks: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    size: int = 0
    received: int = 0
    ended: bool = False
    error: HTTPException | None = None


@dataclass
class Connection:
    ws: WebSocket
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message, before_send=None):
        async with self.send_lock:
            if before_send is not None and not before_send():
                raise HTTPException(403, 'أُلغي اقتران الهاتف قبل إرسال الأمر.')
            if len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_MESSAGE:
                raise HTTPException(413, 'حجم الرسالة أكبر من المسموح؛ قلل البيانات ثم أعد الطلب.')
            await asyncio.wait_for(self.ws.send_json(message), timeout=10)


class Gateway:
    def __init__(self, settings, database):
        self.settings, self.db = settings, database
        self.connections: dict[str, Connection] = {}
        self.pending: dict[str, Pending] = {}
        self.hits = defaultdict(deque)

    def throttle(self, bucket, maximum=20):
        now = time.monotonic()
        if len(self.hits) > 4096:
            self.hits = defaultdict(deque, {k: v for k, v in self.hits.items() if v and v[-1] > now - 60})
        hits = self.hits[bucket]
        while hits and hits[0] < now - 60:
            hits.popleft()
        if len(hits) >= maximum:
            raise HTTPException(429, 'محاولات كثيرة. انتظر دقيقة ثم حاول مجددًا.')
        hits.append(now)

    def phone(self, request, device_id=None):
        raw = request.cookies.get(COOKIE, '')
        with self.db.session.begin() as session:
            phone = session.scalar(select(Phone).where(Phone.token_hash == digest(raw))) if raw else None
            now = time.time()
            if not phone or phone.expires <= now or phone.last_seen < now - self.settings.idle_days * 86400:
                raise HTTPException(401, 'انتهت جلسة الهاتف. أعد الاقتران من الكمبيوتر.')
            if device_id:
                device = session.get(Device, device_id)
                if not device or device.revoked or not session.get(Grant, (phone.id, device_id)):
                    raise HTTPException(403, 'هذا الهاتف غير مقترن بالجهاز المطلوب.')
            phone.last_seen = now
            return phone.id

    def grant_valid(self, phone_id, device_id):
        with self.db.session() as session:
            phone, device = session.get(Phone, phone_id), session.get(Device, device_id)
            return bool(phone and device and not device.revoked and phone.expires > time.time() and
                        phone.last_seen > time.time() - self.settings.idle_days * 86400 and
                        session.get(Grant, (phone_id, device_id)))

    def lock_device(self, session, device_id):
        # All pairing transitions lock this same row. PostgreSQL serializes
        # approval/collection/revocation; the unique grant index is the final
        # invariant even if a different client writes to the database.
        device = session.scalar(select(Device).where(Device.id == device_id).with_for_update()
                                .execution_options(populate_existing=True))
        if not device or device.revoked:
            raise HTTPException(403, 'أُلغي تسجيل هذا الجهاز.')
        return device

    def occupied(self, session, device_id, now):
        grant = session.scalars(select(Grant).where(Grant.device_id == device_id)).one_or_none()
        if not grant:
            return False
        phone = session.get(Phone, grant.phone_id)
        if phone and phone.expires > now and phone.last_seen > now-self.settings.idle_days*86400:
            return True
        # An expired/idle browser has no live permission. Free only that
        # device's slot; it cannot be refreshed back into an authorized grant.
        session.delete(grant)
        session.flush()
        return False

    @staticmethod
    def approved_claim(session, device_id, now, excluding=None):
        statement = select(Claim.id).where(Claim.device_id == device_id,
                      Claim.status == 'approved', Claim.expires > now)
        if excluding:
            statement = statement.where(Claim.id != excluding)
        return session.scalar(statement.limit(1))

    async def cancel(self, pending, error=None):
        if error:
            pending.error = error
            if not pending.ready.done():
                pending.ready.set_result(None)
            # Clear already buffered bytes on cancellation/revocation.
            while not pending.chunks.empty():
                pending.chunks.get_nowait()
            pending.chunks.put_nowait(None)
        conn = self.connections.get(pending.device_id)
        if conn:
            try:
                await conn.send({'type': 'cancel', 'id': pending.id})
            except Exception:
                pass

    async def cancel_grants(self, device_id, phone_id=None):
        for pending in list(self.pending.values()):
            if pending.device_id == device_id and (phone_id is None or pending.phone_id == phone_id):
                await self.cancel(pending, HTTPException(403, 'أُلغي اقتران الهاتف.'))

    async def begin(self, device_id, phone_id, action, data, file=False):
        if not self.grant_valid(phone_id, device_id):
            raise HTTPException(403, 'أُلغي اقتران الهاتف.')
        conn = self.connections.get(device_id)
        if not conn:
            raise HTTPException(503, 'الجهاز غير متصل. افتح ورق على الكمبيوتر.')
        if len(self.pending) >= 128 or sum(p.device_id == device_id for p in self.pending.values()) >= 16:
            raise HTTPException(503, 'الجهاز مشغول بطلبات كثيرة؛ حاول بعد لحظات.')
        ident = token()
        pending = Pending(ident, device_id, phone_id, file, asyncio.get_running_loop().create_future())
        self.pending[ident] = pending
        timeout = self.settings.transfer_timeout if file else self.settings.request_timeout
        try:
            await conn.send({'type': 'request', 'id': ident, 'action': action, 'data': data,
                             'deadline': time.time() + timeout, 'protocol': 1},
                            before_send=lambda: not pending.error and self.grant_valid(phone_id, device_id))
        except HTTPException:
            self.pending.pop(ident, None)
            raise
        except Exception:
            self.pending.pop(ident, None)
            raise HTTPException(503, 'انقطع اتصال الكمبيوتر. تحقق من حالة المهمة قبل إعادة الأمر.') from None
        return pending

    async def wait_ready(self, request, pending):
        timeout = self.settings.transfer_timeout if pending.file else self.settings.request_timeout
        until = time.monotonic() + timeout
        while not pending.ready.done():
            if await request.is_disconnected():
                raise ClientDisconnect()
            if time.monotonic() >= until:
                raise HTTPException(504, 'انتهت مهلة الطلب؛ تحقق من حالة المهمة قبل تكراره.')
            await asyncio.wait({pending.ready}, timeout=min(0.2, max(0, until-time.monotonic())))
        if pending.error:
            raise pending.error
        if not self.grant_valid(pending.phone_id, pending.device_id):
            raise HTTPException(403, 'أُلغي اقتران الهاتف.')
        return pending.ready.result()

    async def control(self, device_id, action, data):
        now = time.time()
        if action == 'pair_create':
            value = token()
            with self.db.session.begin() as session:
                self.lock_device(session, device_id)
                now = time.time()
                if self.occupied(session, device_id, now):
                    raise HTTPException(409, PHONE_EXISTS)
                if self.approved_claim(session, device_id, now):
                    raise HTTPException(409, PAIR_RESERVED)
                session.execute(update(Claim).where(Claim.device_id == device_id,
                                Claim.status.in_(('pending', 'approved'))).values(status='rejected'))
                session.execute(delete(Ticket).where(Ticket.device_id == device_id))
                session.add(Ticket(ticket_hash=digest(value), device_id=device_id, expires=now+300))
            return {'ticket': value, 'url': self.settings.public_base_url + '/pair#' + value, 'expires_at': now+300}
        if action == 'pair_approve':
            if not isinstance(data.get('approved'), bool):
                raise HTTPException(400, 'قرار الاقتران غير صالح.')
            with self.db.session.begin() as session:
                self.lock_device(session, device_id)
                now = time.time()
                claim = session.get(Claim, str(data.get('claim_id', '')))
                if not claim or claim.device_id != device_id or claim.status != 'pending' or claim.expires <= now:
                    raise HTTPException(404, 'طلب الاقتران غير متاح.')
                if data['approved']:
                    if self.occupied(session, device_id, now):
                        raise HTTPException(409, PHONE_EXISTS)
                    if self.approved_claim(session, device_id, now, excluding=claim.id):
                        raise HTTPException(409, PAIR_RESERVED)
                    session.execute(update(Claim).where(Claim.device_id == device_id, Claim.id != claim.id,
                                    Claim.status.in_(('pending', 'approved'))).values(status='rejected'))
                    session.execute(delete(Ticket).where(Ticket.device_id == device_id))
                claim.status = 'approved' if data['approved'] else 'rejected'
            return {'status': claim.status}
        if action == 'phones':
            with self.db.session.begin() as session:
                self.lock_device(session, device_id)
                self.occupied(session, device_id, now)
                rows = session.execute(select(Grant, Phone).join(Phone).where(Grant.device_id == device_id)).all()
                return {'phones': [{'id': p.id, 'name': g.name, 'created_at': g.created_at,
                                    'last_seen': p.last_seen} for g, p in rows if p.expires > now]}
        if action in ('revoke', 'device_revoke'):
            phone_id = str(data.get('phone_id', '')) if action == 'revoke' else None
            with self.db.session.begin() as session:
                self.lock_device(session, device_id)
                query = delete(Grant).where(Grant.device_id == device_id)
                if phone_id is not None:
                    query = query.where(Grant.phone_id == phone_id)
                session.execute(query)
                # Pending and not-yet-collected approvals must not restore revoked grants.
                session.execute(update(Claim).where(Claim.device_id == device_id).values(status='rejected'))
                session.execute(delete(Ticket).where(Ticket.device_id == device_id))
                if action == 'device_revoke':
                    session.get(Device, device_id).revoked = True
            await self.cancel_grants(device_id, phone_id)
            return {'message': 'أُلغي الربط.'}
        raise HTTPException(404, 'إجراء إدارة غير مدعوم.')


def create_app(settings=None, database=None):
    settings = settings or Settings.from_env()
    database = database or Database(settings.database_url)
    gateway = Gateway(settings, database)

    @asynccontextmanager
    async def lifespan(app):
        yield
        for conn in list(gateway.connections.values()):
            await conn.ws.close(code=1012)
        database.engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gateway
    app.add_middleware(SecurityMiddleware, settings=settings)

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse({'error': exc.detail}, exc.status_code,
                            headers={'Retry-After': '60'} if exc.status_code == 429 else None)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'error': 'صيغة الطلب غير صالحة.'}, 400)

    @app.get('/healthz')
    async def health():
        with database.session() as session:
            session.execute(select(1))
        return {'status': 'ok', 'protocol': 1}

    @app.get('/api/bootstrap')
    async def bootstrap():
        return {'mode': 'internet', 'protocol': 1}

    @app.get('/')
    async def root():
        return RedirectResponse('/internet', 302)

    @app.get('/internet')
    @app.get('/pair')
    @app.get('/devices/{device_id}')
    async def index():
        return FileResponse(ASSETS / 'index.html')

    @app.get('/assets/{name:path}')
    async def assets(name: str):
        if name not in ASSET_TYPES:
            raise HTTPException(404, 'الملف غير موجود.')
        path = ASSETS.parent / 'icon.svg' if name == 'icon.svg' else ASSETS / name
        return FileResponse(path, media_type=ASSET_TYPES[name])

    async def pwa_asset(request: Request):
        name, media_type = PWA_PAGES[request.url.path]
        headers = {'Service-Worker-Allowed': '/'} if name == 'sw.js' else {}
        return FileResponse(ASSETS / name, media_type=media_type, headers=headers)
    for path in PWA_PAGES:
        app.add_api_route(path, pwa_asset, methods=['GET'])

    @app.post('/api/enroll')
    async def enroll(body: EnrollBody, request: Request):
        gateway.throttle('enroll-global', 60)
        gateway.throttle(('enroll', request.client.host), 10)
        secret, ident = token(), token()
        with database.session.begin() as session:
            # Conditional UPDATE is atomic across concurrent requests/processes.
            result = session.execute(update(Enrollment).where(Enrollment.code_hash == digest(body.code),
                                      Enrollment.expires > time.time(), Enrollment.used.is_(False)).values(used=True))
            if result.rowcount != 1:
                raise HTTPException(401, 'رمز التسجيل غير صالح أو مستخدم أو منتهي.')
            session.add(Device(id=ident, name=body.name.strip() or 'ورق', secret_hash=digest(secret)))
        return {'device_id': ident, 'device_secret': secret, 'name': body.name}

    @app.post('/api/pair/start')
    async def pair_start(body: PairBody, request: Request):
        gateway.throttle('pair-global', 120)
        gateway.throttle(('pair', request.client.host), 20)
        ident, secret, match = token(), token(), ''.join(secrets.choice('0123456789') for _ in range(6))
        now = time.time()
        with database.session.begin() as session:
            ticket = session.get(Ticket, digest(body.ticket))
            if not ticket or ticket.used or ticket.expires <= now:
                raise HTTPException(401, 'تذكرة الاقتران مستخدمة أو منتهية.')
            device = session.get(Device, ticket.device_id)
            if not device or device.revoked or device.id not in gateway.connections:
                raise HTTPException(503, 'الجهاز غير متصل.')
            gateway.lock_device(session, device.id)
            now = time.time()
            ticket = session.get(Ticket, digest(body.ticket), populate_existing=True)
            if not ticket or ticket.used or ticket.expires <= now:
                raise HTTPException(401, 'تذكرة الاقتران مستخدمة أو منتهية.')
            if gateway.occupied(session, device.id, now):
                raise HTTPException(409, PHONE_EXISTS)
            if gateway.approved_claim(session, device.id, now):
                raise HTTPException(409, PAIR_RESERVED)
            consumed = session.execute(update(Ticket).where(Ticket.ticket_hash == ticket.ticket_hash,
                                      Ticket.used.is_(False)).values(used=True))
            if consumed.rowcount != 1:
                raise HTTPException(401, 'تذكرة الاقتران مستخدمة.')
            session.add(Claim(id=ident, secret_hash=digest(secret), device_id=device.id,
                              phone_name=body.phone_name, match_code=match, expires=now+300))
            device_id, device_name = device.id, device.name
        try:
            await gateway.connections[device_id].send({'type': 'pair_claim', 'claim_id': ident,
                  'phone_name': body.phone_name, 'match_code': match, 'expires_at': now+300})
        except Exception:
            raise HTTPException(503, 'انقطع اتصال الجهاز؛ أعد إنشاء تذكرة الاقتران.') from None
        response = JSONResponse({'claim_id': ident, 'match_code': match, 'device_name': device_name})
        response.set_cookie(PENDING_COOKIE, secret, max_age=300, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/api/pair/status/{claim_id}')
    async def pair_status(claim_id: str, request: Request):
        secret = request.cookies.get(PENDING_COOKIE, '')
        try:
            with database.session.begin() as session:
                claim = session.get(Claim, claim_id)
                if not claim or not secret or not secrets.compare_digest(claim.secret_hash, digest(secret)):
                    raise HTTPException(404, 'طلب الاقتران غير موجود.')
                device = session.scalar(select(Device).where(Device.id == claim.device_id).with_for_update())
                # Re-read after the per-device lock in case approval/revocation
                # changed while a transaction was waiting for that lock.
                claim = session.get(Claim, claim_id, populate_existing=True)
                now = time.time()
                status = 'expired' if claim.expires <= now else claim.status
                if not device or device.revoked:
                    status = 'rejected'
                result = {'status': status, 'device_id': claim.device_id, 'device_name': device.name if device else ''}
                response = JSONResponse(result)
                if status == 'approved':
                    # Approval reserves the slot; collection must verify it
                    # again rather than trusting a stale poll or browser id.
                    if gateway.occupied(session, device.id, now) or gateway.approved_claim(session, device.id, now, excluding=claim.id):
                        claim.status = 'rejected'
                        return JSONResponse({**result, 'status': 'rejected', 'error': PHONE_EXISTS}, 409)
                    consumed = session.execute(update(Claim).where(Claim.id == claim.id,
                              Claim.status == 'approved', Claim.expires > now).values(status='consumed'))
                    if consumed.rowcount != 1:
                        raise HTTPException(409, 'تغيّرت حالة طلب الاقتران. أنشئ QR جديدًا من ورق.')
                    raw = request.cookies.get(COOKIE, '')
                    phone = session.scalar(select(Phone).where(Phone.token_hash == digest(raw))) if raw else None
                    if not phone or phone.expires <= now or phone.last_seen < now-settings.idle_days*86400:
                        raw = token()
                        phone = Phone(id=token(), token_hash=digest(raw), expires=now+settings.session_days*86400, last_seen=now)
                        session.add(phone)
                        session.flush()
                    session.add(Grant(phone_id=phone.id, device_id=device.id, name=claim.phone_name, created_at=now))
                    session.flush()  # Unique device index is the final guard.
                    response.set_cookie(COOKIE, raw, max_age=int(phone.expires-now), secure=True, httponly=True, samesite='strict', path='/')
                    response.delete_cookie(PENDING_COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        except IntegrityError:
            # No cookie or partially created grant survives the rolled-back
            # transaction if a competing writer obtained the device slot.
            raise HTTPException(409, PHONE_EXISTS) from None
        return response

    @app.get('/api/devices')
    async def devices(request: Request):
        phone_id = gateway.phone(request)
        with database.session() as session:
            rows = session.scalars(select(Device).join(Grant).where(Grant.phone_id == phone_id, Device.revoked.is_(False))).all()
            return {'devices': [{'device_id': d.id, 'name': d.name, 'online': d.id in gateway.connections,
                                 'last_seen': d.last_seen} for d in rows]}

    async def perform(request, device_id, action, data):
        phone_id = gateway.phone(request, device_id)
        gateway.throttle(('actions', phone_id), 180)
        pending = await gateway.begin(device_id, phone_id, action, data)
        try:
            return await gateway.wait_ready(request, pending)
        finally:
            gateway.pending.pop(pending.id, None)
            if pending.error or not pending.ready.done():
                await gateway.cancel(pending)

    @app.get('/api/devices/{device_id}/state')
    async def state(device_id: str, request: Request):
        return await perform(request, device_id, 'state', {})

    @app.get('/api/devices/{device_id}/library/files')
    async def library_list(device_id: str, request: Request):
        gateway.phone(request, device_id)
        try:
            data = library_query(request.query_params)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return await perform(request, device_id, 'library_list', data)

    @app.get('/api/devices/{device_id}/library/files/{file_id}/preview')
    async def library_preview(device_id: str, file_id: str, request: Request):
        return await perform(request, device_id, 'library_preview',
                             {'file_id': file_id, 'expected_version': request.query_params.get('expected_version')})

    @app.get('/api/devices/{device_id}/library/files/{file_id}/download')
    async def library_download(device_id: str, file_id: str, request: Request):
        return await stream_download(request, device_id, 'library_file',
                                     {'file_id': file_id, 'expected_version': request.query_params.get('expected_version')})

    @app.post('/api/devices/{device_id}/action/{action}')
    async def action(device_id: str, action: str, request: Request):
        gateway.phone(request, device_id)
        if action not in ACTIONS:
            raise HTTPException(404, 'الأمر غير مدعوم.')
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'بيانات الطلب غير صالحة.') from None
        if not isinstance(data, dict):
            raise HTTPException(400, 'بيانات الطلب غير صالحة.')
        return await perform(request, device_id, action, data)

    @app.get('/api/devices/{device_id}/files/{job_id}/{kind}')
    async def download(device_id: str, job_id: str, kind: str, request: Request):
        gateway.phone(request, device_id)
        if kind not in ('document', 'transcript') or len(job_id) > 128:
            raise HTTPException(404, 'الملف غير موجود.')
        return await stream_download(request, device_id, 'file', {'id': job_id, 'kind': kind})

    async def stream_download(request, device_id, action, data):
        phone_id = gateway.phone(request, device_id)
        gateway.throttle(('files', phone_id), 30)
        pending = await gateway.begin(device_id, phone_id, action, data, file=True)
        try:
            metadata = await gateway.wait_ready(request, pending)
        except BaseException:
            gateway.pending.pop(pending.id, None)
            await gateway.cancel(pending)
            raise

        async def stream():
            until = time.monotonic() + settings.transfer_timeout
            try:
                while True:
                    if not gateway.grant_valid(phone_id, device_id):
                        raise HTTPException(403, 'أُلغي اقتران الهاتف.')
                    chunk = await asyncio.wait_for(pending.chunks.get(), max(0.01, until-time.monotonic()))
                    if pending.error:
                        raise pending.error
                    if chunk is None:
                        break
                    yield chunk
            finally:
                gateway.pending.pop(pending.id, None)
                await gateway.cancel(pending)
        return StreamingResponse(stream(), media_type=metadata['content_type'], headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(metadata['name'], safe=''),
            'Content-Length': str(metadata['size'])})

    @app.post('/api/logout')
    async def logout(request: Request):
        phone_id = gateway.phone(request)
        with database.session.begin() as session:
            session.execute(delete(Grant).where(Grant.phone_id == phone_id))
            session.execute(delete(Phone).where(Phone.id == phone_id))
        for pending in list(gateway.pending.values()):
            if pending.phone_id == phone_id:
                await gateway.cancel(pending, HTTPException(401, 'انتهت جلسة الهاتف.'))
        response = JSONResponse({'message': 'قُطع اتصال هذا الهاتف.'})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.post('/api/devices/{device_id}/logout')
    async def device_logout(device_id: str, request: Request):
        phone_id = gateway.phone(request, device_id)
        with database.session.begin() as session:
            gateway.lock_device(session, device_id)
            session.execute(delete(Grant).where(Grant.phone_id == phone_id, Grant.device_id == device_id))
            session.execute(update(Claim).where(Claim.device_id == device_id,
                            Claim.status.in_(('pending', 'approved'))).values(status='rejected'))
            session.execute(delete(Ticket).where(Ticket.device_id == device_id))
        await gateway.cancel_grants(device_id, phone_id)
        return {'message': 'قُطع اتصال الهاتف بهذا الجهاز.'}

    @app.websocket('/ws/device')
    async def device_socket(ws: WebSocket):
        device_id = ws.query_params.get('device_id', '')
        auth = ws.headers.get('authorization', '')
        candidate = auth[7:] if auth.startswith('Bearer ') else ''
        try:
            gateway.throttle('ws-global', 300)
        except HTTPException:
            await ws.close(code=4429)
            return
        with database.session() as session:
            device = session.get(Device, device_id)
            if not device or device.revoked or not candidate or not secrets.compare_digest(device.secret_hash, digest(candidate)):
                await ws.close(code=4401)
                return
        if device_id in gateway.connections or len(gateway.connections) >= 100:
            await ws.close(code=4409)
            return
        conn = Connection(ws)
        # Reserve before accepting (an await) so two authenticated handshakes
        # cannot race and silently replace the connection for one identity.
        gateway.connections[device_id] = conn
        try:
            await ws.accept()
        except BaseException:
            gateway.connections.pop(device_id, None)
            raise
        try:
            with database.session.begin() as session:
                session.get(Device, device_id).last_seen = time.time()
            await conn.send({'type': 'welcome', 'protocol': 1, 'device_id': device_id})
            while True:
                raw = await ws.receive_text()
                raw_size = len(raw.encode('utf-8'))
                if raw_size > MAX_MESSAGE:
                    await ws.close(code=1009)
                    break
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError()
                    kind = message.get('type')
                    if kind in ('control', 'ping', 'file_start', 'file_end') and raw_size > MAX_CONTROL:
                        raise ValueError()
                    if kind == 'ping':
                        await conn.send({'type': 'pong'})
                        continue
                    if kind == 'control':
                        ident = str(message.get('id', ''))[:128]
                        data = message.get('data', {})
                        if not isinstance(data, dict):
                            raise ValueError()
                        try:
                            gateway.throttle(('control', device_id), 120)
                            result = await gateway.control(device_id, message.get('action'), data)
                            await conn.send({'type': 'control_result', 'id': ident, 'ok': True, 'result': result})
                            if message.get('action') == 'device_revoke':
                                await ws.close(code=4403)
                                break
                        except HTTPException as exc:
                            await conn.send({'type': 'control_result', 'id': ident, 'ok': False, 'error': exc.detail, 'status': exc.status_code})
                        continue
                    pending = gateway.pending.get(message.get('id'))
                    if not pending or pending.device_id != device_id:
                        continue  # Ignore late, expired, or another device's response.
                    if not gateway.grant_valid(pending.phone_id, device_id):
                        await gateway.cancel(pending, HTTPException(403, 'أُلغي اقتران الهاتف.'))
                        continue
                    if kind == 'response':
                        if message.get('ok') is True and not pending.file:
                            if not pending.ready.done():
                                pending.ready.set_result(message.get('result', {}))
                        elif message.get('ok') is False:
                            status = message.get('status', 400)
                            status = status if type(status) is int and status in (400, 403, 404, 409, 413, 429, 503) else 400
                            failure = HTTPException(status, str(message.get('error', 'تعذر تنفيذ الطلب.'))[:2000])
                            # Read errors can arrive after file_start while the
                            # HTTP consumer is waiting for the next chunk.
                            await gateway.cancel(pending, failure)
                    elif kind == 'file_start' and pending.file and not pending.ready.done():
                        size, name = message.get('size'), message.get('name')
                        if (type(size) is not int or not 0 <= size <= settings.max_file_bytes or
                                not isinstance(name, str) or not name or len(name) > 180 or
                                any(c in name for c in '/\\\r\n') or any(ord(c) < 32 for c in name)):
                            await gateway.cancel(pending, HTTPException(413, 'حجم الملف أو اسمه غير مسموح.'))
                            continue
                        pending.size = size
                        content_type = ('application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                                        if name.lower().endswith('.docx') else 'application/octet-stream')
                        pending.ready.set_result({'size': size, 'name': name, 'content_type': content_type})
                    elif kind == 'file_chunk' and pending.file and pending.ready.done() and not pending.error and not pending.ended:
                        data = message.get('data')
                        if not isinstance(data, str) or len(data) > 4*((CHUNK_SIZE+2)//3):
                            raise ValueError()
                        content = base64.b64decode(data, validate=True)
                        pending.received += len(content)
                        if len(content) > CHUNK_SIZE or pending.received > pending.size:
                            raise ValueError()
                        await asyncio.wait_for(pending.chunks.put(content), timeout=10)
                    elif kind == 'file_end' and pending.file and pending.ready.done() and not pending.ended:
                        if pending.received != pending.size:
                            raise ValueError()
                        pending.ended = True
                        await asyncio.wait_for(pending.chunks.put(None), timeout=10)
                except (ValueError, TypeError, asyncio.TimeoutError):
                    await ws.close(code=1008)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if gateway.connections.get(device_id) is conn:
                gateway.connections.pop(device_id, None)
            with database.session.begin() as session:
                session.get(Device, device_id).last_seen = time.time()
            for pending in list(gateway.pending.values()):
                if pending.device_id == device_id and not pending.ended:
                    await gateway.cancel(pending, HTTPException(503, 'انقطع اتصال الكمبيوتر؛ تحقق من الحالة قبل تكرار الأمر.'))
    return app


def application():
    """Uvicorn factory: relay.app:application --factory --workers 1."""
    return create_app()
