/* ==========================================================================
   "How it works" - four diagrams.

   Written against the code as it is: the step names come from `pipeline.py`
   and the rule ids from `rules.py`, so when one of those changes the diagram
   documenting it is the thing to change with it.

   Four, not eight. A page that documents a system in every notation it can
   think of teaches nobody anything; these answer the four questions people
   actually arrive with - what does it decide, how does it get there, what does
   it compare an invoice against, and what happens if I raise a purchase order
   myself. Each is one shape, small enough to take in at a glance.

   Built on the vocabulary in `uml.js`; see the note at the top of that file.
   ========================================================================== */
(function () {
  'use strict';

  const U = window.UML;

  /* ============================================== 1. what it decides ===== */

  function decides() {
    const W = 760;
    const H = 182;
    const y = 62;
    const h = 42;
    const mid = y + h / 2;          // 83
    let svg = '';

    svg += U.box(4, y, 108, h, 'Invoice PDF', { sub: 'any layout', tone: 'in' });
    svg += U.arrow(116, mid, 144, mid);
    svg += U.box(148, y, 112, h, 'Read it', { sub: 'one model call', tone: 'step' });
    svg += U.arrow(264, mid, 292, mid);
    svg += U.box(296, y, 112, h, 'Check it', { sub: 'nine rules', tone: 'step' });
    svg += U.arrow(412, mid, 440, mid);
    svg += U.box(444, y, 104, h, 'Worst result', { sub: 'wins', tone: 'decide' });

    // The fork. Three ends, which is the whole decision.
    const outs = [
      { label: 'APPROVE', sub: 'pay it', y: 18, tone: 'approve' },
      { label: 'FLAG', sub: 'a person looks', y: 64, tone: 'flag' },
      { label: 'REJECT', sub: 'do not pay', y: 110, tone: 'reject' },
    ];
    outs.forEach((out) => {
      svg += U.arrow(552, mid, 580, out.y + 19);
      svg += U.box(584, out.y, 140, 38, out.label, { sub: out.sub, tone: out.tone });
    });

    // What the checking step reads, drawn once rather than as four arrows.
    svg += U.box(272, 132, 160, 34, 'What we already know',
      { sub: 'vendors - POs - history', tone: 'ref' });
    svg += U.arrow(352, 128, 352, 110);

    return U.figure(
      'One invoice in, one of three answers out. The answer always arrives with the check that '
      + 'produced it, what that check expected, and what it actually found.',
      { svg: svg, width: W, height: H });
  }

  /* ================================================ 2. the seven steps === */

  function steps() {
    const W = 760;
    const BX = 4;
    const BW = 176;
    const BH = 34;
    const GAP = 44;
    const TOP = 8;

    const rows = [
      ['Read the file', 'Open the PDF, fingerprint it, and see whether it has real text or is a scan.'],
      ['Extract', 'One model call: the page becomes fields, each with the words it was read from.'],
      ['Verify', 'Look those words up in the page’s own text, to check the figures are there.'],
      ['Find the vendor', 'Match the supplier to the approved list - by tax number, then by name.'],
      ['Find the order', 'Score every open purchase order and pick the one this bills against.'],
      ['Run the checks', 'All nine: totals, tolerances, duplicates, dates, certainty.'],
      ['Decide', 'The most serious failure sets the answer.'],
    ];

    let svg = '';
    rows.forEach((row, i) => {
      const ry = TOP + i * GAP;
      svg += U.box(BX, ry, BW, BH, (i + 1) + '. ' + row[0], { tone: i === 1 ? 'model' : 'step' });
      svg += U.text(BX + BW + 18, ry + 21, row[1], 'dg-say');
      if (i) svg += U.arrow(BX + BW / 2, ry - GAP + BH, BX + BW / 2, ry - 2);
    });

    const lastBottom = TOP + (rows.length - 1) * GAP + BH;   // 306
    const outs = [
      { label: 'APPROVE', x: 4, tone: 'approve' },
      { label: 'FLAG', x: 132, tone: 'flag' },
      { label: 'REJECT', x: 260, tone: 'reject' },
    ];
    outs.forEach((out) => {
      svg += U.thin(BX + BW / 2, lastBottom + 2, out.x + 58, lastBottom + 28);
      svg += U.box(out.x, lastBottom + 30, 116, 30, out.label, { tone: out.tone });
    });

    svg += U.text(404, lastBottom + 44,
      'Every step writes its own row as it happens - the same record you', 'dg-l-left');
    svg += U.text(404, lastBottom + 58,
      'watch live, and a reviewer reads a week later.', 'dg-l-left');

    return U.figure(
      'In order, every time. Only step 2 uses a model, and it is asked one thing: report what is '
      + 'printed on this page. Step 3 then checks that it really is.',
      { svg: svg, width: W, height: lastBottom + 76 });
  }

  /* ========================================= 3. what it is compared to === */

  function against() {
    const W = 760;
    const H = 264;
    let svg = '';

    // The invoice in the middle; what it is held against around it.
    svg += U.box(306, 102, 148, 46, 'This invoice', { sub: 'what the page says', tone: 'in' });

    const sources = [
      { x: 40, y: 26, title: 'Approved vendors', sub: 'who we may pay at all', rules: 'R3' },
      { x: 552, y: 26, title: 'Purchase orders', sub: 'what we ordered, what is left',
        rules: 'R4  R5  R6' },
      { x: 40, y: 188, title: 'Invoices seen before', sub: 'by file, by vendor + amount',
        rules: 'R7' },
      { x: 552, y: 188, title: 'The page itself', sub: 'its own printed text, re-read',
        rules: 'R1  R2  R8  R9' },
    ];

    sources.forEach((source) => {
      svg += U.box(source.x, source.y, 168, 40, source.title, { sub: source.sub, tone: 'ref' });
      svg += U.text(source.x + 84, source.y + 54, source.rules, 'dg-rule');
      const left = source.x < 300;
      const top = source.y < 100;
      svg += U.arrow(
        left ? source.x + 172 : source.x - 4,
        top ? source.y + 30 : source.y + 10,
        left ? 302 : 458,
        top ? 112 : 138
      );
    });

    return U.figure(
      'An invoice on its own proves nothing. Every check holds it against one specific record '
      + '- and the number each one uses is live configuration, not something compiled in.',
      { svg: svg, width: W, height: H });
  }

  /* ================================ 4. raising a PO and checking it ====== */

  function raise() {
    const BW = 124;
    const PITCH = 152;
    const X0 = 14;
    const BY = 42;
    const BH = 46;
    let svg = '';

    const flow = [
      ['You raise a PO', 'number, vendor, value', 'mine'],
      ['It joins the master', 'tagged as yours', 'mine'],
      ['An invoice is written', 'a real PDF', 'step'],
      ['Same seven steps', 'as any upload', 'step'],
      ['Watch it decided', 'expected vs actual', 'step'],
    ];

    flow.forEach((step, i) => {
      const x = X0 + i * PITCH;
      svg += U.num(x + 13, BY - 8, i + 1);
      svg += U.box(x, BY, BW, BH, step[0], { sub: step[1], tone: step[2] });
      if (i) svg += U.arrow(x - PITCH + BW + 3, BY + BH / 2, x - 4, BY + BH / 2);
    });

    svg += U.text(14, 128, 'The three invoices it can write, and what each one earns:', 'dg-l-left');

    const variants = [
      { label: 'Bills 60% of the PO', sub: 'nothing fails', x: 14, tone: 'approve' },
      { label: 'Bills 7.5% over', sub: 'past tolerance - R5', x: 238, tone: 'flag' },
      { label: 'Bills 35% over', sub: 'past the limit - R5', x: 462, tone: 'reject' },
    ];
    variants.forEach((v) => {
      svg += U.box(v.x, 140, 210, 40, v.label, { sub: v.sub, tone: v.tone });
    });

    return U.figure(
      'The same pipeline, reached with a document the system wrote. The amount is the only thing '
      + 'that differs between the three - and it is the only thing that changes the verdict.',
      { svg: svg, width: 760, height: 194 });
  }

  /* ================================================ 5. reading a page ==== */

  function reading() {
    let svg = '';

    svg += U.box(4, 46, 120, 46, 'The page', { sub: 'as printed', tone: 'in' });
    svg += U.arrow(128, 69, 158, 69, 'model');
    svg += U.box(162, 46, 156, 46, 'A field, and the words',
      { sub: 'total: 600,030.00', tone: 'step' });
    svg += U.arrow(322, 69, 352, 69);
    svg += U.box(356, 46, 150, 46, 'Find those words', { sub: 'in the page text', tone: 'decide' });

    // Found, and not found - the branch that decides how much to trust it.
    svg += U.elbow('M506 60 L540 60 L540 32 L574 32', 'found', 540, 24);
    svg += U.box(578, 14, 178, 36, 'Verified', { tone: 'approve' });
    svg += U.elbow('M506 78 L540 78 L540 106 L574 106', 'not found', 540, 124);
    svg += U.box(578, 88, 178, 36, 'Unverified', { tone: 'flag' });

    svg += U.text(4, 150, 'confidence  =  half the model’s own certainty', 'dg-l-left');
    svg += U.text(4, 166, '+  half the share of decision-critical fields that were found', 'dg-l-left');
    svg += U.text(4, 190, 'A scan has no text to search, so nothing can be verified, so confidence', 'dg-l-left');
    svg += U.text(4, 206, 'falls below the floor and R9 fires - even when every other check passes.', 'dg-l-left');

    return U.figure(
      'Why a scanned page comes back less certain rather than wrong. Every figure the model '
      + 'reports carries the words it was read from, and the code goes back to the page to '
      + 'check they are really there.',
      { svg: svg, width: 760, height: 220 });
  }

  /* =============================================== 6. what is stored ===== */

  function stored() {
    let svg = '';

    svg += U.box(4, 20, 150, 44, 'One run', { sub: 'the invoice, the verdict', tone: 'in' });
    svg += U.box(4, 92, 150, 44, 'Seven step rows', { sub: 'one per step', tone: 'step' });
    svg += U.arrow(79, 68, 79, 88);

    const reads = [
      ['Watched live', 'streamed as it happens', 20],
      ['Read as an audit trail', 'the same rows, later', 76],
      ['Replayed on the dashboard', 'the same rows again', 132],
    ];
    reads.forEach((r) => {
      svg += U.arrow(158, 114, 236, r[2] + 20);
      svg += U.box(240, r[2], 220, 40, r[0], { sub: r[1], tone: 'ref' });
    });

    svg += U.box(500, 56, 256, 48, 'There is no second version of events',
      { sub: 'what you watch is what is audited', tone: 'decide' });
    svg += U.arrow(464, 76, 496, 76);

    svg += U.text(4, 162, 'An approved invoice also books what it spent against its purchase', 'dg-l-left');
    svg += U.text(4, 178, 'order - which is what the next invoice against that order is judged on.', 'dg-l-left');

    return U.figure(
      'The step rows are not a log of the interface; they are the interface. The live view, the '
      + 'audit trail and the dashboard replay all read the same rows.',
      { svg: svg, width: 760, height: 194 });
  }

  /* ======================================================== mounting ===== */

  const BUILDERS = {
    decides: decides, steps: steps, reading: reading, against: against,
    raise: raise, stored: stored,
  };

  let mounted = false;

  /** Built on first open: most visits never come here, and building twice
   *  would restart the figure numbering the captions depend on. */
  window.renderHowItWorks = function renderHowItWorks() {
    if (mounted) return;
    U.resetFigures();
    document.querySelectorAll('[data-uml]').forEach((slot) => {
      const build = BUILDERS[slot.dataset.uml];
      if (build) slot.innerHTML = build();
    });
    mounted = true;
  };
})();
