'use strict';

/**
 * Render a PNG thumbnail of each sample invoice.
 *
 *   node tools/invoice_gen/thumbs.js
 *
 * Why this exists: the Layouts tab wants to show five invoices side by side so
 * they can be compared at a glance. Embedding the PDFs in <iframe>s puts
 * Chrome's own PDF viewer on the page five times - it brings its own dark
 * surround that cannot be styled from outside, it cannot be cropped reliably,
 * and it is five PDF renderers running to show five static pictures.
 *
 * A thumbnail is a picture, so make it a picture. This renders the same HTML
 * the PDF was generated from and screenshots the top of the page, which is the
 * part that distinguishes one vendor's paperwork from another's - masthead,
 * invoice number, the start of the table.
 *
 * A development tool like the generator beside it. It is not part of the
 * running app and nobody needs to run it: the PNGs it produces are committed.
 */

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

const ROOT = path.join(__dirname, '..', '..');
const OUT = path.join(ROOT, 'data', 'thumbs');

function resolveModule(name) {
  try { return require(name); } catch (_) { /* fall through */ }
  for (const root of [__dirname, ROOT]) {
    try { return createRequire(path.join(root, 'package.json'))(name); } catch (_) { /* keep looking */ }
  }
  throw new Error(`could not resolve '${name}' - run: cd tools/invoice_gen && npm install`);
}

const puppeteer = resolveModule('puppeteer');
const { INVOICES, compute } = require('./invoices');
const { render } = require('./templates');

function browserPath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const candidates = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  return candidates.find((p) => fs.existsSync(p));
}

// One per layout is enough - the tab compares vendors, not individual invoices.
const WANTED = ['meridian', 'sharma', 'nalanda', 'kaveri', 'orbit'];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  const exe = browserPath();
  const launch = { headless: 'new' };
  if (exe) launch.executablePath = exe;
  const browser = await puppeteer.launch(launch);
  const page = await browser.newPage();

  // A4 at 96dpi is 794px wide. Capture the top 560px at 2x so the thumbnail is
  // sharp on a retina screen.
  await page.setViewport({ width: 794, height: 560, deviceScaleFactor: 2 });

  let written = 0;
  for (const key of WANTED) {
    const invoice = INVOICES.find((i) => i.layout === key && !i.scanned)
      || INVOICES.find((i) => i.layout === key);
    if (!invoice) { console.warn(`no invoice for layout ${key}`); continue; }

    // domcontentloaded, not networkidle0: these templates are self-contained,
    // and waiting for the network to go quiet hangs on whatever a layout happens
    // to reference and never resolves. Fonts get a short settle instead.
    await page.setContent(render(compute(invoice)), { waitUntil: 'domcontentloaded' });
    await page.evaluate(() => document.fonts && document.fonts.ready).catch(() => {});
    await new Promise((r) => setTimeout(r, 350));
    const file = path.join(OUT, `${key}.png`);
    await page.screenshot({ file: undefined, path: file, clip: { x: 0, y: 0, width: 794, height: 560 } });
    const kb = Math.round(fs.statSync(file).size / 1024);
    console.log(`  ${key.padEnd(10)} ${String(kb).padStart(4)} KB  ${path.basename(file)}`);
    written += 1;
  }

  await browser.close();
  console.log(`\n${written} thumbnail(s) written to ${OUT}`);
})().catch((err) => { console.error(err); process.exit(1); });
