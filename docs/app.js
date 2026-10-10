/* Layout-preserving PDF translation in the browser.
 * Pipeline per page:
 * 1. PDF.js extracts positioned text lines with bounding boxes.
 * 2. Lines are grouped into paragraphs and sent to the local server.
 * 3. pdf-lib draws an opaque cover over each source-text rectangle (graphics untouched).
 * 4. Translated text is re-drawn in the same rectangle with auto-shrink fitting.
 * Russian uses Helvetica (WinAnsi Cyrillic); Persian uses an embedded Noto Naskh
 * Arabic-subset font with RTL alignment so joining/shaping is handled by the font.
 */

import * as pdfjs from "./vendor/pdf.min.mjs";
pdfjs.GlobalWorkerOptions.workerSrc = "./vendor/pdf.worker.min.mjs";

// Persian font is vendored alongside the page; CDN copies are fallback only.
const FA_FONT_URLS = [
  "./vendor/NotoNaskhArabic-Regular.ttf",
  "https://cdn.jsdelivr.net/fontsource/fonts/noto-naskh-arabic@latest/arabic-400-normal.ttf",
];
const RU_FONT_URLS = [
  "./vendor/DejaVuSans.ttf",
  "https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans.ttf",
];
// Target fonts are embedded per language; Helvetica is the last-resort fallback.

const $ = (id) => document.getElementById(id);
const log = (m) => { $("log").textContent += m + "\n"; };
const setStatus = (m) => { $("status").textContent = m; };
const setProgress = (f) => {
  const p = $("progress");
  p.hidden = false;
  p.value = Math.round(f * 100);
};

function parseRange(spec, max) {
  if (!spec.trim()) return Array.from({ length: max }, (_, i) => i + 1);
  const out = new Set();
  for (const part of spec.split(",")) {
    const m = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!m) throw new Error(`bad page range: ${part}`);
    const a = +m[1], b = m[2] ? +m[2] : a;
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) if (i >= 1 && i <= max) out.add(i);
  }
  return [...out].sort((x, y) => x - y);
}

async function fetchPdfBytes(url, serverBase) {
  const r = await fetch(`${serverBase}/api/fetch-pdf?url=${encodeURIComponent(url)}`);
  if (r.ok && (r.headers.get("Content-Type") || "").includes("pdf")) return new Uint8Array(await r.arrayBuffer());
  const err = await r.json().catch(() => ({}));
  throw new Error(`PDF fetch failed: ${err.error || `HTTP ${r.status}`}`);
}

async function translateBatch(serverBase, target, texts) {
  const chunks = [];
  for (let i = 0; i < texts.length; i += 40) chunks.push(texts.slice(i, i + 40));
  const out = [];
  for (const c of chunks) {
    const r = await fetch(`${serverBase}/api/translate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, texts: c }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(`translate failed: ${err.error || r.status}`);
    }
    const data = await r.json();
    if (!Array.isArray(data.translations) || data.translations.length !== c.length)
      throw new Error("translate response item count mismatch");
    if (data.provider === "mock" || r.headers.get("X-Translator-Mock"))
      log("NOTE: server has no API key set; output is mock translation for layout testing.");
    out.push(...data.translations);
  }
  return out;
}

// Group PDF.js text items into visual lines: same baseline (y within 2pt)
// and sorted left-to-right within the line.
function linesFromItems(items) {
  const rows = new Map();
  for (const it of items) {
    const [, , , , x, y] = it.transform;
    const key = Math.round(y / 2);
    if (!rows.has(key)) rows.set(key, []);
    rows.get(key).push({ it, x, y });
  }
  const lines = [...rows.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([, v]) => {
      v.sort((a, b) => a.x - b.x);
      const text = v.map((o) => o.it.str).join(" ").replace(/\s+/g, " ").trim();
      const xs = v.flatMap((o) => [o.it.transform[4], o.it.transform[4] + o.it.width]);
      const hs = v.map((o) => Math.hypot(o.it.transform[2], o.it.transform[3]) || Math.hypot(o.it.transform[0], o.it.transform[1]) * 0.12);
      return {
        text,
        x0: Math.min(...xs), x1: Math.max(...xs),
        y: v[0].y,
        h: Math.max(...hs, 8),
      };
    })
    .filter((l) => l.text);
  return lines;
}

// Merge consecutive lines into paragraphs on small vertical gaps.
function paragraphsFromLines(lines) {
  const paras = [];
  for (const l of lines) {
    const prev = paras[paras.length - 1];
    const gap = prev ? prev.topY - l.y : Infinity;
    if (prev && gap < l.h * 1.8 && Math.abs(l.x0 - prev.x0) < 36) {
      prev.text += " " + l.text;
      prev.x0 = Math.min(prev.x0, l.x0);
      prev.x1 = Math.max(prev.x1, l.x1);
      prev.bottomY = l.y;
    } else {
      paras.push({ text: l.text, x0: l.x0, x1: l.x1, topY: l.y, bottomY: l.y, h: l.h });
    }
  }
  return paras.filter((p) => /[A-Za-z]{2,}/.test(p.text));
}

// Persian/Arabic needs visual-order presentation forms: pdf-lib embeds no
// shaping engine, so reshape logical text then reverse runs for RTL display.
// Latin/number runs inside the text keep their order per segment.
function shapePersian(logical) {
  const reshaped = window.ArabicReshaper ? window.ArabicReshaper.convertArabic(logical) : logical;
  const segments = reshaped.match(/[\uFE80-\uFEFF\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF ]+|[^\uFE80-\uFEFF\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF ]+/g) || [reshaped];
  return segments.map((s) => (/^[\uFE80-\uFEFF\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF ]+$/.test(s) ? Array.from(s).reverse().join("") : s)).join("");
}

 function wrapText(text, font, size, maxWidth) {
  const words = text.split(/\s+/);
  const out = [];
  let line = "";
  for (const w of words) {
    const trial = line ? line + " " + w : w;
    if (font.widthOfTextAtSize(trial, size) <= maxWidth || !line) line = trial;
    else { out.push(line); line = w; }
  }
  if (line) out.push(line);
  return out;
}

async function loadFont(pdfDoc, urls, label) {
  for (const u of urls) {
    try {
      const r = await fetch(u);
      if (!r.ok) continue;
      const bytes = await r.arrayBuffer();
      return await pdfDoc.embedFont(new Uint8Array(bytes), { subset: true });
    } catch { /* try next */ }
  }
  log(`WARNING: ${label} font download failed; output may show missing glyphs.`);
  return null;
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("log").textContent = "";
  $("download").hidden = true;
  try {
    const pdfUrl = $("pdfUrl").value.trim();
    const target = $("lang").value;
    const serverBase = $("serverUrl").value.trim().replace(/\/$/, "");
    setStatus("Checking server…");
    const health = await fetch(`${serverBase}/api/health`).then((r) => r.json()).catch(() => null);
    if (!health?.ok) throw new Error(`translator server unreachable at ${serverBase} (start: python3 server/server.py)`);
    log(`server provider: ${health.provider}${health.mock ? " (mock — set an API key for real translation)" : ""}`);

    setStatus("Downloading PDF…");
    const bytes = await fetchPdfBytes(pdfUrl, serverBase);
    const head = String.fromCharCode(...bytes.slice(0, 5));
    if (head !== "%PDF-") throw new Error(`downloaded bytes are not a PDF (got ${JSON.stringify(head)}, ${bytes.length} bytes)`);
    const src = await pdfjs.getDocument({ data: bytes.slice() }).promise;
    const pages = parseRange($("pageRange").value, src.numPages);
    log(`translating pages: ${pages.join(", ")} of ${src.numPages}`);

    // Extract paragraphs with geometry.
    const jobs = [];
    for (const n of pages) {
      const page = await src.getPage(n);
      const tc = await page.getTextContent();
      const viewport = page.getViewport({ scale: 1 });
      const lines = linesFromItems(tc.items);
      for (const p of paragraphsFromLines(lines)) {
        jobs.push({ page: n, viewport, ...p });
      }
    }
    if (!jobs.length) throw new Error("no translatable text found in the selected pages");
    setStatus(`Translating ${jobs.length} blocks…`);
    setProgress(0.1);
    const translations = await translateBatch(serverBase, target, jobs.map((j) => j.text));
    jobs.forEach((j, i) => (j.translated = translations[i]));

    // Rebuild: load original bytes so graphics/fonts survive, then cover + redraw.
    setStatus("Rebuilding PDF…");
    const { PDFDocument, rgb } = PDFLib;
    const pdfDoc = await PDFDocument.load(bytes.slice());
    if (window.fontkit) pdfDoc.registerFontkit(window.fontkit);
    const fontUrls = target === "fa" ? FA_FONT_URLS : RU_FONT_URLS;
    const label = target === "fa" ? "Persian" : "Russian";
    const embedded = await loadFont(pdfDoc, fontUrls, label);
    const font = embedded || (await pdfDoc.embedFont("Helvetica"));
    const rtl = target === "fa";

    const byPage = new Map();
    for (const j of jobs) {
      if (!byPage.has(j.page)) byPage.set(j.page, []);
      byPage.get(j.page).push(j);
    }
    for (const [n, blocks] of byPage) {
      const page = pdfDoc.getPage(n - 1);
      const { height } = page.getSize();
      for (const b of blocks) {
        const pad = 2;
        const x = b.x0 - pad, w = b.x1 - b.x0 + pad * 2;
        // PDF.js y is bottom-up at scale 1; pdf-lib uses the same space.
        const yTop = b.topY + b.h, yBottom = b.bottomY - b.h * 0.25;
        const h = Math.max(yTop - yBottom, b.h);
        page.drawRectangle({ x, y: yBottom, width: w, height: h, color: rgb(1, 1, 1), opacity: 1 });
        const drawText = rtl ? shapePersian(b.translated) : b.translated;
        let size = Math.min(b.h * 0.95, 14);
        let fitted = null;
        while (size >= 5) {
          const wrapped = wrapText(drawText, font, size, w - 4);
          if (wrapped.length * size * 1.15 <= h + size * 0.5 || wrapped.length === 1) { fitted = wrapped; break; }
          size -= 0.5;
        }
        fitted = fitted || wrapText(drawText, font, 5, w - 4);
        let ty = yTop - size * 0.85;
        for (const line of fitted.slice(0, Math.max(1, Math.floor(h / (size * 1.15)) + 1))) {
          page.drawText(line, { x: x + 2, y: ty, size, font, color: rgb(0, 0, 0) });
          ty -= size * 1.15;
        }
      }
    }
    const out = await pdfDoc.save();
    const blob = new Blob([out], { type: "application/pdf" });
    const a = $("download");
    a.href = URL.createObjectURL(blob);
    a.download = `translated-${target}.pdf`;
    a.hidden = false;
    const previewDoc = await pdfjs.getDocument({ data: out.slice() }).promise;
    const previewPage = await previewDoc.getPage(pages[0]);
    const vp = previewPage.getViewport({ scale: 1.2 });
    const cv = $("preview");
    cv.hidden = false;
    cv.width = vp.width; cv.height = vp.height;
    await previewPage.render({ canvasContext: cv.getContext("2d"), viewport: vp }).promise;
    setProgress(1);
    setStatus(`Done. ${jobs.length} blocks translated to ${target === "ru" ? "Russian" : "Persian"}.`);
    log(`saved ${a.download} (${(out.length / 1024).toFixed(0)} KB)`);
  } catch (err) {
    setStatus("Failed.");
    log(`ERROR: ${err.message}`);
  }
});
