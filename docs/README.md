# PDF translator (Russian / Persian)

Static page plus a local translation server. The page keeps the source PDF's
page sizes, positions, and graphics, covers each text block, and re-draws the
translation in place with auto-shrink fitting.

- Live page: https://mattz0rt.github.io/pdf-translator/
- Default PDF: https://www.roykfiles.com/surrenderaccountability2004.pdf
- Repo: https://github.com/mattz0rt/pdf-translator

## Run the local server

```sh
cd ~/pdf-translator
python3 server/server.py
# listens on http://127.0.0.1:8000 (PDF_TRANSLATOR_PORT to change)
```

Open the page, keep the server URL as `http://127.0.0.1:8000`, pick Russian
or Persian, optionally limit pages (e.g. `1-3`), and click Translate.

## Translation providers

Set one key before starting the server. First key found wins.

| Env var | Provider |
|---|---|
| `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`, `TRANSLATION_MODEL`) | OpenAI-compatible chat (default model `gpt-4o-mini`) |
| `TRANSLATION_API_KEY` + `TRANSLATION_BASE_URL` | Any OpenAI-compatible endpoint |
| `DEEPL_API_KEY` (+ optional `DEEPL_BASE_URL`) | DeepL v2 (`RU`, `FA`) |
| `GOOGLE_TRANSLATE_API_KEY` | Google Translate v2 |

No key means clearly-labeled mock output for layout testing. The page shows a
mock warning and the server sends `X-Translator-Mock: 1`. The key never leaves
the server. Static files contain no secrets (`verify_browser.py` scans for
them on every run).

## How layout survives

- PDF.js extracts positioned text lines. Lines merge into paragraphs.
- Paragraphs go to `/api/translate` in order. Responses must match the item
  count or the run fails instead of misplacing text.
- pdf-lib reloads the original bytes (graphics and fonts untouched), paints an
  opaque cover over each source-text rectangle, and draws the translation with
  an embedded font: DejaVu Sans for Russian, Noto Naskh Arabic for Persian.
- Persian text is reshaped to presentation forms and reordered to visual RTL
  before drawing, because PDF has no shaping engine.
- Text that cannot fit shrinks to 5pt and is flagged by its tight box rather
  than clipped silently. Page count and page sizes are unchanged.

## Limits

- PDF fetch: public http(s) only, no credentials in URLs, DNS is re-checked on
  every redirect, 40 MB cap, 30 s timeout.
- Translation: 100 texts and 60,000 characters per request, 4,000 per text.
- Scanned or encrypted PDFs have no extractable text and fail with a clear
  error. Complex tables and footnotes keep positions but may reflow inside
  their own boxes.
- The hosted page is plain http(s) to `127.0.0.1:8000`. Browsers allow the
  page to call the loopback server while you run it locally. Use
  `http://127.0.0.1:8000/index.html` from this directory for fully offline use.

## Checks

```sh
python3 server/test_server.py      # offline boundary tests (9 tests)
python3 server/verify_browser.py   # full browser run, both languages
```

`verify_browser.py` starts its own server, translates pages 1-2 of the default
PDF into Russian and Persian, saves downloads to `verify-out/`, and asserts
the expected script is embedded in each file.
