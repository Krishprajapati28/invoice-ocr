# InvoiceOCR — Local Invoice Extractor

No API key required. Runs 100% on your machine.

## Quick Start

1. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Start the backend**
   ```bash
   python bills.py
   ```
   You'll see: `📡 Listening on http://localhost:5050`

3. **Open the UI**
   Open `index.html` in any browser (double-click it).

## Supported Invoice Types

| Type    | Fields Extracted |
|---------|-----------------|
| FedEx   | Tracking ID, sender, recipient, charges, surcharges, GST, total |
| UPS     | Invoice #, tracking, shipper, weight, charges, tax, total |
| Costco  | Store #, date, member #, items, tax, total, payment method |
| DHL     | Waybill #, origin/dest, weight, freight, VAT, total |
| Amazon  | Order ID, seller, items, shipping, promotion, tax, total |
| Generic | Invoice #, date, from/to, subtotal, tax, discount, total |

## How It Works

- **Frontend**: Pure HTML + CSS + JS (no framework, no build step)
- **Backend**: Python Flask server with `pdfplumber` for PDF text extraction
- **Parsing**: Regex-based field extraction tuned per invoice type
- **No data leaves your machine** — everything runs on localhost

## Notes

- Works best on text-based PDFs (not scanned images)
- For scanned/image PDFs, consider adding `pytesseract` for OCR
- The Generic parser works as a fallback for any invoice format
