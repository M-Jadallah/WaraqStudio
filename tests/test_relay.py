"""Security boundaries are exercised against actual ASGI HTTP/WebSocket routes."""
import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from relay.app import COOKIE, MAX_MESSAGE, PENDING_COOKIE, Connection, Gateway, SecurityMiddleware, create_app
from relay.config import Settings
from relay.db import Claim, Database, Device, Enrollment, Grant, Phone, Ticket, digest

ORIGIN = 'https://relay.test'
HEADERS = {'Origin': ORIGIN, 'X-Waraq-Client': '1'}


@pytest.fixture
def setup_gateway(tmp_path):
    settings = Settings(ORIGIN, f'sqlite:///{(tmp_path / "gateway.db").as_posix()}', request_timeout=1)
    database = Database(settings.database_url)
    database.initialize()
    app = create_app(settings, database)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app.state.gateway


def enroll(client, gateway, name='الجهاز A'):
    code = gateway.db.issue_enrollment()
    result = client.post('/api/enroll', json={'code': code, 'name': name})
    assert result.status_code == 200, result.text
    return result.json()


def connect(client, device):
    return client.websocket_connect(f'wss://relay.test/ws/device?device_id={device["device_id"]}',
                                    headers={'Authorization': 'Bearer '+device['device_secret']})


def control(ws, action, data=None):
    ws.send_json({'type': 'control', 'id': 'control-123456', 'action': action, 'data': data or {}})
    result = ws.receive_json()
    assert result['type'] == 'control_result', result
    return result


def pair(client, ws, phone_name='هاتفي'):
    ticket = control(ws, 'pair_create')['result']['ticket']
    start = client.post('/api/pair/start', json={'ticket': ticket, 'phone_name': phone_name}, headers=HEADERS)
    assert start.status_code == 200, start.text
    event = ws.receive_json()
    assert event['type'] == 'pair_claim'
    assert start.json()['match_code'] == event['match_code']
    ident = start.json()['claim_id']
    pending = client.get('/api/pair/status/'+ident)
    assert pending.json()['status'] == 'pending'
    approved = control(ws, 'pair_approve', {'claim_id': ident, 'approved': True})
    assert approved['ok'] is True
    status = client.get('/api/pair/status/'+ident)
    assert status.json()['status'] == 'approved', status.text
    assert COOKIE in client.cookies
    assert 'Secure' in status.headers['set-cookie']
    assert 'HttpOnly' in status.headers['set-cookie']
    assert 'SameSite=strict' in status.headers['set-cookie']
    return ident


def test_enrollment_is_single_use_hashed_and_operator_only(setup_gateway):
    client, gateway = setup_gateway
    code = gateway.db.issue_enrollment()
    one = client.post('/api/enroll', json={'code': code, 'name': 'A'})
    assert one.status_code == 200
    assert client.post('/api/enroll', json={'code': code, 'name': 'B'}).status_code == 401
    assert client.post('/api/enroll', json={'code': 'x'*43, 'name': 'B'}).status_code == 401
    with gateway.db.session() as session:
        device = session.get(Device, one.json()['device_id'])
        assert device.secret_hash == digest(one.json()['device_secret'])
        assert device.secret_hash != one.json()['device_secret']
        assert session.get(Enrollment, digest(code)).used is True


def test_host_origin_csrf_and_json_are_strict(setup_gateway):
    client, _ = setup_gateway
    for headers in ({'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'}, {'Sec-Fetch-Site': 'cross-site'}):
        assert client.get('/api/bootstrap', headers=headers).status_code == 403
    assert client.post('/api/logout', json={}).status_code == 403
    assert client.post('/api/logout', json={}, headers={'Origin': ORIGIN}).status_code == 403
    assert client.post('/api/logout', content='{}', headers=HEADERS).status_code == 403
    assert client.get('/api/bootstrap').json() == {'mode': 'internet', 'protocol': 1}
    assert client.get('/healthz').json()['status'] == 'ok'
    assert client.get('/api/devices').status_code == 401
    assert client.get('/assets/config.py').status_code == 404
    assert client.get('/api/bootstrap').headers['cache-control'] == 'no-store'


def test_wrong_device_secret_and_duplicate_connection_rejected(setup_gateway):
    client, gateway = setup_gateway
    a, b = enroll(client, gateway), enroll(client, gateway, 'B')
    wrong = {**b, 'device_secret': a['device_secret']}
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(client, wrong):
            pass
    assert exc.value.code == 4401
    with connect(client, a) as first:
        assert first.receive_json()['type'] == 'welcome'
        with pytest.raises(WebSocketDisconnect) as exc:
            with connect(client, a):
                pass
        assert exc.value.code == 4409
        first.send_json({'type': 'ping'})
        assert first.receive_json() == {'type': 'pong'}


def test_pairing_ticket_single_use_and_device_approval_is_scoped(setup_gateway):
    client, gateway = setup_gateway
    a, b = enroll(client, gateway), enroll(client, gateway, 'B')
    with connect(client, a) as wa, connect(client, b) as wb:
        wa.receive_json(); wb.receive_json()
        ticket = control(wa, 'pair_create')['result']['ticket']
        started = client.post('/api/pair/start', headers=HEADERS, json={'ticket': ticket, 'phone_name': 'A phone'})
        assert started.status_code == 200
        wa.receive_json()
        assert client.post('/api/pair/start', headers=HEADERS, json={'ticket': ticket}).status_code == 401
        claim_id = started.json()['claim_id']
        denied = control(wb, 'pair_approve', {'claim_id': claim_id, 'approved': True})
        assert denied['ok'] is False and denied['status'] == 404
        assert client.get('/api/pair/status/'+claim_id).json()['status'] == 'pending'
        assert client.get('/api/pair/status/not-the-claim').status_code == 404
        saved = client.cookies.get(PENDING_COOKIE)
        client.cookies.set(PENDING_COOKIE, 'wrong-secret', domain='relay.test', path='/')
        assert client.get('/api/pair/status/'+claim_id).status_code == 404
        client.cookies.set(PENDING_COOKIE, saved, domain='relay.test', path='/')
        control(wa, 'pair_approve', {'claim_id': claim_id, 'approved': False})
        assert client.get('/api/pair/status/'+claim_id).json()['status'] == 'rejected'
        assert client.get('/api/devices').status_code == 401


def test_phone_a_cannot_view_control_or_download_b(setup_gateway):
    client, gateway = setup_gateway
    a, b = enroll(client, gateway), enroll(client, gateway, 'B')
    with connect(client, a) as wa, connect(client, b) as wb:
        wa.receive_json(); wb.receive_json()
        pair(client, wa)
        listed = client.get('/api/devices').json()['devices']
        assert [item['device_id'] for item in listed] == [a['device_id']]
        assert listed[0]['online'] is True
        base = '/api/devices/'+b['device_id']
        assert client.get(base+'/state').status_code == 403
        assert client.post(base+'/action/queue', json={'running': True}, headers=HEADERS).status_code == 403
        assert client.get(base+'/files/123/document').status_code == 403
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+a['device_id']+'/state')
            command = wa.receive_json()
            assert command['type'] == 'request' and command['action'] == 'state'
            # Device B cannot satisfy a request for A, even if it learns the id.
            wb.send_json({'type': 'response', 'id': command['id'], 'ok': True, 'result': {'device': 'B'}})
            wa.send_json({'type': 'response', 'id': command['id'], 'ok': True, 'result': {'device': 'A'}})
            assert response.result(3).json() == {'device': 'A'}


def test_two_grants_one_browser_explicit_revocation_keeps_other_device(setup_gateway):
    client, gateway = setup_gateway
    a, b = enroll(client, gateway), enroll(client, gateway, 'B')
    with connect(client, a) as wa, connect(client, b) as wb:
        wa.receive_json(); wb.receive_json()
        pair(client, wa)
        pair(client, wb)
        assert len(client.get('/api/devices').json()['devices']) == 2
        phone_id = control(wa, 'phones')['result']['phones'][0]['id']
        assert control(wa, 'revoke', {'phone_id': phone_id})['ok'] is True
        assert client.get('/api/devices/'+a['device_id']+'/state').status_code == 403
        assert [d['device_id'] for d in client.get('/api/devices').json()['devices']] == [b['device_id']]
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+b['device_id']+'/state')
            command = wb.receive_json()
            wb.send_json({'type': 'response', 'id': command['id'], 'ok': True, 'result': {'ok': True}})
            assert response.result(3).status_code == 200


def test_revocation_cancels_in_flight_request(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json()
        pair(client, wa)
        phone_id = control(wa, 'phones')['result']['phones'][0]['id']
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.post, '/api/devices/'+a['device_id']+'/action/queue',
                                   json={'running': True}, headers=HEADERS)
            command = wa.receive_json()
            wa.send_json({'type': 'control', 'id': 'revocation', 'action': 'revoke', 'data': {'phone_id': phone_id}})
            cancelled = wa.receive_json()
            assert cancelled == {'type': 'cancel', 'id': command['id']}
            assert wa.receive_json()['type'] == 'control_result'
            # perform() may send a second idempotent cancellation when it returns.
            assert response.result(3).status_code == 403


def test_timeout_never_replays_command_or_routes_to_other_device(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.post, '/api/devices/'+a['device_id']+'/action/queue',
                                   json={'running': True}, headers=HEADERS)
            command = wa.receive_json()
            assert response.result(4).status_code == 504
            assert wa.receive_json() == {'type': 'cancel', 'id': command['id']}
            wa.send_json({'type': 'response', 'id': command['id'], 'ok': True, 'result': {'old': True}})
            second = pool.submit(client.get, '/api/devices/'+a['device_id']+'/state')
            next_command = wa.receive_json()
            assert next_command['id'] != command['id']
            assert next_command['action'] == 'state'
            wa.send_json({'type': 'response', 'id': next_command['id'], 'ok': True, 'result': {'fresh': True}})
            assert second.result(3).json() == {'fresh': True}


def test_disconnect_fails_pending_and_offline_does_not_queue(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+a['device_id']+'/state')
            wa.receive_json()
            wa.close()
            assert response.result(3).status_code == 503
    assert client.get('/api/devices/'+a['device_id']+'/state').status_code == 503
    assert not gateway.pending


def test_download_preserves_bytes_and_sanitizes_content_type(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    content = ('تفريغ تجريبي\n'*10000).encode()
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+a['device_id']+'/files/job-123/document')
            command = wa.receive_json()
            assert command['action'] == 'file'
            wa.send_json({'type': 'file_start', 'id': command['id'], 'name': 'درس.docx', 'size': len(content),
                          'content_type': 'text/html'})
            for offset in range(0, len(content), 65536):
                wa.send_json({'type': 'file_chunk', 'id': command['id'],
                              'data': base64.b64encode(content[offset:offset+65536]).decode()})
            wa.send_json({'type': 'file_end', 'id': command['id']})
            result = response.result(5)
            assert result.content == content
            assert result.headers['content-type'].startswith('application/vnd.openxmlformats')
            assert 'filename*=UTF-8' in result.headers['content-disposition']
            assert wa.receive_json()['type'] == 'cancel'
            assert not gateway.pending


def test_oversize_file_rejected_before_headers(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+a['device_id']+'/files/job-123/document')
            command = wa.receive_json()
            wa.send_json({'type': 'file_start', 'id': command['id'], 'name': 'file.docx',
                          'size': gateway.settings.max_file_bytes+1})
            assert wa.receive_json()['type'] == 'cancel'
            assert response.result(3).status_code == 413


def test_session_and_grants_persist_across_gateway_restart(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
    cookie = client.cookies.get(COOKIE)
    reopened = Database(gateway.settings.database_url)
    with TestClient(create_app(gateway.settings, reopened), base_url=ORIGIN) as new_client:
        new_client.cookies.set(COOKIE, cookie, domain='relay.test', path='/')
        listed = new_client.get('/api/devices').json()['devices']
        assert len(listed) == 1 and listed[0]['device_id'] == a['device_id']
        assert listed[0]['online'] is False
        assert new_client.post('/api/logout', json={}, headers=HEADERS).status_code == 200
        assert new_client.get('/api/devices').status_code == 401


def test_expired_tickets_and_sessions_fail_closed(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json()
        ticket = control(wa, 'pair_create')['result']['ticket']
        with gateway.db.session.begin() as session:
            session.get(Ticket, digest(ticket)).expires = time.time()-1
        assert client.post('/api/pair/start', json={'ticket': ticket}, headers=HEADERS).status_code == 401
        pair(client, wa)
        with gateway.db.session.begin() as session:
            session.scalar(select(Phone)).last_seen = time.time()-8*86400
        assert client.get('/api/devices').status_code == 401


def test_device_identity_revocation_denies_reconnect(setup_gateway):
    client, gateway = setup_gateway
    a = enroll(client, gateway)
    with connect(client, a) as wa:
        wa.receive_json(); pair(client, wa)
        assert control(wa, 'device_revoke')['ok'] is True
        with pytest.raises(WebSocketDisconnect) as exc:
            wa.receive_json()
        assert exc.value.code == 4403
    assert client.get('/api/devices').json() == {'devices': []}
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(client, a):
            pass
    assert exc.value.code == 4401


def test_configuration_rejects_noncanonical_or_insecure_origins():
    for url in ('http://example.com', 'https://example.com/', 'https://user@example.com', 'https://example.com/path'):
        with pytest.raises(ValueError):
            Settings(url, 'sqlite:///:memory:')


def test_failed_welcome_releases_reserved_connection(setup_gateway, monkeypatch):
    client, gateway = setup_gateway
    device = enroll(client, gateway)
    original_send = Connection.send

    async def fail_welcome(self, message):
        if message['type'] == 'welcome':
            raise WebSocketDisconnect(1006)
        return await original_send(self, message)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, 'send', fail_welcome)
        with connect(client, device):
            pass
    assert not gateway.connections
    with connect(client, device) as ws:
        assert ws.receive_json()['type'] == 'welcome'


def test_body_cap_and_malformed_action_json_return_controlled_errors(setup_gateway):
    client, gateway = setup_gateway
    # Dedicated small-cap ASGI instance keeps this regression inexpensive.
    settings = Settings(ORIGIN, gateway.settings.database_url, max_body_bytes=100)
    with TestClient(create_app(settings, gateway.db), base_url=ORIGIN) as limited:
        response = limited.post('/api/enroll', json={'code': 'x'*200, 'name': 'A'})
        assert response.status_code == 413, response.text
    device = enroll(client, gateway)
    with connect(client, device) as ws:
        ws.receive_json(); pair(client, ws)
        response = client.post('/api/devices/'+device['device_id']+'/action/queue', content='{invalid',
                               headers={**HEADERS, 'Content-Type': 'application/json'})
        assert response.status_code == 400


def test_error_after_file_start_wakes_waiting_stream_immediately(setup_gateway):
    client, gateway = setup_gateway
    device = enroll(client, gateway)
    with connect(client, device) as ws:
        ws.receive_json(); pair(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+device['device_id']+'/files/job-123/document')
            command = ws.receive_json()
            ws.send_json({'type': 'file_start', 'id': command['id'], 'name': 'file.docx', 'size': 20})
            ws.send_json({'type': 'response', 'id': command['id'], 'ok': False,
                          'error': 'File changed during transfer', 'status': 409})
            # Error acknowledgement precedes pong, proving immediate handling
            # rather than waiting for the transfer deadline to cancel.
            ws.send_json({'type': 'ping'})
            assert ws.receive_json() == {'type': 'cancel', 'id': command['id']}
            try:
                result = response.result(3)
                assert result.status_code == 409
            except RuntimeError as exc:
                # Once headers were sent, a failed stream closes the HTTP body.
                assert 'response already started' in str(exc)
            assert not gateway.pending


def test_revoked_command_waiting_for_socket_lock_is_never_transmitted(tmp_path):
    settings = Settings(ORIGIN, f'sqlite:///{(tmp_path / "race.db").as_posix()}')
    database = Database(settings.database_url)
    database.initialize()
    now = time.time()
    with database.session.begin() as session:
        session.add(Device(id='device-A', name='A', secret_hash=digest('secret')))
        session.add(Phone(id='phone-A', token_hash=digest('token'), expires=now+3600, last_seen=now))
        session.flush()
        session.add(Grant(device_id='device-A', phone_id='phone-A', name='Phone', created_at=now))

    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def run():
        gateway = Gateway(settings, database)
        socket = Socket()
        connection = Connection(socket)
        gateway.connections['device-A'] = connection
        await connection.send_lock.acquire()
        command = asyncio.create_task(gateway.begin('device-A', 'phone-A', 'queue', {'running': True}))
        await asyncio.sleep(0)
        assert len(gateway.pending) == 1
        with database.session.begin() as session:
            session.delete(session.get(Grant, ('phone-A', 'device-A')))
        cancellation = asyncio.create_task(gateway.cancel_grants('device-A', 'phone-A'))
        await asyncio.sleep(0)
        connection.send_lock.release()
        with pytest.raises(HTTPException) as exc:
            await command
        assert exc.value.status_code == 403
        await cancellation
        assert all(message['type'] != 'request' for message in socket.messages)
        assert not gateway.pending

    asyncio.run(run())
    database.engine.dispose()


def test_upload_admission_is_nonblocking_and_precedes_body_read():
    async def run():
        release = asyncio.Event()
        body_reads = []

        async def inner(scope, receive, send):
            await receive()
            await release.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})

        middleware = SecurityMiddleware(inner, Settings(ORIGIN, 'sqlite:///:memory:'))

        async def upload(device):
            outputs = []
            scope = {'type': 'http', 'method': 'POST', 'path': '/api/devices/'+device+'/action/upload',
                     'headers': [(b'host', b'relay.test'), (b'origin', ORIGIN.encode()),
                                 (b'x-waraq-client', b'1'), (b'content-type', b'application/json')]}

            async def receive():
                body_reads.append(device)
                return {'type': 'http.request', 'body': b'{}', 'more_body': False}

            async def send(message):
                outputs.append(message)

            await middleware(scope, receive, send)
            return outputs[0]['status']

        a1, a2 = asyncio.create_task(upload('A')), asyncio.create_task(upload('A'))
        await asyncio.sleep(0)
        assert await upload('A') == 503  # Per-device admission cap.
        b1, b2 = asyncio.create_task(upload('B')), asyncio.create_task(upload('B'))
        await asyncio.sleep(0)
        assert await upload('C') == 503  # Global cap, rejected before receive().
        assert body_reads == ['A', 'A', 'B', 'B']
        release.set()
        assert await asyncio.gather(a1, a2, b1, b2) == [200]*4
        assert middleware.total_uploads == 0 and not middleware.uploads
        assert await upload('C') == 200

    asyncio.run(run())


def test_large_bounded_state_snapshot_does_not_disconnect_desktop(setup_gateway):
    client, gateway = setup_gateway
    device = enroll(client, gateway)
    with connect(client, device) as ws:
        ws.receive_json(); pair(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, '/api/devices/'+device['device_id']+'/state')
            command = ws.receive_json()
            snapshot = {'jobs': [{'title': 'x'*(3*1024*1024)}]}
            ws.send_json({'type': 'response', 'id': command['id'], 'ok': True, 'result': snapshot})
            assert response.result(5).json() == snapshot
            ws.send_json({'type': 'ping'})
            assert ws.receive_json() == {'type': 'pong'}


def test_complete_request_envelope_size_is_capped_without_disconnecting(setup_gateway):
    client, gateway = setup_gateway
    device = enroll(client, gateway)
    with connect(client, device) as ws:
        ws.receive_json(); pair(client, ws)
        payload = json.dumps({'unused': 'x'*(MAX_MESSAGE-14)}, separators=(',', ':'))
        assert len(payload) < MAX_MESSAGE
        response = client.post('/api/devices/'+device['device_id']+'/action/upload', content=payload,
                               headers={**HEADERS, 'Content-Type': 'application/json'})
        assert response.status_code == 413, response.text
        assert not gateway.pending
        ws.send_json({'type': 'ping'})
        assert ws.receive_json() == {'type': 'pong'}


def test_one_phone_per_device_requires_explicit_revoke_and_new_qr(setup_gateway):
    phone_a, gateway = setup_gateway
    phone_b = TestClient(phone_a.app, base_url=ORIGIN)
    a, b = enroll(phone_a, gateway, 'A'), enroll(phone_a, gateway, 'B')
    with connect(phone_a, a) as wa, connect(phone_a, b) as wb:
        wa.receive_json(); wb.receive_json()
        pair(phone_a, wa, 'Phone A')
        pair(phone_a, wb, 'Phone A')
        blocked = control(wa, 'pair_create')
        assert blocked['ok'] is False and blocked['status'] == 409
        assert 'ألغِ ربط الهاتف الحالي' in blocked['error']
        assert phone_b.get('/api/devices').status_code == 401
        current = control(wa, 'phones')['result']['phones']
        assert len(current) == 1 and current[0]['name'] == 'Phone A'
        assert control(wa, 'revoke', {'phone_id': current[0]['id']})['ok'] is True
        pair(phone_b, wa, 'Phone B')
        assert phone_a.get('/api/devices/'+a['device_id']+'/state').status_code == 403
        assert [d['device_id'] for d in phone_a.get('/api/devices').json()['devices']] == [b['device_id']]
        assert [d['device_id'] for d in phone_b.get('/api/devices').json()['devices']] == [a['device_id']]
        assert phone_b.get('/api/devices/'+b['device_id']+'/state').status_code == 403
        assert phone_b.post('/api/devices/'+b['device_id']+'/action/queue', json={'running': True}, headers=HEADERS).status_code == 403
        assert phone_b.get('/api/devices/'+b['device_id']+'/files/job-123/document').status_code == 403


def test_approved_uncollected_claim_reserves_slot_and_revoke_invalidates_it(setup_gateway):
    phone_a, gateway = setup_gateway
    phone_b = TestClient(phone_a.app, base_url=ORIGIN)
    device = enroll(phone_a, gateway)
    with connect(phone_a, device) as ws:
        ws.receive_json()
        ticket = control(ws, 'pair_create')['result']['ticket']
        started = phone_a.post('/api/pair/start', json={'ticket': ticket}, headers=HEADERS).json()
        ws.receive_json()
        claim_id = started['claim_id']
        assert control(ws, 'pair_approve', {'claim_id': claim_id, 'approved': True})['ok'] is True
        assert control(ws, 'pair_create')['status'] == 409
        # Simulate a stale outstanding claim from a previous gateway version.
        with gateway.db.session.begin() as session:
            session.add(Claim(id='old-claim-B', secret_hash=digest('pending-secret-B'), device_id=device['device_id'],
                              phone_name='Phone B', match_code='123456', expires=time.time()+300))
        blocked = control(ws, 'pair_approve', {'claim_id': 'old-claim-B', 'approved': True})
        assert blocked['ok'] is False and blocked['status'] == 409
        # Explicit revocation invalidates all stale approvals and tickets.
        assert control(ws, 'revoke', {'phone_id': 'not-yet-issued'})['ok'] is True
        assert phone_a.get('/api/pair/status/'+claim_id).json()['status'] == 'rejected'
        assert phone_a.get('/api/devices').status_code == 401
        pair(phone_b, ws, 'Phone B')
        assert len(control(ws, 'phones')['result']['phones']) == 1


def test_new_qr_invalidates_old_claim_and_parallel_approvals_cannot_both_win(setup_gateway):
    phone_a, gateway = setup_gateway
    phone_b = TestClient(phone_a.app, base_url=ORIGIN)
    device = enroll(phone_a, gateway)
    with connect(phone_a, device) as ws:
        ws.receive_json()
        first_ticket = control(ws, 'pair_create')['result']['ticket']
        first = phone_a.post('/api/pair/start', json={'ticket': first_ticket}, headers=HEADERS).json()
        ws.receive_json()
        second_ticket = control(ws, 'pair_create')['result']['ticket']
        second = phone_b.post('/api/pair/start', json={'ticket': second_ticket}, headers=HEADERS).json()
        ws.receive_json()
        for ident in (first['claim_id'], second['claim_id']):
            ws.send_json({'type': 'control', 'id': ident, 'action': 'pair_approve',
                          'data': {'claim_id': ident, 'approved': True}})
        old, new = ws.receive_json(), ws.receive_json()
        assert old['ok'] is False and old['status'] == 404
        assert new['ok'] is True
        assert phone_a.get('/api/pair/status/'+first['claim_id']).json()['status'] == 'rejected'
        assert phone_b.get('/api/pair/status/'+second['claim_id']).json()['status'] == 'approved'
        with gateway.db.session() as session:
            assert session.scalar(select(func.count()).select_from(Grant)) == 1


def test_simultaneous_collection_of_one_approved_claim_creates_one_grant(setup_gateway):
    client, gateway = setup_gateway
    device = enroll(client, gateway)
    with connect(client, device) as ws:
        ws.receive_json()
        ticket = control(ws, 'pair_create')['result']['ticket']
        claim = client.post('/api/pair/start', json={'ticket': ticket}, headers=HEADERS).json()
        ws.receive_json()
        assert control(ws, 'pair_approve', {'claim_id': claim['claim_id'], 'approved': True})['ok'] is True
        pending_secret = client.cookies.get(PENDING_COOKIE)
        headers = {'Cookie': PENDING_COOKIE+'='+pending_secret}
        with ThreadPoolExecutor(max_workers=2) as pool:
            polls = [pool.submit(client.get, '/api/pair/status/'+claim['claim_id'], headers=headers) for _ in range(2)]
            results = [future.result(4) for future in polls]
        assert sorted(response.json()['status'] for response in results) == ['approved', 'consumed']
        assert sum(COOKIE+'=' in response.headers.get('set-cookie', '') for response in results) == 1
        with gateway.db.session() as session:
            assert session.scalar(select(func.count()).select_from(Grant)) == 1
            assert session.scalar(select(func.count()).select_from(Phone)) == 1


@pytest.mark.parametrize('expired_field', ['expires', 'last_seen'])
def test_expired_or_idle_phone_slot_can_be_safely_repaired(setup_gateway, expired_field):
    old_phone, gateway = setup_gateway
    new_phone = TestClient(old_phone.app, base_url=ORIGIN)
    device = enroll(old_phone, gateway)
    with connect(old_phone, device) as ws:
        ws.receive_json(); old_claim = pair(old_phone, ws, 'Old phone')
        with gateway.db.session.begin() as session:
            phone = session.scalar(select(Phone))
            setattr(phone, expired_field, time.time()-(1 if expired_field == 'expires' else 8*86400))
        assert old_phone.get('/api/devices').status_code == 401
        pair(new_phone, ws, 'New phone')
        assert old_phone.get('/api/devices/'+device['device_id']+'/state').status_code == 401
        assert len(control(ws, 'phones')['result']['phones']) == 1
        with gateway.db.session() as session:
            assert session.scalar(select(func.count()).select_from(Grant)) == 1
            assert session.get(Claim, old_claim).status == 'consumed'


def test_unique_device_index_upgrades_legacy_grants_without_deleting_data(tmp_path):
    database = Database(f'sqlite:///{(tmp_path / "legacy.db").as_posix()}')
    database.initialize()
    now = time.time()
    with database.engine.begin() as connection:
        connection.execute(text('DROP INDEX uq_grants_device_id'))
    with database.session.begin() as session:
        session.add(Device(id='legacy-A', name='A', secret_hash=digest('secret')))
        session.add_all([Phone(id=ident, token_hash=digest(ident), expires=now+3600, last_seen=now)
                         for ident in ('phone-A', 'phone-B')])
        session.flush()
        session.add_all([Grant(device_id='legacy-A', phone_id=ident, name=ident, created_at=now)
                         for ident in ('phone-A', 'phone-B')])
    with pytest.raises(RuntimeError, match='no data was deleted'):
        database.initialize()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Grant)) == 2
    # An administrator explicitly selects which legacy grant to retain.
    with database.session.begin() as session:
        session.delete(session.get(Grant, ('phone-B', 'legacy-A')))
    database.initialize()
    database.initialize()  # Idempotent restart after the upgrade.
    index = next(item for item in inspect(database.engine).get_indexes('grants') if item['name'] == 'uq_grants_device_id')
    assert index['unique'] and index['column_names'] == ['device_id']
    with pytest.raises(IntegrityError):
        with database.session.begin() as session:
            session.add(Grant(device_id='legacy-A', phone_id='phone-B', name='B', created_at=now))
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Grant)) == 1
        assert session.scalar(select(func.count()).select_from(Phone)) == 2
    database.engine.dispose()


def test_stale_previously_approved_claim_cannot_replace_current_phone(setup_gateway):
    phone_a, gateway = setup_gateway
    phone_b = TestClient(phone_a.app, base_url=ORIGIN)
    device = enroll(phone_a, gateway)
    with connect(phone_a, device) as ws:
        ws.receive_json(); pair(phone_a, ws, 'Current phone')
        with gateway.db.session.begin() as session:
            session.add(Claim(id='legacy-approved-B', secret_hash=digest('stale-secret'), device_id=device['device_id'],
                              phone_name='Unwanted phone', match_code='654321', expires=time.time()+300, status='approved'))
        phone_b.cookies.set(PENDING_COOKIE, 'stale-secret', domain='relay.test', path='/')
        response = phone_b.get('/api/pair/status/legacy-approved-B')
        assert response.status_code == 409 and response.json()['status'] == 'rejected'
        assert COOKIE not in phone_b.cookies
        assert phone_b.get('/api/devices').status_code == 401
        assert len(phone_a.get('/api/devices').json()['devices']) == 1
        assert control(ws, 'phones')['result']['phones'][0]['name'] == 'Current phone'
        with gateway.db.session() as session:
            assert session.scalar(select(func.count()).select_from(Grant)) == 1
            assert session.get(Claim, 'legacy-approved-B').status == 'rejected'
