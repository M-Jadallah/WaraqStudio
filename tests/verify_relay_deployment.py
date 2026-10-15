"""Build the production Compose stack and verify real PostgreSQL + HTTP + WS.

Run on a Docker host: python tests/verify_relay_deployment.py
Uses only the host standard library. An isolated, random Compose project owns
all created containers/volumes; cleanup never targets an existing deployment.
The loopback HTTP/WS probes model the proxy-to-container hop, not public TLS.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

# Executed inside the actual production image, using its installed dependencies.
PROBE = r'''
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
import json
import os
import socket
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import func, inspect, select
from relay.config import Settings
from relay.db import Database, Device, Grant, Phone
from websockets.sync.client import connect

settings = Settings.from_env()
host = urlsplit(settings.public_base_url).netloc
database = Database(settings.database_url)
assert database.engine.dialect.name == 'postgresql'
index = next(i for i in inspect(database.engine).get_indexes('grants') if i['name'] == 'uq_grants_device_id')
assert index['unique'] and index['column_names'] == ['device_id']

def http(path, cookies=None, data=None, extra_headers=None):
    headers = {'Host': host, 'Origin': settings.public_base_url, 'X-Waraq-Client': '1'}
    headers.update(extra_headers or {})
    if cookies:
        headers['Cookie'] = '; '.join(k+'='+v for k,v in cookies.items())
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = Request('http://127.0.0.1:8000'+path,
                      data=None if data is None else json.dumps(data).encode(), headers=headers)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        if cookies is not None:
            for header in response.headers.get_all('Set-Cookie', []):
                parsed = SimpleCookie(); parsed.load(header)
                for key, value in parsed.items():
                    if value.value: cookies[key] = value.value
                    else: cookies.pop(key, None)
        raw = response.read()
        content = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
        return response.status, content

assert http('/healthz')[1]['status'] == 'ok'
if sys.argv[1] == 'persisted':
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 2
        assert session.scalar(select(func.count()).select_from(Phone)) == 2
        assert session.scalar(select(func.count()).select_from(Grant)) == 2
    print('PostgreSQL identities, phone grants and unique index survive gateway recreation.')
    sys.exit(0)

assert http('/internet')[0] == 200
qr_navigation = {'Sec-Fetch-Site':'cross-site', 'Sec-Fetch-Mode':'navigate', 'Sec-Fetch-Dest':'document'}
assert http('/pair', extra_headers=qr_navigation)[0] == 200
assert http('/api/devices', extra_headers=qr_navigation)[0] == 403
for asset in ('app.js', 'style.css', 'icon.svg'):
    assert http('/assets/'+asset)[0] == 200

def enroll(name):
    code = subprocess.check_output([sys.executable,'-m','relay','enroll-code','--minutes','10'], text=True).splitlines()[0]
    status, device = http('/api/enroll', data={'code':code, 'name':name})
    assert status == 200
    assert http('/api/enroll', data={'code':code, 'name':'duplicate'})[0] == 401
    return device

def connection(device):
    transport = socket.create_connection(('127.0.0.1',8000), timeout=10)
    return connect('ws://'+host+'/ws/device?device_id='+device['device_id'], sock=transport,
                   additional_headers={'Authorization':'Bearer '+device['device_secret']},
                   proxy=None, open_timeout=10, close_timeout=1)

def control(ws, action, data=None):
    ws.send(json.dumps({'type':'control','id':'check-control','action':action,'data':data or {}}))
    result = json.loads(ws.recv(timeout=10))
    assert result['type'] == 'control_result'
    return result

def pair(ws, name):
    ticket = control(ws,'pair_create')['result']['ticket']
    cookies = {}
    status, claim = http('/api/pair/start', cookies, {'ticket':ticket, 'phone_name':name})
    assert status == 200
    event = json.loads(ws.recv(timeout=10))
    assert event['type'] == 'pair_claim' and event['match_code'] == claim['match_code']
    assert control(ws,'pair_approve',{'claim_id':claim['claim_id'],'approved':True})['ok']
    assert http('/api/pair/status/'+claim['claim_id'], cookies)[1]['status'] == 'approved'
    return cookies

# 120-character names also verify PostgreSQL's real varchar bound.
a, b = enroll('A'*120), enroll('B')
with connection(a) as wa, connection(b) as wb:
    assert json.loads(wa.recv(timeout=10))['device_id'] == a['device_id']
    assert json.loads(wb.recv(timeout=10))['device_id'] == b['device_id']
    phone_a, phone_b = pair(wa,'Phone A'), pair(wb,'Phone B')
    assert [d['device_id'] for d in http('/api/devices',phone_a)[1]['devices']] == [a['device_id']]
    assert [d['device_id'] for d in http('/api/devices',phone_b)[1]['devices']] == [b['device_id']]
    target = '/api/devices/'+b['device_id']
    assert http(target+'/state',phone_a)[0] == 403
    assert http(target+'/action/queue',phone_a,{'running':True})[0] == 403
    assert http(target+'/files/no-job/document',phone_a)[0] == 403
    denied = control(wa,'pair_create')
    assert not denied['ok'] and denied['status'] == 409
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(http,'/api/devices/'+a['device_id']+'/state',phone_a)
        request = json.loads(wa.recv(timeout=10))
        assert request['type'] == 'request' and request['action'] == 'state'
        wa.send(json.dumps({'type':'response','id':request['id'],'ok':True,'result':{'desktop':'A'}}))
        assert response.result(15) == (200, {'desktop':'A'})
print('Production image: assets, operator enrollment, PostgreSQL, WS routing, two phones, A/B denial and one-phone rule passed.')
'''


class DeploymentCheck:
    def __init__(self):
        # Never accept a project name from input: cleanup owns only this run.
        self.project = 'waraq-ci-' + secrets.token_hex(6)
        assert re.fullmatch(r'waraq-ci-[0-9a-f]{12}', self.project)
        self.secret = secrets.token_hex(32)
        self.environment = {**os.environ, 'SERVICE_HEX_64_WARAQDB': self.secret,
                            'PUBLIC_BASE_URL': 'https://gateway.test'}
        self.base = ['docker','compose','--project-name',self.project,
                     '--file',str(ROOT/'compose.relay.yaml')]

    def compose(self, *args, timeout=240, check=True):
        result = subprocess.run([*self.base,*args], cwd=ROOT, env=self.environment,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if check and result.returncode:
            detail = (result.stderr or result.stdout)[-6000:].replace(self.secret,'[redacted]')
            raise RuntimeError(f'Compose {args[0]} failed (exit {result.returncode}):\n{detail}')
        return result

    def validate(self):
        definition = json.loads(self.compose('config','--format','json').stdout)
        services = definition['services']
        assert set(services) == {'gateway','postgres'}
        assert all(not service.get('ports') for service in services.values()), 'Unexpected public port mapping'
        assert services['gateway']['depends_on']['postgres']['condition'] == 'service_healthy'
        assert services['gateway']['environment']['PUBLIC_BASE_URL'] == self.environment['PUBLIC_BASE_URL']
        assert services['postgres']['environment']['POSTGRES_PASSWORD'] == self.secret
        connection = urlsplit(services['gateway']['environment']['DATABASE_URL'])
        assert connection.hostname == 'postgres' and connection.port == 5432 and connection.password == self.secret
        assert services['postgres']['volumes'][0]['type'] == 'volume'

    def verify(self):
        self.validate()
        self.compose('up','--build','--detach','--wait','--wait-timeout','180',timeout=600)
        print(self.compose('exec','-T','gateway','python','-c',PROBE,'initial').stdout.strip())
        # Recreate the gateway from the same source while preserving PostgreSQL.
        self.compose('up','--detach','--force-recreate','--no-deps','--wait',
                     '--wait-timeout','90','gateway',timeout=180)
        print(self.compose('exec','-T','gateway','python','-c',PROBE,'persisted').stdout.strip())


def main():
    if not shutil.which('docker'):
        raise SystemExit('Docker with Compose v2 is required. Run this acceptance check on a Docker host or in the Relay CI job.')
    check = DeploymentCheck()
    try:
        check.verify()
    except Exception:
        try:
            logs = check.compose('logs','--no-color','--tail','60',timeout=30,check=False)
            print(logs.stdout.replace(check.secret,'[redacted]'), file=sys.stderr)
        except Exception:
            print('Diagnostic logs unavailable for '+check.project, file=sys.stderr)
        raise
    finally:
        try:
            cleanup = check.compose('down','--volumes','--remove-orphans','--rmi','local',timeout=120,check=False)
            if cleanup.returncode:
                raise RuntimeError('Cleanup failed for isolated project '+check.project)
        except Exception:
            if sys.exc_info()[0] is not None:
                print('Check Docker resources for isolated project '+check.project, file=sys.stderr)
            raise
    print('Compose deployment acceptance passed; isolated test containers and volumes removed.')


if __name__ == '__main__':
    main()
