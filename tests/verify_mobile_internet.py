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
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_account_bridge import phone_usage_fixtures


def main():
    from playwright.sync_api import sync_playwright, expect

    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    record = {"approved": False, "offline": False, "uncertain": False, "reject_second_phone": False,
              "posts": [], "state_requests": [], "grants": ["device-a", "device-b"],
              "library_calls": [], "deleted": False, "library_capable": True, "stale": False,
              "revoke_preview": False, "hold_next_state": False, "deny_state": False}
    state_held, release_state = threading.Event(), threading.Event()
    names = {"device-a": "كمبيوتر المنزل", "device-b": "كمبيوتر المكتب"}

    def state(device):
        return {"stats": {"total": 1, "active": 0, "done": 1, "attention": 0},
                "running": False, "autoStop": False, "templates": [], "folders": [],
                "capabilities": {key: record["library_capable"] for key in ("library", "library_preview", "library_delete", "library_organize")},
                "workers": [], "source": {"busy": False, "error": "", "revision": 1,
                                             "title": "", "items": []},
                "accountUsage": phone_usage_fixtures(),
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
                content_type = "text/javascript" if name.endswith(".js") else {"style.css": "text/css", "icon.svg": "image/svg+xml"}[name]
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
                    if record["hold_next_state"]:
                        record["hold_next_state"] = False
                        captured = state(device)
                        state_held.set()
                        assert release_state.wait(8), "Browser failed to release held state response"
                        self.reply(captured)
                    elif record["deny_state"]:
                        self.reply({"error": "أُلغي اقتران الهاتف"}, 403)
                    elif record["offline"]:
                        self.reply({"error": "الجهاز غير متصل"}, 503)
                    else:
                        self.reply(state(device))
                elif "/library/files" in self.path:
                    record["library_calls"].append(self.path)
                    query = parse_qs(urlsplit(self.path).query)
                    if "/preview" in self.path:
                        if record["revoke_preview"]:
                            self.reply({"error": "أُلغي اقتران الهاتف"}, 403)
                        else:
                            self.reply({"name": "نص المكتبة.txt", "text": '<script>window.library_xss=1</script>\nنص محفوظ', "truncated": True, "size": 200})
                    elif "/download" in self.path:
                        self.reply(b"library-output", content_type="application/octet-stream")
                    elif record["stale"] and "cursor" in query:
                        self.reply({"error": "تغير فهرس المكتبة"}, 409)
                    else:
                        second = "cursor" in query
                        files = [] if record["deleted"] else [{"file_id": "library-2" if second else "library-1", "name": "نص المكتبة.txt", "kind": "transcript", "group_id": "group-1", "group_name": "مادة طويلة " * 8, "size": 1024, "modified": 1700000000, "version": "v1", "can_download": True, "can_preview": True, "can_delete": True}]
                        self.reply({"files": files, "groups": [{"id": "group-1", "name": "مادة الاختبار"}], "total": 0 if record["deleted"] else 2, "revision": "r1", "next_cursor": None if second or record["deleted"] else "page-two", "busy": False, "error": "", "organizing": False})
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
                if self.path.endswith("/library_delete"):
                    record["deleted"] = True
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
            page.locator('[data-tab="workers"]').click()
            expect(page.locator("#account-usage article")).to_have_count(4)
            expect(page.locator("#account-usage article").nth(0)).to_contain_text("0٪")
            expect(page.locator("#account-usage article").nth(1)).to_contain_text("قراءة قديمة")
            expect(page.locator("#account-usage article").nth(2)).to_contain_text("غير مدعومة")
            assert page.evaluate("window.__quota_xss") is None
            assert page.locator("#account-usage img, #account-usage button").count() == 0
            assert page.evaluate("sessionStorage.length") == 0
            if output:
                page.screenshot(path=str(output / "internet-account-usage.png"), full_page=True)
            page.locator('[data-tab="queue"]').click()
            page.locator("#queue-toggle").click()
            page.locator("#confirm-yes").click()
            expect(page.locator("#toast")).to_have_text("تم التنفيذ")
            assert record["posts"][-1][0] == "/api/devices/device-a/action/queue"
            assert not record["library_calls"], "Queue polling must not scan the library"
            page.locator('[data-tab="files"]').click()
            expect(page.locator("#library-files article")).to_have_count(1)
            assert "/api/devices/device-a/library/files?" in record["library_calls"][-1]
            first_row = page.locator("#library-files article").first
            expect(first_row.locator("summary")).to_be_visible()
            assert first_row.bounding_box()["height"] <= 80
            if output:
                page.screenshot(path=str(output / "mobile-library-compact.png"), full_page=True)
            first_row.locator("summary").click()
            first_row.get_by_role("button", name="معاينة النص").click()
            expect(page.locator("#preview-text")).to_contain_text("<script>")
            expect(page.locator("#preview-status")).to_contain_text("معاينة مختصرة")
            assert page.evaluate("window.library_xss") is None
            if output:
                page.screenshot(path=str(output / "mobile-library-preview.png"), full_page=True)
            page.locator("#preview-close").click()
            expect(page.locator("#preview-text")).to_have_text("")
            record["stale"] = True
            page.locator("#library-more").click()
            expect(page.locator("#library-error")).to_contain_text("حدّث المكتبة")
            record["stale"] = False
            page.locator("#library-refresh").click()
            expect(page.locator("#library-error")).to_have_text("")
            page.locator("#library-more").click()
            expect(page.locator("#library-files article")).to_have_count(2)
            # Appending keyed rows preserves expanded content and keyboard context.
            expect(page.locator("#library-files article").first.locator("details")).to_have_attribute("open", "")
            record["uncertain"] = True
            page.locator("#library-files article").first.get_by_role("button", name="حذف نسخة المكتبة").click()
            page.locator("#confirm-yes").click()
            expect(page.locator("#library-error")).to_contain_text("غير مؤكدة")
            record["uncertain"] = False
            expect(page.locator("#library-organize")).to_be_enabled(timeout=10000)
            delete_requests = len([path for path, _ in record["posts"] if path.endswith("/library_delete")])
            page.locator("#library-files article").first.get_by_role("button", name="حذف نسخة المكتبة").click()
            expect(page.locator("#toast")).to_contain_text("حدّث المكتبة أولًا")
            expect(page.locator("#confirmation")).to_be_hidden()
            assert len([path for path, _ in record["posts"] if path.endswith("/library_delete")]) == delete_requests
            page.locator("#library-refresh").click()
            expect(page.locator("#library-status")).to_contain_text("عرض 1")
            page.locator("#library-files article").first.get_by_role("button", name="حذف نسخة المكتبة").click()
            expect(page.locator("#confirm-message")).to_contain_text(names["device-a"])
            expect(page.locator("#confirm-message")).to_contain_text("نص المكتبة.txt")
            expect(page.locator("#confirm-message")).to_contain_text("يبقى أصل المهمة محفوظًا")
            page.locator("#confirm-yes").click()
            expect(page.locator("#library-files .empty")).to_be_visible()
            assert any(path.endswith("/action/library_delete") and data == {"file_id": "library-1", "expected_version": "v1", "confirm": True} for path, data in record["posts"])
            page.locator("#results-tab").click()
            expect(page.locator("#files article")).to_have_count(1), "Library delete must preserve job output"
            record["library_capable"] = False
            page.locator('[data-tab="queue"]').click()
            page.locator("#refresh").click()
            page.locator('[data-tab="files"]').click()
            page.locator("#library-tab").click()
            expect(page.locator("#library-compat")).to_contain_text("حدّث ورق")
            expect(page.locator("#library-controls")).to_be_hidden()
            page.locator("#results-tab").click()
            expect(page.locator("#files article")).to_have_count(1)
            record["library_capable"] = True
            # A state response authorized before revocation must not resurrect
            # private UI after a concurrent preview detects HTTP403.
            expect(page.locator("#workspace")).to_be_visible()
            page.wait_for_function("!ui.polling")
            record["hold_next_state"] = True
            assert page.evaluate("""() => {
                if (ui.polling) return false;
                window.resumeAuthorizedPoll = poll;
                window.poll = () => Promise.resolve();
                window.heldStatePromise = window.resumeAuthorizedPoll();
                return true;
            }"""), "Could not isolate state request from the automatic polling timer"
            assert state_held.wait(5), "State request was not held"
            record["revoke_preview"] = True
            page.evaluate("() => api(devicePath('/library/files/library-1/preview')).catch(() => null)")
            expect(page.locator("#workspace")).to_be_hidden()
            record["deny_state"] = True
            release_state.set()
            page.evaluate("window.heldStatePromise")
            expect(page.locator("#workspace")).to_be_hidden()
            expect(page.locator("#jobs article")).to_have_count(0)
            expect(page.locator("#library-files article")).to_have_count(0)
            expect(page.locator("#account-usage article")).to_have_count(0)
            record["deny_state"], record["revoke_preview"] = False, False
            page.evaluate("async () => { window.poll=window.resumeAuthorizedPoll; await poll(); }")
            expect(page.locator("#workspace")).to_be_visible()
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
            expect(page.locator("#account-usage .usage-cached")).to_have_count(4)
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
                for width, height in ((320, 740), (375, 812), (844, 390), (768, 1024), (1440, 900)):
                    page.set_viewport_size({"width": width, "height": height})
                    for tab in ("queue", "source", "workers", "files"):
                        page.locator(f'[data-tab="{tab}"]').click()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.set_viewport_size({"width": 320, "height": 740})
                page.evaluate("document.documentElement.style.fontSize='32px'")
                for tab in ("queue", "source", "workers", "files"):
                    page.locator(f'[data-tab="{tab}"]').click()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (scheme, tab, "200% text")
                page.evaluate("document.documentElement.style.fontSize='16px'")
            page.locator("#logout").click()
            expect(page).to_have_url(base + "/internet")
            expect(page.locator("#device-list a")).to_have_count(1)
            assert record["grants"] == ["device-b"]
            # Pair-link imports remain inside the current origin and document.
            assert not page.evaluate("window.waraqPwaBridge.acceptPairLink('https://evil.test/pair#abcdefghijklmnopqrstuvwxyz')")
            assert page.evaluate("window.waraqPwaBridge.acceptPairLink(location.origin + '/pair#abcdefghijklmnopqrstuvwxyz')")
            expect(page.locator("#internet-pair")).to_be_visible()
            assert page.evaluate("location.hash") == ""
            page.goto(base + "/internet")
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
        release_state.set()
        server.shutdown()
        thread.join(3)
        server.server_close()
    print("Internet mobile browser passed: QR fragment removal, desktop approval waiting, exact target "
          "routing, authorized picker, fresh forms on device navigation, forbidden target, offline/recovery, "
          "no automatic mutation replay, device-specific logout, one-phone refusal with replacement guidance, "
          "compact rows, library pagination/stale cursor, safe preview, uncertain delete refresh guard, "
          "original job output retained, legacy capability fallback, PWA pair-link bridge, "
          "320px/200%text layouts, no JS errors.")


if __name__ == "__main__":
    main()
