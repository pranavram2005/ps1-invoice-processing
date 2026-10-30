'use strict';

/**
 * Drive the UI in a real browser and prove it works.
 *
 *   node tools/ui_check.js [baseUrl] [outDir]
 *
 * Loads the app, runs a scenario, waits for the decision banner, expands every
 * stage, and visits all three tabs - failing on any console error, page error,
 * or failed request along the way. Screenshots land in the output directory so
 * the result can be looked at rather than taken on trust.
 *
 * A development tool, like the invoice generator; not part of the app.
 */

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

const BASE = process.argv[2] || 'http://127.0.0.1:8077';
const OUT = path.resolve(process.argv[3] || path.join(__dirname, '..', 'data', 'ui-check'));

function resolveModule(name, extraRoots = []) {
  try {
    return require(name);
  } catch (_) { /* try the extra roots */ }
  for (const root of extraRoots) {
    try {
      return createRequire(path.join(root, 'package.json'))(name);
    } catch (_) { /* keep looking */ }
  }
  throw new Error(`could not resolve '${name}'`);
}

const EXTRA_ROOTS = (process.env.EXTRA_MODULE_ROOTS || '')
  .split(/[;:](?![\\/])/).map((s) => s.trim()).filter(Boolean);
const puppeteer = resolveModule('puppeteer', EXTRA_ROOTS);

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
].filter(Boolean);

function findChrome() {
  try {
    const bundled = puppeteer.executablePath();
    if (bundled && fs.existsSync(bundled)) return bundled;
  } catch (_) { /* none */ }
  const found = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
  if (!found) throw new Error('no Chromium-based browser found; set CHROME_PATH');
  return found;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: findChrome(),
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 2 });

  const consoleErrors = [];
  const pageErrors = [];
  const badRequests = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('requestfailed', (req) =>
    badRequests.push(`${req.method()} ${req.url()} - ${req.failure()?.errorText}`));
  page.on('response', (res) => {
    if (res.status() >= 400) badRequests.push(`${res.status()} ${res.url()}`);
  });

  const shots = [];
  const overflows = [];

  /**
   * Screenshot, and check the page has not grown sideways. Horizontal overflow
   * is the failure mode this layout is most prone to - one long unbroken string
   * in a grid track widens the whole document - and it is invisible in a
   * full-page screenshot, which simply gets wider.
   */
  const shoot = async (name) => {
    const file = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    shots.push(file);
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    const excess = overflow.scrollWidth - overflow.clientWidth;
    if (excess > 2) {
      overflows.push(`${name}: page is ${excess}px wider than the viewport`);
      console.log(`  shot: ${name}.png  (OVERFLOW +${excess}px)`);
    } else {
      console.log(`  shot: ${name}.png`);
    }
  };

  try {
    console.log(`loading ${BASE}`);
    await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 30000 });

    // The env strip and the sample list are both API-driven; if either is
    // empty the page loaded but the API did not answer.
    await page.waitForSelector('.sample', { timeout: 15000 });
    const sampleCount = await page.$$eval('.sample', (nodes) => nodes.length);
    console.log(`  samples listed: ${sampleCount}`);
    if (sampleCount < 1) throw new Error('no sample scenarios rendered');
    await shoot('01-run-empty');

    // Run the split-PO scenario: it exercises the PO ledger, a REJECT verdict,
    // and a skipped rule, so one run covers most renderers.
    console.log('running the split-PO scenario');
    const target = await page.$$eval('.sample', (nodes) =>
      nodes.findIndex((n) => (n.dataset.file || '').includes('split_po')));
    const buttons = await page.$$('.sample');
    await buttons[target < 0 ? 1 : target].click();

    await page.waitForSelector('.stage.is-running, .stage.is-done', { timeout: 15000 });
    await sleep(1600);
    await shoot('02-run-midflight');

    await page.waitForSelector('#decision:not([hidden])', { timeout: 60000 });
    const verdict = await page.$eval('.decision-verdict', (n) => n.textContent.trim());
    const reasons = await page.$$eval('.reasons li', (nodes) => nodes.length);
    console.log(`  verdict: ${verdict} with ${reasons} reason line(s)`);

    const stageStates = await page.$$eval('.stage', (nodes) =>
      nodes.map((n) => n.className.match(/is-(done|failed|skipped|pending|running)/)?.[1]));
    console.log(`  stage states: ${stageStates.join(', ')}`);
    if (stageStates.includes('pending')) throw new Error('a stage never left pending');

    await page.click('#expand-btn');
    await sleep(500);
    await shoot('03-run-expanded');

    // Provenance markers are the headline detail - confirm they rendered and
    // that hovering one produces the tooltip with its quote.
    const markers = await page.$$('.prov');
    console.log(`  provenance markers: ${markers.length}`);
    if (markers.length) {
      await markers[0].hover();
      await sleep(350);
      const visible = await page.$eval('#tooltip', (n) => !n.hidden && n.textContent.length > 10);
      console.log(`  provenance tooltip shows: ${visible}`);
      if (!visible) throw new Error('provenance tooltip did not appear on hover');
      await shoot('04-provenance-tooltip');
    } else {
      throw new Error('no provenance markers rendered');
    }

    console.log('dashboard');
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForSelector('#tiles .tile', { timeout: 15000 });
    const tileCount = await page.$$eval('#tiles .tile', (n) => n.length);
    const rowCount = await page.$$eval('#runs-table tbody tr', (n) => n.length);
    console.log(`  tiles: ${tileCount}, rows: ${rowCount}`);
    if (tileCount < 6) throw new Error('stat tiles missing');
    if (rowCount < 1) throw new Error('no runs in the dashboard table');
    await shoot('05-dashboard');

    console.log('admin');
    await page.click('.tab[data-tab="admin"]');
    await page.waitForSelector('#rules-list .rule-def', { timeout: 15000 });
    const ruleCount = await page.$$eval('#rules-list .rule-def', (n) => n.length);
    const fieldCount = await page.$$eval('#config-form .field', (n) => n.length);
    const poCount = await page.$$eval('#pos-table tbody tr', (n) => n.length);
    const vendorCount = await page.$$eval('#vendors-table tbody tr', (n) => n.length);
    console.log(`  rules: ${ruleCount}, threshold fields: ${fieldCount}, POs: ${poCount}, vendors: ${vendorCount}`);
    if (ruleCount !== 9) throw new Error(`expected 9 rules in the admin tab, got ${ruleCount}`);
    if (fieldCount !== 8) throw new Error(`expected 8 threshold fields, got ${fieldCount}`);
    if (poCount < 1 || vendorCount < 1) throw new Error('PO or vendor master did not render');
    await shoot('06-admin');

    // Clicking a dashboard row must replay that run from the database.
    console.log('dashboard row -> run replay from the database');
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForSelector('#runs-table tbody tr', { timeout: 10000 });
    await page.click('#runs-table tbody tr');
    await page.waitForSelector('#decision:not([hidden])', { timeout: 20000 });
    const replayed = await page.$eval('.decision-verdict', (n) => n.textContent.trim());
    console.log(`  replayed verdict: ${replayed}`);
    await shoot('07-replayed-from-db');
  } finally {
    await browser.close();
  }

  const problems = [];
  if (overflows.length) problems.push(`horizontal overflow: ${overflows.join('; ')}`);
  if (pageErrors.length) problems.push(`${pageErrors.length} page error(s): ${pageErrors.join(' | ')}`);
  if (consoleErrors.length) problems.push(`${consoleErrors.length} console error(s): ${consoleErrors.join(' | ')}`);
  const realBadRequests = badRequests.filter((r) => !/favicon/.test(r));
  if (realBadRequests.length) {
    problems.push(`${realBadRequests.length} failed request(s): ${realBadRequests.slice(0, 5).join(' | ')}`);
  }

  console.log(`\n${shots.length} screenshot(s) in ${OUT}`);
  if (problems.length) {
    console.error('\nPROBLEMS:');
    problems.forEach((p) => console.error(`  - ${p}`));
    process.exit(1);
  }
  console.log('UI check passed: no console errors, no page errors, no failed requests');
}

main().catch((err) => {
  console.error('\nFAILED:', err.message);
  process.exit(1);
});
