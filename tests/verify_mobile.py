"""Optional real-browser LAN acceptance. Requires Playwright and installed Edge.

Run: python tests/verify_mobile.py [screenshot-directory]
Uses a temporary database, a headless browser and service doubles; no paid calls.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store, STAGES


def browser_checks(base, code, output):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True,
                                      has_touch=True, locale="ar-JO", color_scheme="dark",
                                      reduced_motion="reduce", accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        expect(page.locator("#login")).to_be_visible()
        page.locator("#code").fill("wrong")
        page.locator("#pair-button").click()
        expect(page.locator("#pair-error")).to_contain_text("غير صحيح")
        page.locator("#code").fill(code)
        page.locator("#pair-button").click()
        expect(page.locator("#workspace")).to_be_visible()
        expect(page.locator("#total")).to_have_text("3")
        expect(page.locator("#connection")).to_have_text("متصل بالكمبيوتر")
        assert page.evaluate("window.__waraq_xss") is None
        if output:
            page.screenshot(path=str(output / "mobile-queue-dark.png"), full_page=True)

        page.locator("#queue-toggle").click()
        expect(page.locator("#confirmation")).to_be_visible()
        page.locator("#confirm-yes").click()
        expect(page.locator("#queue-state")).to_have_text("الطابور يعمل")
        page.locator("#queue-toggle").click()
        expect(page.locator("#queue-state")).to_have_text("الطابور متوقف")

        page.locator("#filter").select_option("attention")
        expect(page.locator("#jobs article")).to_have_count(1)
        page.locator("#jobs summary").click()
        expect(page.locator("#jobs details")).to_have_attribute("open", "")
        page.locator("#jobs").get_by_role("button", name="إعادة المحاولة").click()
        expect(page.locator("#jobs .empty")).to_be_visible()
        page.locator("#filter").select_option("all")

        page.locator('[data-tab="source"]').click()
        page.locator("#url").fill("https://youtu.be/synthetic")
        page.locator("#discover").click()
        expect(page.locator("#source-selection")).to_be_visible()
        expect(page.locator("#source-items input")).to_have_count(2)
        page.locator("#select-none").click()
        page.locator("#source-items input").first.check()
        page.locator("#folder").select_option(label="مادة الهاتف")
        page.locator("#enqueue").click()
        expect(page.locator("#total")).to_have_text("4")
        expect(page.locator("#source-selection")).to_be_hidden()
        content = b"\xef\xbb\xbf" + "تفريغ من الهاتف\r\nبنص محفوظ".encode()
        page.locator("#transcripts").set_input_files({"name": "درس مرفوع.txt", "mimeType": "text/plain", "buffer": content})
        page.locator("#upload").click()
        expect(page.locator("#total")).to_have_text("5")
        expect(page.locator("#transcripts")).to_have_value("")

        page.locator('[data-tab="workers"]').click()
        service = page.locator("#services article").first
        service.get_by_role("button").click()
        expect(service.locator(".badge")).to_have_text("متوقفة يدويًا")
        service.get_by_role("button").click()
        expect(service.locator(".badge")).to_have_text("تستقبل المهام")

        page.locator('[data-tab="files"]').click()
        with page.expect_download() as download_info:
            page.get_by_role("link", name="تنزيل Word — درس مكتمل").click()
        assert Path(download_info.value.path()).read_bytes() == b"synthetic-docx-output"
        with page.expect_download() as transcript_info:
            page.get_by_role("link", name="تنزيل التفريغ — درس مرفوع").click()
        assert Path(transcript_info.value.path()).read_bytes() == content

        # Check every page in both themes and small, tablet, landscape and desktop widths.
        for scheme in ("dark", "light"):
            page.emulate_media(color_scheme=scheme)
            for width, height in ((360, 740), (375, 812), (768, 1024), (844, 390), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                for tab in ("queue", "source", "workers", "files"):
                    page.locator(f'[data-tab="{tab}"]').click()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (scheme, width, tab)
            page.set_viewport_size({"width": 375, "height": 812})
            page.locator('[data-tab="source"]').click()
            if output:
                page.screenshot(path=str(output / f"mobile-source-{scheme}.png"), full_page=True)
        page.evaluate("document.documentElement.style.fontSize='24px'")
        for tab in ("queue", "source", "workers", "files"):
            page.locator(f'[data-tab="{tab}"]').click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), ("large text", tab)
        page.evaluate("document.documentElement.style.fontSize='16px'")

        page.locator('[data-tab="queue"]').click()
        context.set_offline(True)
        page.locator("#refresh").click()
        expect(page.locator("#offline")).to_be_visible()
        expect(page.locator("#queue-toggle")).to_be_disabled()
        context.set_offline(False)
        expect(page.locator("#offline")).to_be_hidden(timeout=10000)
        expect(page.locator("#queue-toggle")).to_be_enabled()
        page.reload()
        expect(page.locator("#workspace")).to_be_visible()  # HttpOnly cookie restores the session.
        page.locator("#logout").click()
        expect(page.locator("#login")).to_be_visible()
        expect(page.locator("#workspace")).to_be_hidden()
        assert not errors, errors
        browser.close()


def main():
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    app = QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory(prefix="waraq-mobile-test-") as directory:
        home = Path(directory)
        config, store = Config(home), Store(home / "tasks.sqlite3")
        scheduler = Scheduler(store, config, None)
        scheduler.start = lambda: setattr(scheduler, "running", True)
        bridge = Bridge(store, config, None, scheduler)
        bridge.timer.stop()
        store.create_folder("مادة الهاتف")
        ids = store.add([{"video_id": str(i), "position": i + 1, "title": title} for i, title in enumerate([
            "درس مكتمل", "درس ينتظر المعالجة", '<img src=x onerror="window.__waraq_xss=1">'
        ])], config.snapshot())
        for stage in STAGES:
            store.set_stage(ids[0], stage, "completed")
        store.set_stage(ids[2], "download", "failed", error="خطأ تجريبي للشبكة")
        folder = Path(store.get(ids[0])["folder"])
        folder.mkdir(parents=True)
        document = folder / "lesson.docx"
        document.write_bytes(b"synthetic-docx-output")
        store.db.execute("UPDATE jobs SET document=? WHERE id=?", (str(document), ids[0]))
        scheduler.youtube.discover = lambda *args: {"title": "قائمة الهاتف", "items": [
            {"video_id": str(i), "title": f"درس جديد {i}", "position": i,
             "url": f"https://www.youtube.com/watch?v={i}", "available": True, "selected": True} for i in (1, 2)]}
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        bridge.remote.start(port)
        assert bridge.remoteAccess["enabled"]
        urls = bridge.remoteAccess["urls"]
        base = urls[0] if urls else f"http://127.0.0.1:{port}"
        failures = []
        def run_browser():
            try:
                browser_checks(base, bridge.remoteAccess["code"], output)
            except BaseException:
                failures.append(traceback.format_exc())
        thread = threading.Thread(target=run_browser, daemon=True)
        thread.start()
        deadline = time.monotonic() + 180
        while thread.is_alive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.003)
        bridge.remote.stop()
        thread.join(3)
        assert not thread.is_alive(), "Browser acceptance timed out"
        if failures:
            raise AssertionError("\n".join(failures))
        jobs = store.jobs()
        assert len(jobs) == 5
        uploaded = next(j for j in jobs if j["title"] == "درس مرفوع")
        assert Path(uploaded["transcript"]).read_bytes().startswith(b"\xef\xbb\xbf")
        assert store.folder_name(uploaded["folder_id"]) == "مادة الهاتف"
        scheduler.shutdown()
        store.db.close()
    print("Mobile browser passed: pairing, queue start/pause, retry, YouTube selection/enqueue, UTF-8 upload, "
          "service pause/resume, Word/TXT download, XSS text safety, reload/logout, offline/reconnect, "
          "5 viewport sizes in both themes, large text, no horizontal overflow or JavaScript errors.")


if __name__ == "__main__":
    main()
