"""Full local TLS acceptance: real relay, two Qt desktops and phone Edge UI.

Run: python tests/verify_internet_stack.py [screenshot-directory]
No paid worker runs, VPS or Docker required. Temporary CA is pinned by the
Python clients; Edge ignores only this local test certificate.
"""
from __future__ import annotations

from concurrent.futures import Future
import asyncio
import datetime
import html
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from relay.app import create_app
from relay.config import Settings
from relay.db import Database
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


class Vault:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key, "")
    def set(self, key, value): self.values[key] = value
    def delete(self, key): self.values.pop(key, None)


class GuiCalls(QObject):
    submitted = Signal(object)

    def __init__(self):
        super().__init__()
        self.submitted.connect(self.execute, Qt.QueuedConnection)

    @Slot(object)
    def execute(self, task):
        future, operation = task
        if not future.set_running_or_notify_cancel(): return
        try: future.set_result(operation())
        except BaseException as exc: future.set_exception(exc)

    def call(self, operation, timeout=15):
        future = Future()
        self.submitted.emit((future, operation))
        return future.result(timeout)

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            value = self.call(predicate)
            if value: return value
            time.sleep(.04)
        raise AssertionError("GUI condition timed out")


def certificate(home):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    certificate_path, key_path = home/"test-ca.pem", home/"test-key.pem"
    certificate_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return certificate_path, key_path


def browser_checks(base, bridges, gui, output):
    from playwright.sync_api import sync_playwright, expect
    a, b = bridges
    id_a, id_b = gui.call(lambda: (a.internet.state()["deviceId"], b.internet.state()["deviceId"]))
    uploaded_bytes = b"\xef\xbb\xbf" + "تفريغ محفوظ عبر الإنترنت\r\nبالترتيب والحروف نفسها".encode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width":375,"height":812}, is_mobile=True,
                                          has_touch=True, locale="ar-JO", reduced_motion="reduce",
                                          ignore_https_errors=True, accept_downloads=True)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            def open_scanned_link(url):
                # Use a genuinely different site, rather than spoofing browser
                # headers, to cover opening QR links from another app/site.
                class ScannerPage(BaseHTTPRequestHandler):
                    def do_GET(self):
                        content = ('<a id="scanned-link" href="'+html.escape(url, quote=True)+'">Open Waraq</a>').encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(content)))
                        self.end_headers()
                        self.wfile.write(content)
                    def log_message(self, *_args):
                        pass
                scanner = ThreadingHTTPServer(('127.0.0.1', 0), ScannerPage)
                thread = threading.Thread(target=scanner.serve_forever, daemon=True)
                thread.start()
                try:
                    page.goto(f'http://localhost:{scanner.server_port}/')
                    with page.expect_response(lambda r: r.url == base+'/pair' and r.request.is_navigation_request()) as loaded:
                        page.locator('#scanned-link').click()
                    response = loaded.value
                    assert response.request.all_headers()['sec-fetch-site'] == 'cross-site'
                    assert response.status == 200, response.status
                finally:
                    scanner.shutdown(); scanner.server_close(); thread.join(3)

            def api(path, data=None):
                return page.evaluate("""async ({path,data}) => {
                  const options = data === null ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Waraq-Client':'1'},body:JSON.stringify(data)};
                  const response=await fetch(path,options); let body;
                  try {body=await response.json()} catch {body=null}
                  return {status:response.status,body};
                }""", {"path":path,"data":data})

            def pair(bridge, ident, name):
                gui.call(bridge.internet.createPair)
                url = gui.wait(lambda: bridge.internet.state()["pairUrl"])
                assert url.startswith(base+"/pair#")
                open_scanned_link(url)
                expect(page.locator("#internet-pair")).to_be_visible()
                assert page.evaluate("location.hash") == ""
                page.locator("#phone-name").fill("هاتف الاختبار")
                page.locator("#internet-pair-button").click()
                expect(page.locator("#pair-device-name")).to_have_text(name)
                claim = gui.wait(lambda: bridge.internet.state()["pendingPhones"] and bridge.internet.state()["pendingPhones"][0])
                expect(page.locator("#pair-match-code")).to_have_text(claim["match_code"])
                assert claim["phone_name"] == "هاتف الاختبار"
                expect(page.locator("#workspace")).to_be_hidden()
                if output: page.screenshot(path=str(output/("pending-"+ident[:8]+".png")),full_page=True)
                gui.call(lambda: bridge.internet.approvePair(claim["claim_id"]))
                expect(page).to_have_url(base+"/devices/"+ident,timeout=15000)
                expect(page.locator("#device-name")).to_have_text(name)
                expect(page.locator("#workspace")).to_be_visible()
                assert page.evaluate("targetDevice") == ident

            pair(a,id_a,"كمبيوتر المنزل")
            print("Phone A paired against actual TLS relay.",flush=True)
            assert api(f"/api/devices/{id_a}/state")["status"] == 200
            # Same cookie cannot authorize B by changing the path, body or file URL.
            assert api(f"/api/devices/{id_b}/state")["status"] == 403
            assert api(f"/api/devices/{id_b}/action/queue",{"running":True,"device_id":id_a})["status"] == 403
            assert api(f"/api/devices/{id_b}/files/fake/transcript")["status"] == 403
            page.locator("#queue-toggle").click()
            page.locator("#confirm-yes").click()
            expect(page.locator("#queue-state")).to_have_text("الطابور يعمل")
            assert gui.call(lambda: (a.running,b.running)) == (True,False)
            page.locator('[data-tab="source"]').click()
            page.locator("#transcripts").set_input_files({"name":"درس الإنترنت.txt","mimeType":"text/plain","buffer":uploaded_bytes})
            page.locator("#upload").click()
            expect(page.locator("#total")).to_have_text("1",timeout=15000)
            job = gui.call(lambda: a.store.jobs()[0])
            assert gui.call(lambda: Path(a.store.get(job["id"])["transcript"]).read_bytes()) == uploaded_bytes
            assert gui.call(lambda: len(b.store.jobs())) == 0
            page.locator('[data-tab="files"]').click()
            with page.expect_download() as transfer:
                page.get_by_role("link",name="تنزيل التفريغ — درس الإنترنت").click()
            assert Path(transfer.value.path()).read_bytes() == uploaded_bytes
            assert api(f"/api/devices/{id_a}/action/cancel",{"id":job["id"]})["status"] == 200
            assert gui.call(lambda: a.store.get(job["id"])["stages"]["opencode"]["status"]) == "cancelled"
            page.reload()
            expect(page.locator("#workspace")).to_be_visible()
            assert any(c["name"] == "__Host-waraq_session" and c["httpOnly"] and c["secure"] for c in context.cookies())
            pair(b,id_b,"كمبيوتر المكتب")
            print("Upload/download/cancel and independent phone B pairing passed.",flush=True)
            devices = api("/api/devices")
            assert devices["status"] == 200
            assert {d["device_id"] for d in devices["body"]["devices"]} == {id_a,id_b}
            page.locator("#change-device").click()
            expect(page.locator("#device-list a")).to_have_count(2)
            page.get_by_role("link",name="كمبيوتر المنزل",exact=True).click()
            expect(page.locator("#device-name")).to_have_text("كمبيوتر المنزل")
            expect(page.locator("#workspace")).to_be_visible()
            gui.call(a.internet.stop)
            # Await relay-side disconnect rather than relying on timing.
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                result=api(f"/api/devices/{id_a}/state")
                if result["status"]==503: break
                time.sleep(.1)
            assert result["status"] == 503,result
            assert api(f"/api/devices/{id_a}/action/queue",{"running":False})["status"] == 503
            assert api(f"/api/devices/{id_b}/state")["status"] == 200
            assert gui.call(lambda: (a.running,b.running)) == (True,False)
            page.locator("#refresh").click()
            expect(page.locator("#offline")).to_be_visible()
            expect(page.locator("#device-name")).to_have_text("كمبيوتر المنزل")
            if output: page.screenshot(path=str(output/"offline-device-a.png"),full_page=True)
            gui.call(a.internet.start)
            gui.wait(lambda: a.internet.state()["connected"])
            assert api(f"/api/devices/{id_a}/state")["status"] == 200
            gui.call(a.internet.refreshPhones)
            phones=gui.wait(lambda: a.internet.state()["phones"])
            assert len(phones)==1
            gui.call(lambda: a.internet.revokePhone(phones[0]["id"]))
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                denied=api(f"/api/devices/{id_a}/state")
                if denied["status"]==403:break
                time.sleep(.1)
            assert denied["status"]==403,denied
            assert api(f"/api/devices/{id_a}/action/queue",{"running":False})["status"]==403
            assert api(f"/api/devices/{id_a}/files/{job['id']}/transcript")["status"]==403
            assert api(f"/api/devices/{id_b}/state")["status"]==200
            page.goto(base+"/internet")
            expect(page.locator("#device-list a")).to_have_count(1)
            page.get_by_role("link",name="كمبيوتر المكتب",exact=True).click()
            expect(page.locator("#device-name")).to_have_text("كمبيوتر المكتب")
            expect(page.locator("#total")).to_have_text("0")
            page.reload()
            expect(page.locator("#workspace")).to_be_visible()
            assert page.evaluate("localStorage.length")==0
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if output: page.screenshot(path=str(output/"paired-device-b.png"),full_page=True)
            assert not errors,errors
        finally:
            browser.close()


def main():
    import uvicorn
    import websockets.sync.client
    output=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else None
    if output:output.mkdir(parents=True,exist_ok=True)
    app=QCoreApplication.instance() or QCoreApplication([])
    gui=GuiCalls()
    with tempfile.TemporaryDirectory(prefix="waraq-internet-stack-") as directory:
        home=Path(directory)
        cert,key=certificate(home)
        ca=ssl.create_default_context(cafile=str(cert))
        listener=socket.socket()
        listener.bind(("127.0.0.1",0))
        port=listener.getsockname()[1]
        base=f"https://127.0.0.1:{port}"
        database=Database("sqlite:///"+(home/"relay.sqlite3").as_posix())
        database.initialize()
        relay=create_app(Settings(base,database_url="sqlite:///"+(home/"relay.sqlite3").as_posix()),database)
        server=uvicorn.Server(uvicorn.Config(relay,host="127.0.0.1",port=port,ssl_certfile=str(cert),
                                             ssl_keyfile=str(key),access_log=False,log_level="critical",
                                             timeout_graceful_shutdown=3))
        def serve_relay():
            factory=asyncio.SelectorEventLoop if os.name=="nt" else asyncio.new_event_loop
            with asyncio.Runner(loop_factory=factory) as runner:
                runner.run(server.serve(sockets=[listener]))
        relay_thread=threading.Thread(target=serve_relay,daemon=True,name="test-relay-tls")
        bridges=[]
        browser_thread=None
        failures=[]
        original_connect=websockets.sync.client.connect
        with patch.dict(os.environ,{"REQUESTS_CA_BUNDLE":str(cert)}), patch.object(websockets.sync.client,"connect",lambda *a,**kw:original_connect(*a,ssl=ca,**kw)):
            try:
                relay_thread.start()
                until=time.monotonic()+10
                while not server.started and time.monotonic()<until:time.sleep(.02)
                assert server.started,"TLS relay failed to start"
                for index,name in enumerate(("كمبيوتر المنزل","كمبيوتر المكتب")):
                    desktop=home/f"desktop-{index}"
                    desktop.mkdir()
                    config=Config(desktop)
                    store=Store(desktop/"tasks.sqlite3")
                    vault=Vault()
                    scheduler=Scheduler(store,config,vault)
                    scheduler.start=lambda scheduler=scheduler:setattr(scheduler,"running",True)
                    bridge=Bridge(store,config,vault,scheduler)
                    bridge.timer.stop()
                    bridges.append(bridge)
                    bridge.internet.register(base,database.issue_enrollment(),name)
                until=time.monotonic()+20
                while time.monotonic()<until and not all(b.internet.state()["connected"] for b in bridges):
                    app.processEvents();time.sleep(.003)
                assert all(b.internet.state()["connected"] for b in bridges),[b.internet.state() for b in bridges]
                def run_browser():
                    try:browser_checks(base,bridges,gui,output)
                    except BaseException:failures.append(traceback.format_exc())
                browser_thread=threading.Thread(target=run_browser,daemon=True,name="test-phone-edge")
                browser_thread.start()
                until=time.monotonic()+180
                while browser_thread.is_alive() and time.monotonic()<until:
                    app.processEvents();time.sleep(.003)
                assert not browser_thread.is_alive(),"Phone browser acceptance timed out"
                if failures:raise AssertionError("\n".join(failures))
            finally:
                for bridge in bridges:
                    bridge.internet.stop();bridge.internet_commands.stop();bridge.remote.stop();bridge.scheduler.shutdown()
                # Continue pumping Qt so already queued commands are cancelled before DB cleanup.
                until=time.monotonic()+12
                while time.monotonic()<until and any(b.internet._thread and b.internet._thread.is_alive() for b in bridges):
                    app.processEvents();time.sleep(.01)
                for bridge in bridges:
                    if bridge.internet._thread:
                        bridge.internet._thread.join(1)
                        assert not bridge.internet._thread.is_alive(),"Connector remained live during cleanup"
                server.should_exit=True
                relay_thread.join(10)
                assert not relay_thread.is_alive(),"Relay remained live during cleanup"
                listener.close()
                if browser_thread:browser_thread.join(3)
                assert not browser_thread or not browser_thread.is_alive(),"Browser worker remained live"
                for bridge in bridges:bridge.store.db.close()
                database.engine.dispose()
                app.processEvents()
    print("Full TLS Internet stack passed: external QR/link navigation, real enrollment/WSS, two Qt desktop identities, phone approval, "
          "target tampering denied, queue isolation, BOM TXT byte preservation/download, task cancellation, "
          "independent second pairing, picker, offline503 without fallback, native phone revocation, cookie "
          "reload, no JS errors, and clean connector/relay/browser shutdown.")


if __name__=="__main__":main()
