"""Local PDF-translator API + static file server (stdlib only).

Serves ../web over HTTP for local use and exposes:
  GET  /api/health
  POST /api/translate   {target: ru|fa, texts: [...], source?: en}
  GET  /api/fetch-pdf?url=<http(s) URL>

The API key stays on this server. The static page never sees it.
Provider auto-selection (first key found wins):
  OPENAI_API_KEY (+ optional OPENAI_BASE_URL) -> OpenAI-compatible chat
  TRANSLATION_API_KEY (+ TRANSLATION_BASE_URL) -> generic OpenAI-compatible
  DEEPL_API_KEY (+ optional DEEPL_BASE_URL) -> DeepL v2
  GOOGLE_TRANSLATE_API_KEY -> Google Translate v2
No key -> clearly-marked mock translator so the UI stays testable.
"""

import json
import os
import re
import socket
import ipaddress
import urllib.request
import urllib.parse
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR.parent / "web"
if not WEB_DIR.exists():
    WEB_DIR = BASE_DIR / "web"

TARGETS = ("ru", "fa")
MAX_TEXTS = 100
MAX_TEXT_LEN = 4000
MAX_TOTAL_CHARS = 60000
MAX_PDF_BYTES = 40 * 1024 * 1024
FETCH_TIMEOUT = 30

MOCK_DICT_RU = {
    "surrender": "Капитуляция",
    "accountability": "Ответственность",
    "recovery": "Восстановление",
    "god": "Бог",
    "hope": "Надежда",
    "faith": "Вера",
    "sobriety": "Трезвость",
}
MOCK_DICT_FA = {
    "surrender": "تسلیم",
    "accountability": "پاسخگویی",
    "recovery": "بهبودی",
    "god": "خدا",
    "hope": "امید",
    "faith": "ایمان",
    "sobriety": "هوشیاری",
}


def provider_name():
    if os.environ.get("OPENAI_API_KEY") or (
        os.environ.get("TRANSLATION_API_KEY") and os.environ.get("TRANSLATION_BASE_URL")
    ):
        return "openai-compatible"
    if os.environ.get("DEEPL_API_KEY"):
        return "deepl"
    if os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        return "google"
    return "mock"


def mock_translate(texts, target):
    table = MOCK_DICT_RU if target == "ru" else MOCK_DICT_FA
    tag = "[RU-mock] " if target == "ru" else "[FA-mock] نمونه: "

    def one(text):
        low = text.strip().lower()
        if low in table:
            return table[low]
        # translate a few known words inline so output shows real target script
        def repl(m):
            return table.get(m.group(0).lower(), m.group(0))

        converted = re.sub(r"[A-Za-z]+", repl, text)
        return tag + converted

    return [one(t) for t in texts]


def openai_translate(texts, target, timeout=60):
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("TRANSLATION_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
        "TRANSLATION_BASE_URL", "https://api.openai.com/v1"
    )
    model = os.environ.get("TRANSLATION_MODEL", "gpt-4o-mini")
    lang = "Russian" if target == "ru" else "Persian (Farsi)"
    url = base.rstrip("/") + "/chat/completions"
    items = [{"id": i, "text": t} for i, t in enumerate(texts)]
    prompt = (
        f"Translate each JSON item's text from English to {lang}. "
        "Preserve meaning, keep numbers, names, and punctuation. "
        "Return ONLY a JSON array of translated strings in the same order, no commentary."
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    try:
        out = json.loads(content)
        if isinstance(out, list) and len(out) == len(texts):
            return [str(x) for x in out]
    except (ValueError, KeyError):
        pass
    # fallback: try to extract a JSON array from prose
    m = re.search(r"\[.*\]", content, re.S)
    if m:
        out = json.loads(m.group(0))
        if isinstance(out, list) and len(out) == len(texts):
            return [str(x) for x in out]
    raise ValueError("translation provider returned an unparseable response")


def deepl_translate(texts, target, timeout=60):
    key = os.environ.get("DEEPL_API_KEY", "")
    base = os.environ.get("DEEPL_BASE_URL", "https://api-free.deepl.com/v2").rstrip("/")
    lang = "RU" if target == "ru" else "FA"
    form = [("target_lang", lang)]
    for t in texts:
        form.append(("text", t))
    body = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
    req = urllib.request.Request(
        base + "/translate",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"DeepL-Auth-Key {key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    out = [x["text"] for x in payload.get("translations", [])]
    if len(out) != len(texts):
        raise ValueError("translation provider returned the wrong item count")
    return out


def google_translate(texts, target, timeout=60):
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    url = "https://translation.googleapis.com/language/translate/v2?key=" + urllib.parse.quote(key)
    body = json.dumps({"q": texts, "target": target, "format": "text"}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    out = [x["translatedText"] for x in payload.get("data", {}).get("translations", [])]
    if len(out) != len(texts):
        raise ValueError("translation provider returned the wrong item count")
    return out


def translate_texts(texts, target):
    name = provider_name()
    if name == "openai-compatible":
        return openai_translate(texts, target), name
    if name == "deepl":
        return deepl_translate(texts, target), name
    if name == "google":
        return google_translate(texts, target), name
    return mock_translate(texts, target), name


# ---- SSRF-safe PDF fetch ----

def _host_is_public(hostname):
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"refusing non-public address for {hostname}")
    return True


def fetch_pdf_bytes(url, max_redirects=3):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are not allowed")
    if not parsed.hostname:
        raise ValueError("URL has no host")
    current = url
    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlparse(current)
        _host_is_public(parsed.hostname)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": "pdf-translator-local/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                status = getattr(r, "status", 200)
                if status in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        raise ValueError("redirect without Location")
                    current = urllib.parse.urljoin(current, loc)
                    continue
                length = r.headers.get("Content-Length")
                if length and int(length) > MAX_PDF_BYTES:
                    raise ValueError("PDF exceeds the 40 MB limit")
                chunks, total = [], 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise ValueError("PDF exceeds the 40 MB limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "pdf" not in ctype and not data[:5] == b"%PDF-":
                    raise ValueError("URL did not return a PDF document")
                if not data[:5] == b"%PDF-":
                    raise ValueError("URL did not return a PDF document")
                return data
        except urllib.error.HTTPError as e:
            raise ValueError(f"fetch failed with HTTP {e.code}")
        except urllib.error.URLError as e:
            raise ValueError(f"fetch failed: {e.reason}")
    raise ValueError("too many redirects")


def validate_translate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    target = payload.get("target")
    texts = payload.get("texts")
    if target not in TARGETS:
        raise ValueError('target must be "ru" or "fa"')
    if not isinstance(texts, list) or not texts:
        raise ValueError("texts must be a non-empty array")
    if len(texts) > MAX_TEXTS:
        raise ValueError(f"texts is capped at {MAX_TEXTS} items per request")
    total = 0
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            raise ValueError("every text must be a non-empty string")
        if len(t) > MAX_TEXT_LEN:
            raise ValueError(f"each text is capped at {MAX_TEXT_LEN} characters")
        total += len(t)
    if total > MAX_TOTAL_CHARS:
        raise ValueError(f"request total is capped at {MAX_TOTAL_CHARS} characters")
    return target, texts


class Handler(SimpleHTTPRequestHandler):
    server_version = "pdf-translator-local/1.0"

    def log_message(self, *args):
        pass  # quiet; errors go through JSON responses

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(200, {
                "ok": True,
                "provider": provider_name(),
                "targets": list(TARGETS),
                "mock": provider_name() == "mock",
            })
            return
        if parsed.path == "/api/fetch-pdf":
            qs = urllib.parse.parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0]
            if not url:
                self._json(400, {"error": "missing url parameter"})
                return
            try:
                data = fetch_pdf_bytes(url)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        # static files for local dev
        self.directory = str(WEB_DIR)
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/translate":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2 * 1024 * 1024:
            self._json(400, {"error": "invalid request body size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            target, texts = validate_translate_payload(payload)
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": str(e) or "invalid JSON body"})
            return
        try:
            translations, name = translate_texts(texts, target)
        except Exception as e:
            self._json(502, {"error": f"translation provider failed: {e}"})
            return
        headers = {"X-Translator-Provider": name}
        if name == "mock":
            headers["X-Translator-Mock"] = "1"
        self._json(200, {"translations": translations, "provider": name}, headers)


def main():
    port = int(os.environ.get("PDF_TRANSLATOR_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"pdf-translator server on http://127.0.0.1:{port} (provider={provider_name()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
