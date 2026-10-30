'use strict';

/**
 * Render the sample invoices to PDF.
 *
 *   node tools/invoice_gen/generate.js
 *
 * Eight of the nine go out as normal digital PDFs with a real text layer.
 * The ninth is the scanned edge case and is built differently: the page is
 * screenshotted to a PNG and that image is embedded into an otherwise empty
 * PDF, so the result genuinely has **no text layer at all**. No scanner, no
 * OCR stack, and nothing fake about it - `pypdf` finds zero extractable
 * characters, exactly as it would with a real scan.
 *
 * This is a one-off development tool. It is not imported by the application,
 * it is not in `requirements.txt`, and the generated PDFs are committed so
 * nobody needs to run it to use the project.
 */

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

const { INVOICES, compute, inr } = require('./invoices');
const { render } = require('./templates');

const OUT_DIR = path.resolve(__dirname, '..', '..', 'data', 'invoices');

// --------------------------------------------------------------------------- //
// Dependency resolution
// --------------------------------------------------------------------------- //

/**
 * Resolve a module from this folder, then from any extra roots. Lets the tool
 * borrow an existing puppeteer / pdf-lib install instead of insisting on a
 * fresh several-hundred-megabyte download for a script that runs once.
 */
function resolveModule(name, extraRoots = []) {
  try {
    return require(name);
  } catch (_) {
    /* fall through to the extra roots */
  }
  for (const root of extraRoots) {
    try {
      const localRequire = createRequire(path.join(root, 'package.json'));
      return localRequire(name);
    } catch (_) {
      /* keep looking */
    }
  }
  throw new Error(
    `Could not resolve '${name}'.\n` +
      `Run  npm install  inside tools/invoice_gen, or set EXTRA_MODULE_ROOTS to a ` +
      `semicolon-separated list of project folders that already have it installed.`
  );
}

const EXTRA_ROOTS = (process.env.EXTRA_MODULE_ROOTS || '')
  .split(/[;:](?![\\/])/)
  .map((s) => s.trim())
  .filter(Boolean);

const puppeteer = resolveModule('puppeteer', EXTRA_ROOTS);
const pdfLib = resolveModule('pdf-lib', EXTRA_ROOTS);

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);

function findChrome() {
  // Puppeteer's own download, when one exists, is the best answer.
  try {
    const bundled = puppeteer.executablePath();
    if (bundled && fs.existsSync(bundled)) return bundled;
  } catch (_) {
    /* no bundled browser */
  }
  for (const candidate of CHROME_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    'No Chromium-based browser found. Install one, or set CHROME_PATH to a chrome.exe / msedge.exe.'
  );
}

// --------------------------------------------------------------------------- //
// Rendering
// --------------------------------------------------------------------------- //

const PDF_OPTIONS = {
  format: 'A4',
  printBackground: true,
  margin: { top: '0mm', right: '0mm', bottom: '0mm', left: '0mm' },
};

async function renderDigital(page, invoice) {
  await page.setContent(render(invoice), { waitUntil: 'load' });
  return Buffer.from(await page.pdf(PDF_OPTIONS));
}

/**
 * Build a PDF whose only content is a picture of the page.
 *
 * A mild grayscale/contrast filter and a fraction of a degree of rotation make
 * it read like something that came off an office MFP rather than a pristine
 * render. The important property is structural, not cosmetic: there is no text
 * in the file, so provenance cannot be verified and R9 must flag it.
 */
async function renderScanned(page, invoice) {
  const { PDFDocument, rgb, degrees } = pdfLib;

  await page.setContent(render(invoice), { waitUntil: 'load' });
  await page.addStyleTag({
    content: `html { filter: grayscale(1) contrast(1.07) brightness(0.985); }`,
  });

  // ~150 DPI for A4: 210mm x 297mm at 150dpi is about 1240 x 1754 px.
  await page.setViewport({ width: 1240, height: 1754, deviceScaleFactor: 1 });
  const png = Buffer.from(await page.screenshot({ type: 'png', fullPage: true }));

  const pdf = await PDFDocument.create();
  const image = await pdf.embedPng(png);
  const A4 = { width: 595.28, height: 841.89 };   // points
  const sheet = pdf.addPage([A4.width, A4.height]);

  // Faint off-white ground so the scan does not sit on pure white.
  sheet.drawRectangle({
    x: 0, y: 0, width: A4.width, height: A4.height, color: rgb(0.976, 0.973, 0.965),
  });

  const scale = Math.min(A4.width / image.width, A4.height / image.height) * 0.985;
  const drawWidth = image.width * scale;
  const drawHeight = image.height * scale;
  sheet.drawImage(image, {
    x: (A4.width - drawWidth) / 2 + 1.5,
    y: (A4.height - drawHeight) / 2 - 1.5,
    width: drawWidth,
    height: drawHeight,
    rotate: degrees(-0.45),
  });

  return Buffer.from(await pdf.save());
}

/** 'DD/MM/YYYY' -> 'YYYY-MM-DD'. */
function isoDate(printed) {
  const [d, m, y] = printed.split('/');
  return `${y}-${m}-${d}`;
}

/**
 * Emit the true field values of every generated invoice.
 *
 * `invoices.js` is the single source of truth for what these documents say, so
 * the ground truth is written from it rather than transcribed by hand. It feeds
 * `tools/make_fixtures.py`, which turns it into the offline extraction
 * fixtures, and it is the reference for judging extraction accuracy.
 */
function writeGroundTruth(computed) {
  const payload = {
    generated_at: new Date().toISOString(),
    note:
      'True field values of the generated sample invoices, written directly from the ' +
      'generator definitions. Not model output - this is what the documents actually say.',
    invoices: computed.map((inv) => ({
      file: inv.file,
      layout: inv.layout,
      scanned: !!inv.scanned,
      vendor_name: inv.vendor.name,
      vendor_gstin: inv.vendor.gstin,
      invoice_number: inv.invoiceNo,
      invoice_date: inv.date,
      invoice_date_iso: isoDate(inv.date),
      currency: 'INR',
      po_reference: inv.poRef,
      po_placement: inv.poPlacement,
      line_items: inv.items.map((it) => ({
        description: it.desc,
        quantity: it.qty,
        unit_price: it.rate,
        line_total: it.amount,
      })),
      subtotal: inv.subtotal,
      tax_amount: inv.taxMode === 'inclusive' ? null : inv.taxTotal,
      total_amount: inv.total,
      computed_total: inv.computedTotal,
      total_is_misstated: inv.statedTotal != null && inv.statedTotal !== inv.computedTotal,
      expect: inv.expect,
    })),
  };
  const target = path.join(OUT_DIR, 'ground_truth.json');
  fs.writeFileSync(target, JSON.stringify(payload, null, 2) + '\n');
  console.log(`\nground truth: ${target}`);
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const executablePath = findChrome();
  console.log(`browser: ${executablePath}`);
  console.log(`output:  ${OUT_DIR}\n`);

  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
  });

  const rows = [];
  const computed = [];
  try {
    for (const raw of INVOICES) {
      const invoice = compute(raw);
      computed.push(invoice);
      const page = await browser.newPage();
      await page.setViewport({ width: 1240, height: 1754, deviceScaleFactor: 2 });

      const bytes = invoice.scanned
        ? await renderScanned(page, invoice)
        : await renderDigital(page, invoice);

      await page.close();
      fs.writeFileSync(path.join(OUT_DIR, invoice.file), bytes);

      const mismatch =
        invoice.statedTotal != null && invoice.statedTotal !== invoice.computedTotal
          ? ` (computed ${inr(invoice.computedTotal)} - deliberate mismatch)`
          : '';
      rows.push({
        file: invoice.file,
        kind: invoice.scanned ? 'image-only' : 'text',
        total: inr(invoice.total) + mismatch,
        po: invoice.poRef ? `${invoice.poRef} (${invoice.poPlacement})` : 'none',
        expect: invoice.expect.decision,
        kb: Math.round(bytes.length / 1024),
      });
      console.log(
        `  ${invoice.scanned ? '[img]' : '[txt]'} ${invoice.file.padEnd(44)} ` +
          `${String(Math.round(bytes.length / 1024)).padStart(4)} KB  ` +
          `total ${inr(invoice.total).padStart(13)}  expect ${invoice.expect.decision}`
      );
    }
  } finally {
    await browser.close();
  }

  writeGroundTruth(computed);

  console.log(`\n${rows.length} invoice(s) written.`);
  console.log('\nScenario coverage:');
  for (const row of rows) {
    console.log(`  ${row.expect.padEnd(8)} ${row.file.padEnd(44)} PO: ${row.po}`);
  }
}

main().catch((err) => {
  console.error('\nFAILED:', err.message);
  process.exit(1);
});
