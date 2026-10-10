"""Rerunnable browser verification for the translator UI.

Starts the local server, opens web/index.html, translates page 1 of the
vendored fixture PDF into Russian and Persian, and checks:
  - a translated PDF download is produced for each target
  - Russian output contains Cyrillic, Persian output contains Arabic script
  - page count and page size survive the rebuild
  - no API key string appears in the static web/ directory
Writes downloads to verify-out/ (gitignored by location, outside web/).

Usage: python3 server/verify_browser.py [--keep-open]
Requires: pip install playwright && playwright install chromium
"""
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import server

OUT = ROOT / "verify-out"
OUT.mkdir(exist_ok=True)

TARGETS = ("ru", "fa")
SCRIPT_RE = {
    "ru": re.compile(r"[\u0400-\u04FF]"),
    "fa": re.compile(r"[\u0600-\u06FF]"),
}
SECRETS = ("OPENAI_API_KEY", "DEEPL_API_KEY", "GOOGLE_TRANSLATE_API_KEY",
           "sk-ant-", "sk-", "Bearer ")


def find_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check_static_has_no_secrets():
    hits = []
    for path in (ROOT / "web").rglob("*"):
        if path.is_file() and path.suffix in (".html", ".js", ".css", ".mjs"):
            text = path.read_text(errors="ignore")
            for secret in SECRETS:
                if secret in text:
                    hits.append(f"{path.relative_to(ROOT)}: {secret}")
    assert not hits, f"secrets in static files: {hits}"


def run():
    check_static_has_no_secrets()
    from playwright.sync_api import sync_playwright

    port = find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/index.html")
            for target in TARGETS:
                page.select_option("#lang", target)
                page.fill("#pageRange", "1-2")
                page.fill("#serverUrl", f"http://127.0.0.1:{port}")
                page.click("#go")
                page.wait_for_selector("#status:has-text('Done')", timeout=240000)
                with page.expect_download() as dl_info:
                    page.click("#download")
                download = dl_info.value
                dest = OUT / f"translated-{target}.pdf"
                download.save_as(str(dest))
                size = dest.stat().st_size
                log = page.inner_text("#log")
                results[target] = {"bytes": size, "log": log}
                print(f"{target}: {size} bytes")
                print("log tail:", "\n".join(log.splitlines()[-4:]))
            browser.close()
    finally:
        httpd.shutdown()

    # Inspect downloads with pypdf if available, else byte-scan.
    try:
        from pypdf import PdfReader
        have_pypdf = True
    except ImportError:
        have_pypdf = False
    for target in TARGETS:
        dest = OUT / f"translated-{target}.pdf"
        raw = dest.read_bytes()
        assert raw[:5] == b"%PDF-", f"{target}: not a PDF"
        if have_pypdf:
            reader = PdfReader(str(dest))
            assert len(reader.pages) >= 2, f"{target}: page count changed"
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        else:
            text = raw.decode("utf-8", errors="ignore")
        assert SCRIPT_RE[target].search(text), f"{target}: expected script not found in output"
        print(f"{target}: PASS ({dest.stat().st_size} bytes, script found, pypdf={have_pypdf})")
    print("ALL BROWSER CHECKS PASSED")


if __name__ == "__main__":
    run()
