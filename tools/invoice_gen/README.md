# Sample invoice generator

A one-off development tool. It is **not** part of the running application, is
not in `requirements.txt`, and nobody needs to run it — the nine PDFs it
produces are committed under `data/invoices/`.

```bash
cd tools/invoice_gen
npm install
node generate.js
```

If you already have `puppeteer` and `pdf-lib` installed in another project on
the machine, you can borrow them instead of downloading Chromium again:

```bash
EXTRA_MODULE_ROOTS="/path/to/project-a;/path/to/project-b" node generate.js
```

The script finds a browser in this order: puppeteer's own downloaded Chromium,
then `CHROME_PATH`, then the usual Chrome and Edge install locations. Any
Chromium-based browser works.

## What it produces

Nine invoices across five visually distinct vendor layouts, written to
`data/invoices/`, plus `ground_truth.json` recording what each document
actually says.

The variety is deliberate. If every fixture used one template, the extraction
step would be reading a form rather than reading an invoice, and the demo would
prove nothing about handling real vendor documents. The five layouts differ in
structure, not just colour:

| Layout | Structure | Tax | Where the PO reference hides |
|---|---|---|---|
| Meridian | clean itemised table | CGST + SGST broken out | labelled header field |
| Sharma Steel | traditional bordered, serif | IGST broken out | header block, or a footnote |
| Nalanda | one bundled line, no itemisation | embedded in the rate, never stated | inside a remarks paragraph |
| Kaveri | dense freight table | GST at 5% | a numbered footnote at the bottom |
| Orbit | services table | CGST + SGST broken out | **nowhere** — the document names no PO |

## The scanned invoice

`INV-KAV-7731_kaveri_scanned.pdf` is built differently. The page is rendered,
screenshotted to a PNG, and that image is embedded into an otherwise empty PDF.
The result genuinely has **no text layer**: `pypdf` extracts zero characters
from it, exactly as it would from a real scan. No scanner and no OCR stack were
involved in making it, and none is needed to read it.

Verify with:

```bash
python tools/probe_samples.py
```

## Changing the fixtures

`invoices.js` is the single source of truth for what the documents say.
Amounts, dates, PO references and the expected decision all live there, and
`generate.js` writes `ground_truth.json` from the same definitions.

After changing anything, regenerate downstream artefacts:

```bash
node tools/invoice_gen/generate.js     # PDFs + ground truth
python tools/make_fixtures.py          # offline extraction fixtures
python -m backend.seed --runs --clear  # reprocess and re-assert the scenarios
python tools/check_scenarios.py        # assert each fails for the right rule
```
