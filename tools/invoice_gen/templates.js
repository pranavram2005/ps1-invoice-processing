'use strict';

/**
 * Five visually distinct vendor layouts.
 *
 * The variety is the point. If every fixture used one template, the extraction
 * step would be reading a form rather than reading an invoice, and the demo
 * would prove nothing about handling real vendor documents. So the five differ
 * in structure, not just colour: itemised vs bundled, tax broken out vs
 * embedded in the rate, and the PO reference sitting in a labelled header
 * field, buried in a remarks paragraph, hidden in a footnote, or absent
 * entirely.
 */

const { BUYER, inr } = require('./invoices');

const SYNTHETIC_FOOTER =
  'Synthetic sample invoice generated for the PS-1 case-study demo - not a real commercial document.';

function esc(text) {
  return String(text == null ? '' : text).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])
  );
}

function base(css, body) {
  return `<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page { size: A4; margin: 0; }
  * { box-sizing: border-box; }
  body { margin: 0; width: 210mm; min-height: 297mm; -webkit-print-color-adjust: exact; }
  .sheet { padding: 16mm 15mm; min-height: 297mm; position: relative; }
  .synthetic { position: absolute; bottom: 7mm; left: 15mm; right: 15mm;
               font-size: 6.6pt; color: #9aa3ad; text-align: center; letter-spacing: .02em; }
  table { width: 100%; border-collapse: collapse; }
  .num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  ${css}
</style></head><body><div class="sheet">${body}
  <div class="synthetic">${SYNTHETIC_FOOTER}</div>
</div></body></html>`;
}

function poHeaderRow(inv, label = 'Purchase Order') {
  if (!inv.poRef || inv.poPlacement !== 'header') return '';
  return `<div><span class="lbl">${label}</span><span class="val">${esc(inv.poRef)}</span></div>`;
}

// --------------------------------------------------------------------------- //
// 1. Meridian - clean modern itemised, CGST/SGST broken out, PO in the header
// --------------------------------------------------------------------------- //

function meridian(inv) {
  const v = inv.vendor;
  const rows = inv.items
    .map(
      (it, i) => `<tr>
      <td class="c">${i + 1}</td>
      <td>${esc(it.desc)}</td>
      <td class="c">${esc(it.hsn)}</td>
      <td class="num">${it.qty}</td>
      <td class="c">${esc(it.uom)}</td>
      <td class="num">${inr(it.rate)}</td>
      <td class="num">${inr(it.amount)}</td></tr>`
    )
    .join('');

  const taxRows = inv.taxLines
    .map((t) => `<tr><td>${esc(t.label)}</td><td class="num">${inr(t.amount)}</td></tr>`)
    .join('');

  return base(
    `
    body { font-family: "Segoe UI", Arial, sans-serif; color: #1f2933; font-size: 9.2pt; }
    .top { display: flex; justify-content: space-between; align-items: flex-start;
           border-bottom: 2.5pt solid #0f766e; padding-bottom: 7mm; }
    .brand { font-size: 19pt; font-weight: 700; color: #0f766e; letter-spacing: -.01em; }
    .brand .sub { font-size: 8pt; font-weight: 400; color: #52606d; letter-spacing: .14em;
                  text-transform: uppercase; margin-top: 2.5mm; }
    .vaddr { font-size: 8.2pt; color: #52606d; line-height: 1.55; margin-top: 3.5mm; }
    .doc { text-align: right; }
    .doc h1 { margin: 0; font-size: 15pt; letter-spacing: .13em; color: #0f766e; font-weight: 600; }
    .meta { margin-top: 4mm; font-size: 8.6pt; }
    .meta div { display: flex; justify-content: flex-end; gap: 5mm; margin-bottom: 1.6mm; }
    .meta .lbl { color: #7b8794; }
    .meta .val { font-weight: 600; min-width: 32mm; text-align: right; }
    .parties { display: flex; gap: 10mm; margin: 8mm 0 6mm; }
    .party { flex: 1; }
    .party .cap { font-size: 7.2pt; letter-spacing: .14em; text-transform: uppercase;
                  color: #7b8794; margin-bottom: 2mm; }
    .party .nm { font-weight: 600; font-size: 9.8pt; }
    .party .ln { font-size: 8.2pt; color: #52606d; line-height: 1.55; }
    thead th { background: #f0fdfa; color: #115e59; font-size: 7.6pt; letter-spacing: .07em;
               text-transform: uppercase; padding: 2.6mm 2mm; text-align: left;
               border-bottom: 1pt solid #99f6e4; }
    tbody td { padding: 2.4mm 2mm; border-bottom: .4pt solid #e4e7eb; vertical-align: top; }
    td.c { text-align: center; }
    .totals { display: flex; justify-content: flex-end; margin-top: 6mm; }
    .totals table { width: 78mm; }
    .totals td { padding: 1.9mm 2mm; font-size: 9pt; }
    .totals tr.sub td { border-top: .8pt solid #cbd2d9; font-weight: 600; }
    .totals tr.grand td { border-top: 1.6pt solid #0f766e; border-bottom: 1.6pt solid #0f766e;
                          font-weight: 700; font-size: 11pt; color: #0f766e; }
    .words { margin-top: 5mm; font-size: 8.4pt; color: #52606d; }
    .foot { margin-top: 9mm; display: flex; justify-content: space-between; gap: 10mm;
            font-size: 7.8pt; color: #52606d; line-height: 1.6; }
    .foot .bank b { color: #1f2933; }
    .sign { text-align: right; min-width: 50mm; }
    .sign .line { margin-top: 15mm; border-top: .6pt solid #9aa3ad; padding-top: 1.6mm; }
  `,
    `
    <div class="top">
      <div>
        <div class="brand">${esc(v.name)}<div class="sub">Office &amp; Stationery Supplies</div></div>
        <div class="vaddr">${v.address.map(esc).join('<br>')}<br>
          GSTIN ${esc(v.gstin)} &nbsp;&middot;&nbsp; PAN ${esc(v.pan)}<br>
          ${esc(v.phone)} &nbsp;&middot;&nbsp; ${esc(v.email)}</div>
      </div>
      <div class="doc">
        <h1>TAX INVOICE</h1>
        <div class="meta">
          <div><span class="lbl">Invoice No.</span><span class="val">${esc(inv.invoiceNo)}</span></div>
          <div><span class="lbl">Invoice Date</span><span class="val">${esc(inv.date)}</span></div>
          <div><span class="lbl">Due Date</span><span class="val">${esc(inv.dueDate)}</span></div>
          ${poHeaderRow(inv)}
        </div>
      </div>
    </div>

    <div class="parties">
      <div class="party">
        <div class="cap">Billed to</div>
        <div class="nm">${esc(BUYER.name)}</div>
        <div class="ln">${BUYER.address.map(esc).join('<br>')}<br>GSTIN ${esc(BUYER.gstin)}</div>
      </div>
      <div class="party">
        <div class="cap">Ship to</div>
        <div class="nm">${esc(BUYER.name)}</div>
        <div class="ln">${BUYER.address.map(esc).join('<br>')}<br>Attn: Stores &amp; Receiving</div>
      </div>
    </div>

    <table>
      <thead><tr>
        <th style="width:8mm">#</th><th>Description</th><th style="width:14mm">HSN</th>
        <th style="width:16mm" class="num">Qty</th><th style="width:16mm">UOM</th>
        <th style="width:22mm" class="num">Rate</th><th style="width:26mm" class="num">Amount</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>

    <div class="totals"><table>
      <tr class="sub"><td>Taxable value</td><td class="num">${inr(inv.subtotal)}</td></tr>
      ${taxRows}
      <tr class="grand"><td>Total payable</td><td class="num">INR ${inr(inv.total)}</td></tr>
    </table></div>

    <div class="words">${esc(inv.terms)}</div>

    <div class="foot">
      <div class="bank">
        <b>Bank details for NEFT/RTGS</b><br>
        ${esc(v.bank.name)}<br>A/c ${esc(v.bank.account)} &nbsp;&middot;&nbsp; IFSC ${esc(v.bank.ifsc)}
      </div>
      <div class="sign">For ${esc(v.name)}<div class="line">Authorised Signatory</div></div>
    </div>
  `
  );
}

// --------------------------------------------------------------------------- //
// 2. Sharma Steel - traditional industrial, serif, boxed, IGST, PO header or footnote
// --------------------------------------------------------------------------- //

function sharma(inv) {
  const v = inv.vendor;
  const rows = inv.items
    .map(
      (it, i) => `<tr>
      <td class="c">${i + 1}</td>
      <td>${esc(it.desc)}</td>
      <td class="c">${esc(it.hsn)}</td>
      <td class="num">${it.qty.toLocaleString('en-IN')}</td>
      <td class="c">${esc(it.uom)}</td>
      <td class="num">${inr(it.rate)}</td>
      <td class="num">${inr(it.amount)}</td></tr>`
    )
    .join('');

  const taxRows = inv.taxLines
    .map(
      (t) =>
        `<tr><td colspan="6" class="rlbl">${esc(t.label)}</td><td class="num">${inr(t.amount)}</td></tr>`
    )
    .join('');

  const footnote =
    inv.poRef && inv.poPlacement === 'footnote'
      ? `<div class="fnote">* Billed against Purchase Order ${esc(inv.poRef)} and our quotation
         SSW/Q/2026/0914. Measurement sheets countersigned by your site engineer are enclosed.</div>`
      : '';

  return base(
    `
    body { font-family: "Georgia", "Times New Roman", serif; color: #11151c; font-size: 9pt; }
    .hdr { text-align: center; border: 1.4pt solid #11325c; padding: 5mm 4mm 4mm; }
    .hdr .nm { font-size: 17pt; font-weight: 700; color: #11325c; letter-spacing: .01em; }
    .hdr .tag { font-size: 8pt; font-style: italic; color: #4a5568; margin-top: 1.4mm; }
    .hdr .ad { font-size: 8pt; margin-top: 2.6mm; line-height: 1.5; }
    .title { text-align: center; font-size: 11.5pt; font-weight: 700; letter-spacing: .3em;
             margin: 5mm 0 4mm; color: #11325c; }
    .grid { display: flex; border: 1pt solid #11325c; }
    .grid > div { padding: 3.4mm 4mm; font-size: 8.4pt; line-height: 1.6; }
    .grid .left { flex: 1.35; border-right: 1pt solid #11325c; }
    .grid .right { flex: 1; }
    .cap { font-size: 7pt; letter-spacing: .12em; text-transform: uppercase; color: #4a5568; }
    .nm2 { font-weight: 700; font-size: 9.4pt; }
    .kv { display: flex; justify-content: space-between; gap: 4mm; margin-bottom: 1.3mm; }
    .kv .lbl { color: #4a5568; }
    .kv .val { font-weight: 700; }
    table.items { border: 1pt solid #11325c; border-top: none; }
    table.items thead th { background: #11325c; color: #fff; font-size: 7.4pt; letter-spacing: .08em;
                           text-transform: uppercase; padding: 2.4mm 2mm; text-align: left;
                           font-family: "Segoe UI", Arial, sans-serif; font-weight: 600; }
    table.items tbody td { padding: 2.3mm 2mm; border-bottom: .4pt solid #c8cfda; vertical-align: top; }
    table.items td.c { text-align: center; }
    .rlbl { text-align: right; font-weight: 700; color: #4a5568; }
    tr.sub td { border-top: 1pt solid #11325c; font-weight: 700; }
    tr.grand td { background: #eef2f8; font-weight: 700; font-size: 10.5pt; color: #11325c;
                  border-top: 1.2pt solid #11325c; }
    .fnote { margin-top: 4mm; font-size: 7.8pt; font-style: italic; color: #4a5568; line-height: 1.6; }
    .terms { margin-top: 4mm; font-size: 8.2pt; }
    .bottom { margin-top: 8mm; display: flex; justify-content: space-between; gap: 8mm; font-size: 7.8pt; }
    .bottom .line { margin-top: 14mm; border-top: .6pt solid #11151c; padding-top: 1.5mm;
                    text-align: center; width: 52mm; }
    .decl { flex: 1; line-height: 1.6; color: #4a5568; }
  `,
    `
    <div class="hdr">
      <div class="nm">${esc(v.name)}</div>
      <div class="tag">Structural Steel &middot; Fabrication &middot; Erection</div>
      <div class="ad">${v.address.map(esc).join(', ')}<br>
        Tel ${esc(v.phone)} &nbsp;|&nbsp; ${esc(v.email)}<br>
        <b>GSTIN ${esc(v.gstin)}</b> &nbsp;|&nbsp; PAN ${esc(v.pan)}</div>
    </div>

    <div class="title">TAX INVOICE</div>

    <div class="grid">
      <div class="left">
        <div class="cap">Buyer (bill to)</div>
        <div class="nm2">${esc(BUYER.name)}</div>
        ${BUYER.address.map(esc).join('<br>')}<br>
        GSTIN ${esc(BUYER.gstin)}
      </div>
      <div class="right">
        <div class="kv"><span class="lbl">Invoice No.</span><span class="val">${esc(inv.invoiceNo)}</span></div>
        <div class="kv"><span class="lbl">Dated</span><span class="val">${esc(inv.date)}</span></div>
        <div class="kv"><span class="lbl">Payment due</span><span class="val">${esc(inv.dueDate)}</span></div>
        ${
          inv.poRef && inv.poPlacement === 'header'
            ? `<div class="kv"><span class="lbl">Buyer's Order No.</span><span class="val">${esc(inv.poRef)}</span></div>`
            : ''
        }
        <div class="kv"><span class="lbl">Place of supply</span><span class="val">Karnataka (29)</span></div>
      </div>
    </div>

    <table class="items">
      <thead><tr>
        <th style="width:8mm">Sl</th><th>Description of Goods / Services</th>
        <th style="width:14mm">HSN/SAC</th><th style="width:19mm" class="num">Qty</th>
        <th style="width:17mm">UOM</th><th style="width:20mm" class="num">Rate</th>
        <th style="width:26mm" class="num">Amount</th>
      </tr></thead>
      <tbody>
        ${rows}
        <tr class="sub"><td colspan="6" class="rlbl">Taxable Value</td><td class="num">${inr(inv.subtotal)}</td></tr>
        ${taxRows}
        <tr class="grand"><td colspan="6" class="rlbl">Grand Total (INR)</td><td class="num">${inr(inv.total)}</td></tr>
      </tbody>
    </table>

    ${footnote}
    <div class="terms">${esc(inv.terms)}</div>

    <div class="bottom">
      <div class="decl">
        <b>Declaration:</b> We declare that this invoice shows the actual price of the goods
        described and that all particulars are true and correct.<br><br>
        <b>Bank:</b> ${esc(v.bank.name)}, A/c ${esc(v.bank.account)}, IFSC ${esc(v.bank.ifsc)}
      </div>
      <div><div style="text-align:center">For ${esc(v.name)}</div>
        <div class="line">Authorised Signatory</div></div>
    </div>
  `
  );
}

// --------------------------------------------------------------------------- //
// 3. Nalanda - compact, one bundled line, tax inclusive, PO inside a remarks paragraph
// --------------------------------------------------------------------------- //

function nalanda(inv) {
  const v = inv.vendor;
  const rows = inv.items
    .map(
      (it, i) => `<tr>
      <td class="c">${i + 1}</td>
      <td>${esc(it.desc)}<div class="hsn">HSN/SAC ${esc(it.hsn)}</div></td>
      <td class="c">${it.qty} ${esc(it.uom)}</td>
      <td class="num">${inr(it.amount)}</td></tr>`
    )
    .join('');

  const remarks =
    inv.poRef && inv.poPlacement === 'remarks'
      ? `Work carried out against your Purchase Order ${esc(inv.poRef)} dated 01-09-2026 and the
         art-work approval received by email on 29-09-2026. ${esc(inv.terms)}`
      : esc(inv.terms);

  return base(
    `
    body { font-family: "Trebuchet MS", "Segoe UI", sans-serif; color: #241c18; font-size: 9.4pt; }
    .band { background: #b8470f; color: #fff; padding: 6mm 7mm; display: flex;
            justify-content: space-between; align-items: flex-end; }
    .band .nm { font-size: 18pt; font-weight: 700; letter-spacing: -.01em; }
    .band .sub { font-size: 7.6pt; letter-spacing: .18em; text-transform: uppercase;
                 opacity: .9; margin-top: 1.6mm; }
    .band .right { text-align: right; font-size: 8pt; line-height: 1.6; }
    .band .right .big { font-size: 12pt; font-weight: 700; letter-spacing: .18em; }
    .strip { display: flex; background: #fdf3ec; border-bottom: 1pt solid #f0cdb4;
             padding: 3.6mm 7mm; font-size: 8.4pt; gap: 8mm; }
    .strip div span { color: #8a6a57; }
    .strip div b { display: block; font-size: 9.4pt; }
    .body2 { padding: 6mm 7mm 0; }
    .to .cap { font-size: 7pt; letter-spacing: .14em; text-transform: uppercase; color: #8a6a57; }
    .to .nm { font-weight: 700; font-size: 10pt; margin-top: 1.4mm; }
    .to .ln { font-size: 8.4pt; color: #5c4a40; line-height: 1.55; }
    table.it { margin-top: 6mm; }
    table.it thead th { border-bottom: 1.4pt solid #b8470f; font-size: 7.4pt; letter-spacing: .1em;
                        text-transform: uppercase; color: #b8470f; padding: 2.4mm 2mm; text-align: left; }
    table.it tbody td { padding: 3.4mm 2mm; border-bottom: .4pt solid #ead9ce; vertical-align: top; }
    table.it td.c { text-align: center; }
    .hsn { font-size: 7.4pt; color: #8a6a57; margin-top: 1.4mm; }
    .tot { margin-top: 5mm; display: flex; justify-content: flex-end; }
    .tot .box { background: #fdf3ec; border-left: 3pt solid #b8470f; padding: 3.6mm 5mm;
                min-width: 72mm; display: flex; justify-content: space-between; align-items: center; }
    .tot .box .l { font-size: 8.4pt; letter-spacing: .08em; text-transform: uppercase; color: #8a6a57; }
    .tot .box .r { font-size: 13pt; font-weight: 700; color: #b8470f; }
    .incl { margin-top: 2.4mm; text-align: right; font-size: 7.8pt; color: #8a6a57; font-style: italic; }
    .rem { margin-top: 7mm; font-size: 8.4pt; line-height: 1.7; color: #3d3029;
           border-top: .5pt dashed #d8bca8; padding-top: 4mm; }
    .rem .cap { font-size: 7pt; letter-spacing: .14em; text-transform: uppercase;
                color: #8a6a57; display: block; margin-bottom: 1.6mm; }
    .end { margin-top: 8mm; display: flex; justify-content: space-between;
           font-size: 7.8pt; color: #5c4a40; }
  `,
    `
    <div class="band">
      <div><div class="nm">${esc(v.name)}</div><div class="sub">Commercial Printing &amp; Packaging</div></div>
      <div class="right"><div class="big">INVOICE</div>
        ${esc(v.phone)}<br>${esc(v.email)}</div>
    </div>
    <div class="strip">
      <div><span>Invoice No.</span><b>${esc(inv.invoiceNo)}</b></div>
      <div><span>Date</span><b>${esc(inv.date)}</b></div>
      <div><span>Due</span><b>${esc(inv.dueDate)}</b></div>
      <div><span>GSTIN</span><b>${esc(v.gstin)}</b></div>
    </div>

    <div class="body2">
      <div class="to">
        <div class="cap">Invoice to</div>
        <div class="nm">${esc(BUYER.name)}</div>
        <div class="ln">${BUYER.address.map(esc).join(', ')}<br>GSTIN ${esc(BUYER.gstin)}</div>
      </div>

      <table class="it">
        <thead><tr><th style="width:8mm">#</th><th>Particulars</th>
          <th style="width:24mm" class="c">Quantity</th><th style="width:30mm" class="num">Amount</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>

      <div class="tot"><div class="box">
        <span class="l">Total payable</span><span class="r">Rs. ${inr(inv.total)}</span>
      </div></div>
      <div class="incl">Amount is inclusive of GST at 18% as per our approved rate card.</div>

      <div class="rem"><span class="cap">Remarks</span>${remarks}</div>

      <div class="end">
        <div>${esc(v.bank.name)}<br>A/c ${esc(v.bank.account)} &middot; IFSC ${esc(v.bank.ifsc)}</div>
        <div style="text-align:right">For ${esc(v.name)}<br><br><br>Proprietor</div>
      </div>
    </div>
  `
  );
}

// --------------------------------------------------------------------------- //
// 4. Kaveri - utilitarian freight bill, dense, PO reference down in a footnote
// --------------------------------------------------------------------------- //

function kaveri(inv) {
  const v = inv.vendor;
  const rows = inv.items
    .map(
      (it, i) => `<tr>
      <td class="c">${i + 1}</td>
      <td>${esc(it.desc)}</td>
      <td class="c">${esc(it.hsn)}</td>
      <td class="num">${it.qty}</td>
      <td class="c">${esc(it.uom)}</td>
      <td class="num">${inr(it.rate)}</td>
      <td class="num">${inr(it.amount)}</td></tr>`
    )
    .join('');

  const taxRows = inv.taxLines
    .map(
      (t) =>
        `<tr><td colspan="6" class="rl">${esc(t.label)}</td><td class="num">${inr(t.amount)}</td></tr>`
    )
    .join('');

  const footnote =
    inv.poRef && inv.poPlacement === 'footnote'
      ? `<li>Ref: ${esc(inv.poRef)} / transport rate agreement dated 20-09-2026. Trip sheets and
         lorry receipts attached as annexure A.</li>`
      : '';

  return base(
    `
    body { font-family: "Arial Narrow", Arial, sans-serif; color: #10171f; font-size: 8.8pt; }
    .rule { border-top: 3pt double #16304a; margin-bottom: 4mm; }
    .hd { display: flex; justify-content: space-between; align-items: flex-start; }
    .hd .nm { font-size: 15.5pt; font-weight: 700; color: #16304a; text-transform: uppercase;
              letter-spacing: .02em; }
    .hd .ad { font-size: 7.8pt; line-height: 1.5; margin-top: 2mm; }
    .hd .bx { border: 1pt solid #16304a; padding: 2.6mm 3.4mm; text-align: center; min-width: 46mm; }
    .hd .bx .t { font-size: 9.6pt; font-weight: 700; letter-spacing: .16em; color: #16304a; }
    .hd .bx .no { font-size: 11pt; font-weight: 700; margin-top: 1.6mm; }
    .hd .bx .dt { font-size: 7.6pt; color: #4a5a6a; margin-top: 1mm; }
    .meta2 { display: flex; gap: 0; margin: 5mm 0 0; border: .8pt solid #a8b4c0; }
    .meta2 > div { flex: 1; padding: 2.6mm 3mm; border-right: .8pt solid #a8b4c0; font-size: 7.8pt; }
    .meta2 > div:last-child { border-right: none; }
    .meta2 .k { color: #4a5a6a; font-size: 6.9pt; letter-spacing: .09em; text-transform: uppercase; }
    .meta2 .v { font-weight: 700; font-size: 8.6pt; margin-top: .8mm; }
    .consignee { margin-top: 4mm; font-size: 8.2pt; line-height: 1.5; }
    .consignee b { font-size: 9pt; }
    table.fr { margin-top: 4mm; border: .8pt solid #16304a; }
    table.fr thead th { background: #e8edf3; border-bottom: .8pt solid #16304a; font-size: 7pt;
                        letter-spacing: .07em; text-transform: uppercase; padding: 2mm;
                        text-align: left; color: #16304a; }
    table.fr tbody td { padding: 2.2mm 2mm; border-bottom: .35pt solid #cfd8e1; }
    table.fr td.c { text-align: center; }
    .rl { text-align: right; font-weight: 700; color: #4a5a6a; }
    tr.st td { border-top: .8pt solid #16304a; font-weight: 700; }
    tr.gt td { background: #16304a; color: #fff; font-weight: 700; font-size: 10pt; }
    .notes { margin-top: 4mm; font-size: 7.6pt; color: #33414f; }
    .notes ol { margin: 1.6mm 0 0 4.5mm; padding: 0; line-height: 1.65; }
    .sgn { margin-top: 7mm; display: flex; justify-content: space-between; font-size: 7.6pt; }
  `,
    `
    <div class="rule"></div>
    <div class="hd">
      <div>
        <div class="nm">${esc(v.name)}</div>
        <div class="ad">${v.address.map(esc).join('<br>')}<br>
          Ph ${esc(v.phone)} &nbsp;&middot;&nbsp; ${esc(v.email)}<br>
          GSTIN ${esc(v.gstin)} &nbsp;&middot;&nbsp; PAN ${esc(v.pan)}</div>
      </div>
      <div class="bx">
        <div class="t">TAX INVOICE</div>
        <div class="no">${esc(inv.invoiceNo)}</div>
        <div class="dt">Dated ${esc(inv.date)}</div>
      </div>
    </div>

    <div class="meta2">
      <div><div class="k">Invoice date</div><div class="v">${esc(inv.date)}</div></div>
      <div><div class="k">Payment due</div><div class="v">${esc(inv.dueDate)}</div></div>
      <div><div class="k">Service</div><div class="v">Goods transport by road</div></div>
      <div><div class="k">Place of supply</div><div class="v">Karnataka (29)</div></div>
    </div>

    <div class="consignee">
      <b>${esc(BUYER.name)}</b><br>
      ${BUYER.address.map(esc).join(', ')}<br>
      GSTIN ${esc(BUYER.gstin)}
    </div>

    <table class="fr">
      <thead><tr>
        <th style="width:7mm">Sl</th><th>Particulars of service</th><th style="width:13mm">SAC</th>
        <th style="width:14mm" class="num">Qty</th><th style="width:16mm">Unit</th>
        <th style="width:20mm" class="num">Rate</th><th style="width:25mm" class="num">Amount</th>
      </tr></thead>
      <tbody>
        ${rows}
        <tr class="st"><td colspan="6" class="rl">Freight value</td><td class="num">${inr(inv.subtotal)}</td></tr>
        ${taxRows}
        <tr class="gt"><td colspan="6" class="rl" style="color:#fff">Total (INR)</td><td class="num">${inr(inv.total)}</td></tr>
      </tbody>
    </table>

    <div class="notes"><b>Notes</b>
      <ol>
        <li>${esc(inv.terms)}</li>
        ${footnote}
        <li>Any discrepancy to be reported within 7 days of receipt of this invoice.</li>
      </ol>
    </div>

    <div class="sgn">
      <div>${esc(v.bank.name)}<br>A/c ${esc(v.bank.account)} &middot; IFSC ${esc(v.bank.ifsc)}</div>
      <div style="text-align:right">For ${esc(v.name)}<br><br><br>Authorised Signatory</div>
    </div>
  `
  );
}

// --------------------------------------------------------------------------- //
// 5. Orbit - services invoice with no PO field anywhere on the document
// --------------------------------------------------------------------------- //

function orbit(inv) {
  const v = inv.vendor;
  const rows = inv.items
    .map(
      (it) => `<tr>
      <td>${esc(it.desc)}<div class="sac">SAC ${esc(it.hsn)}</div></td>
      <td class="num">${it.qty.toLocaleString('en-IN')}</td>
      <td class="c">${esc(it.uom)}</td>
      <td class="num">${inr(it.rate)}</td>
      <td class="num">${inr(it.amount)}</td></tr>`
    )
    .join('');

  const taxRows = inv.taxLines
    .map((t) => `<tr><td>${esc(t.label)}</td><td class="num">${inr(t.amount)}</td></tr>`)
    .join('');

  return base(
    `
    body { font-family: "Verdana", "Segoe UI", sans-serif; color: #1c1a2e; font-size: 8.8pt; }
    .head { display: flex; gap: 8mm; align-items: center; padding-bottom: 5mm;
            border-bottom: 1pt solid #ded9f0; }
    .logo { width: 17mm; height: 17mm; border-radius: 50%; background: #4c35a8; color: #fff;
            display: flex; align-items: center; justify-content: center;
            font-size: 15pt; font-weight: 700; font-family: Arial, sans-serif; }
    .head .nm { font-size: 15pt; font-weight: 700; color: #4c35a8; }
    .head .sub { font-size: 7.6pt; color: #6b6585; margin-top: 1mm; letter-spacing: .1em;
                 text-transform: uppercase; }
    .head .right { margin-left: auto; text-align: right; font-size: 7.8pt;
                   color: #6b6585; line-height: 1.6; }
    .title2 { margin: 6mm 0 5mm; display: flex; justify-content: space-between; align-items: flex-end; }
    .title2 h1 { margin: 0; font-size: 13pt; letter-spacing: .14em; color: #4c35a8; font-weight: 700; }
    .title2 .per { font-size: 8.4pt; color: #6b6585; }
    .cards { display: flex; gap: 5mm; }
    .card { flex: 1; background: #f6f4fd; border-radius: 2mm; padding: 4mm 4.5mm; }
    .card .cap { font-size: 6.9pt; letter-spacing: .13em; text-transform: uppercase;
                 color: #8b84a8; margin-bottom: 1.8mm; }
    .card .nm { font-weight: 700; font-size: 9.2pt; }
    .card .ln { font-size: 7.9pt; color: #55506e; line-height: 1.55; margin-top: 1mm; }
    .card .kv { display: flex; justify-content: space-between; margin-bottom: 1.3mm; font-size: 8.2pt; }
    .card .kv span:first-child { color: #8b84a8; }
    .card .kv span:last-child { font-weight: 700; }
    table.sv { margin-top: 6mm; }
    table.sv thead th { background: #4c35a8; color: #fff; font-size: 7.2pt; letter-spacing: .08em;
                        text-transform: uppercase; padding: 2.6mm 2.4mm; text-align: left; }
    table.sv tbody td { padding: 3.2mm 2.4mm; border-bottom: .4pt solid #e4e0f2; vertical-align: top; }
    table.sv td.c { text-align: center; }
    .sac { font-size: 7.2pt; color: #8b84a8; margin-top: 1.2mm; }
    .sum { margin-top: 5mm; display: flex; justify-content: flex-end; }
    .sum table { width: 76mm; }
    .sum td { padding: 2mm 2.4mm; font-size: 8.8pt; }
    .sum tr.s td { border-top: .8pt solid #ded9f0; font-weight: 700; }
    .sum tr.g td { background: #4c35a8; color: #fff; font-weight: 700; font-size: 11pt; }
    .note2 { margin-top: 6mm; font-size: 8pt; color: #55506e; line-height: 1.65;
             background: #faf9fe; border-radius: 2mm; padding: 4mm 4.5mm; }
    .ft { margin-top: 7mm; display: flex; justify-content: space-between; font-size: 7.6pt;
          color: #6b6585; }
  `,
    `
    <div class="head">
      <div class="logo">O</div>
      <div><div class="nm">${esc(v.name)}</div>
        <div class="sub">Integrated Facility Management</div></div>
      <div class="right">${v.address.map(esc).join('<br>')}<br>
        ${esc(v.phone)} &middot; ${esc(v.email)}<br>GSTIN ${esc(v.gstin)}</div>
    </div>

    <div class="title2">
      <h1>TAX INVOICE</h1>
      <div class="per">Service period: 01 October 2026 to 31 October 2026</div>
    </div>

    <div class="cards">
      <div class="card">
        <div class="cap">Billed to</div>
        <div class="nm">${esc(BUYER.name)}</div>
        <div class="ln">${BUYER.address.map(esc).join('<br>')}<br>GSTIN ${esc(BUYER.gstin)}</div>
      </div>
      <div class="card">
        <div class="cap">Invoice details</div>
        <div class="kv"><span>Invoice number</span><span>${esc(inv.invoiceNo)}</span></div>
        <div class="kv"><span>Invoice date</span><span>${esc(inv.date)}</span></div>
        <div class="kv"><span>Payment due</span><span>${esc(inv.dueDate)}</span></div>
        <div class="kv"><span>Place of supply</span><span>Karnataka (29)</span></div>
      </div>
    </div>

    <table class="sv">
      <thead><tr><th>Service description</th><th style="width:20mm" class="num">Qty</th>
        <th style="width:22mm">Unit</th><th style="width:20mm" class="num">Rate</th>
        <th style="width:26mm" class="num">Amount</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>

    <div class="sum"><table>
      <tr class="s"><td>Taxable value</td><td class="num">${inr(inv.subtotal)}</td></tr>
      ${taxRows}
      <tr class="g"><td>Amount payable</td><td class="num">INR ${inr(inv.total)}</td></tr>
    </table></div>

    <div class="note2">${esc(inv.terms)}<br>
      Deployment is governed by our master services agreement; monthly invoices are raised on
      actual attendance and do not quote individual work orders.</div>

    <div class="ft">
      <div>${esc(v.bank.name)}<br>A/c ${esc(v.bank.account)} &middot; IFSC ${esc(v.bank.ifsc)}</div>
      <div style="text-align:right">For ${esc(v.name)}<br><br><br>Authorised Signatory</div>
    </div>
  `
  );
}

const LAYOUTS = { meridian, sharma, nalanda, kaveri, orbit };

function render(invoice) {
  const layout = LAYOUTS[invoice.layout];
  if (!layout) throw new Error(`unknown layout: ${invoice.layout}`);
  return layout(invoice);
}

module.exports = { render, LAYOUTS };
