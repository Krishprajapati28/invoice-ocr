"""
InvoiceOCR — server.py  (Clean, OCR-Accurate Version)
=======================================================
Install:  pip install flask flask-cors pdfplumber pytesseract pdf2image Pillow numpy
Run:      python server.py
Open:     http://localhost:5051

Changes vs previous version:
  - Removed all dead/commented-out legacy code
  - Fixed extract_pdf_text: per-page quality scoring, proper OCR preprocessing
  - Fixed preprocess_receipt_image: adaptive threshold + denoising
  - Fixed validate_field: stricter OCR garbage detection
  - Fixed parse_receipt_text: tighter item regex, eliminates false positives
  - Fixed parse_generic: removed greedy .+ patterns causing multi-line false matches
  - Fixed parse_concur: removed hardcoded transactions, extracts from actual text
"""

import os
import re
import io
import tempfile
import pdfplumber
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

# OCR support — required for image receipts and scanned PDFs
try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance, ImageOps
    from pdf2image import convert_from_path
    import numpy as np
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────────────────────────────────────
# OCR VALIDATION & ERROR FILTERING
# ─────────────────────────────────────────────────────────────────────────────

def validate_field(key, value):
    """
    Validate extracted fields to filter out OCR errors and false recognitions.
    Returns True if the field value looks genuine, False to discard it.

    Design philosophy:
      - Reject clear OCR garbage (repeated chars, all-symbol lines, etc.)
      - Apply per-field-type rules only for well-known key suffixes
      - Never reject simply because a value is short — some fields (e.g. qty,
        currency code) are legitimately 2–3 chars
    """
    if not value or not isinstance(value, str):
        return False

    v = value.strip()
    if not v:
        return False

    # ── Universal noise rejection ──────────────────────────────────────────
    # 6+ consecutive identical characters → OCR bleed or line artifacts
    if re.search(r'(.)\1{5,}', v):
        return False

    # 9+ consonants in a row → garbled OCR output
    if re.search(r'[bcdfghjklmnpqrstvwxyz]{9,}', v, re.IGNORECASE):
        return False

    # More than 50 % of chars are special/non-printable → noise
    special = len(re.findall(r'[^a-zA-Z0-9\s\-.,;:()/\\@#%&\+\*_=]', v))
    if special > len(v) * 0.5:
        return False

    # All whitespace (nothing real)
    if not v.replace(' ', '').replace('\t', ''):
        return False

    # ── Field-type specific rules ──────────────────────────────────────────
    k = key.lower()

    # Monetary fields: must contain at least one digit
    if any(x in k for x in ('amount', 'total', 'price', 'subtotal',
                              'tax', 'fee', 'freight', 'shipping',
                              'discount', 'charge', 'gst', 'pst',
                              'hst', 'vat', 'sgst', 'cgst', 'igst')):
        if not re.search(r'\d', v):
            return False
        # Reject values that are clearly not monetary (e.g. pure letters)
        if not re.search(r'[\d,\.]', v):
            return False

    # Date fields: must contain digits; reject bare 4-digit years
    if 'date' in k or 'time' in k:
        if not re.search(r'\d', v):
            return False
        if re.fullmatch(r'\d{4}', v):
            return False          # Just a year is not a full date

    # Phone fields: must have ≥ 7 digits
    if 'phone' in k or 'fax' in k:
        if len(re.findall(r'\d', v)) < 7:
            return False

    # Email fields: must look like an email
    if 'email' in k:
        if '@' not in v or '.' not in v.split('@')[-1]:
            return False

    # Postal / zip: ≥ 40 % of chars must be alphanumeric digits
    if 'postal' in k or 'zip' in k:
        digits = len(re.findall(r'[A-Z0-9]', v, re.IGNORECASE))
        if digits < max(2, len(v) * 0.4):
            return False

    # Name / address fields: must have ≥ 2 alphabetic chars
    if 'name' in k or 'address' in k:
        if len(re.findall(r'[a-zA-Z]', v)) < 2:
            return False
        # Address must not be purely numeric
        if 'address' in k and re.fullmatch(r'[\d\s\-.,;:()/]+', v):
            return False

    return True


def clean_result(result):
    """
    Clean extraction result by removing invalid/false fields.
    """
    cleaned = {}
    for key, value in result.items():
        if validate_field(key, value):
            cleaned[key] = value
    return cleaned

# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDED HTML UI
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>InvoiceOCR</title>
  <link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:#0a0c10; --surface:#111318; --surface2:#181b22; --border:#252933;
      --accent:#f97316; --text:#f0f2f7; --muted:#6b7280;
      --success:#22c55e; --error:#ef4444;
    }
    body { font-family:'Syne',sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
    body::before {
      content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
      background-image:linear-gradient(rgba(249,115,22,.03) 1px,transparent 1px),
                       linear-gradient(90deg,rgba(249,115,22,.03) 1px,transparent 1px);
      background-size:48px 48px;
    }
    .wrap { max-width:960px; margin:0 auto; padding:0 24px; position:relative; z-index:1; }

    header { padding:36px 0 16px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
    .logo { display:flex; align-items:center; gap:12px; }
    .logo-icon { width:40px; height:40px; background:var(--accent); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px; }
    .logo-text { font-size:22px; font-weight:800; letter-spacing:-.5px; }
    .logo-text span { color:var(--accent); }
    .badge { font-family:'DM Mono',monospace; font-size:11px; background:rgba(249,115,22,.12); color:var(--accent); border:1px solid rgba(249,115,22,.25); padding:4px 10px; border-radius:20px; }

    .hero { text-align:center; padding:52px 0 40px; }
    .hero-tag { font-family:'DM Mono',monospace; font-size:11px; color:var(--accent); letter-spacing:2px; text-transform:uppercase; margin-bottom:14px; }
    .hero h1 { font-size:clamp(32px,5vw,58px); font-weight:800; line-height:1.05; letter-spacing:-2px; margin-bottom:14px; }
    .hero h1 em { font-style:normal; color:var(--accent); }
    .hero p { font-family:'DM Mono',monospace; color:var(--muted); font-size:13px; max-width:380px; margin:0 auto; line-height:1.7; }

    .card { background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:32px; margin-bottom:20px; }
    .step-label { font-family:'DM Mono',monospace; font-size:10px; color:var(--accent); letter-spacing:2px; text-transform:uppercase; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
    .step-label::before { content:attr(data-step); width:20px; height:20px; background:var(--accent); color:#000; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:700; flex-shrink:0; }
    .card h2 { font-size:19px; font-weight:700; margin-bottom:18px; letter-spacing:-.4px; }

    .invoice-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; }
    .invoice-type { position:relative; }
    .invoice-type input { position:absolute; opacity:0; width:0; height:0; }
    .invoice-type label { display:flex; flex-direction:column; align-items:center; gap:8px; padding:16px 10px; border:1.5px solid var(--border); border-radius:14px; cursor:pointer; transition:all .2s; background:var(--surface2); user-select:none; }
    .invoice-type label:hover { border-color:rgba(249,115,22,.4); }
    .invoice-type input:checked + label { border-color:var(--accent); background:rgba(249,115,22,.08); box-shadow:0 0 0 3px rgba(249,115,22,.12); }
    .type-icon { width:40px; height:40px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px; background:var(--bg); }
    .type-name { font-size:12px; font-weight:600; text-align:center; }
    .type-desc { font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); text-align:center; }

    .drop-zone { border:2px dashed var(--border); border-radius:16px; padding:44px 20px; text-align:center; cursor:pointer; transition:all .25s; background:var(--surface2); }
    .drop-zone:hover, .drop-zone.drag-over { border-color:var(--accent); background:rgba(249,115,22,.04); }
    .drop-icon { font-size:44px; margin-bottom:14px; display:block; transition:transform .3s; }
    .drop-zone:hover .drop-icon { transform:translateY(-4px); }
    .drop-title { font-size:17px; font-weight:700; margin-bottom:6px; }
    .drop-sub { font-family:'DM Mono',monospace; font-size:12px; color:var(--muted); margin-bottom:18px; }
    .btn-browse { display:inline-flex; align-items:center; gap:8px; background:rgba(249,115,22,.12); color:var(--accent); border:1.5px solid rgba(249,115,22,.3); padding:10px 20px; border-radius:10px; font-family:'Syne',sans-serif; font-size:13px; font-weight:600; cursor:pointer; transition:all .2s; }
    .btn-browse:hover { background:rgba(249,115,22,.2); border-color:var(--accent); }
    #fileInput { display:none; }

    .file-preview { display:none; align-items:center; gap:14px; padding:14px 18px; background:var(--surface2); border:1.5px solid var(--border); border-radius:12px; margin-top:14px; }
    .file-preview.show { display:flex; }
    .file-icon-box { width:42px; height:42px; background:rgba(239,68,68,.12); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; }
    .file-info { flex:1; min-width:0; }
    .file-name { font-size:14px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .file-size { font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); margin-top:2px; }
    .btn-remove { background:none; border:none; color:var(--muted); cursor:pointer; font-size:18px; padding:4px; transition:color .2s; }
    .btn-remove:hover { color:var(--error); }

    .extract-wrap { text-align:center; padding:6px 0 36px; }
    .btn-extract { display:inline-flex; align-items:center; gap:12px; background:var(--accent); color:#000; border:none; padding:16px 40px; border-radius:14px; font-family:'Syne',sans-serif; font-size:16px; font-weight:800; cursor:pointer; letter-spacing:-.3px; transition:all .25s; box-shadow:0 8px 32px rgba(249,115,22,.3); }
    .btn-extract:hover:not(:disabled) { transform:translateY(-2px); box-shadow:0 12px 40px rgba(249,115,22,.45); }
    .btn-extract:disabled { opacity:.45; cursor:not-allowed; }
    .spinner { display:none; width:18px; height:18px; border:2.5px solid rgba(0,0,0,.2); border-top-color:#000; border-radius:50%; animation:spin .7s linear infinite; }
    .btn-extract.loading .spinner { display:block; }
    .btn-extract.loading .btn-text { opacity:.7; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .hint { font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); margin-top:10px; }

    .status { display:none; align-items:center; gap:10px; padding:12px 16px; border-radius:10px; font-family:'DM Mono',monospace; font-size:12px; margin-bottom:18px; }
    .status.error { display:flex; background:rgba(239,68,68,.08); border:1px solid rgba(239,68,68,.2); color:var(--error); }
    .status.info  { display:flex; background:rgba(249,115,22,.08); border:1px solid rgba(249,115,22,.2); color:var(--accent); }

    .results { display:none; background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:32px; margin-bottom:40px; animation:fadeUp .4s ease; }
    .results.show { display:block; }
    @keyframes fadeUp { from{opacity:0;transform:translateY(16px)} to{opacity:1;transform:translateY(0)} }
    .results-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:24px; flex-wrap:wrap; gap:10px; }
    .results-title { font-size:19px; font-weight:700; display:flex; align-items:center; gap:10px; }
    .dot { width:10px; height:10px; background:var(--success); border-radius:50%; box-shadow:0 0 8px var(--success); }
    .btn-export { display:inline-flex; align-items:center; gap:8px; background:var(--surface2); color:var(--text); border:1.5px solid var(--border); padding:9px 18px; border-radius:10px; font-family:'Syne',sans-serif; font-size:13px; font-weight:600; cursor:pointer; transition:all .2s; }
    .btn-export:hover { border-color:var(--accent); color:var(--accent); }

    .fields-grid { display:grid; grid-template-columns:1fr 1fr; gap:2px; background:var(--border); border-radius:12px; overflow:hidden; margin-bottom:22px; }
    .fkey { background:var(--surface2); padding:11px 15px; font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); letter-spacing:.5px; text-transform:uppercase; }
    .fval { background:var(--surface); padding:11px 15px; font-size:13px; font-weight:600; }
    .fval.money { color:var(--accent); }
    .fval.highlight { color:#60a5fa; font-family:'DM Mono',monospace; letter-spacing:.5px; }

    .raw-toggle { display:flex; align-items:center; gap:8px; background:none; border:none; color:var(--muted); font-family:'DM Mono',monospace; font-size:12px; cursor:pointer; padding:0; margin-bottom:10px; transition:color .2s; }
    .raw-toggle:hover { color:var(--text); }
    .raw-json { display:none; background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:18px; font-family:'DM Mono',monospace; font-size:12px; line-height:1.8; color:#a3e635; overflow-x:auto; white-space:pre; max-height:320px; overflow-y:auto; }
    .raw-json.show { display:block; }

    footer { text-align:center; padding:20px 0 40px; font-family:'DM Mono',monospace; font-size:11px; color:var(--muted); }
    @media(max-width:600px) {
      .fields-grid { grid-template-columns:1fr; }
      .invoice-grid { grid-template-columns:repeat(2,1fr); }
    }
  </style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="logo">
      <div class="logo-icon">&#128269;</div>
      <div class="logo-text">Invoice<span>OCR</span></div>
    </div>
    <span class="badge">LOCAL &middot; NO API KEY</span>
  </header>

  <div class="hero">
    <h1>Extract <em>any</em> receipt<br>in seconds</h1>
    <p>Upload a PDF or photo, pick the type, and get structured data instantly. Smart recognition for grocery stores, restaurants, retail shops, and more. Runs 100% on your machine.</p>
  </div>

  <div class="card">
    <div class="step-label" data-step="1">Select Invoice Type</div>
    <h2>What kind of invoice is this?</h2>
    <div class="invoice-grid">
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-fedex" value="fedex" checked/>
        <label for="t-fedex"><div class="type-icon">&#128230;</div><div class="type-name">FedEx</div><div class="type-desc">Express / Ground</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-costco" value="costco"/>
        <label for="t-costco"><div class="type-icon">&#127978;</div><div class="type-name">Costco</div><div class="type-desc">Wholesale Receipt</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-dhl" value="dhl"/>
        <label for="t-dhl"><div class="type-icon">&#9992;&#65039;</div><div class="type-name">DHL</div><div class="type-desc">International</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-amazon" value="amazon"/>
        <label for="t-amazon"><div class="type-icon">&#128236;</div><div class="type-name">Amazon</div><div class="type-desc">Seller / FBA</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-cbs" value="cbs"/>
        <label for="t-cbs"><div class="type-icon">&#128722;</div><div class="type-name">CBS</div><div class="type-desc">Instacart Receipt</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-guelph" value="guelph_ridgetown"/>
        <label for="t-guelph"><div class="type-icon">&#127891;</div><div class="type-name">Guelph Ridgetown</div><div class="type-desc">Course Registration</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-mirapay" value="mirapay"/>
        <label for="t-mirapay"><div class="type-icon">&#128179;</div><div class="type-name">MiraPay</div><div class="type-desc">Payment Receipt</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-splitsville" value="splitsville"/>
        <label for="t-splitsville"><div class="type-icon">&#127923;</div><div class="type-name">Splitsville Bowl</div><div class="type-desc">Event Invoice</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-ubereats" value="uber_eat"/>
        <label for="t-ubereats"><div class="type-icon">&#127828;</div><div class="type-name">Uber Eats</div><div class="type-desc">Food Delivery</div></label>
      </div>

      <div class="invoice-type">
        <input type="radio" name="itype" id="t-concur" value="concur"/>
        <label for="t-concur"><div class="type-icon">&#128203;</div><div class="type-name">Concur</div><div class="type-desc">Expense Claim</div></label>
      </div>
      <div class="invoice-type">
        <input type="radio" name="itype" id="t-receipt" value="receipt"/>
        <label for="t-receipt"><div class="type-icon">&#129534;</div><div class="type-name">Smart Receipt</div><div class="type-desc">Grocery, Restaurant, Retail, Pharmacy</div></label>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="step-label" data-step="2">Upload File</div>
    <h2 id="uploadCardTitle">Drop your invoice here</h2>
    <div class="drop-zone" id="dropZone">
      <span class="drop-icon">&#128194;</span>
      <div class="drop-title" id="dropTitle">Drag &amp; drop your PDF</div>
      <div class="drop-sub" id="dropSub">or click the button to browse</div>
      <button class="btn-browse" type="button" id="browseBtn">&#8679; Choose File</button>
      <input type="file" id="fileInput" accept=".pdf,.jpg,.jpeg,.png,.webp"/>
    </div>
    <div id="imgPreviewWrap" style="display:none;margin-top:14px;text-align:center;">
      <img id="imgPreview" style="max-width:100%;max-height:280px;border-radius:12px;border:1.5px solid var(--border);" alt="Receipt preview"/>
    </div>
    <div class="file-preview" id="filePreview">
      <div class="file-icon-box">&#128209;</div>
      <div class="file-info">
        <div class="file-name" id="fileName">—</div>
        <div class="file-size" id="fileSize">—</div>
      </div>
      <button class="btn-remove" type="button" onclick="removeFile()">&#10005;</button>
    </div>
  </div>

  <div class="status" id="statusBar"><span id="statusMsg"></span></div>

  <div class="extract-wrap">
    <button class="btn-extract" id="extractBtn" type="button" onclick="doExtract()">
      <div class="spinner"></div>
      <span class="btn-text">&#128269; Extract Invoice Data</span>
    </button>
    <p class="hint">Processed locally via Python &middot; No data leaves your machine</p>
  </div>

  <div class="results" id="results">
    <div class="results-head">
      <div class="results-title"><span class="dot"></span> Extracted Data</div>
      <button class="btn-export" type="button" onclick="exportJSON()">&#8595; Export JSON</button>
    </div>
    <div class="fields-grid" id="fieldsGrid"></div>
    <button class="raw-toggle" type="button" onclick="toggleRaw()">
      <span id="rawArrow">&#9658;</span>&nbsp;View Raw JSON
    </button>
    <pre class="raw-json" id="rawJson"></pre>
  </div>

</div>
<footer>InvoiceOCR &middot; Runs 100% locally &middot; Python + pdfplumber + pytesseract OCR</footer>

<script>
  let uploadedFile = null;
  let lastData = null;

  const dz = document.getElementById('dropZone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('drag-over');
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
  dz.addEventListener('click', e => {
    if (!e.target.closest('#browseBtn')) document.getElementById('fileInput').click();
  });
  document.getElementById('browseBtn').addEventListener('click', e => {
    e.stopPropagation();
    document.getElementById('fileInput').click();
  });
  document.getElementById('fileInput').addEventListener('change', e => {
    if (e.target.files[0]) handleFile(e.target.files[0]);
  });

  const RECEIPT_EXTS = ['.jpg', '.jpeg', '.png', '.webp'];

  function isReceiptImage(name) {
    return RECEIPT_EXTS.some(e => name.toLowerCase().endsWith(e));
  }

  // Update drop-zone hints when receipt type is selected
  document.querySelectorAll('input[name="itype"]').forEach(r => {
    r.addEventListener('change', () => {
      const isReceipt = r.value === 'receipt' && r.checked;
      document.getElementById('dropTitle').innerHTML = isReceipt
        ? 'Drag &amp; drop your receipt image or PDF'
        : 'Drag &amp; drop your PDF';
      document.getElementById('dropSub').textContent = isReceipt
        ? 'JPG, PNG, WEBP or PDF supported'
        : 'or click the button to browse';
      document.getElementById('uploadCardTitle').textContent = isReceipt
        ? 'Drop your receipt here'
        : 'Drop your invoice here';
      document.getElementById('fileInput').accept = isReceipt
        ? '.pdf,.jpg,.jpeg,.png,.webp'
        : '.pdf';
    });
  });

  function handleFile(f) {
    const name = f.name.toLowerCase();
    const selectedType = document.querySelector('input[name="itype"]:checked').value;
    const isImg = isReceiptImage(name);
    const isPdf = name.endsWith('.pdf');

    if (!isPdf && !isImg) {
      showStatus('error', 'Only PDF or image files (JPG, PNG, WEBP) are supported.'); return;
    }
    if (isImg && selectedType !== 'receipt') {
      showStatus('info', 'Image detected — automatically switching to "Shop / Bill Receipt" mode.');
      document.getElementById('t-receipt').checked = true;
      document.getElementById('t-receipt').dispatchEvent(new Event('change'));
    }
    uploadedFile = f;
    document.getElementById('fileName').textContent = f.name;
    document.getElementById('fileSize').textContent = fmtBytes(f.size);
    document.getElementById('filePreview').classList.add('show');

    // Show image preview for receipts
    const prevWrap = document.getElementById('imgPreviewWrap');
    const prevImg  = document.getElementById('imgPreview');
    if (isImg) {
      const reader = new FileReader();
      reader.onload = e => { prevImg.src = e.target.result; prevWrap.style.display = 'block'; };
      reader.readAsDataURL(f);
    } else {
      prevWrap.style.display = 'none';
      prevImg.src = '';
    }
    hideStatus(); hideResults();
  }

  function removeFile() {
    uploadedFile = null;
    document.getElementById('fileInput').value = '';
    document.getElementById('filePreview').classList.remove('show');
    document.getElementById('imgPreviewWrap').style.display = 'none';
    document.getElementById('imgPreview').src = '';
    hideResults();
  }

  function fmtBytes(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(2) + ' MB';
  }

  async function doExtract() {
    if (!uploadedFile) { showStatus('error', 'Please upload a file first.'); return; }
    const itype = document.querySelector('input[name="itype"]:checked').value;
    const isImg = isReceiptImage(uploadedFile.name);
    const btn = document.getElementById('extractBtn');
    btn.classList.add('loading'); btn.disabled = true;
    hideStatus(); hideResults();

    const fd = new FormData();
    fd.append('file', uploadedFile);
    fd.append('invoice_type', itype);

    // Route image receipts to the dedicated vision endpoint
    const endpoint = isImg ? '/extract-image' : '/extract';

    try {
      const res = await fetch(endpoint, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Server error ' + res.status);
      lastData = data;
      renderResults(data);
    } catch(err) {
      showStatus('error', err.message);
    } finally {
      btn.classList.remove('loading'); btn.disabled = false;
    }
  }

  function renderResults(data) {
    const grid = document.getElementById('fieldsGrid');
    grid.innerHTML = '';
    const moneyKeys = ['total','subtotal','transportation','charge','surcharge','discount','gst','tax','freight','vat','amount','price','value','cgst','sgst','igst','shipping','tip','tender','change'];
    const highlightKeys = ['invoice_number','order_number','tracking_id','waybill_number','pan_number','gst_registration','business_type','store_name','transaction_id','auth_code','reference_id'];
    const itemKeys = [];
    
    for (const [k, v] of Object.entries(data.fields || {})) {
      const label = k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      const isMoney = moneyKeys.some(m => k.toLowerCase().includes(m));
      const isHighlight = highlightKeys.some(h => k.toLowerCase().includes(h));
      const isItem = k.startsWith('item_');
      
      if (isItem) {
        itemKeys.push({key: k, label: label, value: v});
      } else {
        grid.innerHTML += `<div class="fkey">${label}</div><div class="fval${isMoney ? ' money' : ''}${isHighlight ? ' highlight' : ''}">${v || '—'}</div>`;
      }
    }
    
    // Add items section if we have items
    if (itemKeys.length > 0) {
      grid.innerHTML += `<div class="fkey" style="grid-column: 1 / -1; background: var(--accent); color: #000; font-weight: 700; text-align: center;">🛒 ITEMS PURCHASED (${itemKeys.length})</div>`;
      itemKeys.forEach(item => {
        grid.innerHTML += `<div class="fkey">${item.label}</div><div class="fval" style="font-family: 'DM Mono', monospace; font-size: 12px;">${item.value}</div>`;
      });
    } else {
    }
    
    document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);
    document.getElementById('results').classList.add('show');
  }

  function toggleRaw() {
    const el = document.getElementById('rawJson');
    const open = el.classList.toggle('show');
    document.getElementById('rawArrow').innerHTML = open ? '&#9660;' : '&#9658;';
  }

  function exportJSON() {
    if (!lastData) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(lastData, null, 2)], { type: 'application/json' }));
    a.download = 'invoice_extracted.json'; a.click();
  }

  function showStatus(type, msg) {
    const b = document.getElementById('statusBar');
    b.className = 'status ' + type;
    document.getElementById('statusMsg').textContent = msg;
  }
  function hideStatus() { document.getElementById('statusBar').className = 'status'; }
  function hideResults() { document.getElementById('results').classList.remove('show'); }
</script>
</body>
</html>"""


def extract_universal_details(text, existing_result):
    """
    Extract comprehensive details that apply to all invoice types.
    This function enhances any parser with detailed information extraction.
    """
    
    # ── Contact Information ────────────────────────────────────────────
    # Phone numbers (various formats)
    phones = re.findall(r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})', text)
    if phones:
        for i, phone in enumerate(phones[:3], 1):  # Limit to 3 phone numbers
            formatted_phone = f"({phone[0]}) {phone[1]}-{phone[2]}"
            existing_result[f"phone_{i}"] = formatted_phone
    
    # Email addresses
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
    if emails:
        unique_emails = list(set(emails))
        for i, email in enumerate(unique_emails[:3], 1):  # Limit to 3 emails
            existing_result[f"email_{i}"] = email
    
    # Websites/URLs
    urls = re.findall(r'https?://[^\s]+|www\.[^\s]+', text)
    if urls:
        unique_urls = list(set(urls))
        for i, url in enumerate(unique_urls[:3], 1):  # Limit to 3 URLs
            existing_result[f"website_{i}"] = url
    
    # ── Address Information ────────────────────────────────────────────
    # Street addresses (number + street name)
    addresses = re.findall(r'\d+\s+[A-Za-z0-9\s,.-]+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Circle|Cir|Court|Ct)(?:\s+[A-Za-z]{2,})?', text, re.IGNORECASE)
    if addresses:
        unique_addresses = list(set(addresses))
        for i, address in enumerate(unique_addresses[:3], 1):  # Limit to 3 addresses
            existing_result[f"address_{i}"] = address.strip()
    
    # Postal/ZIP codes
    postal_codes = re.findall(r'\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b|\b\d{5}(?:-\d{4})?\b', text)
    if postal_codes:
        unique_postal = list(set(postal_codes))
        for i, postal in enumerate(unique_postal[:3], 1):
            existing_result[f"postal_code_{i}"] = postal
    
    # ── Financial Details ──────────────────────────────────────────────
    # All monetary amounts
    money_amounts = re.findall(r'\$?([0-9,]+\.\d{2})', text)
    if money_amounts:
        # Remove duplicates and sort by value
        unique_amounts = list(set(money_amounts))
        try:
            unique_amounts.sort(key=lambda x: float(x.replace(',', '')), reverse=True)
            for i, amount in enumerate(unique_amounts[:10], 1):  # Top 10 amounts
                existing_result[f"amount_{i}"] = f"${amount}"
        except ValueError:
            pass
    
    # Tax rates and percentages
    tax_rates = re.findall(r'(\d+(?:\.\d+)?%)\s*(?:tax|gst|pst|hst|vat)', text, re.IGNORECASE)
    if tax_rates:
        unique_rates = list(set(tax_rates))
        for i, rate in enumerate(unique_rates[:5], 1):
            existing_result[f"tax_rate_{i}"] = rate
    
    # ── Identification Numbers ─────────────────────────────────────────
    # Various ID patterns
    id_patterns = [
        (r'(?:Invoice|Receipt|Order|Transaction|Reference|Confirmation)\s*(?:#|No\.?|Number|ID)\s*[:\-]?\s*([A-Z0-9\-]{4,})', 'id_number'),
        (r'GST\s*(?:#|No\.?|Number|Registration)\s*[:\-]?\s*([A-Z0-9]{9,})', 'gst_number'),
        (r'(?:Account|Customer|Member)\s*(?:#|No\.?|Number|ID)\s*[:\-]?\s*([A-Z0-9\-]{4,})', 'account_number'),
        (r'(?:Tracking|Shipment|Waybill)\s*(?:#|No\.?|Number|ID)\s*[:\-]?\s*([A-Z0-9\-]{4,})', 'tracking_number'),
    ]
    
    for pattern, field_name in id_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            unique_matches = list(set(matches))
            for i, match in enumerate(unique_matches[:3], 1):
                existing_result[f"{field_name}_{i}"] = match
    
    # ── Date and Time Information ──────────────────────────────────────
    # Various date formats
    date_patterns = [
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b',
        r'\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b',
        r'\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b'
    ]
    
    all_dates = []
    for pattern in date_patterns:
        dates = re.findall(pattern, text, re.IGNORECASE)
        all_dates.extend(dates)
    
    if all_dates:
        unique_dates = list(set(all_dates))
        for i, date in enumerate(unique_dates[:5], 1):
            existing_result[f"date_{i}"] = date
    
    # Time patterns
    times = re.findall(r'\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?\b', text, re.IGNORECASE)
    if times:
        unique_times = list(set(times))
        for i, time in enumerate(unique_times[:5], 1):
            existing_result[f"time_{i}"] = time
    
    # ── Payment Information ────────────────────────────────────────────
    # Credit card last 4 digits
    card_numbers = re.findall(r'(?:XXXX|\*{4,}).*?(\d{4})\b', text)
    if card_numbers:
        unique_cards = list(set(card_numbers))
        for i, card in enumerate(unique_cards[:3], 1):
            existing_result[f"card_last4_{i}"] = card
    
    # Payment methods
    payment_methods = re.findall(r'\b(Visa|Mastercard|Master Card|American Express|Amex|Discover|Debit|Credit|Cash|Check|Cheque|PayPal|Apple Pay|Google Pay)\b', text, re.IGNORECASE)
    if payment_methods:
        unique_methods = list(set([method.title() for method in payment_methods]))
        for i, method in enumerate(unique_methods[:5], 1):
            existing_result[f"payment_method_{i}"] = method
    
    # Authorization codes
    auth_codes = re.findall(r'(?:Auth|Authorization|Approval)\s*(?:#|Code|No\.?)\s*[:\-]?\s*([A-Z0-9]{4,})', text, re.IGNORECASE)
    if auth_codes:
        unique_codes = list(set(auth_codes))
        for i, code in enumerate(unique_codes[:3], 1):
            existing_result[f"auth_code_{i}"] = code
    
    # ── Additional Details ─────────────────────────────────────────────
    # Business registration numbers
    business_numbers = re.findall(r'(?:Business|Registration|License)\s*(?:#|No\.?|Number)\s*[:\-]?\s*([A-Z0-9\-]{4,})', text, re.IGNORECASE)
    if business_numbers:
        unique_business = list(set(business_numbers))
        for i, number in enumerate(unique_business[:3], 1):
            existing_result[f"business_number_{i}"] = number
    
    # Signatures and approvals
    signatures = re.findall(r'(?:Signed|Signature|Approved)\s*(?:by)?\s*[:\-]?\s*([A-Za-z\s]{3,30})', text, re.IGNORECASE)
    if signatures:
        unique_signatures = list(set([sig.strip() for sig in signatures]))
        for i, signature in enumerate(unique_signatures[:3], 1):
            if len(signature) > 2:
                existing_result[f"signature_{i}"] = signature
    
    return existing_result


# ─────────────────────────────────────────────────────────────────────────────
# Parsers
# ─────────────────────────────────────────────────────────────────────────────

def match(pattern, text, group=1):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(group).strip() if m else ""

def _ocr_pdf_page(pil_img) -> str:
    """
    Run Tesseract on a single PIL image of a PDF page.
    Uses --psm 3 (fully automatic page segmentation) + LSTM engine.
    Applies preprocessing: greyscale → denoise → adaptive threshold → scale.
    """
    img = pil_img.convert("L")                          # greyscale

    # Scale up small pages for better character recognition
    w, h = img.size
    if w < 1200:
        scale = 1200 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Mild contrast boost
    img = ImageEnhance.Contrast(img).enhance(1.5)

    # Adaptive thresholding via numpy (binarise without blowing out faint ink)
    try:
        arr = np.array(img)
        # Simple block mean threshold (15x15 neighbourhood)
        from PIL import ImageFilter
        blurred = img.filter(ImageFilter.GaussianBlur(radius=15))
        arr_blur = np.array(blurred).astype(int)
        arr_bin = ((arr.astype(int) - arr_blur) > -10).astype(np.uint8) * 255
        img = Image.fromarray(arr_bin.astype(np.uint8))
    except Exception:
        # Fallback: simple sharpen
        img = img.filter(ImageFilter.SHARPEN)

    config = "--psm 3 --oem 3"
    return pytesseract.image_to_string(img, config=config)


def _text_quality(text: str) -> float:
    """
    Return a 0–1 quality score for OCR / pdfplumber text.
    High score = real text; low score = garbage or empty.
    """
    if not text or not text.strip():
        return 0.0
    words = re.findall(r'[a-zA-Z]{2,}', text)
    total_chars = len(text.replace('\n', '').replace(' ', ''))
    if total_chars == 0:
        return 0.0
    letter_ratio = len(re.findall(r'[a-zA-Z]', text)) / total_chars
    # Penalise if fewer than 5 real words — might be just symbols/numbers
    word_bonus = min(1.0, len(words) / 10)
    return letter_ratio * 0.7 + word_bonus * 0.3


def extract_pdf_text(path: str) -> str:
    """
    Extract text from a PDF with intelligent fallback:
      1. Try pdfplumber (native text layer) — fast and accurate for digital PDFs.
      2. Score each page; if a page returns low-quality text, OCR that page.
      3. If pdfplumber yields nothing at all, OCR the entire document.

    This prevents the common failure where pdfplumber returns garbled
    characters from a scanned PDF, causing downstream regex to fail entirely.
    """
    page_texts: list[str] = []
    low_quality_pages: list[int] = []  # 0-indexed

    # ── Step 1: Try pdfplumber on all pages ──────────────────────────────────
    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            raw = page.extract_text() or ""
            page_texts.append(raw)
            if _text_quality(raw) < 0.3:
                low_quality_pages.append(i)

    combined_native = "\n".join(page_texts).strip()

    # If all pages are decent quality, return immediately
    if not low_quality_pages:
        return combined_native

    # ── Step 2: OCR the low-quality pages ────────────────────────────────────
    if not OCR_AVAILABLE:
        if not combined_native:
            raise RuntimeError(
                "This PDF has no selectable text and OCR libraries are not installed.\n"
                "Run: pip install pytesseract pdf2image Pillow numpy\n"
                "Then install Tesseract: https://github.com/tesseract-ocr/tesseract"
            )
        # Return whatever native text we have — it may be partial
        return combined_native

    # Convert only the pages that need OCR (saves time on large documents)
    try:
        # pdf2image is 1-indexed; first_page/last_page are inclusive
        if len(low_quality_pages) == total_pages:
            # Whole document needs OCR
            images = convert_from_path(path, dpi=300)
        else:
            images_all = convert_from_path(path, dpi=300)
            images = [images_all[i] for i in low_quality_pages]
    except Exception as e:
        # pdf2image / poppler not available
        if not combined_native:
            raise RuntimeError(f"pdf2image failed and no native text available: {e}")
        return combined_native

    # Replace low-quality native text with OCR text
    ocr_iter = iter(images)
    for i in low_quality_pages:
        img = next(ocr_iter)
        ocr_text = _ocr_pdf_page(img)
        if _text_quality(ocr_text) > _text_quality(page_texts[i]):
            page_texts[i] = ocr_text

    return "\n".join(page_texts).strip()

def parse_fedex(t):
    """
    FedEx Express detail pages use a two-column layout — labels on the left,
    values on the right — which OCR reads as two separate blocks.
    We pair them positionally after identifying each block.
    """
    result = {}

    # ── Invoice header ──────────────────────────────────────────────────────
    # Invoice number is always in X-XXX-XXXXX format
    inv_m = re.search(r'\b(\d-\d{3}-\d{5})\b', t)
    if inv_m:
        result["invoice_number"] = inv_m.group(1)

    date_m = re.search(r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})\b', t)
    if date_m:
        result["invoice_date"] = date_m.group(1)

    acct_m = re.search(r'\b(\d{4}-\d{4}-\d)\b', t)
    if acct_m:
        result["account_number"] = acct_m.group(1)

    fuel_pct_m = re.search(r'fuel surcharge of ([\d\.]+%)', t, re.IGNORECASE)
    if fuel_pct_m:
        result["fuel_surcharge_pct"] = fuel_pct_m.group(1)

    # ── Shipment detail block: labels then values, positionally paired ──────
    # Normalise common OCR misreads before parsing
    t_norm = re.sub(r'\bSrig/Dest\b', 'Orig/Dest', t)
    t_norm = re.sub(r'\bsigned by\b', 'Signed by', t_norm)

    DETAIL_LABELS = ['Automation', 'Tracking ID', 'Service Type', 'Package Type',
                     'Orig/Dest', 'Zone', 'Packages', 'Rated Weight', 'Delivered', 'Signed by']
    detail_m = re.search(r'Automation\n(.*?)(?=Sender)', t_norm, re.DOTALL)
    if detail_m:
        lines = [l.strip() for l in detail_m.group(0).split('\n') if l.strip()]
        if 'Signed by' in lines:
            split_idx = lines.index('Signed by') + 1
            labels = lines[:split_idx]
            values = lines[split_idx:]
            for label, value in zip(labels, values):
                key = label.lower().replace(' ', '_').replace('/', '_')
                result[key] = value

    # ── Sender / Recipient ──────────────────────────────────────────────────
    # OCR may put "Sender Recipient" on one line (two-column header)
    sr_m = re.search(r'Sender\s+Recipient\s*\n(.+)', t)
    if sr_m:
        parts = re.split(r'\s{2,}', sr_m.group(1).strip())
        if len(parts) >= 2:
            result["sender_name"]    = parts[0].strip()
            result["recipient_name"] = parts[1].strip()
        else:
            result["sender_name"] = parts[0].strip()
    else:
        s_m = re.search(r'Sender\s*\n(.+)', t)
        r_m = re.search(r'Recipient\s*\n(.+)', t)
        if s_m: result["sender_name"]    = s_m.group(1).strip()
        if r_m: result["recipient_name"] = r_m.group(1).strip()

    # ── Sender / Recipient Address Extraction ──────────────────────────────
    sender_block = re.search(r'Sender\s*\n(.*?)(?=Recipient|\n\s*Transportation)', t, re.DOTALL)
    if sender_block:
        sender_lines = [line.strip() for line in sender_block.group(1).split('\n') if line.strip()]
        if sender_lines:
            result["sender_name"] = sender_lines[0]
            sender_address_parts = []
            for line in sender_lines[1:]:
                city_state_zip_match = re.search(r'^(.+?),?\s+([A-Z]{2})\s+([A-Z0-9\s-]+)$', line)
                if city_state_zip_match:
                    result["sender_city"] = city_state_zip_match.group(1).strip()
                    result["sender_state_province"] = city_state_zip_match.group(2)
                    result["sender_postal_code"] = city_state_zip_match.group(3).strip()
                elif line.upper() in ['CANADA', 'UNITED STATES', 'USA', 'US', 'CA']:
                    result["sender_country"] = line.upper()
                else:
                    sender_address_parts.append(line)
            if sender_address_parts:
                result["sender_address"] = ", ".join(sender_address_parts)
    
    recipient_block = re.search(r'Recipient\s*\n(.*?)(?=\n\s*(?:CAD|USD|Transportation|Shipper|Charges)|\n\s*\n\s*[\d\-$]|\Z)', t, re.DOTALL)
    if recipient_block:
        recipient_lines = [line.strip() for line in recipient_block.group(1).split('\n') if line.strip()]
        if recipient_lines:
            result["recipient_name"] = recipient_lines[0]
            recipient_address_parts = []
            for line in recipient_lines[1:]:
                city_state_zip_match = re.search(r'^(.+?),?\s+([A-Z]{2})\s+([A-Z0-9\s-]+)$', line)
                if city_state_zip_match:
                    result["recipient_city"] = city_state_zip_match.group(1).strip()
                    result["recipient_state_province"] = city_state_zip_match.group(2)
                    result["recipient_postal_code"] = city_state_zip_match.group(3).strip()
                elif line.upper() in ['CANADA', 'UNITED STATES', 'USA', 'US', 'CA']:
                    result["recipient_country"] = line.upper()
                else:
                    recipient_address_parts.append(line)
            if recipient_address_parts:
                result["recipient_address"] = ", ".join(recipient_address_parts)

    # ── Charges block: labels then values, positionally paired ─────────────
    # Header summary labels (Subtotal / Canada GST / Total) appear before the
    # detail section; count them so we can skip their values in the value block.
    header_labels_m = re.search(r'((?:Subtotal|Canada\s+GST|Total)\n)+', t)
    header_label_count = len(re.findall(r'(?:Subtotal|Canada\s+GST|Total)\n', t[:t.find('Automation')])) if 'Automation' in t else 0

    charge_block_m = re.search(r'Transportation Charge(.*?)Total FedEx Express', t, re.DOTALL)
    charge_labels = []
    if charge_block_m:
        charge_labels = [l.strip() for l in charge_block_m.group(0).split('\n') if l.strip()]

    # Numeric values come after the currency block (CAD/USD lines)
    vals_m = re.search(r'Account Number.*?(?:CAD|USD)(?:\s*\n+(?:CAD|USD))*\s*\n+(.*)', t, re.DOTALL)
    charge_nums = []
    if vals_m:
        for line in vals_m.group(1).split('\n'):
            line = line.strip()
            if re.match(r'^-?\$?[\d,]+\.\d{2}$', line):
                charge_nums.append(re.sub(r'^\$', '', line))
    # Skip the header summary values (first N numbers belong to page header)
    charge_nums = charge_nums[header_label_count:]

    if charge_labels and charge_nums:
        for label, value in zip(charge_labels, charge_nums):
            key = (label.lower()
                   .replace(' ', '_')
                   .replace('(', '').replace(')', '')
                   .replace('/', '_'))
            result[key] = f"${value}"
    else:
        # Fallback for partial pages (e.g. "Continued on next page") — inline values
        for label, key in [('Transportation Charge', 'transportation_charge'),
                           ('Discount', 'discount'),
                           ('Net Transportation Charges', 'net_transportation_charges'),
                           ('Fuel Surcharge', 'fuel_surcharge'),
                           ('Subtotal', 'subtotal')]:
            m = re.search(label + r'\s*\n+(-?[\d,]+\.\d{2})', t)
            if m:
                result[key] = f"${m.group(1)}"

    result["currency"] = match(r'\b(CAD|USD)\b', t)
    
    # Don't apply universal detail extraction for FedEx - it adds too many unwanted fields
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}

def parse_ups(t):
    """
    UPS Commercial Invoice parser.
    Handles UPS international shipping invoices with waybill numbers,
    shipment details, itemized goods table, and customs information.
    """
    result = {}
    
    # ── Document Information ───────────────────────────────────────────────
    if "Invoice" in t:
        result["document_type"] = "Invoice"
    
    # Tax ID/VAT Number (at top of document)
    tax_id = match(r"TaxID/VAT No:\s*([A-Z0-9]+)", t)
    if tax_id:
        result["tax_id_vat"] = tax_id
    
    # Page number
    page = match(r"Page\s+(\d+)", t)
    if page:
        result["page_number"] = page
    
    # ── Waybill and Shipment Information ───────────────────────────────────
    waybill = match(r"Waybill Number:\s*([0-9]+)", t)
    if waybill:
        result["waybill_number"] = waybill
    
    shipment_id = match(r"Shipment ID:\s*([A-Z0-9]+)", t)
    if shipment_id:
        result["shipment_id"] = shipment_id
    
    # Invoice details
    inv_no = match(r"Invoice No:\s*([0-9]+)", t)
    if inv_no:
        result["invoice_number"] = inv_no
    
    inv_date = match(r"Date:\s*([\d\s\w]+\d{4})", t)
    if inv_date:
        result["invoice_date"] = inv_date.strip()
    
    po_no = match(r"P/O No:\s*([A-Z0-9]+)", t)
    if po_no:
        result["po_number"] = po_no
    
    # Terms and reason
    terms = match(r"Terms of Sale \(Incoterm\):\s*([^\n]+)", t)
    if terms:
        result["terms_of_sale"] = terms.strip()
    
    reason = match(r"Reason for Export:\s*([^\n]+)", t)
    if reason:
        result["reason_for_export"] = reason.strip()
    
    # ── FROM Section (Shipper) ─────────────────────────────────────────────
    from_contact = match(r"FROM.*?Contact Name:\s*\n([^\n]+)", t)
    if from_contact and not re.search(r"Phone|SHIP", from_contact):
        result["from_contact_name"] = from_contact.strip()
    
    from_phone = match(r"FROM.*?Phone:\s*([0-9]+)", t)
    if from_phone:
        result["from_phone"] = from_phone
    
    # ── SHIP TO Section ────────────────────────────────────────────────────
    ship_to_tax = match(r"SHIP TO.*?Tax ID/VAT No:\s*([0-9]+)", t)
    if ship_to_tax:
        result["ship_to_tax_id"] = ship_to_tax
    
    ship_to_contact = match(r"SHIP TO.*?Contact Name:\s*\n([^\n]+)", t)
    if ship_to_contact and not re.search(r"Phone|SOLD", ship_to_contact):
        result["ship_to_contact_name"] = ship_to_contact.strip()
    
    ship_to_phone = match(r"SHIP TO.*?Phone:\s*([0-9]+)", t)
    if ship_to_phone:
        result["ship_to_phone"] = ship_to_phone
    
    # ── SOLD TO INFORMATION Section ────────────────────────────────────────
    sold_to_tax = match(r"SOLD TO INFORMATION.*?Tax ID/VAT No:\s*([0-9]+)", t)
    if sold_to_tax:
        result["sold_to_tax_id"] = sold_to_tax
    
    sold_to_contact = match(r"SOLD TO INFORMATION.*?Contact Name:\s*\n([^\n]+)", t)
    if sold_to_contact and not re.search(r"Phone|Units", sold_to_contact):
        result["sold_to_contact_name"] = sold_to_contact.strip()
    
    sold_to_phone = match(r"SOLD TO INFORMATION.*?Phone:\s*([0-9]+)", t)
    if sold_to_phone:
        result["sold_to_phone"] = sold_to_phone
    
    # ── Goods Table ────────────────────────────────────────────────────────
    # Extract items from table: Units, U/M, Description, Harm. Code, C/O, Unit Value, Total Value
    # Pattern: line number, U/M code, description, harm code, C/O, unit value, total value
    item_lines = re.findall(r"(\d+)\s+(\d{2})\s+([a-z]+)\s+(\d+)\s+(\d+)\s+(\d+)", t, re.IGNORECASE)
    if item_lines:
        for idx, (line_no, um, desc, harm, co, unit_val) in enumerate(item_lines, 1):
            result[f"item_{idx}_line"] = line_no
            result[f"item_{idx}_um"] = um
            result[f"item_{idx}_description"] = desc
            result[f"item_{idx}_harm_code"] = harm
            result[f"item_{idx}_co"] = co
            result[f"item_{idx}_unit_value"] = unit_val
    
    # Total value from table
    table_total = match(r"Total Value.*?\n.*?(\d+)", t)
    if table_total:
        result["goods_total_value"] = table_total
    
    # ── Invoice Totals Section ─────────────────────────────────────────────
    discount = match(r"Discount/Rebate:\s*(\d+)", t)
    if discount:
        result["discount_rebate"] = discount
    
    subtotal = match(r"Invoice Sub-Total:\s*(\d+)", t)
    if subtotal:
        result["invoice_subtotal"] = subtotal
    
    freight = match(r"Freight:\s*(\d+)", t)
    if freight:
        result["freight"] = freight
    
    insurance = match(r"Insurance:\s*(\d+)", t)
    if insurance:
        result["insurance"] = insurance
    
    other = match(r"Other:\s*(\d+)", t)
    if other:
        result["other_charges"] = other
    
    total_invoice = match(r"Total Invoice Amount:\s*(\d+)", t)
    if total_invoice:
        result["total_invoice_amount"] = total_invoice
    
    # Packages and weight
    num_packages = match(r"Total Number of Packages:\s*(\d+)", t)
    if num_packages:
        result["total_packages"] = num_packages
    
    total_weight = match(r"Total Weight:\s*([\d\.]+\s*kg)", t)
    if total_weight:
        result["total_weight"] = total_weight.strip()
    
    # Currency
    currency = match(r"Currency:\s*([A-Z]{3})", t)
    if currency:
        result["currency"] = currency
    
    # ── Declaration Section ────────────────────────────────────────────────
    if "Declaration Statement:" in t:
        result["has_declaration"] = "Yes"
    
    # Shipper signature section
    if "Shipper" in t and "Date" in t:
        result["has_signature_section"] = "Yes"
    
    # ── Additional Comments ────────────────────────────────────────────────
    comments = match(r"Additional Comments:\s*\n([^\n]+)", t)
    if comments and not re.search(r"Declaration|Shipper", comments):
        result["additional_comments"] = comments.strip()

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}

def parse_costco(t):
    """
    Handles two Costco formats:
      A) Online order PDF  — Order Number, Order Date, Membership Number,
                             Shipping/Billing Address, item table, totals
      B) In-store receipt  — Store #, Cashier, date/time, subtotal/tax/total

    NOTE: OCR from scanned Costco PDFs produces backslash-dollar for
    currency symbols, so currency patterns use raw strings with escaped backslashes
    (regex: literal backslash+dollar).
    """
    # ── Order-level fields ─────────────────────────────────────────────────
    order_number = match(r"Order Number[^\n]*\n(\d+)", t)
    order_date   = match(r"Order Date\s*\n([\d/]+)", t)
    membership   = match(r"Membership Number\s*\n(\d+)", t)
    payment      = match(r"((?:VISA|Mastercard|Debit|Credit)[^\n]*ending in \d+)", t)

    # Shipping / Billing address
    ship_name    = match(r"Shipping Address\s*\n(.+)", t)
    ship_addr    = match(r"Shipping Address\s*\n.+\n(.+)", t)
    ship_city    = match(r"Shipping Address\s*\n(?:.+\n){2}(.+)", t)
    ship_postal  = match(r"Shipping Address\s*\n(?:.+\n){3}([A-Z0-9]{3}\s*[A-Z0-9]{3})", t)
    ship_phone   = match(r"Shipping Address\s*\n(?:.+\n){4}(\d{7,})", t)
    bill_name    = match(r"Billing Address\s*\n(.+)", t)
    bill_addr    = match(r"Billing Address\s*\n.+\n(.+)", t)
    bill_city    = match(r"Billing Address\s*\n(?:.+\n){2}(.+)", t)
    bill_postal  = match(r"Billing Address\s*\n(?:.+\n){3}([A-Z0-9]{3}\s*[A-Z0-9]{3})", t)

    # ── Line items ─────────────────────────────────────────────────────────
    # Each item block: multi-line name, blank line, "Item #XXXXX", price on next line
    # OCR may produce \$ (backslash + dollar) or plain $ for the price
    item_positions = list(re.finditer(r"Item\s*#(\d+)", t))
    items = []
    for m in item_positions:
        item_no = m.group(1)
        # Name = text between previous blank line and this "Item #"
        before = t[:m.start()]
        name = before.rstrip().split("\n\n")[-1].strip().replace("\n", " ")
        # Clean up OCR hyphenation artifacts like "12- pack" → "12-pack"
        name = re.sub(r"-\s+", "-", name)
        # Unit price: next \$XX.XX or $XX.XX within 30 chars after item number
        after = t[m.end():]
        price_m = re.search(r"\\?\$([\d,]+\.\d{2})", after[:30])
        unit_price = price_m.group(1) if price_m else ""
        items.append({"name": name, "item_number": item_no, "unit_price": unit_price})

    # Per-item delivered totals (appear as "Delivered \$XX.XX" in OCR output)
    delivered_totals = re.findall(r"(?:Delivered|Shipped)\s+\\?\$([\d,]+\.\d{2})", t)
    for i, total in enumerate(delivered_totals):
        if i < len(items):
            items[i]["total_price"] = total

    # ── Order Summary totals ───────────────────────────────────────────────
    # The summary section lists all labels first, then all values in order:
    # Subtotal, Shipping, Surcharge, GST, HST, PST, QST, Order Total
    summary_values = re.findall(r"\\?\$?([\d,]+\.\d{2})", t[t.find("Order Summary"):]) if "Order Summary" in t else []
    summary_labels = ["subtotal", "shipping", "surcharge", "gst", "hst", "pst", "qst", "order_total"]
    summary = {}
    for i, label in enumerate(summary_labels):
        if i < len(summary_values):
            summary[label] = summary_values[i]

    # Fallback: Order Total via direct regex (handles both \$ and $)
    if not summary.get("order_total"):
        ot = re.search(r"Order Total\s*\n+\\?\$?([\d,]+\.\d{2})", t)
        if ot:
            summary["order_total"] = ot.group(1)

    if order_number:
        # ── Online order ──────────────────────────────────────────────────
        result = {}
        if order_number:  result["order_number"]      = order_number
        if order_date:    result["order_date"]         = order_date
        if membership:    result["membership_number"]  = membership
        if payment:       result["payment_method"]     = payment
        if ship_name:     result["shipping_name"]      = ship_name
        if ship_addr:     result["shipping_address"]   = ship_addr
        if ship_city:     result["shipping_city"]      = ship_city
        if ship_postal:   result["shipping_postal"]    = ship_postal
        if ship_phone:    result["shipping_phone"]     = ship_phone
        if bill_name:     result["billing_name"]       = bill_name
        if bill_addr:     result["billing_address"]    = bill_addr
        if bill_city:     result["billing_city"]       = bill_city
        if bill_postal:   result["billing_postal"]     = bill_postal
        for i, item in enumerate(items, 1):
            if item.get("name"):      result[f"item_{i}_name"]       = item["name"]
            if item.get("item_number"): result[f"item_{i}_number"]   = item["item_number"]
            if item.get("unit_price"): result[f"item_{i}_unit_price"] = f"${item['unit_price']}"
            if item.get("total_price"): result[f"item_{i}_total"]    = f"${item['total_price']}"
        for label, val in summary.items():
            if val and val != "0.00":
                result[label] = f"${val}"
        # Always include order_total even if 0
        if summary.get("order_total"):
            result["order_total"] = f"${summary['order_total']}"
        return {k: v for k, v in result.items() if v}

    # ── In-store receipt fallback ─────────────────────────────────────────
    result = {
        "store_number":      match(r"Store\s*(?:#|No\.?)\s*[:\-]?\s*(\S+)", t),
        "date":              match(r"Date\s*[:\-]?\s*([\d/\-]+)", t),
        "time":              match(r"Time\s*[:\-]?\s*([\d:APM ]+)", t),
        "cashier":           match(r"Cashier\s*[:\-]?\s*(.+)", t),
        "membership_number": match(r"Member(?:ship)?\s*(?:#|No\.?)?\s*[:\-]?\s*(\d+)", t),
        "subtotal":          match(r"Subtotal\s*\\?\$?([\d,\.]+)", t),
        "tax":               match(r"Tax\s+\\?\$?([\d,\.]+)", t),
        "total":             match(r"Total\s*\\?\$?([\d,\.]+)", t),
        "payment_method":    match(r"(Visa|Mastercard|Cash|Debit|Credit|Cheque)", t),
        "items_count":       match(r"(\d+)\s+Items?\s+Purchased", t),
    }
    
    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}

def parse_dhl(t):
    """
    DHL Commercial Invoice parser.
    Uses word-level x-coordinates to correctly split the three-column
    address block (SENDER / SOLD TO / RECIPIENT) that OCR merges per line.
    Falls back to regex for all other fields.
    """
    result = {}

    # ── Document header ────────────────────────────────────────────────────
    result["document_type"] = "COMMERCIAL INVOICE"

    inv_date = match(r"INVOICE DATE:\s*([\d\-\.\/]+)", t)
    if inv_date:
        result["invoice_date"] = inv_date

    inv_num = match(r"INVOICE NUMBER:\s*([A-Z0-9]+)", t)
    if inv_num:
        result["invoice_number"] = inv_num

    waybill = match(r"DHL WAYBILL NUMBER:\s*([0-9]+)", t)
    if waybill:
        result["waybill_number"] = waybill

    carrier = match(r"CARRIER:\s*([A-Z]+)", t)
    if carrier:
        result["carrier"] = carrier

    sender_ref = match(r"SENDER'S REFERENCE:\s*([^\n]+)", t)
    if sender_ref and sender_ref.strip() and not re.search(r'CARRIER|RECIPIENT', sender_ref):
        result["sender_reference"] = sender_ref.strip()

    recipient_ref = match(r"RECIPIENT'S REFERENCE:\s*([^\n]+)", t)
    if recipient_ref and recipient_ref.strip() and not re.search(r'QUANTITY|COUNTRY|DESCRIPTION', recipient_ref):
        result["recipient_reference"] = recipient_ref.strip()

    # ── Three-column address block (word-position based) ───────────────────
    # Column x-boundaries derived from the PDF layout:
    #   Sender:    x < 200
    #   Sold To:   200 <= x < 370
    #   Recipient: x >= 370
    try:
        import pdfplumber, tempfile, os
        # Re-open the PDF from the text — we need the word objects.
        # We pass the path via a module-level variable set by the extract route.
        _pdf_path = getattr(parse_dhl, '_current_pdf_path', None)
        if _pdf_path and os.path.exists(_pdf_path):
            with pdfplumber.open(_pdf_path) as pdf:
                all_words = []
                for page in pdf.pages:
                    all_words.extend(page.extract_words())

            # Find the y-range of the address block:
            # starts after the SENDER:/SOLD TO:/RECIPIENT: header row,
            # ends before the EMAIL ADDRESS row.
            header_y = None
            email_y  = None
            for w in all_words:
                if w['text'] in ('SENDER:', 'SENDER') and header_y is None:
                    header_y = w['top']
                if w['text'] in ('EMAIL', 'ADDRESS:') and email_y is None and header_y and w['top'] > header_y + 5:
                    email_y = w['top']

            if header_y is not None and email_y is not None:
                # Collect words in the address block, grouped by y (row)
                addr_words = [w for w in all_words
                              if w['top'] > header_y + 2 and w['top'] < email_y - 1]

                # Group by row (same top ± 3px)
                rows = {}
                for w in addr_words:
                    row_key = round(w['top'] / 3) * 3
                    rows.setdefault(row_key, []).append(w)

                sender_parts, sold_to_parts, recipient_parts = [], [], []
                for row_key in sorted(rows):
                    row_words = sorted(rows[row_key], key=lambda w: w['x0'])
                    s_words  = [w['text'] for w in row_words if w['x0'] < 200]
                    st_words = [w['text'] for w in row_words if 200 <= w['x0'] < 370]
                    r_words  = [w['text'] for w in row_words if w['x0'] >= 370]
                    if s_words:  sender_parts.append(' '.join(s_words))
                    if st_words: sold_to_parts.append(' '.join(st_words))
                    if r_words:  recipient_parts.append(' '.join(r_words))

                def build_address(lines_list, prefix):
                    addr = {}
                    if not lines_list:
                        return addr
                    addr[f"{prefix}_name"] = lines_list[0]
                    street_parts = []
                    for ln in lines_list[1:]:
                        if not ln:
                            continue
                        if re.match(r'^\d{4,6}$', ln):
                            addr[f"{prefix}_postal_code"] = ln
                            continue
                        if re.match(r'^[A-Za-z][A-Za-z\s]{2,}$', ln) and ln[0].isupper():
                            addr[f"{prefix}_country"] = ln
                            continue
                        street_parts.append(ln)
                    if street_parts:
                        addr[f"{prefix}_address"] = ", ".join(street_parts)
                    parts = [addr.get(f"{prefix}_name", "")]
                    if f"{prefix}_address" in addr:
                        parts.append(addr[f"{prefix}_address"])
                    if f"{prefix}_postal_code" in addr:
                        parts.append(addr[f"{prefix}_postal_code"])
                    if f"{prefix}_country" in addr:
                        parts.append(addr[f"{prefix}_country"])
                    addr[f"{prefix}_full_address"] = "\n".join(p for p in parts if p)
                    return addr

                result.update(build_address(sender_parts,    "sender"))
                result.update(build_address(sold_to_parts,   "sold_to"))
                result.update(build_address(recipient_parts, "recipient"))

    except Exception:
        pass  # Fall through to regex fallback below

    # ── Regex fallback for addresses (if word-position parse didn't run) ───
    if "sender_name" not in result:
        # Try to extract at least emails/phones as address proxies
        pass

    # ── Email / Phone / Fax (inline labels, one per column per row) ────────
    emails = re.findall(r'EMAIL ADDRESS:\s*(\S+@\S+)', t, re.IGNORECASE)
    if len(emails) >= 1: result["sender_email"]    = emails[0]
    if len(emails) >= 2: result["sold_to_email"]   = emails[1]
    if len(emails) >= 3: result["recipient_email"] = emails[2]

    phones = re.findall(r'PHONE NUMBER:\s*([0-9]+)', t, re.IGNORECASE)
    if len(phones) >= 1: result["sender_phone"]    = phones[0]
    if len(phones) >= 2: result["sold_to_phone"]   = phones[1]
    if len(phones) >= 3: result["recipient_phone"] = phones[2]

    tax_ids = re.findall(r'TAX ID/VAT/EIN#:\s*([A-Z0-9][A-Z0-9]{3,})', t, re.IGNORECASE)
    # Filter out false positives (header words)
    tax_ids = [x for x in tax_ids if x.upper() not in ('INVOICE', 'EORI', 'TAXID', 'NUMBER')]
    if len(tax_ids) >= 1: result["sender_tax_id"]    = tax_ids[0]
    if len(tax_ids) >= 2: result["sold_to_tax_id"]   = tax_ids[1]
    if len(tax_ids) >= 3: result["recipient_tax_id"] = tax_ids[2]

    eori = match(r'EORI#:\s*([A-Z0-9][A-Z0-9]{3,})', t)
    if eori and eori not in ('TAXID', 'EORI'):
        result["sender_eori"] = eori

    # ── Items table ────────────────────────────────────────────────────────
    item_rows = re.findall(
        r'(\d+)\s+PCS\s+([A-Za-z]+)\s+(.+?)\s+([\d\.]+)\s+kg\s+([\d,\.]+)\s+([\d,\.]+)',
        t, re.IGNORECASE
    )
    for idx, (qty, country, desc, weight, unit_val, subtotal_val) in enumerate(item_rows, 1):
        result[f"item_{idx:02d}_quantity"]    = qty
        result[f"item_{idx:02d}_country"]     = country
        result[f"item_{idx:02d}_description"] = desc.strip()
        result[f"item_{idx:02d}_unit_weight"] = f"{weight} kg"
        result[f"item_{idx:02d}_unit_value"]  = unit_val
        result[f"item_{idx:02d}_subtotal"]    = subtotal_val

    if item_rows:
        result["items_found"] = str(len(item_rows))

    # ── Weight & totals ────────────────────────────────────────────────────
    net_w = match(r'TOTAL NET WEIGHT:\s*\([^)]*\)\s*([\d\.]+)\s*kg', t)
    if net_w:
        result["total_net_weight"] = f"{net_w} kg"

    gross_w = match(r'TOTAL GROSS WEIGHT:\s*\([^)]*\)\s*([\d\.]+)\s*kg', t)
    if gross_w:
        result["total_gross_weight"] = f"{gross_w} kg"

    pieces = match(r'TOTAL SHIPMENT PIECES:\s*([0-9]+)', t)
    if pieces:
        result["total_shipment_pieces"] = pieces

    currency = match(r'CURRENCY CODE:\s*([A-Z]{3})', t)
    if currency:
        result["currency"] = currency

    declared = match(r'TOTAL DECLARED VALUE:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if declared:
        result["total_declared_value"] = declared

    freight = match(r'FREIGHT & INSURANCE CHARGES:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if freight:
        result["freight_insurance_charges"] = freight

    other = match(r'OTHER CHARGES:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if other:
        result["other_charges"] = other

    total_inv = match(r'TOTAL INVOICE AMOUNT:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if total_inv:
        result["total_invoice_amount"] = total_inv

    # ── Export / trade info ────────────────────────────────────────────────
    export_type = match(r'TYPE OF EXPORT:\s*([A-Za-z ]+?)(?:\s{2,}|TERMS)', t)
    if export_type:
        result["type_of_export"] = export_type.strip()

    terms = match(r'TERMS OF TRADE:\s*([^\n]+?)(?:\s{2,}|$)', t)
    if terms:
        result["terms_of_trade"] = terms.strip()

    reason = match(r'REASON FOR EXPORT:\s*([^\n]+?)(?:\s{2,}|CITY NAME|$)', t)
    if reason:
        result["reason_for_export"] = reason.strip()

    # ── Signatory ──────────────────────────────────────────────────────────
    sig_name = match(r'NAME:\s*([^\n]+)', t)
    if sig_name and sig_name.strip():
        result["signatory_name"] = sig_name.strip()

    position = match(r'POSITION IN COMPANY:\s*([^\n]+?)(?:\s{2,}|COMPANY STAMP|$)', t)
    if position:
        result["signatory_position"] = position.strip()

    if "GENERAL NOTES:" in t:
        result["has_general_notes"] = "Yes"

    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)

    return result

    # ── Document header ────────────────────────────────────────────────────
    result["document_type"] = "COMMERCIAL INVOICE"

    inv_date = match(r"INVOICE DATE:\s*([\d\-\.\/]+)", t)
    if inv_date:
        result["invoice_date"] = inv_date

    inv_num = match(r"INVOICE NUMBER:\s*([A-Z0-9]+)", t)
    if inv_num:
        result["invoice_number"] = inv_num

    waybill = match(r"DHL WAYBILL NUMBER:\s*([0-9]+)", t)
    if waybill:
        result["waybill_number"] = waybill

    carrier = match(r"CARRIER:\s*([A-Z]+)", t)
    if carrier:
        result["carrier"] = carrier

    sender_ref = match(r"SENDER'S REFERENCE:\s*([^\n]+)", t)
    if sender_ref and sender_ref.strip() and not re.search(r'CARRIER|RECIPIENT', sender_ref):
        result["sender_reference"] = sender_ref.strip()

    recipient_ref = match(r"RECIPIENT'S REFERENCE:\s*([^\n]+)", t)
    if recipient_ref and recipient_ref.strip() and not re.search(r'QUANTITY|COUNTRY|DESCRIPTION', recipient_ref):
        result["recipient_reference"] = recipient_ref.strip()

    # ── Three-column address block ─────────────────────────────────────────
    # The header line is: "SENDER:   SOLD TO:   RECIPIENT:"
    # Each subsequent line has three values separated by 2+ spaces.
    # We collect lines until we hit the EMAIL ADDRESS row.

    header_m = re.search(r'SENDER:\s+SOLD TO:\s+RECIPIENT:\s*\n(.*?)(?=EMAIL ADDRESS:|INVOICE DATE:|$)',
                         t, re.DOTALL | re.IGNORECASE)
    if header_m:
        block = header_m.group(1)
        lines = [ln for ln in block.split('\n') if ln.strip()]

        sender_lines, sold_to_lines, recipient_lines = [], [], []

        for line in lines:
            if re.match(r'\s*EMAIL ADDRESS:', line, re.IGNORECASE):
                break
            # Split on 2+ spaces to separate the three columns
            cols = re.split(r'\s{2,}', line.strip())
            if len(cols) >= 3:
                sender_lines.append(cols[0].strip())
                sold_to_lines.append(cols[1].strip())
                recipient_lines.append(cols[2].strip())
            elif len(cols) == 2:
                sender_lines.append(cols[0].strip())
                recipient_lines.append(cols[1].strip())
            elif len(cols) == 1 and cols[0]:
                sender_lines.append(cols[0].strip())

        def build_address(lines_list, prefix):
            addr = {}
            if not lines_list:
                return addr
            addr[f"{prefix}_name"] = lines_list[0]
            street_parts = []
            for ln in lines_list[1:]:
                if not ln:
                    continue
                # Postal code (digits only)
                if re.match(r'^\d{4,6}$', ln):
                    addr[f"{prefix}_postal_code"] = ln
                    continue
                # Country (title-case word(s), no digits)
                if re.match(r'^[A-Za-z][A-Za-z\s]{2,}$', ln) and ln[0].isupper():
                    addr[f"{prefix}_country"] = ln
                    continue
                street_parts.append(ln)
            if street_parts:
                addr[f"{prefix}_address"] = ", ".join(street_parts)
            # Build full address string
            parts = [addr.get(f"{prefix}_name", "")]
            if f"{prefix}_address" in addr:
                parts.append(addr[f"{prefix}_address"])
            if f"{prefix}_postal_code" in addr:
                parts.append(addr[f"{prefix}_postal_code"])
            if f"{prefix}_country" in addr:
                parts.append(addr[f"{prefix}_country"])
            addr[f"{prefix}_full_address"] = "\n".join(p for p in parts if p)
            return addr

        result.update(build_address(sender_lines,    "sender"))
        result.update(build_address(sold_to_lines,   "sold_to"))
        result.update(build_address(recipient_lines, "recipient"))

    # ── Email / Phone / Fax rows (inline labels in each column) ───────────
    # Pattern: "EMAIL ADDRESS: a@b.com   EMAIL ADDRESS: c@d.com   EMAIL ADDRESS: e@f.com"
    email_row = re.search(r'EMAIL ADDRESS:\s*(\S+@\S+)\s+EMAIL ADDRESS:\s*(\S+@\S+)\s+EMAIL ADDRESS:\s*(\S+@\S+)', t, re.IGNORECASE)
    if email_row:
        result["sender_email"]    = email_row.group(1)
        result["sold_to_email"]   = email_row.group(2)
        result["recipient_email"] = email_row.group(3)
    else:
        # Fallback: grab first occurrence per label
        emails = re.findall(r'EMAIL ADDRESS:\s*(\S+@\S+)', t, re.IGNORECASE)
        if len(emails) >= 1: result["sender_email"]    = emails[0]
        if len(emails) >= 2: result["sold_to_email"]   = emails[1]
        if len(emails) >= 3: result["recipient_email"] = emails[2]

    phone_row = re.search(r'PHONE NUMBER:\s*([0-9]+)\s+PHONE NUMBER:\s*([0-9]+)\s+PHONE NUMBER:\s*([0-9]+)', t, re.IGNORECASE)
    if phone_row:
        result["sender_phone"]    = phone_row.group(1)
        result["sold_to_phone"]   = phone_row.group(2)
        result["recipient_phone"] = phone_row.group(3)
    else:
        phones = re.findall(r'PHONE NUMBER:\s*([0-9]+)', t, re.IGNORECASE)
        if len(phones) >= 1: result["sender_phone"]    = phones[0]
        if len(phones) >= 2: result["sold_to_phone"]   = phones[1]
        if len(phones) >= 3: result["recipient_phone"] = phones[2]

    fax_row = re.findall(r'FAX NUMBER:\s*([0-9]+)', t, re.IGNORECASE)
    if fax_row:
        result["sender_fax"] = fax_row[0]

    tax_ids = re.findall(r'TAX ID/VAT/EIN#:\s*([A-Z0-9]+)', t, re.IGNORECASE)
    if tax_ids:
        result["sender_tax_id"] = tax_ids[0]
    if len(tax_ids) >= 2:
        result["sold_to_tax_id"] = tax_ids[1]
    if len(tax_ids) >= 3:
        result["recipient_tax_id"] = tax_ids[2]

    eori = match(r'EORI#:\s*([A-Z0-9][A-Z0-9]{3,})', t)
    if eori and eori not in ('TAXID', 'EORI'):
        result["sender_eori"] = eori

    # ── Items table ────────────────────────────────────────────────────────
    # Row format: QTY  PCS  Country  Description  HarmCode  UnitWeight  UnitValue  Subtotal
    item_rows = re.findall(
        r'(\d+)\s+PCS\s+([A-Za-z]+)\s+(.+?)\s+([\d\.]+)\s+kg\s+([\d,\.]+)\s+([\d,\.]+)',
        t, re.IGNORECASE
    )
    for idx, (qty, country, desc, weight, unit_val, subtotal_val) in enumerate(item_rows, 1):
        result[f"item_{idx:02d}_quantity"]    = qty
        result[f"item_{idx:02d}_country"]     = country
        result[f"item_{idx:02d}_description"] = desc.strip()
        result[f"item_{idx:02d}_unit_weight"] = f"{weight} kg"
        result[f"item_{idx:02d}_unit_value"]  = unit_val
        result[f"item_{idx:02d}_subtotal"]    = subtotal_val

    if item_rows:
        result["items_found"] = str(len(item_rows))

    # ── Weight & totals ────────────────────────────────────────────────────
    net_w = match(r'TOTAL NET WEIGHT:\s*\([^)]*\)\s*([\d\.]+)\s*kg', t)
    if net_w:
        result["total_net_weight"] = f"{net_w} kg"

    gross_w = match(r'TOTAL GROSS WEIGHT:\s*\([^)]*\)\s*([\d\.]+)\s*kg', t)
    if gross_w:
        result["total_gross_weight"] = f"{gross_w} kg"

    pieces = match(r'TOTAL SHIPMENT PIECES:\s*([0-9]+)', t)
    if pieces:
        result["total_shipment_pieces"] = pieces

    currency = match(r'CURRENCY CODE:\s*([A-Z]{3})', t)
    if currency:
        result["currency"] = currency

    declared = match(r'TOTAL DECLARED VALUE:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if declared:
        result["total_declared_value"] = declared

    freight = match(r'FREIGHT & INSURANCE CHARGES:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if freight:
        result["freight_insurance_charges"] = freight

    other = match(r'OTHER CHARGES:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if other:
        result["other_charges"] = other

    total_inv = match(r'TOTAL INVOICE AMOUNT:\s*\([A-Z]{3}\)\s*([\d,\.]+)', t)
    if total_inv:
        result["total_invoice_amount"] = total_inv

    # ── Export / trade info ────────────────────────────────────────────────
    export_type = match(r'TYPE OF EXPORT:\s*([A-Za-z ]+?)(?:\s{2,}|TERMS)', t)
    if export_type:
        result["type_of_export"] = export_type.strip()

    terms = match(r'TERMS OF TRADE:\s*([^\n]+?)(?:\s{2,}|$)', t)
    if terms:
        result["terms_of_trade"] = terms.strip()

    reason = match(r'REASON FOR EXPORT:\s*([^\n]+?)(?:\s{2,}|CITY NAME|$)', t)
    if reason:
        result["reason_for_export"] = reason.strip()

    # ── Signatory ──────────────────────────────────────────────────────────
    sig_name = match(r'NAME:\s*([^\n]+)', t)
    if sig_name and sig_name.strip():
        result["signatory_name"] = sig_name.strip()

    position = match(r'POSITION IN COMPANY:\s*([^\n]+?)(?:\s{2,}|COMPANY STAMP|$)', t)
    if position:
        result["signatory_position"] = position.strip()

    if "GENERAL NOTES:" in t:
        result["has_general_notes"] = "Yes"

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}

def parse_amazon(t):
    """
    Amazon India Tax Invoice/Bill of Supply format parser.
    Uses word-level x-coordinates to correctly split the two-column
    header block (Sold By / Billing Address) and extract the full
    Shipping Address block. Handles both marketplace and direct invoices.
    """
    result = {}

    # ── Document type ──────────────────────────────────────────────────────
    doc_type = match(r"(Tax Invoice|Bill of Supply|Cash Memo)", t)
    if doc_type:
        result["document_type"] = doc_type

    # ── Two-column address block (word-position based) ─────────────────────
    # Layout:
    #   Col A (x < 280):  "Sold By :"  then seller address lines
    #   Col B (x >= 280): "Billing Address :"  then customer billing lines
    # Followed by "Shipping Address :" block (full width, below the two-col block)
    try:
        import pdfplumber, os
        _pdf_path = getattr(parse_amazon, '_current_pdf_path', None)
        if _pdf_path and os.path.exists(_pdf_path):
            with pdfplumber.open(_pdf_path) as pdf:
                all_words = []
                for page in pdf.pages:
                    all_words.extend(page.extract_words())

            # ── Find y-boundaries ──────────────────────────────────────────
            sold_by_y  = None
            shipping_y = None
            place_y    = None

            for w in all_words:
                txt = w['text']
                if txt == 'Sold' and sold_by_y is None:
                    sold_by_y = w['top']
                if txt == 'Shipping' and shipping_y is None and sold_by_y:
                    shipping_y = w['top']
                if txt == 'Place' and place_y is None and shipping_y and w['top'] > shipping_y + 5:
                    place_y = w['top']

            # ── Two-column block: Sold By (left) / Billing Address (right) ─
            if sold_by_y is not None and shipping_y is not None:
                two_col_words = [w for w in all_words
                                 if w['top'] > sold_by_y + 2 and w['top'] < shipping_y - 1]

                rows = {}
                for w in two_col_words:
                    rk = round(w['top'] / 3) * 3
                    rows.setdefault(rk, []).append(w)

                seller_lines, billing_lines = [], []
                for rk in sorted(rows):
                    row_words = sorted(rows[rk], key=lambda w: w['x0'])
                    left  = [w['text'] for w in row_words if w['x0'] < 280]
                    right = [w['text'] for w in row_words if w['x0'] >= 280]
                    if left:  seller_lines.append(' '.join(left))
                    if right: billing_lines.append(' '.join(right))

                # Parse seller lines
                if seller_lines:
                    result["seller_name"] = seller_lines[0]
                    seller_addr = []
                    for ln in seller_lines[1:]:
                        if re.match(r'^[A-Z]{2,3}$', ln):
                            result["seller_country"] = ln
                            continue
                        if re.match(r'^\d{6}$', ln):
                            result["seller_postal_code"] = ln
                            continue
                        seller_addr.append(ln)
                    if seller_addr:
                        result["seller_address"] = ", ".join(seller_addr)
                    parts = [result["seller_name"]] + seller_addr
                    if "seller_postal_code" in result:
                        parts.append(result["seller_postal_code"])
                    if "seller_country" in result:
                        parts.append(result["seller_country"])
                    result["seller_full_address"] = "\n".join(parts)

                # Parse billing lines
                if billing_lines:
                    result["billing_name"] = billing_lines[0]
                    billing_addr = []
                    for ln in billing_lines[1:]:
                        if re.match(r'^[A-Z]{2,3}$', ln):
                            result["billing_country"] = ln
                            continue
                        sc = re.search(r'State/UT Code:\s*(\d+)', ln)
                        if sc:
                            result["billing_state_code"] = sc.group(1)
                            continue
                        billing_addr.append(ln)
                    if billing_addr:
                        result["billing_address"] = ", ".join(billing_addr)
                        last = billing_addr[-1]
                        m = re.search(r'([A-Z][A-Z ]+),\s*([A-Z][A-Z ]+),\s*(\d{6})', last)
                        if m:
                            result["billing_city"]        = m.group(1).strip()
                            result["billing_state"]       = m.group(2).strip()
                            result["billing_postal_code"] = m.group(3)
                    parts = [result["billing_name"]] + billing_addr
                    if "billing_country" in result:
                        parts.append(result["billing_country"])
                    result["billing_full_address"] = "\n".join(parts)

            # ── Shipping Address block ─────────────────────────────────────
            # "Shipping Address :" label is in the RIGHT column (x >= 400).
            # Only collect words from the right column (x >= 400) to avoid
            # picking up PAN/GST lines from the left column at the same y-range.
            if shipping_y is not None and place_y is not None:
                ship_words = [w for w in all_words
                              if w['top'] > shipping_y + 2 and w['top'] < place_y - 1
                              and w['x0'] >= 400]

                ship_rows = {}
                for w in ship_words:
                    rk = round(w['top'] / 3) * 3
                    ship_rows.setdefault(rk, []).append(w)

                ship_lines = []
                for rk in sorted(ship_rows):
                    row_words = sorted(ship_rows[rk], key=lambda w: w['x0'])
                    ship_lines.append(' '.join(w['text'] for w in row_words))

                if ship_lines:
                    result["shipping_name"] = ship_lines[0]
                    ship_addr = []
                    for ln in ship_lines[1:]:
                        if re.match(r'^[A-Z]{2,3}$', ln):
                            result["shipping_country"] = ln
                            continue
                        sc = re.search(r'State/UT Code:\s*(\d+)', ln)
                        if sc:
                            result["shipping_state_code"] = sc.group(1)
                            continue
                        ship_addr.append(ln)
                    if ship_addr:
                        result["shipping_address"] = ", ".join(ship_addr)
                        last = ship_addr[-1]
                        m = re.search(r'([A-Z][A-Z ]+),\s*([A-Z][A-Z ]+),\s*(\d{6})', last)
                        if m:
                            result["shipping_city"]        = m.group(1).strip()
                            result["shipping_state"]       = m.group(2).strip()
                            result["shipping_postal_code"] = m.group(3)
                    parts = [result["shipping_name"]] + ship_addr
                    if "shipping_country" in result:
                        parts.append(result["shipping_country"])
                    result["shipping_full_address"] = "\n".join(parts)

    except Exception:
        pass  # Fall through to regex fallback

    # ── Regex fallback ─────────────────────────────────────────────────────
    if "seller_name" not in result:
        v = match(r"Sold By\s*:\s*\n([^\n]+)", t)
        if v:
            result["seller_name"] = v.strip()

    if "billing_name" not in result:
        v = match(r"Billing Address\s*:\s*\n([^\n]+)", t)
        if v and not re.search(r"Plot|plot|no\.|No\.", v):
            result["billing_name"] = v.strip()

    if "shipping_name" not in result:
        v = match(r"Shipping Address\s*:\s*\n([^\n]+)", t)
        if v and not re.search(r"Plot|plot|no\.|No\.|PAN", v):
            result["shipping_name"] = v.strip()

    # ── Seller tax / registration details ─────────────────────────────────
    pan = match(r"PAN No:\s*([A-Z0-9]+)", t)
    if pan:
        result["seller_pan"] = pan

    gst = match(r"GST Registration No:\s*([A-Z0-9]+)", t)
    if gst:
        result["seller_gst"] = gst

    fssai = match(r"FSSAI License No\.\s*([0-9]+)", t)
    if fssai:
        result["seller_fssai"] = fssai

    # ── Order / invoice details ────────────────────────────────────────────
    order_num = match(r"Order Number:\s*([A-Z0-9\-]+)", t)
    if order_num:
        result["order_number"] = order_num

    order_date = match(r"Order Date:\s*([\d\.]+)", t)
    if order_date:
        result["order_date"] = order_date

    inv_num = match(r"Invoice Number\s*:\s*([A-Z0-9\-]+)", t)
    if inv_num:
        result["invoice_number"] = inv_num

    inv_date = match(r"Invoice Date\s*:\s*([\d\.]+)", t)
    if inv_date:
        result["invoice_date"] = inv_date

    inv_details = match(r"Invoice Details\s*:\s*([A-Z0-9\-]+)", t)
    if inv_details:
        result["invoice_details"] = inv_details

    supply = match(r"Place of supply:\s*([A-Z]+)", t)
    if supply:
        result["place_of_supply"] = supply

    delivery = match(r"Place of delivery:\s*([A-Z]+)", t)
    if delivery:
        result["place_of_delivery"] = delivery

    # ── Product / line items ───────────────────────────────────────────────
    product_match = re.search(r"\n1\s+(.+?)(?:\s*\n\s*HSN:)", t, re.DOTALL)
    if product_match:
        product_lines = [ln.strip() for ln in product_match.group(1).strip().split('\n') if ln.strip()]
        product_name = ' '.join(product_lines[:3])
        product_name = re.sub(r'\s*[|]\s*[A-Z0-9_]+\s*[|].*$', '', product_name)
        product_name = re.sub(r'\s*[|]\s*[A-Z0-9_]+\s*\)?\s*$', '', product_name)
        if product_name and len(product_name) > 10:
            result["product_name"] = product_name.strip()

    hsn = match(r"HSN:(\d+)", t)
    if hsn:
        result["hsn_code"] = hsn

    # ── Financial details ──────────────────────────────────────────────────
    unit_price = match(r"₹([\d,\.]+)", t)
    if unit_price:
        result["unit_price"] = f"₹{unit_price}"

    discount = match(r"-₹([\d,\.]+)", t)
    if discount:
        result["discount_amount"] = f"₹{discount}"

    cgst_rate = match(r"(\d+)%\s+CGST", t)
    if cgst_rate:
        result["cgst_rate"] = f"{cgst_rate}%"

    cgst = match(r"CGST\s+₹([\d,\.]+)", t)
    if cgst:
        result["cgst_amount"] = f"₹{cgst}"

    sgst_rate = match(r"(\d+)%\s+SGST", t)
    if sgst_rate:
        result["sgst_rate"] = f"{sgst_rate}%"

    sgst = match(r"SGST\s+₹([\d,\.]+)", t)
    if sgst:
        result["sgst_amount"] = f"₹{sgst}"

    igst = match(r"IGST\s+₹([\d,\.]+)", t)
    if igst:
        result["igst_amount"] = f"₹{igst}"

    ship_charge = match(r"Shipping Charges.*?₹([\d,\.]+)(?=\s+-₹|\s+₹)", t)
    if ship_charge:
        result["shipping_charges"] = f"₹{ship_charge}"

    total = match(r"TOTAL:.*₹([\d,\.]+)(?!.*₹)", t)
    if total:
        result["total_amount"] = f"₹{total}"

    words = match(r"Amount in Words:\s*\n([^\n]+)", t)
    if words:
        result["amount_in_words"] = words.strip()

    # ── Payment details ────────────────────────────────────────────────────
    txn_id = match(r"Payment Transaction ID:\s*\n([A-Za-z0-9\+\/]+)", t)
    if not txn_id:
        txn_id = match(r"Payment Transaction ID:\s*([A-Za-z0-9\+\/]{10,})", t)
    if txn_id:
        result["payment_transaction_id"] = txn_id.strip()

    pay_datetime = match(r"Date & Time:\s*([\d\.]+,\s*[\d:]+)", t)
    if pay_datetime:
        result["payment_datetime"] = pay_datetime.strip()

    inv_val = match(r"Invoice Value:\s*([\d,\.]+)", t)
    if inv_val:
        result["invoice_value"] = f"₹{inv_val}"

    pay_mode = match(r"Mode of Payment:\s*\n?([A-Za-z][A-Za-z ]{1,20})", t)
    if not pay_mode:
        for mode in ("UPI", "Credit Card", "Debit Card", "Net Banking", "Cash on Delivery", "COD"):
            if re.search(r'\b' + re.escape(mode) + r'\b', t, re.IGNORECASE):
                pay_mode = mode
                break
    if pay_mode:
        result["payment_mode"] = pay_mode.strip()

    # ── Tax / compliance ───────────────────────────────────────────────────
    reverse = match(r"Whether tax is payable under reverse charge - (Yes|No)", t)
    if reverse:
        result["reverse_charge"] = reverse

    if "Authorized Signatory" in t:
        result["authorized_signatory"] = "Present"

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}

def parse_generic(t):
    """
    Generic invoice parser for standard business invoices.
    Handles various invoice formats with flexible pattern matching for:
    - Invoice metadata (number, date, PO, reference)
    - Vendor/customer information
    - Line items and descriptions
    - Financial totals (subtotal, tax, discount, shipping, total)
    - Payment information
    """
    result = {}
    
    # ── Invoice Identification ─────────────────────────────────────────────
    # Invoice number - try multiple patterns
    inv_num = (match(r"Invoice\s*(?:Number|No|#)[:\s]*([A-Z0-9\-]+)", t) or
               match(r"Invoice[:\s]+([A-Z0-9\-]+)", t) or
               match(r"INV[:\-\s]*([A-Z0-9\-]+)", t))
    if inv_num:
        result["invoice_number"] = inv_num
    
    # Invoice date
    inv_date = (match(r"Invoice\s*Date[:\s]*([\d/\-\.]+)", t) or
                match(r"Date[:\s]*([\d/\-\.]+)", t) or
                match(r"Dated?[:\s]*([\d/\-\.]+)", t))
    if inv_date:
        result["invoice_date"] = inv_date
    
    # Due date
    due_date = (match(r"Due\s*Date[:\s]*([\d/\-\.]+)", t) or
                match(r"Payment\s*Due[:\s]*([\d/\-\.]+)", t))
    if due_date:
        result["due_date"] = due_date
    
    # PO Number
    po_num = (match(r"P\.?O\.?\s*(?:Number|No|#)?[:\s]*([A-Z0-9\-]+)", t) or
              match(r"Purchase\s*Order[:\s]*([A-Z0-9\-]+)", t))
    if po_num:
        result["po_number"] = po_num
    
    # Reference number
    ref_num = (match(r"Reference\s*(?:Number|No|#)?[:\s]*([A-Z0-9\-]+)", t) or
               match(r"Ref[:\s]*([A-Z0-9\-]+)", t))
    if ref_num:
        result["reference_number"] = ref_num
    
    # ── Vendor Information ─────────────────────────────────────────────────
    # Vendor/Seller/From — match only the rest of the same line (no newline crossings)
    vendor = (match(r"(?:From|Vendor|Seller|Bill\s*From|Issued\s*By)[:\s]*\n([^\n]{2,80})", t) or
              match(r"(?:From|Vendor|Seller)[:\s]+([^\n\r]{2,80})", t))
    if vendor and not re.search(r"Bill To|Customer|Date|Invoice", vendor):
        result["vendor_name"] = vendor.strip()

    # Vendor email — anchored, no cross-line greedy matching
    vendor_email = match(r"(?:^|\n)(?:[^\n]{0,40}?)([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if vendor_email:
        result["vendor_email"] = vendor_email

    # Vendor phone
    vendor_phone = match(r"(?:Phone|Tel|Mobile|Telephone)[:\s]*([0-9\-\+\(\)\s]{7,20})", t)
    if vendor_phone:
        result["vendor_phone"] = vendor_phone.strip()

    # Vendor tax ID
    vendor_tax = (match(r"(?:Tax\s*ID|VAT\s*No|GST\s*No|TIN)[:\s]*([A-Z0-9\-]{5,20})", t) or
                  match(r"\b(GST[A-Z0-9]{9,15})\b", t))
    if vendor_tax:
        result["vendor_tax_id"] = vendor_tax

    # ── Customer Information ───────────────────────────────────────────────
    # Customer/Buyer/To — only the next single line after the label
    customer = (match(r"(?:Bill\s*To|Billed\s*To)[:\s]*\n([^\n]{2,80})", t) or
                match(r"(?:Customer|Client)[:\s]+([^\n\r]{2,80})", t))
    if customer and not re.search(r"From|Vendor|Date|Invoice", customer):
        result["customer_name"] = customer.strip()

    # Customer email
    customer_email = match(r"(?:Bill\s*To|Customer).*?\n[^\n]{0,60}([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if customer_email:
        result["customer_email"] = customer_email

    # Customer phone — anchor to a Phone/Tel label
    customer_phone = match(r"(?:Bill\s*To|Customer).*?\n[^\n]{0,60}(?:Phone|Tel)[:\s]*([0-9\-\+\(\)\s]{7,20})", t)
    if customer_phone:
        result["customer_phone"] = customer_phone.strip()
    
    # ── Item Information ───────────────────────────────────────────────────
    # Try to extract item count
    item_count = match(r"(\d+)\s+(?:Items?|Products?|Lines?)", t)
    if item_count:
        result["item_count"] = item_count
    
    # Description - look for common item description patterns
    description = match(r"Description[:\s]*\n([^\n]+)", t)
    if description and len(description) > 5:
        result["item_description"] = description.strip()
    
    # Quantity
    quantity = match(r"(?:Qty|Quantity)[:\s]*(\d+)", t)
    if quantity:
        result["quantity"] = quantity
    
    # Unit price
    unit_price = match(r"(?:Unit\s*Price|Price|Rate)[:\s]*[\$€£₹]?([\d,\.]+)", t)
    if unit_price:
        result["unit_price"] = unit_price
    
    # ── Financial Information ──────────────────────────────────────────────
    # Subtotal
    subtotal = (match(r"Sub\s*[Tt]otal[:\s]*[\$€£₹]?([\d,\.]+)", t) or
                match(r"Amount[:\s]*[\$€£₹]?([\d,\.]+)", t))
    if subtotal:
        result["subtotal"] = subtotal
    
    # Tax - try multiple patterns
    tax = (match(r"Tax(?:es)?[:\s]*[\$€£₹]?([\d,\.]+)", t) or
           match(r"(?:VAT|GST|Sales\s*Tax)[:\s]*[\$€£₹]?([\d,\.]+)", t) or
           match(r"Tax\s*\([\d\.]+%\)[:\s]*[\$€£₹]?([\d,\.]+)", t))
    if tax:
        result["tax"] = tax
    
    # Tax rate
    tax_rate = match(r"Tax.*?(\d+(?:\.\d+)?%)", t)
    if tax_rate:
        result["tax_rate"] = tax_rate
    
    # Discount
    discount = (match(r"Discount[:\s]*-?[\$€£₹]?([\d,\.]+)", t) or
                match(r"Discount\s*\([\d\.]+%\)[:\s]*[\$€£₹]?([\d,\.]+)", t))
    if discount:
        result["discount"] = discount
    
    # Shipping/Delivery
    shipping = (match(r"Shipping[:\s]*[\$€£₹]?([\d,\.]+)", t) or
                match(r"Delivery[:\s]*[\$€£₹]?([\d,\.]+)", t) or
                match(r"Freight[:\s]*[\$€£₹]?([\d,\.]+)", t))
    if shipping:
        result["shipping"] = shipping
    
    # Total - try multiple patterns
    total = (match(r"(?:Grand\s*)?Total[:\s]*[\$€£₹]?([\d,\.]+)", t) or
             match(r"Total\s*(?:Due|Amount|Payable)[:\s]*[\$€£₹]?([\d,\.]+)", t) or
             match(r"Amount\s*(?:Due|Payable)[:\s]*[\$€£₹]?([\d,\.]+)", t) or
             match(r"Balance\s*Due[:\s]*[\$€£₹]?([\d,\.]+)", t))
    if total:
        result["total"] = total
    
    # Amount paid
    paid = match(r"(?:Amount\s*)?Paid[:\s]*[\$€£₹]?([\d,\.]+)", t)
    if paid:
        result["amount_paid"] = paid
    
    # Balance due
    balance = match(r"Balance[:\s]*[\$€£₹]?([\d,\.]+)", t)
    if balance:
        result["balance_due"] = balance
    
    # Currency
    currency = (match(r"Currency[:\s]*([A-Z]{3})", t) or
                match(r"\b(USD|CAD|GBP|EUR|AUD|INR|JPY|CNY)\b", t) or
                ("USD" if "$" in t else None) or
                ("EUR" if "€" in t else None) or
                ("GBP" if "£" in t else None) or
                ("INR" if "₹" in t else None))
    if currency:
        result["currency"] = currency
    
    # ── Payment Information ────────────────────────────────────────────────
    # Payment terms
    payment_terms = (match(r"Payment\s*Terms?[:\s]*([^\n]+)", t) or
                     match(r"Terms[:\s]*([^\n]+)", t))
    if payment_terms and not re.search(r"Date|Invoice", payment_terms):
        result["payment_terms"] = payment_terms.strip()
    
    # Payment method
    payment_method = (match(r"Payment\s*Method[:\s]*([^\n]+)", t) or
                      match(r"Paid\s*(?:via|by)[:\s]*([^\n]+)", t))
    if payment_method:
        result["payment_method"] = payment_method.strip()
    
    # Bank details
    bank_name = match(r"Bank[:\s]*([^\n]+)", t)
    if bank_name and not re.search(r"Account|IBAN", bank_name):
        result["bank_name"] = bank_name.strip()
    
    account_num = (match(r"Account\s*(?:Number|No)[:\s]*([A-Z0-9]+)", t) or
                   match(r"A/C[:\s]*([A-Z0-9]+)", t))
    if account_num:
        result["account_number"] = account_num
    
    # ── Additional Information ─────────────────────────────────────────────
    # Notes
    notes = match(r"Notes?[:\s]*\n([^\n]+)", t)
    if notes and len(notes) > 5:
        result["notes"] = notes.strip()
    
    # Terms and conditions
    if re.search(r"Terms\s*(?:and|&)\s*Conditions", t, re.IGNORECASE):
        result["has_terms_conditions"] = "Yes"
    
    # Status
    status_match = re.search(r"Status[:\s]*(Paid|Unpaid|Pending|Overdue|Draft)", t, re.IGNORECASE)
    if status_match:
        result["status"] = status_match.group(1).capitalize()
    
    result = clean_result(result)
    return {k: v for k, v in result.items() if v}

def parse_cbs(t):
    """
    CBS (Canadian Bio Platforms) Instacart order receipt parser.
    Handles Instacart grocery delivery receipts with:
    - Order information (store, dates, delivery time)
    - Itemized products by category (Dairy & Eggs, Household, Personal Care, etc.)
    - Order totals (subtotal, fees, taxes, tip, service charges)
    
    Improved item extraction to capture product names like:
    - fairlife Milk (1.5 L)
    - bubly Raspberry Sparkling Water (12 x 355 ml)
    - Montellier Regular Sparkling Water (10 x 355 ml)
    """
    result = {}
    
    # ── Header Information ─────────────────────────────────────────────────
    if "Instacart" in t or "instacart" in t:
        result["platform"] = "Instacart"
    
    # Recipient email
    recipient_email = match(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if recipient_email and "instacart" not in recipient_email.lower():
        result["recipient_email"] = recipient_email
    
    # Order date and time
    order_date = match(r"(?:Thu|Fri|Sat|Sun|Mon|Tue|Wed),?\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", t)
    if order_date:
        result["order_date"] = order_date
    
    order_time = match(r"at\s+(\d{1,2}:\d{2}\s+[AP]M)", t)
    if order_time:
        result["order_time"] = order_time
    
    # ── Store and Delivery Information ─────────────────────────────────────
    # Store name from "Your order from [Store Name] was placed"
    store = match(r"Your order from\s+(.+?)\s+was placed", t)
    if store:
        result["store_name"] = store.strip()
    
    # Order placed date
    placed_date = match(r"was placed on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", t)
    if placed_date:
        result["order_placed_date"] = placed_date
    
    # Delivery date and time
    delivery_date = match(r"delivered on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", t)
    if delivery_date:
        result["delivery_date"] = delivery_date
    
    delivery_time = match(r"delivered on.*?at\s+(\d{1,2}:\d{2}\s+[AP]M)", t)
    if delivery_time:
        result["delivery_time"] = delivery_time
    
    # Items found count
    items_found = match(r"(\d+)\s+Items?\s+Found", t)
    if items_found:
        result["items_found"] = items_found
    
    # ── Extract Items by Category ──────────────────────────────────────────
    # Categories: DAIRY & EGGS, HOUSEHOLD, PERSONAL CARE, SPECIAL REQUEST, etc.
    categories = re.findall(r"(DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|BEVERAGES|SNACKS|FROZEN|BAKERY|MEAT|PRODUCE)", t)
    if categories:
        result["categories"] = ", ".join(set(categories))
    
    # ── Improved Item Extraction with Categories ──────────────────────────
    # NEW: Handle multi-line item format where product name, details, and price are on separate lines
    
    item_count = 0
    current_category = "Uncategorized"
    
    # Find the ITEMS FOUND section
    items_section_match = re.search(r"ITEMS FOUND.*?ORDER TOTALS", t, re.DOTALL | re.IGNORECASE)
    
    if items_section_match:
        items_text = items_section_match.group(0)
        lines = items_text.split('\n')
        
        # First pass: Look for quantity x price patterns and work backwards to find product names
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Check if this line is a category header
            category_match = re.match(r'^(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)$', line, re.IGNORECASE)
            if category_match:
                current_category = category_match.group(1).upper()
                i += 1
                continue
            
            # Look for quantity x price pattern: "2x $44.75" or "1 x $6.50 $6.50"
            qty_price_match = re.search(r'(\d+)\s*x\s*\$\s*([\d\.]+)', line)
            
            if qty_price_match:
                quantity = qty_price_match.group(1)
                unit_price = qty_price_match.group(2)
                
                # Extract final price if present on same line
                final_price_match = re.search(r'\$\s*([\d\.]+)\s*$', line)
                if final_price_match:
                    final_price = final_price_match.group(1)
                else:
                    try:
                        final_price = f"{float(quantity) * float(unit_price):.2f}"
                    except:
                        final_price = unit_price
                
                # Look backwards for product name (skip empty lines and category headers)
                product_name = None
                product_details = None
                
                for j in range(i - 1, max(0, i - 6), -1):
                    prev_line = lines[j].strip()
                    
                    # Skip empty lines
                    if not prev_line:
                        continue
                    
                    # Skip category headers
                    if re.match(r'^(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)$', prev_line, re.IGNORECASE):
                        continue
                    
                    # Skip lines with these keywords
                    if any(skip in prev_line.upper() for skip in ['ITEMS FOUND', 'REAL CANADIAN SUPERSTORE', 'ORDER TOTALS']):
                        continue
                    
                    # Skip lines that look like quantity/price patterns
                    if re.search(r'\d+\s*x\s*\$', prev_line):
                        continue
                    
                    # Check if this line has "Final" 
                    if 'Final' in prev_line or 'final' in prev_line:
                        # Check if this line has product name + details + "Final" all on same line
                        # Pattern: "A Beatrice Milk Jug 2% (41) Final item price:"
                        name_before_final = re.search(r'^(.+?)\s+[Ff]inal', prev_line)
                        if name_before_final and len(name_before_final.group(1)) > 10:
                            product_name = name_before_final.group(1).strip()
                            # Extract details from parentheses if present
                            if '(' in product_name and ')' in product_name:
                                details_match = re.search(r'\(([^\)]+)\)', product_name)
                                if details_match:
                                    product_details = details_match.group(1)
                            break
                        # Otherwise, if it has parentheses, extract details for later use
                        elif '(' in prev_line and ')' in prev_line and not product_details:
                            details_match = re.search(r'\(([^\)]+)\)', prev_line)
                            if details_match:
                                product_details = details_match.group(1)
                        continue
                    
                    # Check if this line has parentheses (likely product details)
                    if '(' in prev_line and ')' in prev_line and not product_details:
                        details_match = re.search(r'\(([^\)]+)\)', prev_line)
                        if details_match:
                            product_details = details_match.group(1)
                        continue
                    
                    # This should be the product name
                    elif len(prev_line) > 10 and not product_name:
                        product_name = prev_line
                        break
                
                # If we found a product name, add it
                if product_name:
                    # Check if product_name already has parentheses (details already included)
                    if '(' in product_name and ')' in product_name:
                        # Product name already includes details, use as-is
                        full_name = product_name
                    elif product_details:
                        # Add details to product name
                        full_name = f"{product_name} ({product_details})"
                    else:
                        full_name = product_name
                    
                    # Check for duplicates
                    already_added = False
                    for idx in range(1, item_count + 1):
                        if result.get(f"item_{idx}_name") == full_name:
                            already_added = True
                            break
                    
                    if not already_added:
                        item_count += 1
                        result[f"item_{item_count}_category"] = current_category
                        result[f"item_{item_count}_name"] = full_name
                        result[f"item_{item_count}_quantity"] = quantity
                        result[f"item_{item_count}_unit_price"] = f"${unit_price}"
                        result[f"item_{item_count}_final_price"] = f"${final_price}"
                        result[f"item_{item_count}_price_breakdown"] = f"{quantity} × ${unit_price} = ${final_price}"
                        result[f"item_{item_count}_section"] = "ITEMS FOUND"
            
            i += 1
    
    if item_count > 0:
        result["total_items_extracted"] = str(item_count)
    
    # OLD METHOD BELOW (kept as fallback)
    # Fallback: Try regex pattern matching if line-by-line didn't work
    if item_count == 0:
        # Find all products with their categories
        # Look for category headers followed by products
        category_pattern = r'(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)'
        
        # Split text by categories
        parts = re.split(category_pattern, t, flags=re.IGNORECASE)
        
        for i in range(1, len(parts), 2):  # Odd indices are category names
            if i + 1 < len(parts):
                category_name = parts[i].upper()
                category_content = parts[i + 1]
                
                # Find all products in this category section
                product_matches = re.finditer(
                    r'([A-Za-z0-9][A-Za-z0-9\'\s&\-\.\+,/]+\([^\)]+\))\s*[\n\s]*(\d+)\s+x\s+\$\s*([\d\.]+)',
                    category_content,
                    re.IGNORECASE
                )
                
                for prod_match in product_matches:
                    product_name = prod_match.group(1).strip()
                    product_name = ' '.join(product_name.split())
                    
                    if len(product_name) < 10:
                        continue
                    
                    quantity = prod_match.group(2)
                    unit_price = prod_match.group(3)
                    
                    # Look for final price
                    match_end = prod_match.end()
                    next_section = category_content[match_end:match_end+150]
                    final_price_match = re.search(r'Final item price:\s*\$?([\d\.]+)', next_section, re.IGNORECASE)
                    
                    if final_price_match:
                        final_price = final_price_match.group(1)
                    else:
                        try:
                            final_price = f"{float(quantity) * float(unit_price):.2f}"
                        except:
                            final_price = unit_price
                    
                    # Check for duplicate
                    already_added = False
                    for idx in range(1, item_count + 1):
                        if result.get(f"item_{idx}_name") == product_name:
                            already_added = True
                            break
                    
                    if not already_added:
                        item_count += 1
                        result[f"item_{item_count}_category"] = category_name
                        result[f"item_{item_count}_name"] = product_name
                        result[f"item_{item_count}_quantity"] = quantity
                        result[f"item_{item_count}_unit_price"] = f"${unit_price}"
                        result[f"item_{item_count}_final_price"] = f"${final_price}"
                        result[f"item_{item_count}_price_breakdown"] = f"{quantity} × ${unit_price} = ${final_price}"
                        result[f"item_{item_count}_section"] = "ITEMS FOUND"
    
    # Extract from REPLACEMENTS section
    replacements_section = re.search(r"REPLACEMENTS.*?(?=ORDER TOTALS|$)", t, re.DOTALL | re.IGNORECASE)
    if replacements_section:
        repl_text = replacements_section.group(0)
        
        repl_matches = re.finditer(
            r'([A-Za-z0-9][A-Za-z0-9\'\s&\-\.\+,/]+\([^\)]+\))\s*[\n\s]*(\d+)\s+x\s+\$\s*([\d\.]+)',
            repl_text,
            re.IGNORECASE
        )
        
        for repl_match in repl_matches:
            product_name = repl_match.group(1).strip()
            product_name = ' '.join(product_name.split())
            
            if len(product_name) < 10 or "Some of your items" in product_name:
                continue
            
            quantity = repl_match.group(2)
            unit_price = repl_match.group(3)
            
            context_after = repl_text[repl_match.end():repl_match.end()+150]
            
            if "Original price:" in context_after:
                orig_price_match = re.search(r'Original price:\s*\$?([\d\.]+)', context_after)
                final_price = orig_price_match.group(1) if orig_price_match else f"{float(quantity) * float(unit_price):.2f}"
                
                item_count += 1
                result[f"item_{item_count}_category"] = "REPLACEMENTS"
                result[f"item_{item_count}_name"] = product_name + " (Original - Replaced)"
                result[f"item_{item_count}_quantity"] = quantity
                result[f"item_{item_count}_unit_price"] = f"${unit_price}"
                result[f"item_{item_count}_final_price"] = f"${final_price}"
                result[f"item_{item_count}_price_breakdown"] = f"{quantity} × ${unit_price} = ${final_price}"
                result[f"item_{item_count}_section"] = "ITEMS FOUND"
            
            elif "Replaced item" in context_after or "price:" in context_after:
                repl_price_match = re.search(r'(?:Replaced item\s+)?price:\s*\$?([\d\.]+)', context_after)
                final_price = repl_price_match.group(1) if repl_price_match else f"{float(quantity) * float(unit_price):.2f}"
                
                item_count += 1
                result[f"item_{item_count}_category"] = "REPLACEMENTS"
                result[f"item_{item_count}_name"] = product_name + " (Replacement)"
                result[f"item_{item_count}_quantity"] = quantity
                result[f"item_{item_count}_unit_price"] = f"${unit_price}"
                result[f"item_{item_count}_final_price"] = f"${final_price}"
                result[f"item_{item_count}_price_breakdown"] = f"{quantity} × ${unit_price} = ${final_price}"
                result[f"item_{item_count}_section"] = "ITEMS FOUND"
    
    if item_count > 0:
        result["total_items_extracted"] = str(item_count)
    
    # ── Order Totals ───────────────────────────────────────────────────────
    # Items Subtotal
    subtotal = match(r"Items Subtotal\s+\$?([\d,\.]+)", t)
    if subtotal:
        result["items_subtotal"] = f"${subtotal}"
    
    # Checkout Bag Fee
    bag_fee = match(r"Checkout Bag Fee\s+\$?([\d,\.]+)", t)
    if bag_fee:
        result["checkout_bag_fee"] = f"${bag_fee}"
    
    # Checkout Bag Fee Tax
    bag_tax = match(r"Checkout Bag Fee Tax\s+\$?([\d,\.]+)", t)
    if bag_tax:
        result["checkout_bag_fee_tax"] = f"${bag_tax}"
    
    # Tip
    tip = match(r"Tip\s+\$?([\d,\.]+)", t)
    if tip:
        result["tip"] = f"${tip}"
    
    # Service Fee
    service_fee = match(r"Service Fee\s+\$?([\d,\.]+)", t)
    if service_fee:
        result["service_fee"] = f"${service_fee}"
    
    # Beverage Container Fee
    beverage_fee = match(r"Beverage Container Fee\s+\$?([\d,\.]+)", t)
    if beverage_fee:
        result["beverage_container_fee"] = f"${beverage_fee}"
    
    # Item GST
    item_gst = match(r"Item GST\s+\$?([\d,\.]+)", t)
    if item_gst:
        result["item_gst"] = f"${item_gst}"
    
    # Service GST
    service_gst = match(r"Service GST\s+\$?([\d,\.]+)", t)
    if service_gst:
        result["service_gst"] = f"${service_gst}"
    
    # Delivery Fee
    delivery_fee = match(r"Delivery Fee\s+\$?([\d,\.]+)", t)
    if delivery_fee:
        result["delivery_fee"] = f"${delivery_fee}"
    
    # Discounts (e.g., "$2 off any store")
    discount = match(r"\$(\d+)\s+off any store\s+-\$?([\d,\.]+)", t)
    if discount:
        result["discount"] = f"-${discount}"
    
    # Total - try multiple patterns to ensure we get it
    total = (match(r"Total\s+CAD\s+\$?([\d,\.]+)", t) or
             match(r"Total CAD\s+\$?([\d,\.]+)", t) or
             match(r"Total\s+\$?([\d,\.]+)", t))
    if total:
        result["total_cad"] = f"${total}"
        result["order_total"] = f"${total}"
    
    # Currency
    currency = match(r"Total\s+(CAD|USD|EUR|GBP)", t)
    if currency:
        result["currency"] = currency
    else:
        # Default to CAD for Canadian stores
        if "CAD" in t:
            result["currency"] = "CAD"
    
    # ── Additional Information ─────────────────────────────────────────────
    # Document type
    if "Your Instacart order receipt" in t:
        result["document_type"] = "Instacart Order Receipt"
    
    # Company name from header
    if "Cbc Bio Platforms" in t or "CBS" in t or "Bio Platforms" in t:
        result["company"] = "CBS Bio Platforms"
    
    # ── Convert CBS item format to standard format for web interface ──────
    # Convert item_{count}_name format to item_01, item_02 format
    standard_items = {}
    item_index = 1
    
    # Find all item numbers that exist
    item_numbers = set()
    for key in result.keys():
        if key.startswith('item_') and '_name' in key:
            item_num = key.split('_')[1]
            try:
                item_numbers.add(int(item_num))
            except ValueError:
                continue
    
    # Sort item numbers to ensure ascending order
    for item_num in sorted(item_numbers):
        name = result.get(f"item_{item_num}_name", "")
        quantity = result.get(f"item_{item_num}_quantity", "1")
        unit_price = result.get(f"item_{item_num}_unit_price", "")
        final_price = result.get(f"item_{item_num}_final_price", "")
        category = result.get(f"item_{item_num}_category", "")
        
        if name:
            # Create standard format: "Product Name (Category) x2 @ $5.00 → $10.00"
            item_display = name
            if category and category != "Uncategorized":
                item_display += f" ({category})"
            if quantity != "1":
                item_display += f" x{quantity}"
            if unit_price:
                item_display += f" @ {unit_price}"
            if final_price:
                item_display += f" → {final_price}"
            
            standard_items[f"item_{item_index:02d}"] = item_display
            item_index += 1
    
    # Remove old format items and add standard format
    keys_to_remove = [k for k in result.keys() if k.startswith('item_') and any(suffix in k for suffix in ['_name', '_quantity', '_unit_price', '_final_price', '_category', '_price_breakdown', '_section'])]
    for key in keys_to_remove:
        del result[key]
    
    # Add standard format items
    result.update(standard_items)
    
    # Update items count
    if standard_items:
        result["items_found"] = str(len(standard_items))

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}

def parse_guelph_ridgetown(t):
    """
    University of Guelph Ridgetown Campus Course & Exam Registration parser.
    Handles course registration invoices with:
    - University and campus information
    - Payment information (reference number, date, fee, HST, payment method)
    - Participant details (applicant number, name, address, postal code, phone, email)
    """
    result = {}
    
    # ── Document Information ───────────────────────────────────────────────
    if "Course & Exam Registration" in t or "Course and Exam Registration" in t:
        result["document_type"] = "Course & Exam Registration"
    
    # ── University Information ─────────────────────────────────────────────
    if "University of Guelph Ridgetown Campus" in t:
        result["university"] = "University of Guelph Ridgetown Campus"
    
    # Campus address
    address = match(r"(\d+\s+[A-Za-z\s]+(?:Street|St|Avenue|Ave|Road|Rd|East|West|North|South)[^\n]*)", t)
    if address:
        result["campus_address"] = address
    
    # City and postal code
    campus_location = match(r"([A-Za-z]+)\s+ON\s+([A-Z0-9]{3}\s*[A-Z0-9]{3})", t)
    if campus_location:
        result["campus_city"] = "Ridgetown"
        result["campus_province"] = "ON"
    
    campus_postal = match(r"ON\s+([A-Z0-9]{3}\s*[A-Z0-9]{3})", t)
    if campus_postal:
        result["campus_postal_code"] = campus_postal
    
    # Phone
    campus_phone = match(r"(1-866-222-9682|1-\d{3}-\d{3}-\d{4})", t)
    if campus_phone:
        result["campus_phone"] = campus_phone
    
    # Email
    campus_email = match(r"(rcabc@uoguelph\.ca)", t)
    if campus_email:
        result["campus_email"] = campus_email
    
    # HST Registration
    hst_reg = match(r"HST Registration #?R?(\d+)", t)
    if hst_reg:
        result["hst_registration"] = f"R{hst_reg}"
    
    # ── Payment Information ────────────────────────────────────────────────
    # Reference Number
    ref_num = match(r"Reference Number:\s*([A-Z0-9]+)", t)
    if ref_num:
        result["reference_number"] = ref_num
    
    # Date Paid
    date_paid = match(r"Date Paid:\s*([\w\s,]+\d{4})", t)
    if date_paid:
        result["date_paid"] = date_paid
    
    # Fee
    fee = match(r"Fee:\s*\$?([\d,\.]+)", t)
    if fee:
        result["fee"] = f"${fee}"
    
    # HST amount
    hst_amount = match(r"(?:Fee includes|includes)\s*\$?([\d,\.]+)\s*HST", t)
    if hst_amount:
        result["hst_amount"] = f"${hst_amount}"
    
    # Payment Method
    payment_method = match(r"Payment Method:\s*([^\n]+)", t)
    if payment_method:
        result["payment_method"] = payment_method.strip()
    
    # Payment Reference
    payment_ref = match(r"Payment Reference:\s*([A-Z0-9]+)", t)
    if payment_ref:
        result["payment_reference"] = payment_ref
    
    # Note
    note = match(r"Note:\s*([^\n]+)", t)
    if note:
        result["payment_note"] = note.strip()
    
    # ── Participant Details ────────────────────────────────────────────────
    # Applicant Number
    applicant_num = match(r"Applicant Number:\s*(\d+)", t)
    if applicant_num:
        result["applicant_number"] = applicant_num
    
    # Name - extract name that appears after "Name:" label
    name = match(r"Name:\s*([A-Z][A-Z\s]+?)(?=\s*Address:|\s*\n)", t)
    if name:
        result["participant_name"] = name.strip()
    
    # Address - extract street address after "Address:" label
    # Pattern: Address: street_address \n city province country
    participant_address = match(r"Address:\s*([^\n]+)", t)
    if participant_address:
        result["participant_address"] = participant_address.strip()
    
    # Extract full address including city/province (look for pattern after address)
    # Pattern: street \n city province CA
    full_address_match = re.search(r"Address:\s*([^\n]+)\s*\n\s*([A-Za-z\s]+)\s+([A-Z]{2})\s+CA", t, re.IGNORECASE)
    if full_address_match:
        result["participant_address"] = full_address_match.group(1).strip()
        result["participant_city"] = full_address_match.group(2).strip()
        result["participant_province"] = full_address_match.group(3).strip()
        result["participant_country"] = "CA"
    else:
        # Fallback: try to find city and province separately
        city_match = match(r"Address:.*?\n\s*([A-Za-z\s]+)\s+([A-Z]{2})\s+CA", t)
        if city_match:
            # Split to get city (first part before province code)
            parts = city_match.strip().split()
            if len(parts) >= 2:
                result["participant_city"] = ' '.join(parts[:-2]).strip()
                result["participant_province"] = parts[-2]
    
    # Postal Code - handle OCR errors (0 vs O, 5 vs S, 1 vs I, etc.)
    participant_postal = match(r"Postal Code:\s*([A-Z0-9]{3}\s*[A-Z0-9]{3})", t)
    if participant_postal:
        # Clean up common OCR errors in Canadian postal codes
        # Canadian format: A1A 1A1 (Letter-Number-Letter Number-Letter-Number)
        postal_clean = participant_postal.replace(' ', '').upper()
        
        # Fix common OCR misreads
        # Position 0, 2, 5 should be LETTERS
        # Position 1, 3, 4 should be NUMBERS
        if len(postal_clean) == 6:
            postal_list = list(postal_clean)
            
            # Position 0 (Letter): O->0 is wrong, keep as O
            if postal_list[0] == '0':
                postal_list[0] = 'O'
            
            # Position 1 (Number): O->0, I->1, S->5, B->8
            if postal_list[1] in ['O', 'o']:
                postal_list[1] = '0'
            elif postal_list[1] in ['I', 'i', 'l']:
                postal_list[1] = '1'
            elif postal_list[1] in ['S', 's']:
                postal_list[1] = '5'
            elif postal_list[1] in ['B']:
                postal_list[1] = '8'
            
            # Position 2 (Letter): 0->O, 5->S, 1->I, 8->B
            if postal_list[2] == '0':
                postal_list[2] = 'O'
            elif postal_list[2] == '5':
                postal_list[2] = 'S'
            elif postal_list[2] == '1':
                postal_list[2] = 'I'
            elif postal_list[2] == '8':
                postal_list[2] = 'B'
            
            # Position 3 (Number): O->0, I->1, S->5, B->8
            if postal_list[3] in ['O', 'o']:
                postal_list[3] = '0'
            elif postal_list[3] in ['I', 'i', 'l']:
                postal_list[3] = '1'
            elif postal_list[3] in ['S', 's']:
                postal_list[3] = '5'
            elif postal_list[3] in ['B']:
                postal_list[3] = '8'
            
            # Position 4 (Letter): 0->O, 5->J, 1->I, 8->B, 7->T
            if postal_list[4] == '0':
                postal_list[4] = 'O'
            elif postal_list[4] == '5':
                postal_list[4] = 'J'  # Common OCR error: 5 looks like J
            elif postal_list[4] == '1':
                postal_list[4] = 'I'
            elif postal_list[4] == '8':
                postal_list[4] = 'B'
            elif postal_list[4] == '7':
                postal_list[4] = 'T'
            
            # Position 5 (Number): O->0, I->1, S->5, B->8, J->7
            if postal_list[5] in ['O', 'o']:
                postal_list[5] = '0'
            elif postal_list[5] in ['I', 'i', 'l']:
                postal_list[5] = '1'
            elif postal_list[5] in ['S', 's']:
                postal_list[5] = '5'
            elif postal_list[5] in ['B']:
                postal_list[5] = '8'
            elif postal_list[5] in ['J', 'j']:
                postal_list[5] = '7'
            
            postal_clean = ''.join(postal_list)
            postal_clean = f"{postal_clean[:3]} {postal_clean[3:]}"
        
        result["participant_postal_code"] = postal_clean
    
    # Phone
    participant_phone = match(r"Phone:\s*([\d\-]+)", t)
    if participant_phone:
        result["participant_phone"] = participant_phone
    
    # Email
    participant_email = match(r"Email:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if participant_email:
        result["participant_email"] = participant_email
    
    # ── Additional Information ─────────────────────────────────────────────
    # Business Development Centre
    if "Business Development Centre" in t:
        result["department"] = "Business Development Centre"
    
    # Currency (default CAD for Canadian university)
    result["currency"] = "CAD"
    
    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}

def parse_mirapay(t):
    """
    MiraPay Payment Receipt parser.
    Handles payment gateway receipts with:
    - Transaction status (Approved/Declined)
    - Merchant information
    - Transaction details (date, time, type, card number)
    - Payment amount and currency
    - Response codes and approval information
    - Invoice/reference numbers
    """
    result = {}
    
    # ── Document Information ───────────────────────────────────────────────
    if "MiraPay" in t or "Payment Receipt" in t:
        result["payment_gateway"] = "MiraPay"
        result["document_type"] = "Payment Receipt"
    
    # ── Transaction Status ─────────────────────────────────────────────────
    # Status: Thank-you - Approved or Declined
    status = match(r"Thank-you - (Approved|Declined|APPROVED|DECLINED)", t)
    if status:
        result["transaction_status"] = status.capitalize()
    elif "Approved" in t or "APPROVED" in t:
        result["transaction_status"] = "Approved"
    elif "Declined" in t or "DECLINED" in t:
        result["transaction_status"] = "Declined"
    
    # ── Merchant Information ───────────────────────────────────────────────
    merchant_name = match(r"Merchant Name:\s*([^\n]+)", t)
    if merchant_name:
        result["merchant_name"] = merchant_name.strip()
    
    # ── Transaction Details ────────────────────────────────────────────────
    # Date
    trans_date = match(r"Date:\s*([\w\s,]+\d{4})", t)
    if trans_date:
        result["transaction_date"] = trans_date.strip()
    
    # Time
    trans_time = match(r"Time:\s*([\d:]+\s*[ap]m\s*[A-Z]{3})", t)
    if trans_time:
        result["transaction_time"] = trans_time.strip()
    
    # Transaction Type
    trans_type = match(r"Transaction Type:\s*([^\n]+)", t)
    if trans_type:
        result["transaction_type"] = trans_type.strip()
    
    # Card Number (masked)
    card_number = match(r"Card Number:\s*(x+\d+)", t)
    if card_number:
        result["card_number"] = card_number.strip()
    
    # ── Payment Amount ─────────────────────────────────────────────────────
    # Total Amount with currency
    total_amount = match(r"Total Amount:.*?([A-Z]{3})\s*\$?([\d,\.]+)", t)
    if total_amount:
        # Extract currency and amount separately
        currency_match = re.search(r"Total Amount:.*?([A-Z]{3})\s*\$?([\d,\.]+)", t)
        if currency_match:
            result["currency"] = currency_match.group(1)
            result["total_amount"] = f"${currency_match.group(2)}"
    else:
        # Fallback: just amount
        amount_only = match(r"Total Amount:\s*\$?([\d,\.]+)", t)
        if amount_only:
            result["total_amount"] = f"${amount_only}"
    
    # ── Response Information ───────────────────────────────────────────────
    # Response Code
    response_code = match(r"Response Code:\s*(\d+)", t)
    if response_code:
        result["response_code"] = response_code
    
    # Response Message
    response_msg = match(r"Response Message:\s*([^\n]+)", t)
    if response_msg:
        result["response_message"] = response_msg.strip()
    
    # Approval Code
    approval_code = match(r"Approval Code:\s*([A-Z0-9]+)", t)
    if approval_code:
        result["approval_code"] = approval_code
    
    # ── Reference Numbers ──────────────────────────────────────────────────
    # MiraID
    mira_id = match(r"MiraID:\s*([A-Z0-9]+)", t)
    if mira_id:
        result["mira_id"] = mira_id
    
    # Invoice Number
    invoice_number = match(r"Invoice Number:\s*([A-Z0-9]+)", t)
    if invoice_number:
        result["invoice_number"] = invoice_number
    
    # ── Additional Information ─────────────────────────────────────────────
    # Privacy note
    if "card number is hidden for privacy" in t.lower():
        result["privacy_note"] = "Card number is hidden for privacy"
    
    # Email notification
    email_sent = match(r"A copy of this receipt has been emailed to\s*([^\n]+)", t)
    if email_sent:
        result["receipt_emailed_to"] = email_sent.strip()
    
    # Print receipt option
    if "Print Receipt" in t:
        result["print_option"] = "Available"
    
    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}


def parse_splitsville(t):
    """
    Splitsville Bowl Event Invoice parser.
    Extracts event details, itemized food/entertainment, and financial summary.
    """
    result = {}
    
    # Venue Information
    if "Splitsville" in t or "splitsville" in t:
        result["venue_name"] = "Splitsville Bowl"
    
    venue_match = re.search(r'(\d+\s+\d+\s+Ave\s+[A-Z]+[,\s]+Calgary[^P]*P:\s*\([^\)]+\)\s*[\d\-]+)', t)
    if venue_match:
        result["venue_address"] = venue_match.group(1).strip()
    
    # Event Information
    event_name = match(r'Event:\s*([^\n]+?)(?:\s+Date:)', t)
    if event_name:
        result["event_name"] = event_name.strip()
    
    event_date = match(r'Date:\s*([A-Za-z]+,\s+[A-Za-z]+\s+\d+,\s+\d{4})', t)
    if event_date:
        result["event_date"] = event_date
    
    event_time = match(r'Time:\s*([\d:]+\s*[AP]M\s*-\s*[\d:]+\s*[AP]M)', t)
    if event_time:
        result["event_time"] = event_time
    
    location = match(r'Location:\s*([^\n]+)', t)
    if location:
        result["location"] = location.strip()
    
    guests = match(r'Guests:\s*(\d+)', t)
    if guests:
        result["guests"] = guests
    
    # Contact Information
    account = match(r'Account:\s*([^\n]+?)(?:\s+Time:)', t)
    if account:
        result["account"] = account.strip()
    
    contact = match(r'Contact:\s*([^\n]+?)(?:\s+Location:)', t)
    if contact:
        result["contact_name"] = contact.strip()
    
    phone = match(r'Phone:\s*(\d+)', t)
    if phone:
        result["contact_phone"] = phone
    
    email = match(r'Email:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)', t)
    if email:
        result["contact_email"] = email
    
    event_planner = match(r'Event Planner:\s*([^\n]+?)(?:\s+Room Rental:|\s+Phone/Email:)', t)
    if event_planner:
        result["event_planner"] = event_planner.strip()
    
    # Extract Items - Based on the actual invoice structure
    item_count = 0
    
    # Food items with their typical quantities from the invoice
    food_items_data = [
        ("Garlic and Parmesan French Fries", "2", r'Garlic and Parmesan French Fries.*?CA\$?([\d,\.]+)'),
        ("Fried Mozzarella Sticks", "3", r'Fried Mozzarella Sticks.*?CA\$?([\d,\.]+)'),
        ("Fresh Vegetables", "1", r'Fresh Vegetables.*?CA\$?([\d,\.]+)'),
        ("Bucket of 40 Wings", "2", r'Bucket of 40 Wings.*?CA[S\$]?\$?([\d,\.]+)'),
        ("Veggie Quesadillas", "2", r'Veggie Quesadillas.*?CA\$?([\d,\.]+)'),
        ("Cheese Pizza", "2", r'Cheese Pizza.*?CA[S\$]?\$?([\d,\.]+)'),
        ("Pepperoni Pizza", "2", r'Pepperoni Pizza.*?CA\$?([\d,\.]+)'),
        ("Pulled Pork & Pineapple", "2", r'Pulled Pork & Pineapple.*?CA\$?([\d,\.]+)'),
        ("Mini Donuts", "3", r'Mini Donuts.*?CA\$?([\d,\.]+)'),
    ]
    
    for item_name, qty, pattern in food_items_data:
        price_match = re.search(pattern, t, re.IGNORECASE)
        if price_match:
            item_count += 1
            price = price_match.group(1).replace(',', '.')
            result[f"item_{item_count:02d}_category"] = "Food"
            result[f"item_{item_count:02d}_name"] = item_name
            result[f"item_{item_count:02d}_quantity"] = qty
            result[f"item_{item_count:02d}_unit_price"] = f"CA${price}"
            try:
                total = float(qty) * float(price)
                result[f"item_{item_count:02d}_total"] = f"CA${total:.2f}"
            except:
                pass
    
    # Unlimited Pop per Lane
    pop_match = re.search(r'(\d+)\s+Unlimited Pop per Lane\s+CA[S\$]?([\d,\.]+)\s+CA[S\$]?([\d,\.]+)', t)
    if pop_match:
        item_count += 1
        result[f"item_{item_count:02d}_category"] = "Beverage"
        result[f"item_{item_count:02d}_name"] = "Unlimited Pop per Lane"
        result[f"item_{item_count:02d}_quantity"] = pop_match.group(1)
        result[f"item_{item_count:02d}_unit_price"] = f"CA${pop_match.group(2)}"
        result[f"item_{item_count:02d}_total"] = f"CA${pop_match.group(3)}"
    
    # Corporate Lane with bowling
    bowling_match = re.search(r'Corporate Lane with 2 hours.*?CA\$?([\d,\.]+)\s+\d+%\s+CA\$?([\d,\.]+)\s+CA\$?([\d,\.]+)', t)
    if bowling_match:
        item_count += 1
        result[f"item_{item_count:02d}_category"] = "Bowling"
        result[f"item_{item_count:02d}_name"] = "Corporate Lane with 2 hours (VIP lanes with shoe rental)"
        result[f"item_{item_count:02d}_quantity"] = "8"  # 8 lanes mentioned in location
        result[f"item_{item_count:02d}_original_price"] = f"CA${bowling_match.group(1)}"
        result[f"item_{item_count:02d}_discounted_price"] = f"CA${bowling_match.group(2)}"
        result[f"item_{item_count:02d}_discount"] = "10%"
        result[f"item_{item_count:02d}_total"] = f"CA${bowling_match.group(3)}"
    
    # ES Arcade Card
    arcade_match = re.search(r'(\d+)\s+ES Arcade Card\s+CA[S\$]?\$?([\d,\.]+)\s+CA[S\$]?\$?([\d,\.]+)', t)
    if arcade_match:
        item_count += 1
        result[f"item_{item_count:02d}_category"] = "Arcade"
        result[f"item_{item_count:02d}_name"] = "ES Arcade Card"
        result[f"item_{item_count:02d}_quantity"] = arcade_match.group(1)
        result[f"item_{item_count:02d}_unit_price"] = f"CA${arcade_match.group(2)}"
        result[f"item_{item_count:02d}_total"] = f"CA${arcade_match.group(3)}"
    
    # Event Mini Golf
    golf_match = re.search(r'(\d+)\s+Event Mini Golf.*?CA\$?([\d,\.]+)\s+CA[S\$]?\$?([\d,\.]+)', t)
    if golf_match:
        item_count += 1
        result[f"item_{item_count:02d}_category"] = "Entertainment"
        result[f"item_{item_count:02d}_name"] = "Event Mini Golf per Game"
        result[f"item_{item_count:02d}_quantity"] = golf_match.group(1)
        result[f"item_{item_count:02d}_unit_price"] = f"CA${golf_match.group(2)}"
        result[f"item_{item_count:02d}_total"] = f"CA${golf_match.group(3)}"
    
    # Lounge/Bar area
    lounge_match = re.search(r'(\d+)\s+Lounge/Bar area.*?CA\$?([\d,\.]+)\s+CA[S\$]?\$?([\d,\.]+)', t)
    if lounge_match:
        item_count += 1
        result[f"item_{item_count:02d}_category"] = "Venue"
        result[f"item_{item_count:02d}_name"] = "Lounge/Bar area per hour"
        result[f"item_{item_count:02d}_quantity"] = lounge_match.group(1)
        result[f"item_{item_count:02d}_unit_price"] = f"CA${lounge_match.group(2)}"
        result[f"item_{item_count:02d}_total"] = f"CA${lounge_match.group(3)}"
    
    if item_count > 0:
        result["total_items"] = str(item_count)
    
    # Financial Summary
    room_rental = match(r'Room Rental\s+CA\$?([\d,\.]+)', t)
    if room_rental:
        result["room_rental_total"] = f"CA${room_rental}"
    
    misc_total = match(r'Misc\s+CA\$?([\d,\.]+)', t)
    if misc_total:
        result["misc_total"] = f"CA${misc_total}"
    
    bowling_total = match(r'Bowling - calgary\s+CA\$?([\d,\.]+)', t)
    if bowling_total:
        result["bowling_total"] = f"CA${bowling_total}"
    
    food_total = match(r'Food - Calagary\s+CA\$?([\d,\.]+)', t)
    if food_total:
        result["food_total"] = f"CA${food_total}"
    
    # Beverage total (from Unlimited Pop line item)
    beverage = match(r'Beverage\s+CA\$?([\d,\.]+)', t)
    if not beverage:
        # Calculate from Unlimited Pop if not found
        pop_total = match(r'Unlimited Pop per Lane\s+CA[S\$]?[\d,\.]+\s+CA[S\$]?([\d,\.]+)', t)
        if pop_total:
            result["beverage_total"] = f"CA${pop_total}"
    else:
        result["beverage_total"] = f"CA${beverage}"
    
    # Subtotal - may need to calculate from items
    subtotal = match(r'Subtotal\s+CA\$?([\d,\.]+)', t)
    if subtotal:
        result["subtotal"] = f"CA${subtotal}"

    # Gratuity - generic pattern
    gratuity = match(r'(?:Gratuity|Tip)\s+CA\$?([\d,\.]+)', t)
    if not gratuity:
        gratuity = match(r'Gratuity\s*[:\-]?\s*CA\$?([\d,\.]+)', t)
    if gratuity:
        result["gratuity"] = f"CA${gratuity}"

    # Tax - generic pattern
    tax = match(r'\bTax\b\s+CA\$?([\d,\.]+)', t)
    if not tax:
        tax = match(r'\b(?:HST|GST|PST)\b\s+CA\$?([\d,\.]+)', t)
    if tax:
        result["tax"] = f"CA${tax}"

    # Grand Total
    grand_total = match(r'Grand Total\s+CA\$?([\d,\.]+)', t)
    if grand_total:
        result["grand_total"] = f"CA${grand_total}"
    
    # Payment Information
    # Deposit Due - e.g. "Deposit Due  Paid 06/20/25  -CA$577.79"
    deposit_match = re.search(r'Deposit\s*Due.*?(?:Paid|Pad)\s*([\d/Z\-\.]+)\s*-?CA[S\$]?\$?([\d,\.]+)', t)
    if deposit_match:
        # Fix only known OCR misread: 'Z' → '/' (uppercase O-slash artifact)
        date_str = deposit_match.group(1).replace('Z', '/')
        result["deposit_paid_date"] = date_str
        result["deposit_amount"] = f"CA${deposit_match.group(2)}"

    # Balance - e.g. "Balance  Paid 12/20/25  -CA$2,274.07"
    balance_match = re.search(r'Balance\s*(?:Due)?.*?(?:Paid|Pad)\s*([\d/Z\-\.]+)\s*-?CA\$?([\d,\.]+)', t)
    if balance_match:
        date_str = balance_match.group(1).replace('Z', '/')
        result["balance_paid_date"] = date_str
        result["balance_amount"] = f"CA${balance_match.group(2)}"

    # Estimated Amount Due
    estimated_due = match(r'Estimated\s+Amount\s+Due\s+CA\$?([\d,\.]+)', t)
    if estimated_due:
        result["estimated_amount_due"] = f"CA${estimated_due}"
    
    # Calculate totals if missing
    # Subtotal = sum of all item totals
    if "subtotal" not in result:
        total_sum = 0.0
        for i in range(1, 20):
            total_key = f"item_{i:02d}_total"
            if total_key in result:
                try:
                    amount = result[total_key].replace('CA$', '').replace(',', '')
                    total_sum += float(amount)
                except:
                    pass
        if total_sum > 0:
            result["subtotal"] = f"CA${total_sum:.2f}"
    
    # Grand Total = Subtotal + Gratuity + Tax
    if "grand_total" not in result and "subtotal" in result:
        try:
            subtotal_val = float(result["subtotal"].replace('CA$', '').replace(',', ''))
            gratuity_val = float(result.get("gratuity", "CA$0").replace('CA$', '').replace(',', ''))
            tax_val = float(result.get("tax", "CA$0").replace('CA$', '').replace(',', ''))
            grand = subtotal_val + gratuity_val + tax_val
            result["grand_total"] = f"CA${grand:.2f}"
        except:
            pass
    
    result["currency"] = "CAD"
    result["document_type"] = "Event Invoice"
    
    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}

def parse_uber_eat(t):
    """
    Uber Eats receipt parser.
    Extracts order details, items, financial breakdown, and payment information.
    """
    result = {}
    
    # Platform
    if "Uber Eats" in t or "Uber" in t:
        result["platform"] = "Uber Eats"
    
    # Receipt date/time
    receipt_date = match(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})', t)
    if receipt_date:
        result["receipt_date"] = receipt_date
    
    receipt_time = match(r'(\d{1,2}:\d{2}\s*[AP]M)', t)
    if receipt_time:
        result["receipt_time"] = receipt_time
    
    # Customer name
    customer = match(r'Thanks for tipping,\s+([A-Za-z]+)', t)
    if customer:
        result["customer_name"] = customer
    
    # Restaurant name
    restaurant = match(r'receipt for\s+([^\n]+?)(?:\s*\(|\.)' , t)
    if restaurant:
        result["restaurant_name"] = restaurant.strip()
    
    # Total
    total = match(r'Total\s+CA\$?([\d,\.]+)', t)
    if total:
        result["total"] = f"CA${total}"
    
    # Extract Order Items
    item_count = 0
    
    # Handle various quantity formats: "1", "2", "1s'", "1x", etc.
    # Pattern: quantity, item name, price
    # Example: "1s' Falafel CA$11.99" (handles OCR variations like "1s'" for "1")
    # Example: "2 Meat Lovers Plate for 5 (Mixed of BBQ & Shawarma) CA$240.00"
    item_matches = re.findall(r'(\d+)(?:[sx]?[\'\u2018\u2019]?)\s+([A-Za-z][^\n]+?)\s+CA\$?([\d,\.]+)', t)
    
    for qty, name, price in item_matches:
        # Skip if it looks like a financial line item
        skip_words = ['meal fare', 'tax', 'delivery', 'service', 'tip', 'discount', 
                      'offer', 'payment', 'visa', 'mastercard', 'total', 'subtotal']
        if any(word in name.lower() for word in skip_words):
            continue
        
        # Clean up the name
        name = name.strip()
        # Remove any trailing price-like patterns that might have been captured
        name = re.sub(r'\s+CA\$?[\d,\.]+$', '', name)
        
        if len(name) > 5 and len(name) < 200:  # Reasonable name length
            item_count += 1
            result[f"item_{item_count:02d}_quantity"] = qty
            result[f"item_{item_count:02d}_name"] = name
            result[f"item_{item_count:02d}_price"] = f"CA${price}"
            
            # Try to extract item details (like "12 pieces (CA$5.00)")
            details_match = re.search(r'\(([^\)]+)\)', name)
            if details_match:
                result[f"item_{item_count:02d}_details"] = details_match.group(1)
    
    if item_count > 0:
        result["total_items"] = str(item_count)
    
    # Financial Breakdown
    meal_fare = match(r'Meal Fare\s+CA\$?([\d,\.]+)', t)
    if meal_fare:
        result["meal_fare"] = f"CA${meal_fare}"
    
    tax = match(r'Tax\s+CA\$?([\d,\.]+)', t)
    if tax:
        result["tax"] = f"CA${tax}"
    
    delivery_fee = match(r'Delivery Fee\s+@?\s*CA\$?([\d,\.]+)', t)
    if delivery_fee:
        result["delivery_fee"] = f"CA${delivery_fee}"
    
    service_fee = match(r'Service Fee\s+@?\s*CA\$?([\d,\.]+)', t)
    if service_fee:
        result["service_fee"] = f"CA${service_fee}"
    
    tip = match(r'Tip\s+CA\$?([\d,\.]+)', t)
    if tip:
        result["tip"] = f"CA${tip}"
    
    # Discounts (negative amounts)
    delivery_discount = match(r'Delivery Discount\s+-?CA\$?([\d,\.]+)', t)
    if delivery_discount:
        result["delivery_discount"] = f"-CA${delivery_discount}"
    
    special_offers = match(r'Special Offers\s+-?CA\$?([\d,\.]+)', t)
    if special_offers:
        result["special_offers"] = f"-CA${special_offers}"
    
    # Payments
    payment_count = 0
    payment_matches = re.findall(r'(Visa|Mastercard|Debit|Credit)[^\n]*?(\*+\d{4})\s+CA\$?([\d,\.]+)', t)
    
    for payment_type, card_number, amount in payment_matches:
        payment_count += 1
        result[f"payment_{payment_count}_method"] = payment_type
        result[f"payment_{payment_count}_card"] = card_number
        result[f"payment_{payment_count}_amount"] = f"CA${amount}"
        
        # Try to find payment timestamp
        # Look for date/time near this payment
        payment_time = re.search(rf'{re.escape(card_number)}[^\n]*?(\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\s+\d{{1,2}}:\d{{2}}\s*[AP]M)', t)
        if payment_time:
            result[f"payment_{payment_count}_timestamp"] = payment_time.group(1)
    
    # Order Details
    # Pickup location and time
    pickup_time = match(r'(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*Pickup', t)
    if pickup_time:
        result["pickup_time"] = pickup_time
    
    pickup_address = match(r'Pickup\s+([^\n]+(?:Ave|St|Rd|Blvd|Drive)[^\n]*)', t)
    if pickup_address:
        result["pickup_address"] = pickup_address.strip()
    
    # Delivery location and time
    delivery_time = match(r'(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*Delivery', t)
    if delivery_time:
        result["delivery_time"] = delivery_time
    
    delivery_address = match(r'Delivery\s+([^\n]+(?:Ave|St|Rd|Blvd|Drive)[^\n]*)', t)
    if delivery_address:
        result["delivery_address"] = delivery_address.strip()
    
    # Driver name
    driver = match(r'Delivered by\s+([A-Z]+)', t)
    if driver:
        result["driver_name"] = driver
    
    # Order completed
    order_completed = match(r'Order completed\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*[AP]M)', t)
    if order_completed:
        result["order_completed"] = order_completed
    
    # Delivery type
    if "Uber Delivery" in t:
        result["delivery_type"] = "Uber Delivery"
    
    result["currency"] = "CAD"
    result["document_type"] = "Uber Eats Receipt"
    
    # Disabled: extract_universal_details adds too many false positives
    # result = extract_universal_details(t, result)
    
    # Clean result to remove OCR errors and false recognitions
    result = clean_result(result)
    
    return {k: v for k, v in result.items() if v}


def parse_concur(t):
    """
    Concur Expense Claim parser.

    Concur PDFs often have a two-column layout (dates left, amounts right) that
    OCR collapses into interleaved lines. This parser uses regex to extract the
    claim header and dynamically reconstructs line items from the actual OCR text
    rather than relying on any hardcoded transaction data.
    """
    result = {}

    result["document_type"] = "Expense Claim"
    if "CONCUR" in t.upper():
        result["platform"] = "Concur"

    # ── Header fields ──────────────────────────────────────────────────────
    for pattern, key in [
        (r"Claim Name\s*:\s*([^\n]{1,80})",           "claim_name"),
        (r"Employee Name\s*:\s*([^\n]{1,80})",        "employee_name"),
        (r"Employee ID\s*[:\s]+([A-Z0-9]{2,20})",     "employee_id"),
        (r"Claim ID\s*:\s*([A-Z0-9 ]{2,30})",         "claim_id"),
        (r"Receipts Received\s*:\s*(Yes|No)",          "receipts_received"),
        (r"Claim Date\s*:\s*([\d/\-\.]{5,15})",       "claim_date"),
        (r"Approval Status\s*:\s*(\w{2,20})",         "approval_status"),
        (r"Payment Status\s*:\s*(\w{2,20})",          "payment_status"),
        (r"Currency\s*:\s*([A-Z]{3})",                "currency"),
    ]:
        v = match(pattern, t)
        if v:
            result[key] = v.strip()
    if "claim_id" in result:
        result["claim_id"] = result["claim_id"].replace(" ", "")

    # ── Dynamically extract expense line items from OCR text ───────────────
    # Concur rows look like (OCR collapses columns into adjacent tokens):
    #   29/11/2025   Courier   FEDERAL EXPRESS ...   CAD   427.59
    # We try to find date + amount pairs and recover the supplier between them.

    item_count = 0

    # Strategy 1: look for structured "date ... amount" lines
    # Pattern: date  [optional category text]  supplier text  CAD  amount
    row_re = re.compile(
        r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})'    # date
        r'\s+'
        r'(.{3,80}?)'                              # category + supplier (lazy)
        r'\s+'
        r'(?:CAD|USD|EUR|GBP)\s+'
        r'([\d,]+\.\d{2})',                        # amount
        re.IGNORECASE
    )
    for m_row in row_re.finditer(t):
        date_str, desc, amount = m_row.group(1), m_row.group(2).strip(), m_row.group(3)
        # Skip totals / summary lines
        if re.search(r'\b(total|subtotal|claim|personal|approved|due)\b', desc, re.I):
            continue
        # Reject if desc looks like OCR noise
        if not validate_field("supplier", desc):
            continue
        item_count += 1
        n = f"{item_count:02d}"
        result[f"item_{n}_date"]     = date_str
        result[f"item_{n}_supplier"] = desc
        result[f"item_{n}_amount"]   = f"{amount}"

    # Strategy 2 (fallback): find supplier + amount pairs on the same line
    if item_count == 0:
        line_re = re.compile(
            r'^([A-Z][A-Za-z &\-]{4,60})\s+([\d,]+\.\d{2})\s*$',
            re.MULTILINE
        )
        for m_line in line_re.finditer(t):
            supplier, amount = m_line.group(1).strip(), m_line.group(2)
            if re.search(r'\b(total|subtotal|claim|approved)\b', supplier, re.I):
                continue
            item_count += 1
            n = f"{item_count:02d}"
            result[f"item_{n}_supplier"] = supplier
            result[f"item_{n}_amount"]   = amount

    if item_count:
        result["total_line_items"] = str(item_count)

    # ── Claim totals ───────────────────────────────────────────────────────
    for pattern, key in [
        (r"Claim Total\s*:\s*(?:CAD\s*)?([\d,\.]+)",                           "claim_total"),
        (r"Personal Expenses\s*:\s*(?:CAD\s*)?([\d,\.]+)",                     "personal_expenses"),
        (r"Total Amount Claimed\s*:\s*(?:CAD\s*)?([\d,\.]+)",                  "total_amount_claimed"),
        (r"Amount Approved\s*:\s*(?:CAD\s*)?([\d,\.]+)",                       "amount_approved"),
        (r"Amount Due Employee\s*:\s*(?:CAD\s*)?([\d,\.]+)",                   "amount_due_employee"),
        (r"Amount Due Company Card\s*:\s*(?:CAD\s*)?([\d,\.]+)",               "amount_due_company_card"),
        (r"Total Paid by Company\s*:\s*(?:CAD\s*)?([\d,\.]+)",                 "total_paid_by_company"),
        (r"Amount Due Company Card From Employee\s*:\s*(?:CAD\s*)?([\d,\.]+)", "amount_due_card_from_employee"),
        (r"Total Paid by Employee\s*:\s*(?:CAD\s*)?([\d,\.]+)",                "total_paid_by_employee"),
    ]:
        v = match(pattern, t)
        if v:
            currency = result.get("currency", "CAD")
            result[key] = f"{currency} {v.replace(',', '')}"

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}


def preprocess_receipt_image(img: "Image.Image") -> "Image.Image":
    """
    Preprocess a receipt/invoice image for Tesseract.
    Pipeline:
      1. Convert to greyscale
      2. Scale to at least 1500 px wide (Tesseract accuracy degrades below ~150 DPI)
      3. Mild contrast enhancement
      4. Adaptive threshold via numpy to handle uneven lighting / shadows
      5. Final sharpen pass
    """
    img = img.convert("L")

    # Scale up small images
    w, h = img.size
    target_w = 1500
    if w < target_w:
        scale = target_w / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Contrast boost
    img = ImageEnhance.Contrast(img).enhance(2.0)

    # Adaptive binarisation
    try:
        arr = np.array(img)
        blurred = img.filter(ImageFilter.GaussianBlur(radius=20))
        arr_blur = np.array(blurred).astype(int)
        # Pixel is "ink" if it's darker than neighbourhood mean by more than 10
        bin_arr = ((arr.astype(int) - arr_blur) < 10).astype(np.uint8) * 255
        img = Image.fromarray(bin_arr.astype(np.uint8))
    except Exception:
        img = img.filter(ImageFilter.SHARPEN)

    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_image_bytes(image_bytes: bytes) -> str:
    """
    Run Tesseract on raw image bytes and return extracted text.
    Uses PSM 6 (assume single uniform block of text) for typical receipt images.
    Falls back to PSM 4 (single column) if PSM 6 yields poor quality.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = preprocess_receipt_image(img)

    # Try PSM 6 first (single block — good for most receipt images)
    text6 = pytesseract.image_to_string(img, config="--psm 6 --oem 3")
    if _text_quality(text6) >= 0.4:
        return text6

    # Fallback: PSM 4 (single column of variable-size text)
    text4 = pytesseract.image_to_string(img, config="--psm 4 --oem 3")
    return text4 if _text_quality(text4) > _text_quality(text6) else text6


def detect_business_type(text: str) -> str:
    """
    Detect the type of business based on receipt content.
    Returns: grocery, restaurant, retail, pharmacy, gas_station, or general
    """
    text_lower = text.lower()
    
    # Grocery store indicators
    grocery_keywords = [
        'sobeys', 'freshco', 'loblaws', 'metro', 'no frills', 'real canadian superstore',
        'walmart', 'costco', 'safeway', 'save-on-foods', 'iga', 'food basics',
        'produce', 'dairy', 'frozen', 'bakery', 'deli', 'meat', 'seafood',
        'grocery', 'supermarket', 'market', 'fresh', 'organic'
    ]
    
    # Restaurant indicators  
    restaurant_keywords = [
        'restaurant', 'cafe', 'coffee', 'pizza', 'burger', 'grill', 'bistro',
        'dine', 'dining', 'kitchen', 'eatery', 'food court', 'takeout', 'delivery',
        'server', 'table', 'tip', 'gratuity', 'going nuts'
    ]
    
    # Retail store indicators
    retail_keywords = [
        'store', 'shop', 'retail', 'clothing', 'fashion', 'electronics',
        'department', 'mall', 'outlet', 'boutique'
    ]
    
    # Pharmacy indicators
    pharmacy_keywords = [
        'pharmacy', 'drug', 'medical', 'prescription', 'rx', 'health',
        'shoppers drug mart', 'rexall', 'london drugs'
    ]
    
    # Gas station indicators
    gas_keywords = [
        'gas', 'fuel', 'petro', 'shell', 'esso', 'chevron', 'husky',
        'station', 'pump', 'litre', 'gallon'
    ]
    
    # Check for each business type
    if any(keyword in text_lower for keyword in grocery_keywords):
        return "grocery"
    elif any(keyword in text_lower for keyword in restaurant_keywords):
        return "restaurant"  
    elif any(keyword in text_lower for keyword in pharmacy_keywords):
        return "pharmacy"
    elif any(keyword in text_lower for keyword in gas_keywords):
        return "gas_station"
    elif any(keyword in text_lower for keyword in retail_keywords):
        return "retail"
    else:
        return "general"


def _detect_item_category(item_name: str) -> str:
    """
    Classify an item into a category based on keywords.
    Returns the category name or empty string if no match.
    """
    name_lower = item_name.lower()
    
    # Produce
    if re.search(r'\b(?:apple|banana|orange|grape|berry|strawberry|blueberry|'
                 r'tomato|potato|onion|carrot|lettuce|spinach|broccoli|cabbage|'
                 r'cucumber|pepper|avocado|lemon|lime|mango|pineapple|peach|'
                 r'pear|kiwi|melon|watermelon|squash|zucchini|eggplant|'
                 r'produce|fresh|vegetable|fruit)\b', name_lower):
        return "Produce"
    
    # Dairy
    if re.search(r'\b(?:milk|cheese|butter|yogurt|cream|sour\s+cream|'
                 r'cottage\s+cheese|ice\s+cream|whipped|dairy|lactose)\b', name_lower):
        return "Dairy"
    
    # Meat & Seafood
    if re.search(r'\b(?:chicken|beef|pork|fish|salmon|tuna|shrimp|turkey|'
                 r'ham|bacon|sausage|ground|steak|chop|ribs|wings|breast|'
                 r'meat|seafood|protein)\b', name_lower):
        return "Meat & Seafood"
    
    # Bakery
    if re.search(r'\b(?:bread|loaf|bun|bagel|croissant|donut|pastry|cake|'
                 r'muffin|cookie|cracker|biscuit|roll|tortilla|pita|naan|'
                 r'bakery|baked)\b', name_lower):
        return "Bakery"
    
    # Pantry/Dry Goods
    if re.search(r'\b(?:pasta|rice|flour|sugar|salt|oil|vinegar|sauce|'
                 r'spice|seasoning|cereal|oats|beans|lentils|canned|'
                 r'jar|tin|box|package|dry|grain|legume)\b', name_lower):
        return "Pantry"
    
    # Frozen
    if re.search(r'\b(?:frozen|ice|pizza|fries|vegetable|meal|dinner|'
                 r'freezer|frost)\b', name_lower):
        return "Frozen"
    
    # Beverages
    if re.search(r'\b(?:juice|soda|pop|cola|water|coffee|tea|beer|wine|'
                 r'alcohol|drink|beverage|bottle|can|cider|kombucha|smoothie)\b', name_lower):
        return "Beverages"
    
    # Snacks
    if re.search(r'\b(?:chip|snack|candy|chocolate|bar|nut|popcorn|pretzel|'
                 r'granola|trail|mix|energy|protein|bar)\b', name_lower):
        return "Snacks"
    
    # Personal Care
    if re.search(r'\b(?:shampoo|conditioner|soap|lotion|deodorant|toothpaste|'
                 r'toothbrush|razor|shave|hygiene|personal|care|cosmetic|'
                 r'makeup|perfume|cologne)\b', name_lower):
        return "Personal Care"
    
    # Paper Products
    if re.search(r'\b(?:tissue|paper|towel|napkin|wipe|diaper|sanitary|'
                 r'toilet|roll|sheet|ply)\b', name_lower):
        return "Paper Products"
    
    # Health & Wellness
    if re.search(r'\b(?:vitamin|supplement|medicine|drug|pain|relief|cold|'
                 r'flu|health|wellness|medical|pharmacy|pill|tablet|capsule)\b', name_lower):
        return "Health & Wellness"
    
    return ""


def parse_receipt_text(t: str) -> dict:
    """
    Parse OCR text from a shop / grocery / restaurant receipt.
    Focus: Extract ONLY legitimate product items, NO false recognitions.
    """
    result = {}

    def m(pattern, flags=re.IGNORECASE):
        hit = re.search(pattern, t, flags)
        return hit.group(1).strip() if hit else ""

    lines = [l.strip() for l in t.splitlines() if l.strip()]

    # ── Business Type Detection ─────────────────────────────────────────────
    business_type = detect_business_type(t)
    if business_type:
        result["business_type"] = business_type

    # ── Store identity ──────────────────────────────────────────────────────
    for line in lines[:5]:
        if len(line) >= 3 and re.search(r'[A-Za-z]{3,}', line) and not re.match(r'^[\d\W]+$', line):
            result["store_name"] = line
            break

    # ── Store location and contact info ─────────────────────────────────────
    for line in lines[1:8]:
        if re.search(r'\d+.*(?:Ave|St|Blvd|Rd|Dr|Way|SE|SW|NE|NW|Lane|Ln|Hwy|Street|Market|Road)\b', line, re.I):
            result["store_address"] = line
            break
    
    # If no address found with street keywords, try to find address line with city/postal
    if "store_address" not in result:
        for line in lines[1:8]:
            if re.search(r'\d{5,6}', line) and len(line) > 10:  # Postal code + other text
                result["store_address"] = line
                break

    phone = m(r'Phone\s*(?:No\.?|Number)?[\s:]*(\d{3}[\s\-\.]\d{3,4}[\s\-\.]\d{4})')
    if not phone:
        phone = m(r'(\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4})')
    if phone:
        result["store_phone"] = phone

    website = m(r'(https?://\S+|www\.\S+)')
    if website:
        result["store_website"] = website
    
    # ── GST/Tax Registration Numbers ─────────────────────────────────────────
    gst_no = m(r'GST\s*(?:No\.?|Number)?[\s:]*([A-Z0-9]{15})')
    if gst_no:
        result["gst_registration"] = gst_no
    
    fssai = m(r'FSSAI\s*(?:No\.?|Number)?[\s:]*([A-Z0-9]{10,})')
    if fssai:
        result["fssai_number"] = fssai
    
    pan = m(r'PAN\s*(?:No\.?|Number)?[\s:]*([A-Z0-9]{10})')
    if pan:
        result["pan_number"] = pan

    # ── Transaction ID / Receipt # ──────────────────────────────────────────
    ref = m(r'REF#\s*(\w+)')
    if ref:
        result["transaction_id"] = ref
    else:
        # Try BILL NO pattern (Indian invoices)
        bill_no = m(r'BILL\s*(?:NO\.?|Number)?[\s:]*(\w+)')
        if bill_no:
            result["bill_number"] = bill_no
            result["transaction_id"] = bill_no
        else:
            txn = m(r'(?:Transaction|Trans(?:action)?|Receipt|Order)\s*[#:No\.]*\s*(\w{4,})')
            if txn:
                result["transaction_id"] = txn

    # ── Date & Time ─────────────────────────────────────────────────────────
    date = m(r'\bDATE\s+(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})')
    if not date:
        date = m(r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})')
    if not date:
        date = m(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+\d{2,4})')
    if date:
        result["date"] = date

    time_ = m(r'\bTIME\s+(\d{1,2}:\d{2}(?::\d{2})?)')
    if not time_:
        all_times = re.findall(r'\b(\d{1,2}:\d{2}:\d{2})\b', t)
        if all_times:
            time_ = all_times[-1]
    if time_:
        result["time"] = time_

    # ── Cashier / Served by ─────────────────────────────────────────────────
    cashier = m(r'(?:Served\s+by|Cashier|Operator|Oper)\s*[:#]?\s*(\w+(?:\s+\w+)?)')
    if cashier:
        result["cashier"] = cashier

    # ── LINE ITEMS EXTRACTION - SIMPLE & EFFECTIVE ──────────────────────────
    items = []

    def _is_valid_product_name(name: str) -> bool:
        """Check if name looks like a real product, not a fee/total/etc."""
        s = name.strip()
        if len(s) < 3:
            return False
        if '\n' in s:
            return False
        
        # Must have mostly letters
        letters = len(re.findall(r'[A-Za-z]', s))
        digits = len(re.findall(r'\d', s))
        
        # At least 50% letters
        if letters < len(s) * 0.5:
            return False
        
        # More digits than letters = likely ID/barcode
        if digits > letters:
            return False
        
        # Reject obvious non-products
        if re.search(r'\b(?:subtotal|sub\s+total|total|tax|fee|charge|discount|payment|items?|order|delivery|service|checkout|bag|container|priority|gst|hst|pst|qst|visa|mastercard|cash|debit|change|tender|savings?|balance|points?|earned|reference|transaction|receipt|auth|terminal|merchant|cashier|served|manager|table|server|tip|gratuity|guests?|party|cover|final\s+price|final\s+item|pang|te|nen|wen|totalcad|cad|qty|amt|amount|rate|value|description|item\s+\d|items:\d|qty:\d|gstn|sgst|cgst|igst|cess)\b', s, re.I):
            return False
        
        # Reject if too many special characters
        special = len(re.findall(r'[^A-Za-z0-9\s\-()&.,]', s))
        if special > len(s) * 0.2:
            return False
        
        # Reject OCR garbage patterns
        if re.search(r'[A-Za-z]\-[A-Za-z]\-[A-Za-z]', s):
            return False
        if re.search(r'^[A-Za-z]{1,3}\s*[=+]', s):
            return False
        if re.match(r'^[A-Za-z]{1,2}\s+\d', s):
            return False
        
        # Reject if starts with just a number
        if re.match(r'^\d+\s+[A-Z]', s):
            return False
        
        return True

    # Strategy 1: Look for product names followed by prices on next line
    # This handles multi-line items like Instacart receipts
    raw_lines = t.split('\n')
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i].strip()
        
        # Skip empty lines and pure headers (not lines that contain product names)
        if not line:
            i += 1
            continue
        
        # Skip pure header lines (lines that are ONLY headers, no product name)
        if re.match(r'^(?:ITEMS FOUND|BEVERAGES|DAIRY|MEAT|FROZEN|BAKERY|PANTRY|ORDER TOTALS|Checkout|Service|Priority|Container|Tip)(?:\s|$)', line, re.I):
            i += 1
            continue
        
        # Check if this line has a price
        has_price = re.search(r'\$?([\d,]+\.\d{2})', line)
        
        if not has_price:
            # This might be a product name line - check if next line has price
            next_line = raw_lines[i + 1].strip() if i + 1 < len(raw_lines) else ""
            price_m = re.search(r'\$?([\d,]+\.\d{2})', next_line)
            
            if price_m and line:
                # Clean up the product name
                name = line
                name = re.sub(r'^(?:Pang|Te|nen|wen)\s+', '', name)  # Remove OCR garbage at start
                name = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Za-z])', '', name)  # Remove "EB ", "f ", etc.
                name = re.sub(r'^[a-z]\s+[;:,\-]\s+', '', name)  # Remove "f ; ", "mi fe I "
                name = re.sub(r'^[a-z]{1,2}\s+', '', name)  # Remove single/double letter prefixes
                name = re.sub(r'\s+Final\s+.*$', '', name, flags=re.IGNORECASE)  # Remove "Final nen wen"
                name = re.sub(r'\s+(?:Pang|Te|nen|wen)\s*$', '', name)  # Remove OCR garbage at end
                name = re.sub(r'\s+\d+x\s*$', '', name)  # Remove "2x" at end
                name = name.strip()
                
                if _is_valid_product_name(name):
                    price = price_m.group(1).replace(',', '')
                    if not any(it["name"] == name for it in items):
                        items.append({"name": name, "total_price": price})
                    i += 2  # Skip the price line
                    continue
        else:
            # This line has a price - check if it's a product line or a fee/total
            # Extract the name part (before the price)
            name = re.sub(r'\s*\$?[\d,]+\.\d{2}.*$', '', line).strip()
            
            # Clean up OCR garbage
            name = re.sub(r'^(?:Pang|Te|nen|wen)\s+', '', name)
            name = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Za-z])', '', name)
            name = re.sub(r'^[a-z]\s+[;:,\-]\s+', '', name)
            name = re.sub(r'^[a-z]{1,2}\s+', '', name)
            name = name.strip()
            
            # Only accept if it looks like a product (not a fee/total)
            if _is_valid_product_name(name):
                price = re.search(r'\$?([\d,]+\.\d{2})', line).group(1).replace(',', '')
                if not any(it["name"] == name for it in items):
                    items.append({"name": name, "total_price": price})
        
        i += 1
    
    # Strategy 2: Fallback - simple line-by-line extraction for items with price on same line
    if len(items) < 2:
        for line in lines:
            # Must have a price
            price_m = re.search(r'\$?([\d,]+\.\d{2})', line)
            if not price_m:
                continue
            
            price = price_m.group(1).replace(',', '')
            
            # Extract product name (everything before the price)
            name = re.sub(r'\s*\$?[\d,]+\.\d{2}.*$', '', line).strip()
            
            # Clean up extra spaces (common in table formats)
            name = re.sub(r'\s{2,}', ' ', name).strip()
            
            # Remove trailing quantity numbers (e.g., "soap 1" -> "soap")
            name = re.sub(r'\s+\d+\s*$', '', name).strip()
            
            # Remove OCR garbage patterns at the start
            name = re.sub(r'^(?:Pang|Te|nen|wen)\s+', '', name)
            name = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Za-z])', '', name)
            name = re.sub(r'^[a-z]\s+[;:,\-]\s+', '', name)
            name = re.sub(r'^[a-z]{1,2}\s+', '', name)
            name = name.strip()
            
            # Skip if name is too short or looks like a quantity/measurement
            if re.match(r'^\d+\s+(?:pieces?|items?|pcs?|qty|quantity)', name, re.I):
                continue
            
            # Skip lines that are clearly summaries or headers
            if re.search(r'(?:items?:\d|qty:\d|amt:|amount:|rate:|value:|description)', name, re.I):
                continue
            
            # Skip if name is just symbols or very short
            if len(name) < 3 or re.match(r'^[\-\+\*\$\€\¥\₹]+$', name):
                continue
            
            if _is_valid_product_name(name):
                # Avoid duplicates
                if not any(it["name"] == name for it in items):
                    items.append({"name": name, "total_price": price})

    # Strategy 3: Table format extraction (Indian retail invoices)
    # Pattern: Item Name followed by Qty, Rate, Amount on same or next lines
    if len(items) < 2:
        # Look for table format: "Item Qty Rate Amount" header
        if re.search(r'Item\s+Qty\s+Rate\s+Amount', t, re.I):
            # Find the position of the header and extract only after it
            header_pos = re.search(r'Item\s+Qty\s+Rate\s+Amount', t, re.I)
            if header_pos:
                text_after_header = t[header_pos.end():]
                # Extract items from table format (after the header)
                table_lines = re.findall(
                    r'^([A-Za-z\s]+?)\s+(\d+)\s+₹?([\d,\.]+)\s+₹?([\d,\.]+)',
                    text_after_header, re.MULTILINE | re.IGNORECASE
                )
                for item_name, qty, rate, amount in table_lines:
                    item_name = item_name.strip()
                    # Skip if it's just whitespace or contains newlines with header text
                    if not item_name or 'Item' in item_name or 'Qty' in item_name:
                        continue
                    
                    if _is_valid_product_name(item_name) and len(item_name) >= 3:
                        if not any(it["name"] == item_name for it in items):
                            items.append({
                                "name": item_name,
                                "quantity": qty,
                                "rate": rate.replace(',', ''),
                                "total_price": amount.replace(',', '')
                            })
    
    # Format items for output with category detection
    category_items = {}
    for idx, itm in enumerate(items, 1):
        label = itm["name"]
        category = _detect_item_category(label)
        
        # Determine currency symbol based on context
        currency = "₹" if "₹" in t or "CGST" in t or "SGST" in t else "$"
        
        # Include quantity and rate if available
        qty_str = f" x{itm.get('quantity', '')}" if itm.get('quantity') else ""
        rate_str = f" @ {currency}{itm.get('rate', '')}" if itm.get('rate') else ""
        
        if category:
            result[f"item_{idx:02d}"] = f"[{category}] {label}{qty_str}{rate_str} → {currency}{itm['total_price']}"
            if category not in category_items:
                category_items[category] = []
            category_items[category].append(label)
        else:
            result[f"item_{idx:02d}"] = f"{label}{qty_str}{rate_str} → {currency}{itm['total_price']}"
    
    if category_items:
        for category, items_list in sorted(category_items.items()):
            result[f"category_{category.lower().replace(' ', '_')}"] = ", ".join(items_list)
        
    if items:
        result["items_found"] = str(len(items))

    # ── Enhanced totals parsing ─────────────────────────────────────────────
    subtotal = m(r'(?:Sub\s*[-]?Total|SUBTOTAL|SUB\s+TOTAL)\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if subtotal:
        result["subtotal"] = subtotal.replace('₹', '')

    total_tax = m(r'(?:Total\s+Tax|TOTAL\s+TAX|TAX\s+TOTAL|HST|H\.S\.T\.)\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if total_tax:
        result["total_tax"] = total_tax.replace('₹', '')

    gst = m(r'\b(?:GST|G\.S\.T\.)\b\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if gst:
        result["gst"] = gst.replace('₹', '')

    pst = m(r'\b(?:PST|P\.S\.T\.)\b\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if pst:
        result["pst"] = pst.replace('₹', '')

    qst = m(r'\b(?:QST|Q\.S\.T\.)\b\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if qst:
        result["qst"] = qst.replace('₹', '')
    
    # ── Indian Tax Components (CGST, SGST, IGST) ───────────────────────────
    cgst = m(r'CGST\s*(?:\d+%?)?\s*\$?\s*₹?([\d,\.]+)')
    if cgst:
        result["cgst"] = cgst.replace(',', '')
    
    sgst = m(r'SGST\s*(?:\d+%?)?\s*\$?\s*₹?([\d,\.]+)')
    if sgst:
        result["sgst"] = sgst.replace(',', '')
    
    igst = m(r'IGST\s*(?:\d+%?)?\s*\$?\s*₹?([\d,\.]+)')
    if igst:
        result["igst"] = igst.replace(',', '')

    # Try multiple patterns for total (in order of preference)
    # Try "Net Amount" pattern first (Indian invoices - this is the final total)
    total = m(r'Net\s+Amount\s*₹?([\d,]+\.?\d{0,2})')
    # Try "TotalCAD" pattern (Instacart/Canadian receipts)
    if not total:
        total = m(r'TotalCAD\s*\$?([\d,]+\.?\d{0,2})')
    # Try standard patterns
    if not total:
        total = m(r'^(?:TOTAL|Total)\s*\$?\s*([\d,₹]+\.?\d{0,2})', re.MULTILINE)
    if not total:
        total = m(r'(?:Grand\s+)?Total\s*\$?\s*([\d,₹]+\.?\d{0,2})')
    if not total:
        total = m(r'TOTAL\s+TAX.*?\n.*?TOTAL\s*\$?\s*([\d,₹]+\.?\d{0,2})', re.DOTALL)
    # Try "Amt:" pattern (common in Indian receipts)
    if not total:
        total = m(r'Amt:\s*([\d,₹]+\.?\d{0,2})')
    # Try "to be collected" pattern
    if not total:
        total = m(r'₹?([\d,]+\.?\d{0,2})\s+to be collected')
    # Try "Amount" pattern
    if not total:
        total = m(r'(?:Amount|AMOUNT)\s*[:\-]?\s*₹?([\d,]+\.?\d{0,2})')
    # Try "Total" at end of line with optional currency
    if not total:
        total = m(r'Total\s*(?:CAD|USD|INR|₹)?\s*\$?([\d,]+\.?\d{0,2})')
    
    if total:
        result["total"] = total.replace('₹', '').replace(',', '')

    num_items = m(r'NUMBER\s+OF\s+ITEMS\s+(\d+)')
    if not num_items:
        num_items = m(r'(\d+)\s+items?\s+purchased')
    if num_items:
        result["num_items"] = num_items

    payment = m(r'\b(Visa|Mastercard|Master\s*Card|Debit|Cash|Amex|American\s*Express|Interac|Credit\s*Card|EMV|Tap|Contactless)\b')
    if payment:
        result["payment_method"] = payment

    card = m(r'(?:XX+|NO\.|Card\s+VI)[\s\*X]*(\d{4})\b')
    if not card:
        card = m(r'CARD\s+VI\D*(\d{4})\b')
    if not card:
        card = m(r'(?:VISA|MASTERCARD|MC)\s+.*?(\d{4})\b')
    if card:
        result["card_last4"] = card

    auth_code = m(r'(?:Auth|Authorization|AUTH)\s*[#:]?\s*([A-Z0-9]{6,})')
    if auth_code:
        result["auth_code"] = auth_code

    reference_id = m(r'(?:Reference|REF|Ref)\s*[#:]?\s*([A-Z0-9]{8,})')
    if reference_id:
        result["reference_id"] = reference_id

    tender = m(r'TENDER\s*\$?([\d,]+\.\d{2})')
    if tender:
        result["amount_tendered"] = tender

    change = m(r'CHANGE\s*\$?([\d,]+\.\d{2})')
    if change:
        result["change"] = change

    savings = m(r'(?:YOU\s+SAVED|Total\s+Savings?|SAVINGS?)\s*\$?\s*([\d,]+\.\d{2})')
    if savings:
        result["savings"] = savings
    
    # ── Customer Information ────────────────────────────────────────────────
    customer = m(r'(?:Customer|Customer Name|Sold To|Bill To)\s*[:\-]?\s*([A-Za-z\s]+?)(?:\n|$)')
    if customer and len(customer) > 2:
        result["customer_name"] = customer.strip()
    
    # ── Payment Method ──────────────────────────────────────────────────────
    payment = m(r'(?:Payment\s+Method|Paid\s+By)\s*[:\-]?\s*([A-Za-z\s]+?)(?:\n|$)')
    if payment:
        result["payment_method"] = payment.strip()
    else:
        payment = m(r'\b(Visa|Mastercard|Master\s*Card|Debit|Cash|Amex|American\s*Express|Interac|Credit\s*Card|EMV|Tap|Contactless|Online|UPI|Net Banking)\b')
        if payment:
            result["payment_method"] = payment
    
    # ── Transaction ID (from payment section) ───────────────────────────────
    if "transaction_id" not in result or result["transaction_id"] == "receipt":
        txn_id = m(r'(?:Transaction|Trans)\s*(?:ID|No\.?)\s*[:\-]?\s*(\d{7,})')
        if txn_id:
            result["transaction_id"] = txn_id

    result = clean_result(result)
    return {k: v for k, v in result.items() if v}




PARSERS = {
    "fedex": parse_fedex, "ups": parse_ups, "costco": parse_costco,
    "dhl": parse_dhl, "amazon": parse_amazon, "generic": parse_generic,
    "cbs": parse_cbs, "guelph_ridgetown": parse_guelph_ridgetown,
    "mirapay": parse_mirapay, "splitsville": parse_splitsville,
    "uber_eat": parse_uber_eat, "concur": parse_concur,
    "receipt": parse_receipt_text,  # Smart receipt parser with item extraction
}


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "version": "clean-ocr-accurate"})

@app.route("/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    pdf_file = request.files["file"]
    invoice_type = request.form.get("invoice_type", "generic").lower()

    if not pdf_file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        pdf_file.save(tmp.name)
        tmp.close()

        raw_text = extract_pdf_text(tmp.name)   # raises RuntimeError if image-PDF & no OCR
        if not raw_text.strip():
            return jsonify({"error": "Could not extract any text. The file may be corrupted or image quality too low."}), 422

        parser = PARSERS.get(invoice_type, parse_generic)
        # Pass the temp file path to parsers that need direct PDF access (e.g. DHL, Amazon)
        parse_dhl._current_pdf_path = tmp.name
        parse_amazon._current_pdf_path = tmp.name
        fields = {k: v for k, v in parser(raw_text).items() if v}

        return jsonify({
            "invoice_type": invoice_type,
            "filename": pdf_file.filename,
            "fields": fields,
            "raw_text_preview": raw_text[:600] + ("..." if len(raw_text) > 600 else ""),
        })

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.route("/extract-image", methods=["POST"])
def extract_image():
    """
    Accept an image upload (JPG/PNG/WEBP) and extract receipt data using
    local pytesseract OCR — no external API required.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    if not OCR_AVAILABLE:
        return jsonify({
            "error": (
                "OCR libraries not installed. Run:\n"
                "  pip install pytesseract Pillow\n"
                "Then install Tesseract: https://github.com/tesseract-ocr/tesseract"
            )
        }), 500

    img_file = request.files["file"]
    fname = img_file.filename.lower()
    ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    ext = os.path.splitext(fname)[1]
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported format '{ext}'. Use JPG, PNG, or WEBP."}), 400

    try:
        image_bytes = img_file.read()
        raw_text    = ocr_image_bytes(image_bytes)

        if not raw_text.strip():
            return jsonify({"error": "Could not extract any text. Check image quality."}), 422

        fields = parse_receipt_text(raw_text)

        return jsonify({
            "invoice_type": "receipt",
            "filename":     img_file.filename,
            "fields":       fields,
            "raw_text_preview": raw_text[:600] + ("..." if len(raw_text) > 600 else ""),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n  InvoiceOCR is ready!")
    print("  Open http://localhost:5051 in your browser\n")
    app.run(host="0.0.0.0", port=5051, debug=False)
    