"""Deterministic browser acceptance for Internet phone UI (no VPS or paid calls).

Run: python tests/verify_mobile_internet.py [screenshot-directory]
Relay authorization/transport itself is tested separately by test_relay.py.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    from playwright.sync_api import sync_playwright, expect

    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    record = {"approved": False, "offline": False, "uncertain": False, "reject_second_phone": False,
              "posts": [], "state_requests": [], "grants": ["device-a", "device-b"]}
    names = {"device-a": "كمبيوتر المنزل", "device-b": "كمبيوتر المكتب"}

    def state(device):
        return {"stats": {"total": 1, "active": 0, "done": 1, "attention": 0},
                "running": False, "autoStop": False, "templates": [], "folders": [],
                "workers": [], "source": {"busy": False, "error": "", "revision": 1,
                                             "title": "", "items": []},
                "jobs": [{"id": "job-" + device, "title": "درس " + names[device],
                          "status": "completed", "statusLabel": "مكتملة", "stageLabel": "",
                          "template": "", "folder_name": "", "files": ["document"]}]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, status=200, content_type="application/json"):
            raw = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith("/assets/"):
                name = self.path.rsplit("/", 1)[1]
                path = ROOT / "waraq" / ("icon.svg" if name == "icon.svg" else "mobile/" + name)
                content_type = {"app.js": "text/javascript", "style.css": "text/css",
                                "icon.svg": "image/svg+xml"}[name]
                self.reply(path.read_bytes(), content_type=content_type)
            elif self.path == "/api/bootstrap":
                self.reply({"mode": "internet", "protocol": 1})
            elif self.path == "/api/devices":
                self.reply({"devices": [{"device_id": device, "name": names[device],
                                         "online": not record["offline"], "last_seen": 1720000000}
                                        for device in record["grants"]]})
            elif self.path.startswith("/api/pair/status/"):
                self.reply({"status": "approved" if record["approved"] else "pending",
                            "device_id": "device-a", "device_name": names["device-a"]})
            elif self.path.startswith("/api/devices/"):
                device = self.path.split("/")[3]
                if device not in record["grants"]:
                    self.reply({"error": "هذا الهاتف غير مقترن بهذا الكمبيوتر"}, 403)
                elif self.path.endswith("/state"):
                    record["state_requests"].append(device)
                    if record["offline"]:
                        self.reply({"error": "الجهاز غير متصل"}, 503)
                    else:
                        self.reply(state(device))
                elif "/files/" in self.path:
                    self.reply(b"result-" + device.encode(), content_type="application/octet-stream")
                else:
                    self.reply({"error": "غير موجود"}, 404)
            else:
                self.reply((ROOT / "waraq/mobile/index.html").read_bytes(), content_type="text/html")

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            record["posts"].append((self.path, payload))
            assert self.headers.get("X-Waraq-Client") == "1"
            if self.path == "/api/pair/start":
                if record["reject_second_phone"]:
                    self.reply({"error": "هذا الجهاز مرتبط بهاتف بالفعل. ألغِ ربط الهاتف الحالي من ورق ثم أنشئ QR جديدًا."}, 409)
                else:
                    self.reply({"claim_id": "claim-one", "device_name": names["device-a"], "match_code": "123456"})
            elif self.path.endswith("/logout"):
                record["grants"].remove(self.path.split("/")[3])
                self.reply({"ok": True})
            elif record["uncertain"]:
                self.connection.close()  # Mutation has reached server; do not replay it.
            else:
                self.reply({"message": "تم التنفيذ"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_address[1])
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 375, "height": 812},
                                          locale="ar-JO", is_mobile=True, has_touch=True,
                                          color_scheme="dark", reduced_motion="reduce")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/pair#one-use-secret")
            expect(page.locator("#internet-pair")).to_be_visible()
            assert page.evaluate("location.hash") == ""
            assert not record["posts"]
            page.locator("#phone-name").fill("هاتف الاختبار")
            page.locator("#internet-pair-button").click()
            expect(page.locator("#pair-device-name")).to_have_text(names["device-a"])
            expect(page.locator("#pair-match-code")).to_have_text("123456")
            expect(page.locator("#workspace")).to_be_hidden()
            assert record["posts"] == [("/api/pair/start", {"ticket": "one-use-secret", "phone_name": "هاتف الاختبار"})]
            if output:
                page.screenshot(path=str(output / "internet-pair-pending.png"), full_page=True)
            record["approved"] = True
            expect(page).to_have_url(base + "/devices/device-a", timeout=10000)
            expect(page.locator("#device-name")).to_have_text(names["device-a"])
            expect(page.locator("#workspace")).to_be_visible()
            expect(page.locator("#jobs a")).to_have_attribute("href", "/api/devices/device-a/files/job-device-a/document")
            assert page.evaluate("localStorage.length") == 0
            page.locator("#queue-toggle").click()
            page.locator("#confirm-yes").click()
            expect(page.locator("#toast")).to_have_text("تم التنفيذ")
            assert record["posts"][-1][0] == "/api/devices/device-a/action/queue"
            page.locator('[data-tab="source"]').click()
            page.locator("#url").fill("https://youtu.be/for-device-a")
            page.locator("#change-device").click()
            expect(page.locator("#internet-home")).to_be_visible()
            expect(page.locator("#device-list a")).to_have_count(2)
            page.get_by_role("link", name=names["device-b"], exact=True).click()
            expect(page.locator("#device-name")).to_have_text(names["device-b"])
            expect(page.locator("#url")).to_have_value("")
            expect(page.locator("#jobs")).to_contain_text("درس " + names["device-b"])
            expect(page.locator("#jobs")).not_to_contain_text(names["device-a"])
            assert page.evaluate("targetDevice") == "device-b"
            page.goto(base + "/devices/not-authorized")
            expect(page.locator("#internet-home")).to_be_visible()
            expect(page.locator("#workspace")).to_be_hidden()
            expect(page.locator("#device-error")).to_contain_text("يلزم اقتران")
            page.goto(base + "/devices/device-a")
            expect(page.locator("#workspace")).to_be_visible()
            record["offline"] = True
            page.locator("#refresh").click()
            expect(page.locator("#offline")).to_contain_text("الجهاز غير متصل")
            expect(page.locator("#queue-toggle")).to_be_disabled()
            expect(page.locator("#device-name")).to_have_text(names["device-a"])
            if output:
                page.screenshot(path=str(output / "internet-device-offline.png"), full_page=True)
            record["offline"] = False
            expect(page.locator("#offline")).to_be_hidden(timeout=10000)
            record["uncertain"] = True
            posts_before = len(record["posts"])
            page.locator("#queue-toggle").click()
            page.locator("#confirm-yes").click()
            expect(page.locator("#toast")).to_contain_text("تحقق من حالة الطابور")
            page.wait_for_timeout(3000)
            assert len(record["posts"]) == posts_before + 1
            record["uncertain"] = False
            for scheme in ("light", "dark"):
                page.emulate_media(color_scheme=scheme)
                for width, height in ((375, 812), (844, 390), (768, 1024)):
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#logout").click()
            expect(page).to_have_url(base + "/internet")
            expect(page.locator("#device-list a")).to_have_count(1)
            assert record["grants"] == ["device-b"]
            expect(page.locator("#internet-home")).to_contain_text("هاتفًا واحدًا فقط")
            expect(page.locator("#internet-home")).to_contain_text("بأكثر من كمبيوتر")

            # A second physical phone sees the server's single-phone policy;
            # it cannot acquire a claim or trigger automatic enrollment retries.
            record["reject_second_phone"] = True
            second_context = browser.new_context(viewport={"width": 375, "height": 812})
            second_page = second_context.new_page()
            second_page.on("pageerror", lambda error: errors.append(str(error)))
            second_page.goto(base + "/pair#second-phone-ticket")
            expect(second_page.locator("#internet-pair-policy")).to_contain_text("هاتفًا واحدًا فقط")
            posts_before = len(record["posts"])
            second_page.locator("#internet-pair-button").click()
            expect(second_page.locator("#internet-pair-error")).to_contain_text("مرتبط بهاتف بالفعل")
            expect(second_page.locator("#internet-pair-error")).to_contain_text("ألغِ ربط الهاتف الحالي")
            expect(second_page.locator("#pair-waiting")).to_be_hidden()
            expect(second_page.locator("#workspace")).to_be_hidden()
            expect(second_page.locator("#internet-pair-button")).to_be_disabled()
            second_page.wait_for_timeout(3000)
            assert len(record["posts"]) == posts_before + 1
            assert record["grants"] == ["device-b"]
            second_context.close()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
    print("Internet mobile browser passed: QR fragment removal, desktop approval waiting, exact target "
          "routing, authorized picker, fresh forms on device navigation, forbidden target, offline/recovery, "
          "no automatic mutation replay, device-specific logout, one-phone refusal with replacement guidance, "
          "mobile layouts, no JS errors.")


if __name__ == "__main__":
    main()
