"""Real HTTPS Service Worker/PWA acceptance in isolated headless Edge.

Browser plugin not available: regular Playwright exercises actual SW/cache APIs.
Run: python tests/verify_pwa.py [screenshot-directory]
No VPS, paid calls, physical camera or real iOS installation is claimed.
"""
from __future__ import annotations

import base64
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / 'waraq/mobile'


def certificate(home):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                  x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
    cp, kp = home/'ca.pem', home/'key.pem'
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cp, kp


def main():
    from playwright.sync_api import sync_playwright, expect
    import qrcode
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output: output.mkdir(parents=True, exist_ok=True)
    record = {'version':1, 'fail_asset':False, 'auth_page':False, 'posts':[]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def handle(self):
            try: super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        def reply(self, data, kind='application/json', status=200):
            raw = json.dumps(data).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store' if self.path != '/sw.js' else 'no-cache')
            self.send_header('Service-Worker-Allowed','/')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; media-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'")
            try:
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        def do_GET(self):
            path = self.path.split('?')[0]
            if path == '/sw.js':
                data = (MOBILE/'sw.js').read_text('utf-8').replace('2026-10-16-pwa-2',f'test-{record["version"]}')
                self.reply(data.encode(),'text/javascript'); return
            if path == '/api/bootstrap': self.reply({'mode':'internet'}); return
            if path == '/api/devices': self.reply({'devices':[]}); return
            if path.startswith('/api/'):
                if '/files/' in path: self.reply(b'private-file-contents','application/octet-stream'); return
                self.reply({'error':'private-route'},status=401); return
            if path == '/internet' and record['auth_page']:
                self.reply(b'HTTP-403-NETWORK','text/plain',403); return
            if path in ('/internet','/pair','/devices/device-A','/pwa-shell.html','/'):
                self.reply((MOBILE/'index.html').read_bytes(),'text/html'); return
            if path == '/fixture.png': self.reply((home/'fixture.png').read_bytes(),'image/png'); return
            if path.startswith('/assets/'):
                relative = path.removeprefix('/assets/')
                asset = ROOT/'waraq/icon.svg' if relative == 'icon.svg' else MOBILE/relative
            else: asset = MOBILE/path.lstrip('/')
            if record['fail_asset'] and path == '/assets/vendor/jsQR.js':
                self.reply(b'not ready','text/plain',503); return
            if not asset.is_file(): self.reply(b'missing','text/plain',404); return
            kind = {'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png',
                    '.webmanifest':'application/manifest+json','.html':'text/html'}.get(asset.suffix,'text/plain')
            self.reply(asset.read_bytes(),kind)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length','0')))
            record['posts'].append((self.path,json.loads(raw)))
            self.reply({'claim_id':'test-claim','device_name':'Test computer','match_code':'123456'})

    with tempfile.TemporaryDirectory(prefix='waraq-pwa-') as temporary:
        home = Path(temporary)
        cp,kp = certificate(home)
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cp,kp)
        server.socket = tls.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin=f'https://localhost:{server.server_port}'
        ticket='Q'*43
        qrcode.make(origin+'/pair#'+ticket).save(home/'fixture.png')
        try:
            with sync_playwright() as pw:
                # Isolated browser only: test CA is temporary/self-signed.
                browser=pw.chromium.launch(channel='msedge',headless=True,args=['--ignore-certificate-errors'])
                context=browser.new_context(ignore_https_errors=True,viewport={'width':390,'height':844},locale='ar-JO')
                page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(origin+'/internet')
                expect(page.locator('#pwa-pair')).to_be_visible()
                page.wait_for_function('()=>!!navigator.serviceWorker.controller')
                assert page.title().startswith('ورق')
                manifest=page.evaluate("async()=>await (await fetch('/manifest.webmanifest')).json()")
                assert manifest['id']==manifest['start_url']=='/internet' and manifest['scope']=='/' and manifest['dir']=='rtl'
                assert {icon['purpose'] for icon in manifest['icons']}=={'any','maskable'}
                keys=page.evaluate("async()=>{let all=[];for(const name of await caches.keys()){const c=await caches.open(name);all.push(...(await c.keys()).map(r=>new URL(r.url).pathname));}return all;}")
                assert len(keys)==13 and '/pwa-shell.html' in keys and '/assets/vendor/jsQR.js' in keys,keys
                # Actual API/file activity must not add anything to cache.
                page.evaluate("async()=>{await fetch('/api/devices');await fetch('/api/devices/device-A/files/job/document');await fetch('/api/preview');}")
                assert page.evaluate("async()=>{let a=[];for(const n of await caches.keys()){for(const r of await(await caches.open(n)).keys())a.push(r.url)}return a.every(x=>!x.includes('/api/')&&!x.includes('/devices/')&&!x.includes('#'));}")
                context.set_offline(True)
                for private in ('/api/devices','/api/devices/device-A/files/job/document','/api/preview'):
                    assert page.evaluate("async p=>{try{await fetch(p);return false}catch{return true}}",private)
                page.reload();expect(page.locator('#internet-home')).to_be_visible()
                expect(page.locator('#login')).to_be_hidden()
                assert not record['posts']
                context.set_offline(False)
                record['auth_page']=True
                rejected=page.goto(origin+'/internet')
                assert rejected.status==403 and 'HTTP-403-NETWORK' in page.content()
                record['auth_page']=False;page.goto(origin+'/internet')
                # Browser feature detection: no fake install before the event.
                expect(page.locator('#pwa-install')).to_be_hidden()
                page.evaluate("""()=>{window.installPrompted=0;const e=new Event('beforeinstallprompt');e.prompt=async()=>{window.installPrompted++};e.userChoice=Promise.resolve({outcome:'dismissed'});dispatchEvent(e)}""")
                page.locator('#pwa-install').click();assert page.evaluate('installPrompted')==1
                expect(page.locator('#pwa-install')).to_be_hidden()
                # Refuse a QR/paste that could navigate to another origin.
                page.locator('#pwa-pair').click();page.locator('#pwa-link').fill('https://evil.invalid/pair#'+ticket)
                page.locator('#pwa-link-form button').click()
                expect(page.locator('#pwa-camera-error')).to_contain_text('الحالية فقط')
                assert page.url==origin+'/internet' and page.locator('#pwa-link').input_value()==''
                # Denied camera keeps paste available and starts no API calls.
                page.evaluate("Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw Error('denied')}})")
                page.locator('#pwa-scan').click();expect(page.locator('#pwa-camera-error')).to_contain_text('لم تُفتح')
                page.locator('#pwa-pair-close').click()
                # Read actual fallback decoder against a deterministic QR image.
                page.add_script_tag(url=origin+'/assets/vendor/jsQR.js')
                decoded=page.evaluate("""async()=>{const i=new Image();i.src='/fixture.png';await i.decode();const c=document.createElement('canvas');c.width=i.width;c.height=i.height;const x=c.getContext('2d');x.drawImage(i,0,0);const d=x.getImageData(0,0,c.width,c.height);return jsQR(d.data,d.width,d.height).data}""")
                assert decoded==origin+'/pair#'+ticket
                # Native mock camera: close and late getUserMedia completion
                # both stop all tracks. No hardware capability is claimed.
                page.evaluate("""()=>{window.stops=0;window.resolveCamera=null;Object.defineProperty(HTMLMediaElement.prototype,'srcObject',{configurable:true,set(v){this._fake=v},get(){return this._fake}});HTMLMediaElement.prototype.play=async()=>{};HTMLMediaElement.prototype.pause=()=>{};window.BarcodeDetector=class{async detect(){return[]}};Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:()=>new Promise(r=>window.resolveCamera=r)});window.fakeStream=()=>({getTracks:()=>[{stop(){window.stops++}},{stop(){window.stops++}}]})}""")
                page.locator('#pwa-pair').click();page.locator('#pwa-scan').click()
                page.wait_for_function('()=>!!window.resolveCamera');page.locator('#pwa-pair-close').click()
                page.evaluate('resolveCamera(fakeStream())');page.wait_for_function('()=>stops===2')
                page.locator('#pwa-pair').click();page.locator('#pwa-scan').click();page.evaluate('resolveCamera(fakeStream())')
                expect(page.locator('#pwa-camera')).to_be_visible();page.locator('#pwa-pair-close').click()
                assert page.evaluate('stops')==4
                page.locator('#pwa-pair').click();page.locator('#pwa-scan').click();page.evaluate('resolveCamera(fakeStream())')
                expect(page.locator('#pwa-camera')).to_be_visible()
                page.evaluate("dispatchEvent(new Event('pagehide'))")
                assert page.evaluate('stops')==6
                page.locator('#pwa-pair-close').click()
                # Exercise the entire camera fallback loop, not just its
                # decoder: synthetic video pixels contain the real fixture.
                page.evaluate("""async()=>{window.BarcodeDetector=undefined;delete window.BarcodeDetector;window.qrImage=new Image();qrImage.src='/fixture.png';await qrImage.decode();Object.defineProperty(HTMLVideoElement.prototype,'readyState',{configurable:true,get:()=>2});Object.defineProperty(HTMLVideoElement.prototype,'videoWidth',{configurable:true,get:()=>qrImage.width});Object.defineProperty(HTMLVideoElement.prototype,'videoHeight',{configurable:true,get:()=>qrImage.height});window.originalDraw=CanvasRenderingContext2D.prototype.drawImage;CanvasRenderingContext2D.prototype.drawImage=function(image,...args){return originalDraw.call(this,image instanceof HTMLVideoElement?qrImage:image,...args)};Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>fakeStream()})}""")
                page.locator('#pwa-pair').click();page.locator('#pwa-scan').click()
                expect(page.locator('#internet-pair')).to_be_visible()
                expect(page.locator('#pwa-pair-dialog')).not_to_be_visible()
                assert page.evaluate('stops')==8
                page.goto(origin+'/internet')
                # Paste goes through local bridge, clears ticket input/address,
                # and remains inside current PWA document awaiting approval.
                page.locator('#pwa-pair').click();page.locator('#pwa-link').fill(origin+'/pair#'+ticket)
                page.locator('#pwa-link-form button').click();expect(page.locator('#internet-pair')).to_be_visible()
                expect(page.locator('#pwa-pair-dialog')).not_to_be_visible()
                assert '#' not in page.url and page.locator('#pwa-link').input_value()==''
                assert page.evaluate("t=>![...Object.values(localStorage),...Object.values(sessionStorage)].some(v=>v.includes(t))",ticket)
                page.goto(origin+'/internet')
                # A failed precache keeps the existing active cache intact.
                record['version']=2;record['fail_asset']=True
                page.evaluate("async()=>await(await navigator.serviceWorker.getRegistration()).update()")
                page.wait_for_function("""async()=>{
                    const reg=await navigator.serviceWorker.getRegistration();
                    return !reg.installing && JSON.stringify(await caches.keys())===JSON.stringify(['waraq-public-test-1']);
                }""",timeout=30000)
                assert page.evaluate("async()=>await caches.keys()") == ['waraq-public-test-1']
                record['fail_asset']=False;record['version']=3
                page.evaluate("async()=>await(await navigator.serviceWorker.getRegistration()).update()")
                page.wait_for_function("async()=>!!(await navigator.serviceWorker.getRegistration()).waiting")
                tab=context.new_page();tab.goto(origin+'/internet');expect(tab.locator('#pwa-pair')).to_be_visible()
                tab.evaluate("window.testBusy=true;window.originalBusy=waraqPwaBridge.isBusy;waraqPwaBridge.isBusy=()=>window.testBusy||window.originalBusy()")
                page.locator('#pwa-update').click();expect(page.locator('#pwa-message')).to_contain_text('كل النوافذ')
                assert page.evaluate("async()=>!!(await navigator.serviceWorker.getRegistration()).waiting")
                tab.evaluate('window.testBusy=false')
                page.locator('#pwa-update').click()
                page.wait_for_function("async()=>!(await navigator.serviceWorker.getRegistration()).waiting")
                page.wait_for_function("async()=>JSON.stringify(await caches.keys())===JSON.stringify(['waraq-public-test-3'])")
                expect(page.locator('#pwa-pair')).to_be_visible()
                if output:page.screenshot(path=str(output/'pwa-mobile.png'),full_page=True)
                assert not errors,errors
                # iOS gets explicit instructions, no unsupported fake prompt.
                ios_context=browser.new_context(ignore_https_errors=True,viewport={'width':375,'height':812},
                    user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 Version/17.2 Mobile/15E148 Safari/604.1')
                ios_page=ios_context.new_page();ios_page.goto(origin+'/internet')
                ios_page.locator('#pwa-install-help').click()
                expect(ios_page.locator('#pwa-message')).to_contain_text('Safari')
                expect(ios_page.locator('#pwa-message')).to_contain_text('الشاشة الرئيسية')
                expect(ios_page.locator('#pwa-install')).to_be_hidden()
                ios_context.close()
                # Ordinary HTTP/LAN remains a browser page with no manifest,
                # service worker registration or misleading install controls.
                lan=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                lan_thread=threading.Thread(target=lan.serve_forever,daemon=True);lan_thread.start()
                try:
                    lan_context=browser.new_context();lan_page=lan_context.new_page()
                    lan_page.goto(f'http://127.0.0.1:{lan.server_port}/internet')
                    assert lan_page.locator('#pwa-controls').count()==0
                    assert lan_page.locator('link[rel=manifest]').count()==0
                    assert lan_page.evaluate('async()=> (await navigator.serviceWorker.getRegistrations()).length')==0
                    lan_context.close()
                finally:lan.shutdown();lan.server_close();lan_thread.join(3)
                browser.close()
        finally:server.shutdown();server.server_close();thread.join(3)
    print('PWA acceptance passed: real HTTPS SW, public cache inventory, offline Internet shell/API network-only, 403 preservation, feature install, safe QR paste/fallback decoder, camera track cleanup, failed update rollback and multi-tab busy coordination. Physical Android/iOS installation and cameras remain untested.')


if __name__=='__main__': main()
