'use strict';

/**
 * The nine sample invoices.
 *
 * Every figure here is chosen so the invoice lands on a *specific* decision
 * path through the rule engine - not just "it parses". The expected outcome and
 * the rule that produces it are recorded on each entry, and
 * `backend/seed.py --runs` asserts them end to end.
 *
 * Both sides of every invoice are fictional. These are synthetic fixtures for a
 * case-study demo, and each rendered page says so in its footer.
 */

const BUYER = {
  name: 'Harbourline Industries India Private Limited',
  address: ['Plot 14, Phase II, Peenya Industrial Area', 'Bengaluru 560058, Karnataka'],
  gstin: '29AABCH4455L1ZQ',
  contact: 'accounts.payable@harbourline.example',
};

const VENDORS = {
  meridian: {
    name: 'Meridian Office Supplies LLP',
    address: ['No. 42, 3rd Cross, Gandhi Bazaar', 'Bengaluru 560004, Karnataka'],
    gstin: '29AACFM5678B1Z3',
    pan: 'AACFM5678B',
    phone: '+91 80 4123 7788',
    email: 'billing@meridianoffice.example',
    bank: { name: 'HDFC Bank, Basavanagudi', account: '50200034117829', ifsc: 'HDFC0001234' },
  },
  sharma: {
    name: 'Sharma Steel Works Private Limited',
    address: ['Survey 88/2, Taloja Industrial Belt', 'Navi Mumbai 410208, Maharashtra'],
    gstin: '27AABCS1234A1Z5',
    pan: 'AABCS1234A',
    phone: '+91 22 2741 9900',
    email: 'accounts@sharmasteelworks.example',
    bank: { name: 'ICICI Bank, Taloja', account: '000705001234', ifsc: 'ICIC0000007' },
  },
  nalanda: {
    name: 'Nalanda Print & Packaging',
    address: ['14/3 Anna Salai, Guindy', 'Chennai 600032, Tamil Nadu'],
    gstin: '33AAFCN9012C1Z7',
    pan: 'AAFCN9012C',
    phone: '+91 44 2234 5510',
    email: 'nalandaprint@example.net',
    bank: { name: 'Canara Bank, Guindy', account: '1194201000456', ifsc: 'CNRB0001194' },
  },
  kaveri: {
    name: 'Kaveri Logistics Private Limited',
    address: ['Gate 3, Container Depot Road, Irungattukottai', 'Sriperumbudur 602117, Tamil Nadu'],
    gstin: '36AAGCK3456D1Z9',
    pan: 'AAGCK3456D',
    phone: '+91 44 6677 2100',
    email: 'freightbilling@kaverilogistics.example',
    bank: { name: 'Axis Bank, Sriperumbudur', account: '918020041122334', ifsc: 'UTIB0001122' },
  },
  orbit: {
    name: 'Orbit Facility Services',
    address: ['B-7, Okhla Industrial Estate Phase III', 'New Delhi 110020'],
    gstin: '07AAHCO7890E1Z1',
    pan: 'AAHCO7890E',
    phone: '+91 11 4155 8080',
    email: 'invoices@orbitfacility.example',
    bank: { name: 'State Bank of India, Okhla', account: '38812044567', ifsc: 'SBIN0001234' },
  },

  // On the vendor master but **blocked**, so an invoice from them fails R3 for a
  // different reason than an unknown vendor does - and stage 4 shows the
  // difference: a confident GSTIN match whose status is `blocked`, rather than
  // no match at all. The GSTIN below is deliberately the one `backend/seed.py`
  // records for V-1006, so resolution matches on GSTIN rather than on the name.
  tricity: {
    name: 'Tri-City Electricals',
    address: ['Shed 9, GIDC Estate, Vatva', 'Ahmedabad 382445, Gujarat'],
    gstin: '24AAJCT2345F1Z4',
    pan: 'AAJCT2345F',
    phone: '+91 79 2583 4410',
    email: 'billing@tricityelectricals.example',
    bank: { name: 'Bank of Baroda, Vatva', account: '04120200001234', ifsc: 'BARB0VATVAX' },
  },

  // Not on the vendor master at all, under any name or GSTIN. The name is
  // deliberately unlike every approved vendor so the fuzzy match lands well
  // below the 0.85 floor instead of scraping past it.
  vectra: {
    name: 'Vectra Switchgear & Controls',
    address: ['Unit 22, Ranjangaon MIDC', 'Pune 412220, Maharashtra'],
    gstin: '27AABCV7788K1Z2',
    pan: 'AABCV7788K',
    phone: '+91 20 6710 3355',
    email: 'accounts@vectraswitchgear.example',
    bank: { name: 'Kotak Mahindra Bank, Ranjangaon', account: '7311456098', ifsc: 'KKBK0001789' },
  },
};

const INVOICES = [
  // ---------------------------------------------------------------- 1. happy
  {
    file: 'INV-MER-0881_meridian_happy.pdf',
    layout: 'meridian',
    vendor: VENDORS.meridian,
    invoiceNo: 'MER/2026/0881',
    date: '20/10/2026',
    dueDate: '19/11/2026',
    poRef: 'PO-4472',
    poPlacement: 'header',
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'A4 copier paper, 75 GSM, 500 sheets/ream', hsn: '4802', qty: 120, uom: 'ream', rate: 310.0 },
      { desc: 'Whiteboard markers, assorted colours', hsn: '9608', qty: 240, uom: 'pcs', rate: 45.0 },
      { desc: 'Box files, foolscap, laminated board', hsn: '4820', qty: 180, uom: 'pcs', rate: 165.0 },
      { desc: 'Desk organiser trays, 3-tier', hsn: '3926', qty: 95, uom: 'pcs', rate: 420.0 },
      { desc: 'Printer toner cartridge, 26A compatible', hsn: '8443', qty: 38, uom: 'pcs', rate: 2290.0 },
    ],
    terms: 'Payment within 30 days. Goods once sold are not returnable.',
    expect: { decision: 'APPROVE', because: 'all nine checks pass; total is 3.4% under PO-4472' },
  },

  // ------------------------------------------------------------- 2. split PO
  {
    file: 'INV-SSW-2211_sharma_split_po.pdf',
    layout: 'sharma',
    vendor: VENDORS.sharma,
    invoiceNo: 'SSW/2026/2211',
    date: '22/10/2026',
    dueDate: '21/11/2026',
    poRef: 'PO-4471',
    poPlacement: 'header',
    taxMode: 'igst',
    items: [
      { desc: 'MS angles 50 x 50 x 6 mm, IS 2062 E250', hsn: '7216', qty: 4200, uom: 'kg', rate: 61.5 },
      { desc: 'MS flats 65 x 8 mm', hsn: '7214', qty: 2600, uom: 'kg', rate: 58.0 },
      { desc: 'Galvanised purlins, Z-section 200 mm', hsn: '7216', qty: 1150, uom: 'kg', rate: 69.0 },
      { desc: 'Cutting, drilling and surface preparation', hsn: '9988', qty: 1, uom: 'lot', rate: 20050.0 },
    ],
    terms: 'Phase 2 part-billing against the referenced purchase order.',
    expect: {
      decision: 'REJECT',
      because: 'R6 - PO-4471 already carries 5,00,000 of prior billing, so this takes it past its value',
    },
  },

  // ------------------------------------------------- 3. duplicate setup
  {
    file: 'INV-NL-118_nalanda_original.pdf',
    layout: 'nalanda',
    vendor: VENDORS.nalanda,
    invoiceNo: 'NL/2026/118',
    date: '14/10/2026',
    dueDate: '13/11/2026',
    poRef: 'PO-4473',
    poPlacement: 'remarks',
    taxMode: 'inclusive',
    items: [
      {
        desc:
          'Product brochure printing - 25,000 copies, 8 page saddle-stitched, 170 GSM art paper, ' +
          'including design proofs, CTP plates, lamination and delivery to Bengaluru warehouse',
        hsn: '4911',
        qty: 1,
        uom: 'lot',
        rate: 176400.0,
      },
    ],
    terms: 'All rates are inclusive of GST at 18%. Delivery completed 12-10-2026.',
    expect: { decision: 'APPROVE', because: 'legitimate first part-bill against the annual print programme PO' },
  },

  // ---------------------------------------------- 4. near duplicate re-issue
  {
    file: 'INV-NL-127_nalanda_reissue.pdf',
    layout: 'nalanda',
    vendor: VENDORS.nalanda,
    invoiceNo: 'NL/2026/127',
    date: '14/10/2026',
    dueDate: '13/11/2026',
    poRef: 'PO-4473',
    poPlacement: 'remarks',
    taxMode: 'inclusive',
    items: [
      {
        desc:
          'Product brochure printing - 25,000 copies, 8 page saddle-stitched, 170 GSM art paper, ' +
          'including design proofs, CTP plates, lamination and delivery to Bengaluru warehouse',
        hsn: '4911',
        qty: 1,
        uom: 'lot',
        rate: 176400.0,
      },
    ],
    terms:
      'All rates are inclusive of GST at 18%. Re-issued against our bill of even date; ' +
      'kindly process this copy for payment.',
    expect: {
      decision: 'FLAG',
      because: 'R7 - same vendor, amount and date as NL/2026/118 under a different number; a hash check sees nothing',
    },
  },

  // ---------------------------------------------------------- 5. scanned
  {
    file: 'INV-KAV-7731_kaveri_scanned.pdf',
    layout: 'kaveri',
    vendor: VENDORS.kaveri,
    invoiceNo: 'KAV/2026/7731',
    date: '17/10/2026',
    dueDate: '16/11/2026',
    poRef: 'PO-4474',
    poPlacement: 'footnote',
    taxMode: 'gst5',
    scanned: true,
    items: [
      { desc: 'Inbound FTL, Chennai Port to Hosur plant, 32 ft MXL', hsn: '9965', qty: 18, uom: 'trips', rate: 11400.0 },
      { desc: 'Inbound FTL, Chennai Port to Sriperumbudur stores', hsn: '9965', qty: 9, uom: 'trips', rate: 7850.0 },
      { desc: 'Detention charges beyond free time', hsn: '9965', qty: 6, uom: 'days', rate: 1200.0 },
    ],
    terms: 'GST under reverse charge not applicable. Lorry receipts enclosed.',
    expect: {
      decision: 'FLAG',
      because: 'R9 - image-only PDF, so no extracted value can be verified against the file and confidence drops below the floor',
    },
  },

  // -------------------------------------------------------- 6. no PO at all
  {
    file: 'INV-ORB-5540_orbit_no_po.pdf',
    layout: 'orbit',
    vendor: VENDORS.orbit,
    invoiceNo: 'ORB/2026/5540',
    date: '25/10/2026',
    dueDate: '24/11/2026',
    poRef: null,
    poPlacement: null,
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'Housekeeping manpower deployment - October 2026', hsn: '9985', qty: 572, uom: 'man-days', rate: 210.0 },
      { desc: 'Cleaning chemicals and consumables', hsn: '3402', qty: 1, uom: 'lot', rate: 8400.0 },
    ],
    terms: 'Monthly service billing as per standing arrangement. Attendance sheets available on request.',
    expect: {
      decision: 'FLAG',
      because: 'R4 - no PO named, so the match falls back to vendor/amount/date and is capped below the floor by design',
    },
  },

  // --------------------------------------------- 7. inside tolerance (1.8%)
  {
    file: 'INV-SSW-2240_sharma_within_tolerance.pdf',
    layout: 'sharma',
    vendor: VENDORS.sharma,
    invoiceNo: 'SSW/2026/2240',
    date: '24/10/2026',
    dueDate: '23/11/2026',
    poRef: 'PO-4477',
    poPlacement: 'footnote',
    taxMode: 'igst',
    items: [
      { desc: 'Mezzanine fabrication - cutting, welding and assembly', hsn: '9988', qty: 1, uom: 'lot', rate: 112000.0 },
      { desc: 'Site erection labour', hsn: '9988', qty: 415, uom: 'man-hours', rate: 42.0 },
    ],
    terms: 'Scrap retained by us as agreed. Work completed and signed off 23-10-2026.',
    expect: { decision: 'APPROVE', because: 'R5 - 1.82% over PO-4477, inside the 2% tolerance, approved with the variance stated' },
  },

  // -------------------------------------------- 8. outside tolerance (7.5%)
  {
    file: 'INV-SSW-2241_sharma_over_tolerance.pdf',
    layout: 'sharma',
    vendor: VENDORS.sharma,
    invoiceNo: 'SSW/2026/2241',
    date: '26/10/2026',
    dueDate: '25/11/2026',
    poRef: 'PO-4482',
    poPlacement: 'header',
    taxMode: 'igst',
    items: [
      { desc: 'MS plates 12 mm, IS 2062 E250, cut to size', hsn: '7208', qty: 5400, uom: 'kg', rate: 64.0 },
      { desc: 'MS channels 150 x 75 mm', hsn: '7216', qty: 1780, uom: 'kg', rate: 61.75 },
    ],
    terms: 'Rate revision on account of mill price increase advised 10-10-2026.',
    expect: {
      decision: 'FLAG',
      because: 'R5 - 7.50% over PO-4482: past the 2% tolerance, inside the 10% reject threshold',
    },
  },

  // ------------------------------------------------- 9. arithmetic mismatch
  {
    file: 'INV-MER-0902_meridian_arithmetic.pdf',
    layout: 'meridian',
    vendor: VENDORS.meridian,
    invoiceNo: 'MER/2026/0902',
    date: '27/10/2026',
    dueDate: '26/11/2026',
    poRef: 'PO-4481',
    poPlacement: 'header',
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'Laptop docking station, USB-C triple display', hsn: '8471', qty: 24, uom: 'pcs', rate: 7450.0 },
      { desc: 'Wireless keyboard and mouse combo', hsn: '8471', qty: 40, uom: 'sets', rate: 1980.0 },
    ],
    // The grand total printed on the page is wrong - 25,000 short of what the
    // lines and tax actually come to, as if it had been typed by hand. The
    // extraction prompt forbids correcting it, which is the only reason R2 can
    // see the error at all.
    statedTotal: 279440.0,
    terms: 'Warranty 3 years onsite, direct with manufacturer.',
    expect: { decision: 'FLAG', because: 'R2 - line items plus tax come to 3,04,440.00 but the invoice states 2,79,440.00' },
  },

  // ------------------------------------- 10. past the reject threshold (18%)
  // The other half of R5. Invoice 7 sits inside tolerance and invoice 8 sits in
  // the band between tolerance and rejection; nothing exercised the far side of
  // it, so the rule's REJECT severity was dead code as far as the samples went.
  // PO-4480 carries no prior billing, which keeps R6 abstaining and leaves this
  // a clean single-rule rejection.
  {
    file: 'INV-ORB-5561_orbit_over_reject.pdf',
    layout: 'orbit',
    vendor: VENDORS.orbit,
    invoiceNo: 'ORB/2026/5561',
    date: '28/10/2026',
    dueDate: '27/11/2026',
    poRef: 'PO-4480',
    poPlacement: 'header',
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'Annual maintenance contract - HVAC, 12 months', hsn: '9987', qty: 1, uom: 'lot', rate: 72000.0 },
      { desc: 'Pest control - quarterly cycle', hsn: '9985', qty: 4, uom: 'cycles', rate: 6250.0 },
      { desc: 'Facade cleaning, high-rise rope access', hsn: '9985', qty: 264, uom: 'sqm', rate: 125.0 },
    ],
    terms: 'Scope expanded mid-contract at the site manager\'s request. Revised schedule attached.',
    expect: {
      decision: 'REJECT',
      because: 'R5 - 18.00% over PO-4480, past the 10% reject threshold rather than inside the flag band',
    },
  },

  // ----------------------------------------- 11. blocked vendor on the master
  // V-1006 is on the vendor master with status `blocked`. The README has always
  // claimed this scenario existed; no invoice actually exercised it. Resolution
  // matches confidently on GSTIN and *then* finds the block, which is a
  // different stage-4 payload from an unknown vendor - the point of the pair.
  {
    file: 'INV-TCE-3310_tricity_blocked_vendor.pdf',
    layout: 'sharma',
    vendor: VENDORS.tricity,
    invoiceNo: 'TCE/2026/3310',
    date: '23/10/2026',
    dueDate: '22/11/2026',
    poRef: null,
    poPlacement: null,
    taxMode: 'igst',
    items: [
      { desc: 'LT distribution panel, 415 V, IP54, fully wired', hsn: '8537', qty: 1, uom: 'no', rate: 86500.0 },
      { desc: 'Perforated cable tray, GI, 300 mm with couplers', hsn: '7308', qty: 240, uom: 'rmt', rate: 385.0 },
      { desc: 'Installation, termination and load testing', hsn: '9987', qty: 1, uom: 'lot', rate: 24000.0 },
    ],
    terms: 'Supply against our quotation TCE/Q/2026/881. Test certificates enclosed.',
    expect: {
      decision: 'REJECT',
      because: 'R3 - vendor is on the master but blocked; R4 also flags because a blocked vendor has no open PO',
    },
  },

  // ---------------------------------------------- 12. vendor not on the master
  {
    file: 'INV-VEC-8120_vectra_unknown_vendor.pdf',
    layout: 'meridian',
    vendor: VENDORS.vectra,
    invoiceNo: 'VEC/2026/8120',
    date: '29/10/2026',
    dueDate: '28/11/2026',
    poRef: null,
    poPlacement: null,
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'MCB distribution boards, 8-way, double door', hsn: '8537', qty: 34, uom: 'pcs', rate: 3450.0 },
      { desc: 'Industrial sockets, 32 A, 5-pin, IP67', hsn: '8536', qty: 120, uom: 'pcs', rate: 640.0 },
    ],
    terms: 'New supplier - onboarding documents submitted to procurement separately.',
    expect: {
      decision: 'REJECT',
      because: 'R3 - no vendor on the master matches by GSTIN or name above the floor; R4 also flags as there is no PO',
    },
  },

  // --------------------------------------------------- 13. stale invoice date
  // Dated well outside the 180-day staleness limit, as a bill that surfaced
  // late genuinely would be. Everything else is clean - the amount is under
  // PO-4478 and nothing has been billed against it - so R8 fires alone.
  {
    file: 'INV-NL-094_nalanda_stale_date.pdf',
    layout: 'nalanda',
    vendor: VENDORS.nalanda,
    invoiceNo: 'NL/2026/094',
    date: '10/03/2026',
    dueDate: '09/04/2026',
    poRef: 'PO-4478',
    poPlacement: 'remarks',
    taxMode: 'inclusive',
    items: [
      {
        desc:
          'Exhibition collateral - 6 roll-up standees, 2 backdrop panels and 1,500 leaflets, ' +
          'including artwork adaptation and freight to the Chennai venue',
        hsn: '4911',
        qty: 1,
        uom: 'lot',
        rate: 93000.0,
      },
    ],
    terms:
      'Rates inclusive of GST at 18%. Submitted late against PO-4478; original bill was ' +
      'handed over at the venue and not routed through accounts.',
    expect: {
      decision: 'FLAG',
      because: 'R8 - dated 10-03-2026, beyond the 180-day staleness limit, while every other check passes',
    },
  },

  // ----------------------------------------- 14. a required field is missing
  // No invoice number anywhere on the page. R1 grades this a FLAG rather than a
  // rejection, because the vendor and the total - the two fields you cannot pay
  // without - are both present. An invoice you cannot reference is a bookkeeping
  // problem, not a fraud signal, and the severity should say so.
  {
    file: 'INV-MER-NONUM_meridian_missing_number.pdf',
    layout: 'meridian',
    vendor: VENDORS.meridian,
    invoiceNo: null,
    date: '30/10/2026',
    dueDate: '29/11/2026',
    poRef: 'PO-4476',
    poPlacement: 'header',
    taxMode: 'cgst_sgst',
    items: [
      { desc: 'Ergonomic task chairs, mesh back, adjustable arms', hsn: '9401', qty: 85, uom: 'pcs', rate: 2400.0 },
      { desc: 'Monitor arms, dual, gas spring', hsn: '8302', qty: 46, uom: 'pcs', rate: 1000.0 },
    ],
    terms: 'Delivered to the Peenya stores against the running furniture PO.',
    expect: {
      decision: 'FLAG',
      because: 'R1 - no invoice number printed; vendor and total are present, so it flags rather than rejects',
    },
  },
];

/** Indian digit grouping: 1180000 -> "11,80,000.00". */
function inr(value) {
  const negative = value < 0;
  const fixed = Math.abs(value).toFixed(2);
  const [whole, decimals] = fixed.split('.');
  let out;
  if (whole.length <= 3) {
    out = whole;
  } else {
    const last3 = whole.slice(-3);
    const rest = whole.slice(0, -3);
    out = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3;
  }
  return (negative ? '-' : '') + out + '.' + decimals;
}

/** Derive line amounts, subtotal, tax lines and the printed total. */
function compute(invoice) {
  const items = invoice.items.map((item) => ({
    ...item,
    amount: Math.round(item.qty * item.rate * 100) / 100,
  }));
  const subtotal = Math.round(items.reduce((sum, i) => sum + i.amount, 0) * 100) / 100;

  let taxLines = [];
  switch (invoice.taxMode) {
    case 'cgst_sgst':
      taxLines = [
        { label: 'CGST @ 9%', amount: Math.round(subtotal * 0.09 * 100) / 100 },
        { label: 'SGST @ 9%', amount: Math.round(subtotal * 0.09 * 100) / 100 },
      ];
      break;
    case 'igst':
      taxLines = [{ label: 'IGST @ 18%', amount: Math.round(subtotal * 0.18 * 100) / 100 }];
      break;
    case 'gst5':
      taxLines = [{ label: 'GST @ 5% (goods transport)', amount: Math.round(subtotal * 0.05 * 100) / 100 }];
      break;
    case 'inclusive':
      taxLines = [];   // tax embedded in the rate and never stated separately
      break;
    default:
      throw new Error(`unknown taxMode: ${invoice.taxMode}`);
  }

  const taxTotal = Math.round(taxLines.reduce((sum, t) => sum + t.amount, 0) * 100) / 100;
  const computedTotal = Math.round((subtotal + taxTotal) * 100) / 100;
  const total = invoice.statedTotal != null ? invoice.statedTotal : computedTotal;

  return { ...invoice, items, subtotal, taxLines, taxTotal, computedTotal, total };
}

module.exports = { BUYER, VENDORS, INVOICES, inr, compute };
