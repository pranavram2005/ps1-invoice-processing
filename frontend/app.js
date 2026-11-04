/* ==========================================================================
   PS-1 Invoice Processing - frontend

   No framework and no build step, which keeps the whole thing one static
   directory served by the same container as the API. The only genuinely
   interesting part is the run view: it renders the seven pipeline stages from
   an EventSource stream, so what you watch is the pipeline's own audit rows
   arriving, not an animation of them.

   Three rules this file follows:

   * Stage payloads are rendered by stage-specific renderers (RENDERERS below).
     A generic JSON dump would technically show everything and communicate
     nothing.
   * Anything the model produced is marked with where it came from. Hover a
     provenance marker and you get the page and the exact quoted text, plus
     whether that quote was found in the document.
   * The UI never recomputes a decision or a score. Every number shown comes
     from the API, so the screen cannot disagree with the audit trail.
   ========================================================================== */

(() => {
  'use strict';

  // ----------------------------------------------------------------- helpers

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const esc = (value) =>
    String(value == null ? '' : value).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const inrFmt = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const money = (value, currency) => {
    if (value == null || value === '') return '—';
    const n = Number(value);
    if (!isFinite(n)) return '—';
    return `${currency ? currency + ' ' : ''}${inrFmt.format(n)}`;
  };

  const plain = (value) => (value == null || value === '' ? '—' : esc(value));

  const num = (value, digits = 2) =>
    value == null || !isFinite(Number(value)) ? '—' : Number(value).toFixed(digits);

  const duration = (ms) => {
    if (ms == null) return '—';
    return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
  };

  const when = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    return d.toLocaleString(undefined, {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    });
  };

  const decisionClass = (decision) =>
    ({ APPROVE: 'approve', FLAG: 'flag', REJECT: 'reject' }[decision] || '');

  const decisionBadge = (decision) =>
    decision
      ? `<span class="badge badge-${decisionClass(decision)}">${esc(decision)}</span>`
      : '<span class="badge">—</span>';

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: options.body && !(options.body instanceof FormData)
        ? { 'Content-Type': 'application/json' } : undefined,
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        if (payload && Array.isArray(payload.detail)) {
          // FastAPI validation errors: one object per field. Pydantic prefixes
          // its own "Value error, ", which is noise in a UI.
          detail = payload.detail
            .map((item) => String(item.msg || '').replace(/^Value error, /, ''))
            .filter(Boolean).join(' ') || detail;
        } else if (payload && payload.detail) {
          detail = payload.detail;
        }
      } catch (_) { /* keep the status line */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.toggle('is-err', !!isError);
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 7000 : 3600);
  }

  // Shared hover tooltip, used for provenance markers and stat-tile footnotes.
  const tooltip = $('#tooltip');
  function bindTooltips(root) {
    $$('[data-tip]', root).forEach((node) => {
      node.addEventListener('mouseenter', () => {
        tooltip.innerHTML = node.dataset.tip;
        tooltip.hidden = false;
        const box = node.getBoundingClientRect();
        const tip = tooltip.getBoundingClientRect();
        let left = box.left + box.width / 2 - tip.width / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - tip.width - 8));
        let top = box.top - tip.height - 9;
        if (top < 8) top = box.bottom + 9;
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
      });
      node.addEventListener('mouseleave', () => { tooltip.hidden = true; });
    });
  }

  // ------------------------------------------------------------------- state

  const state = {
    health: null,
    samples: [],
    run: null,            // the full RunResult, as far as we know it
    stages: [],           // StageRecord[], live
    openStages: new Set(),
    source: null,         // EventSource
    runs: [],
    filter: 'ALL',
    sort: { key: 'started_at', dir: 'desc' },
    config: null,
    defaults: null,
    pos: [],
    posFilter: 'ALL',
    posQuery: '',
    posSort: { key: 'pct', dir: 'desc' },
    vendors: [],
    variants: [],
    intent: null,        // what a generated invoice was built to demonstrate
  };

  // -------------------------------------------------------------------- tabs

  /** A jump list for pages that are taller than a screen.
   *
   *  Rules & data stacks seven sections into nearly three thousand pixels, and
   *  the only way to a given one was to scroll and recognise it. The strip is
   *  built from whatever `.card > .card-head > h2` headings the panel actually
   *  has, so it cannot list a section that is not there or miss one that is.
   *  Panels short enough to take in at once do not get one.
   */
  function buildSectionNav(panel) {
    const existing = $('.subnav', panel);
    if (existing) existing.remove();

    const cards = $$('.card', panel).filter((card) => $('.card-head h2', card));
    if (cards.length < 3) return;

    const nav = document.createElement('nav');
    nav.className = 'subnav';
    nav.setAttribute('aria-label', 'Sections on this page');
    cards.forEach((card, i) => {
      const heading = $('.card-head h2', card);
      if (!card.id) card.id = `sec-${panel.id}-${i}`;
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'subnav-link';
      link.textContent = heading.textContent.trim();
      link.addEventListener('click', () => {
        // Offset by the fixed header, or the heading lands underneath it.
        const top = card.getBoundingClientRect().top + window.scrollY - 92;
        window.scrollTo({ top, behavior: 'smooth' });
      });
      nav.appendChild(link);
    });

    const head = $('.sec-head', panel);
    if (head && head.nextSibling) panel.insertBefore(nav, head.nextSibling);
    else panel.insertBefore(nav, panel.firstChild);

    // Mark whichever section is currently in view.
    const marks = $$('.subnav-link', nav);
    const spy = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const i = cards.indexOf(entry.target);
        marks.forEach((m, j) => m.classList.toggle('is-here', j === i));
      });
    }, { rootMargin: '-90px 0px -65% 0px' });
    cards.forEach((card) => spy.observe(card));

    // The last section can never reach the observer's band: the page bottoms
    // out first, so scrolling to the end would leave the previous link marked.
    window.addEventListener('scroll', () => {
      if (!panel.classList.contains('is-active')) return;
      const atEnd = window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
      if (atEnd) marks.forEach((m, j) => m.classList.toggle('is-here', j === marks.length - 1));
    }, { passive: true });
  }

  /** The floating index beside the documentation.
   *
   *  Built from the `h2` of each `.doc-sec` actually in the page, so it cannot
   *  list a section that is not there or miss one that is. It floats in the
   *  left margin rather than sitting above the text: the page is seven
   *  sections of prose and pictures, and a reader scrolling through them wants
   *  to know where they are without a control taking a slice off the top of
   *  every screen. Below the width where a margin exists it becomes a strip
   *  across the top instead, because being hidden is not the same as being
   *  unnecessary.
   */
  function buildDocIndex() {
    const panel = $('#panel-how');
    if (!panel || $('.doc-index', panel)) return;
    const sections = $$('.doc-sec', panel).filter((section) => $('h2', section));
    if (sections.length < 3) return;

    const nav = document.createElement('nav');
    nav.className = 'doc-index';
    nav.setAttribute('aria-label', 'Sections of this document');
    nav.innerHTML = '<p class="doc-index-k">On this page</p>'
      + sections.map((section, i) => {
        if (!section.id) section.id = `doc-sec-${i}`;
        // The numbering is already in the heading; the index shows the words.
        const title = $('h2', section).textContent.replace(/^\d+\.\s*/, '');
        return `<button class="doc-index-link" type="button" data-to="${esc(section.id)}">
                  <span class="doc-index-n">${i + 1}</span>${esc(title)}
                </button>`;
      }).join('');

    panel.insertBefore(nav, panel.firstChild);

    const links = $$('.doc-index-link', nav);
    links.forEach((link) => link.addEventListener('click', () => {
      const target = document.getElementById(link.dataset.to);
      if (!target) return;
      window.scrollTo({ top: target.getBoundingClientRect().top + window.scrollY - 92,
                        behavior: 'smooth' });
    }));

    const spy = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const i = sections.indexOf(entry.target);
        links.forEach((link, j) => link.classList.toggle('is-here', j === i));
      });
    }, { rootMargin: '-90px 0px -62% 0px' });
    sections.forEach((section) => spy.observe(section));

    // The last section can never reach the observer's band - the page bottoms
    // out first - so scrolling to the end would leave the previous link marked.
    window.addEventListener('scroll', () => {
      if (!panel.classList.contains('is-active')) return;
      const atEnd = window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
      if (atEnd) links.forEach((link, j) => link.classList.toggle('is-here', j === links.length - 1));
    }, { passive: true });
  }

  function showTab(name) {
    $$('.nav-link').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === name));
    $$('.panel').forEach((panel) => panel.classList.toggle('is-active', panel.id === `panel-${name}`));
    // A tab is a different page; land at the top of it rather than wherever the
    // last one happened to be scrolled to.
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    if (name === 'dashboard') loadDashboard();
    if (name === 'pos') { loadPos(); loadPoForm(); }
    if (name === 'admin') loadAdmin();
    if (name === 'layouts') loadLayouts();
    if (name === 'how') {
      // Built on first open rather than at load - most visits never come here.
      // Both calls are idempotent.
      if (window.renderHowItWorks) window.renderHowItWorks();
      buildDocIndex();
    }
  }

  $$('.nav-link').forEach((tab) => tab.addEventListener('click', () => showTab(tab.dataset.tab)));

  /** Rule id -> human title, so a chart axis can say what R6 actually is. */
  async function loadRuleNames() {
    try {
      const payload = await api('/api/rules');
      state.ruleNames = {};
      (payload.rules || []).forEach((r) => { state.ruleNames[r.id] = r.title; });
    } catch (_) { state.ruleNames = {}; }
  }

  // ------------------------------------------------------------ env / health

  async function loadHealth() {
    const strip = $('#env-strip');
    try {
      const health = await api('/api/health');
      state.health = health;
      const offline = health.extraction_mode !== 'live';
      const parts = [
        `<span class="env-item"><span class="dot dot-ok"></span>${health.runs} run${health.runs === 1 ? '' : 's'}</span>`,
        `<span class="env-item">model <b>${esc(health.model)}</b></span>`,
        `<span class="env-item">effort <b>${esc(health.effort)}</b></span>`,
      ];
      if (offline) {
        parts.push(
          `<span class="env-item is-warn" data-tip="${esc(
            '<div class=&quot;tt-head&quot;>Extraction is not calling the model</div>' +
            'EXTRACTION_MODE=' + health.extraction_mode + '. Stage 2 is served from the bundled ' +
            'ground-truth fixtures for the nine sample invoices. Everything after it - ' +
            'normalisation, provenance checks, matching, the rules, the decision - is the real ' +
            'code path.')}">extraction <b>${esc(health.extraction_mode)}</b></span>`
        );
      }
      if (!health.api_key_present) {
        parts.push('<span class="env-item is-warn">no API key</span>');
      }
      if (health.stage_delay_ms) {
        parts.push(
          `<span class="env-item" data-tip="${esc(
            '<div class=&quot;tt-head&quot;>Artificial pacing</div>' +
            health.stage_delay_ms + ' ms is added before each stage so the run is watchable. ' +
            'It is added latency, not work. Set STAGE_DELAY_MS=0 for true pipeline timings.')}">pacing <b>${health.stage_delay_ms} ms</b></span>`
        );
      }
      strip.innerHTML = parts.join('');
      bindTooltips(strip);
      renderProviderSwitch(health);
      const label = $('#env-label');
      if (label) label.textContent = `${health.runs} run${health.runs === 1 ? '' : 's'}`;
    } catch (err) {
      strip.innerHTML = `<span class="env-item is-warn"><span class="dot dot-bad"></span>API unreachable</span>`;
    }
  }

  // ---------------------------------------------------------------- provider

  /** The provider switch.
   *
   *  This control earns its place: the whole design claim is that the model
   *  only extracts and the code decides, which implies the model is swappable.
   *  Being able to change it mid-demo and watch the same invoice reach the same
   *  verdict is the shortest possible proof of that.
   */
  function renderProviderSwitch(health) {
    const node = $('#provider-switch');
    if (!node || !health || !health.providers) return;

    node.innerHTML = health.providers.map((p) => {
      const tip = !p.available
        ? `<div class="tt-head">${esc(p.label)} has no API key</div>Add its key to .env and restart.`
        : !p.reads_images
          ? `<div class="tt-head">${esc(p.label)} reads text, not images</div>`
            + 'Its models are text-only, so this provider reads the PDF text layer. '
            + 'The scanned sample has no text layer and will fail on it by design.'
          : `<div class="tt-head">${esc(p.label)}</div>Reads the page itself, so scanned PDFs work.`;
      return `<button type="button" data-provider="${esc(p.id)}"
                class="${p.active ? 'is-on' : ''}" ${p.available ? '' : 'disabled'}
                data-tip="${esc(tip)}">${esc(p.label)}</button>`;
    }).join('');

    $$('button[data-provider]', node).forEach((button) => {
      button.addEventListener('click', () => switchProvider(button.dataset.provider));
    });
    bindTooltips(node);
  }

  async function switchProvider(provider) {
    try {
      const result = await api('/api/provider', {
        method: 'POST',
        body: JSON.stringify({ provider }),
      });
      toast(`Extraction now runs on ${result.provider} (${result.model})`);
      loadHealth();
    } catch (err) {
      toast(`Could not switch provider: ${err.message}`, true);
    }
  }

  // ----------------------------------------------------------------- samples

  async function loadSamples() {
    try {
      state.samples = await api('/api/samples');
    } catch (err) {
      state.samples = [];
    }
    const node = $('#scenario-groups');
    if (!state.samples.length) {
      node.innerHTML = '<p class="hint">No bundled samples found. Generate them with: '
        + '<code>node tools/invoice_gen/generate.js</code></p>';
      return;
    }

    // Grouped, not a dropdown. Fourteen scenarios in one <select> gave the
    // clean invoice and the broken ones identical weight, so the one thing a
    // reviewer is told to look for - the happy path, then an edge case - was
    // the one thing the UI would not tell them. The grouping is the answer to
    // "which of these is the normal one?".
    //
    // Cards rather than rows: each scenario has to carry a title, the reason it
    // is worth running, the verdict it should earn and its own PDF download,
    // and a card gives those four things room without a reviewer reading
    // across a line. The happy-path group is tinted so the normal case is
    // identifiable before anything is read.
    const GROUPS = [
      {
        key: 'happy',
        title: 'Happy path',
        blurb: 'Nothing is wrong with these. They should sail through, and they do.',
        match: (s) => s.expected === 'APPROVE',
      },
      {
        key: 'edge',
        title: 'Edge cases',
        blurb: 'Each one breaks a different rule. This is where the system earns its keep - '
             + 'the interesting question is not whether it reads a clean invoice, but what it '
             + 'does with a bad one.',
        match: (s) => s.expected !== 'APPROVE',
      },
    ];

    node.innerHTML = GROUPS.map((group) => {
      const items = state.samples.filter(group.match).sort((a, b) => a.order - b.order);
      if (!items.length) return '';
      return `
        <section class="sgroup sgroup-${group.key}">
          <div class="sgroup-head">
            <h3>${esc(group.title)} <span class="sgroup-n">${items.length}</span></h3>
            <p>${esc(group.blurb)}</p>
          </div>
          <div class="sgrid">${items.map((s) => `
            <article class="scard" data-file="${esc(s.file)}" tabindex="0" role="button"
                     aria-label="Run ${esc(s.title)}, expects ${esc(s.expected)}">
              <div class="scard-top">
                <span class="scard-title">${esc(s.title)}</span>
                ${decisionBadge(s.expected)}
              </div>
              <p class="scard-why">${esc(s.why)}</p>
              <div class="scard-foot">
                <span class="scard-run">Run this &rarr;</span>
                <a class="dl" href="/api/samples/${encodeURIComponent(s.file)}/file?download=1"
                   download onclick="event.stopPropagation()">PDF</a>
              </div>
            </article>`).join('')}</div>
        </section>`;
    }).join('');

    $$('.scard', node).forEach((card) => {
      const go = (event) => {
        // The download link lives inside the card; clicking it must not also
        // start a run.
        if (event.target.closest('.dl')) return;
        runSample(card.dataset.file);
      };
      card.addEventListener('click', go);
      card.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); go(event); }
      });
    });
  }

  // --------------------------------------------------------- starting a run

  async function runSample(file) {
    try {
      const started = await api(`/api/samples/${encodeURIComponent(file)}/run`, { method: 'POST' });
      attachRun(started.run_id, file);
    } catch (err) {
      toast(`Could not start: ${err.message}`, true);
    }
  }

  async function uploadFile(file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name)) {
      toast('This pipeline reads PDFs. Pick a .pdf file.', true);
      return;
    }
    const form = new FormData();
    form.append('file', file);
    try {
      const started = await api('/api/runs', { method: 'POST', body: form });
      attachRun(started.run_id, file.name);
    } catch (err) {
      toast(`Upload failed: ${err.message}`, true);
    }
  }

  async function replayRun() {
    if (!state.run) return;
    const runId = state.run.run_id;
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/replay`, { method: 'POST' });
      attachRun(runId, state.run.filename);
      toast('Re-running with the thresholds currently in force');
    } catch (err) {
      toast(`Re-run failed: ${err.message}`, true);
    }
  }

  /** Point the run view at a run id and stream it. */
  function attachRun(runId, filename, intent = null) {
    if (state.source) { state.source.close(); state.source = null; }
    state.openStages.clear();
    state.run = { run_id: runId, filename, status: 'running' };
    state.stages = [];
    state.intent = intent;
    showResultView(true);
    $('#run-filename').textContent = filename || runId;
    $('#run-meta').innerHTML = '';
    $('#decision').hidden = true;
    $('#run-panes').hidden = true;
    $('#run-detail').hidden = false;
    $('#ai-summary').hidden = true;
    renderIntent();
    updateWorking();
    renderTimeline();
    showTab('run');

    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
    state.source = source;

    source.addEventListener('snapshot', (event) => {
      const payload = JSON.parse(event.data);
      state.stages = payload.stages || [];
      renderTimeline();
    });

    source.addEventListener('stage', (event) => {
      const payload = JSON.parse(event.data);
      const stage = payload.stage;
      if (!stage) return;
      const index = state.stages.findIndex((s) => s.seq === stage.seq);
      if (index >= 0) state.stages[index] = stage; else state.stages.push(stage);
      renderTimeline();
      updateWorking();
    });

    source.addEventListener('run_finished', () => { finishRun(runId); });

    source.addEventListener('error', (event) => {
      const data = event.data ? JSON.parse(event.data) : null;
      if (data && data.message) toast(data.message, true);
    });

    source.addEventListener('done', () => {
      source.close();
      state.source = null;
      finishRun(runId);
    });
  }

  let finishing = false;
  async function finishRun(runId) {
    if (finishing) return;
    finishing = true;
    try {
      const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
      state.run = run;
      state.stages = run.stages || state.stages;
      renderTimeline();
      renderIntent();
      renderDecision(run);
      renderRunMeta(run);
      loadHealth();
    } catch (err) {
      toast(`Could not load the finished run: ${err.message}`, true);
    } finally {
      finishing = false;
    }
  }

  async function loadRun(runId) {
    // Clearing the field is not enough - the banner is already on the page,
    // and without a re-render it would attribute one PO's declared expectation
    // and verdict to whatever run is opened next.
    state.intent = null;
    renderIntent();
    try {
      const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
      if (run.status === 'running') { attachRun(runId, run.filename); return; }
      if (state.source) { state.source.close(); state.source = null; }
      state.run = run;
      state.stages = run.stages || [];
      state.openStages.clear();
      showResultView(true);
      $('#run-filename').textContent = run.filename;
      updateWorking();
      renderRunMeta(run);
      renderTimeline();
      renderDecision(run);
      showTab('run');
    } catch (err) {
      toast(`Could not open run: ${err.message}`, true);
    }
  }

  function renderRunMeta(run) {
    const bits = [];
    if (run.status === 'failed') bits.push('<span class="badge badge-reject">failed</span>');
    if (run.pdf_kind) {
      bits.push(run.pdf_kind === 'scanned'
        ? '<span class="badge badge-flag">scanned - image only</span>'
        : '<span class="badge">text layer</span>');
    }
    if (run.page_count) bits.push(`<span class="badge">${run.page_count} page${run.page_count === 1 ? '' : 's'}</span>`);
    if (run.extraction_source === 'cache') {
      bits.push(`<span class="badge badge-accent" data-tip="${esc(
        '<div class=&quot;tt-head&quot;>Extraction reused</div>These exact bytes were read before, so ' +
        'stage 2 cost zero API calls. Rule R7 still treats the resubmission as a duplicate.')}">extraction cached</span>`);
    } else if (run.extraction_source === 'fixture') {
      bits.push(`<span class="badge badge-flag" data-tip="${esc(
        '<div class=&quot;tt-head&quot;>Offline extraction</div>Stage 2 came from a bundled ground-truth ' +
        'fixture rather than the model. Every stage after it is the real code path.')}">offline fixture</span>`);
    }
    if (run.duplicate_of) {
      bits.push(`<span class="badge badge-flag">duplicate of ${esc(run.duplicate_of)}</span>`);
    }
    bits.push(`<span class="badge">${duration(run.duration_ms)}</span>`);
    bits.push(`<span class="badge"><code>${esc((run.file_sha256 || '').slice(0, 12))}</code></span>`);
    const meta = $('#run-meta');
    meta.innerHTML = bits.join('');
    bindTooltips(meta);
  }

  // ---------------------------------------------------------------- timeline

  const STATUS_MARK = { done: '✓', failed: '✕', skipped: '–', pending: '', running: '' };

  /** The seven steps, told in order, as they happen.
   *
   *  This is the first thing on the run view rather than a collapsed section at
   *  the bottom, because watching the work is the point: a reviewer who cannot
   *  see what a system did has to take its verdict on trust.
   *
   *  Each step carries three things, in increasing detail and decreasing
   *  prominence. What the step is for, in one plain sentence - written here,
   *  not by the backend, and phrased in the future, present or past depending
   *  on where the run has got to. What it actually found on *this* invoice -
   *  the stage's own `summary`, straight from the audit row. And, one click
   *  away, everything it produced.
   */
  function renderTimeline() {
    const container = $('#timeline');
    if (!container) return;
    const stages = state.stages.length
      ? state.stages
      : Array.from({ length: 7 }, (_, i) => ({ seq: i + 1, label: '…', status: 'pending', payload: {} }));

    const done = stages.filter((x) => x.status === 'done' || x.status === 'skipped').length;
    container.style.setProperty('--story-done', String(done));
    container.style.setProperty('--story-total', String(stages.length));

    container.innerHTML = stages.map((stage, i) => {
      const phase = PHASES[stage.seq - 1]
        || { name: stage.label, will: '', doing: '', done: '' };
      const open = state.openStages.has(stage.seq);
      const interactive = stage.status !== 'pending';
      const body = interactive ? renderStageBody(stage) : '';

      const mark = stage.status === 'running' ? '<span class="spinner"></span>'
        : stage.status === 'done' ? '✓'
        : stage.status === 'failed' ? '✕'
        : stage.status === 'skipped' ? '–'
        : stage.seq;

      // The same sentence in three tenses, which is what makes a list of steps
      // read as a story rather than as a status table.
      const say = stage.status === 'pending' ? phase.will
        : stage.status === 'running' ? phase.doing
        : phase.done;

      const found = stage.error ? esc(stage.error) : esc(stage.summary || '');

      return `
        <li class="pstep is-${esc(stage.status)}${open ? ' is-open' : ''}" data-seq="${stage.seq}">
          <div class="pstep-rail"><span class="pstep-mark">${mark}</span></div>
          <div class="pstep-main">
            <div class="pstep-head">
              <span class="pstep-n">Step ${stage.seq}</span>
              <h4 class="pstep-name">${esc(phase.name)}</h4>
              ${stage.status === 'running' ? '<span class="pstep-live">happening now</span>' : ''}
              ${stage.status === 'skipped' ? '<span class="pstep-skip">not needed</span>' : ''}
              ${stage.status === 'failed' ? '<span class="pstep-fail">failed</span>' : ''}
              <span class="pstep-time">${stage.duration_ms != null ? duration(stage.duration_ms) : ''}</span>
            </div>
            <p class="pstep-say">${esc(say)}</p>
            ${found ? `<p class="pstep-found ${stage.error ? 'is-err' : ''}">${found}</p>` : ''}
            ${body ? `<button class="pstep-more" type="button">
                        <span class="caret">▸</span>
                        <span class="pstep-more-t">${open ? 'Hide' : 'Show'} what this step produced</span>
                      </button>
                      <div class="stage-body">${body}</div>` : ''}
          </div>
        </li>`;
    }).join('');

    $$('.pstep', container).forEach((node) => {
      const seq = Number(node.dataset.seq);
      const toggle = $('.pstep-more', node);
      if (!toggle) return;
      toggle.addEventListener('click', () => {
        const nowOpen = !state.openStages.has(seq);
        if (nowOpen) state.openStages.add(seq); else state.openStages.delete(seq);
        node.classList.toggle('is-open', nowOpen);
        $('.pstep-more-t', node).textContent =
          `${nowOpen ? 'Hide' : 'Show'} what this step produced`;
      });
    });

    // The heading is in the same tense as the steps under it.
    const title = $('#story-title');
    if (title) {
      const live = stages.some((x) => x.status === 'running' || x.status === 'pending');
      title.textContent = live ? 'How this is being processed' : 'How this was processed';
    }

    // Folded, the panel still has to say what is behind it - otherwise it is a
    // heading with no indication that there are seven steps under it, or how
    // they went.
    const summary = $('#story-summary');
    if (summary) {
      const failed = stages.filter((x) => x.status === 'failed').length;
      const settled = stages.filter((x) => x.status === 'done' || x.status === 'skipped').length;
      summary.textContent = failed
        ? `${stages.length} steps · ${failed} failed`
        : settled >= stages.length
          ? `All ${stages.length} steps, in order. Open to read them.`
          : `Step ${Math.min(settled + 1, stages.length)} of ${stages.length} — still running.`;
    }

    bindTooltips(container);
    const expandBtn = $('#expand-btn');
    if (expandBtn) {
      expandBtn.textContent = state.openStages.size >= stages.length ? 'Collapse all' : 'Expand all';
    }
  }

  // --------------------------------------------------- stage body renderers

  /** Build a provenance marker for one field, from the verified provenance list. */
  function provMarker(field) {
    const items = (state.run && state.run.provenance) || provenanceFromStages();
    const item = items.find((p) => p.field === field);
    if (!item) return '';
    const verified = item.verified;
    const cls = verified === true ? 'prov-ok' : verified === false ? 'prov-bad' : 'prov-unknown';
    const glyph = verified === true ? '✓' : verified === false ? '!' : '?';
    const head = verified === true
      ? `Verified on page ${item.page}`
      : verified === false
        ? 'Could not be found in the document'
        : 'Not verifiable - no text layer';
    const foot = verified === true
      ? `fuzzy match ${num(item.match_score, 2)} against the PDF's own text`
      : verified === false
        ? 'the model quoted text that is not in the extracted text layer'
        : 'this page is an image, so there is nothing to check the quote against';
    const tip = `<div class="tt-head">${head}</div>`
      + `<span class="tt-quote">${esc(item.quote || '')}</span>`
      + `<div class="tt-foot">${foot}</div>`;
    return `<span class="prov ${cls}" data-tip="${esc(tip)}">${glyph}</span>`;
  }

  function provenanceFromStages() {
    const stage = state.stages.find((s) => s.stage === 'NORMALIZE');
    return (stage && stage.payload && stage.payload.provenance) || [];
  }

  function kv(rows) {
    return `<dl class="kv">${rows
      .filter(Boolean)
      .map(([label, value]) => `<dt>${esc(label)}</dt><dd>${value}</dd>`)
      .join('')}</dl>`;
  }

  function sub(title, html) {
    return `<div class="sub"><div class="sub-title">${esc(title)}</div>${html}</div>`;
  }

  function lineItemsTable(items, currency) {
    if (!items || !items.length) {
      return '<p class="hint">Not itemised - this vendor bills as a single bundled line.</p>';
    }
    return `<table class="mini">
      <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead>
      <tbody>${items.map((item) => `<tr>
        <td>${plain(item.description)}</td>
        <td class="num">${item.quantity == null ? '—' : esc(item.quantity)}</td>
        <td class="num">${money(item.unit_price)}</td>
        <td class="num">${money(item.line_total)}</td></tr>`).join('')}</tbody></table>`;
  }

  const RENDERERS = {

    INTAKE(payload) {
      const prior = payload.prior_submission;
      return kv([
        ['File', plain(payload.filename)],
        ['SHA-256', `<span class="mono">${esc(payload.file_sha256 || '')}</span>`],
        ['Size', payload.file_bytes ? `${(payload.file_bytes / 1024).toFixed(0)} KB` : '—'],
        ['Pages', plain(payload.page_count)],
        ['Document type', payload.pdf_kind === 'scanned'
          ? '<span class="badge badge-flag">image only</span>'
          : '<span class="badge">digital text</span>'],
        ['Extractable text', payload.text_layer_chars != null
          ? `${payload.text_layer_chars.toLocaleString()} characters` : '—'],
      ])
      + `<div class="note${payload.pdf_kind === 'scanned' ? ' note-warn' : ''}">${esc(payload.text_layer_note || '')}</div>`
      + (prior ? `<div class="note note-warn">These exact bytes were processed before, as run
          <code>${esc(prior.run_id)}</code> on ${when(prior.started_at)} (decision
          ${esc(prior.decision || 'n/a')}). Rule R7 will treat this as a duplicate submission.</div>` : '')
      + (payload.replay ? `<div class="note note-info">This is a re-run of the same run id, not a new
          submission, so it is not counted against itself for duplicate detection.</div>` : '');
    },

    EXTRACT(payload) {
      const extraction = payload.extraction || {};
      const source = payload.source || (payload.cached ? 'cache' : 'live');
      const head = kv([
        ['Source', source === 'live'
          ? `<span class="badge badge-accent">live API call</span>`
          : source === 'cache'
            ? `<span class="badge badge-accent">cached from ${esc(payload.cache_source_run || '')}</span>`
            : `<span class="badge badge-flag">offline fixture</span>`],
        source === 'live' ? ['Model', `${esc(payload.model)} · effort ${esc(payload.effort)}`] : null,
        source === 'live' ? ['Latency', duration(payload.latency_ms)] : null,
        source === 'live' ? ['Tokens', `${payload.input_tokens} in · ${payload.output_tokens} out`] : null,
        ['Model confidence', num(extraction.model_confidence, 2)],
      ]);

      const fields = kv([
        ['Vendor', plain(extraction.vendor_name) + provMarker('vendor_name')],
        ['GSTIN', plain(extraction.vendor_gstin) + provMarker('vendor_gstin')],
        ['Invoice number', plain(extraction.invoice_number) + provMarker('invoice_number')],
        ['Invoice date', plain(extraction.invoice_date) + provMarker('invoice_date')],
        ['PO reference', (extraction.po_reference
          ? `<code>${esc(extraction.po_reference)}</code>`
          : '<span class="badge badge-flag">none stated</span>') + provMarker('po_reference')],
        ['Subtotal', money(extraction.subtotal) + provMarker('subtotal')],
        ['Tax', (extraction.tax_amount == null
          ? '<span class="badge">not stated separately</span>'
          : money(extraction.tax_amount)) + provMarker('tax_amount')],
        ['Total', `<b>${money(extraction.total_amount, extraction.currency)}</b>` + provMarker('total_amount')],
      ]);

      return head
        + sub('Fields read from the document', fields
            + '<p class="hint">Hover a marker to see the page and the exact text each value was '
            + 'read from, and whether that quote was found in the document.</p>')
        + sub('Line items', lineItemsTable(extraction.line_items))
        + (extraction.notes ? `<div class="note">Model note: ${esc(extraction.notes)}</div>` : '')
        + (payload.note ? `<div class="note note-info">${esc(payload.note)}</div>` : '')
        + (payload.fallback_from_error
            ? `<div class="note note-warn">Live call failed, fell back to the fixture:
               ${esc(payload.fallback_from_error)}</div>` : '');
    },

    NORMALIZE(payload) {
      const normalized = payload.normalized || {};
      const confidence = payload.confidence || {};
      const transforms = (payload.transformations || []).filter((t) => t.after != null || t.before != null);

      const transformTable = `<table class="mini">
        <thead><tr><th>Field</th><th>As printed</th><th>Canonical</th><th>Why</th></tr></thead>
        <tbody>${transforms.map((t) => `<tr>
          <td><code>${esc(t.field)}</code></td>
          <td>${plain(t.before)}</td>
          <td><b>${plain(t.after)}</b></td>
          <td>${esc(t.note)}</td></tr>`).join('')}</tbody></table>`;

      const provRows = (payload.provenance || []).map((item) => {
        const verified = item.verified;
        const badge = verified === true
          ? `<span class="badge badge-approve">verified</span>`
          : verified === false
            ? `<span class="badge badge-reject">not found</span>`
            : `<span class="badge badge-flag">unverifiable</span>`;
        return `<tr>
          <td><code>${esc(item.field)}</code></td>
          <td class="num">${item.page == null ? '—' : item.page}</td>
          <td>${esc(item.quote || '')}</td>
          <td>${badge}</td>
          <td class="num">${item.match_score == null ? '—' : num(item.match_score, 2)}</td></tr>`;
      }).join('');

      const ratio = confidence.provenance_verified_ratio;
      const confBar = `<div class="consumed">
        <div class="bar ${confidence.final >= 0.75 ? 'is-ok' : 'is-warn'}">
          <span style="width:${Math.round((confidence.final || 0) * 100)}%"></span>
        </div><span class="pct">${num(confidence.final, 2)}</span></div>`;

      return kv([
        ['Total', `<b>${money(normalized.total_amount, normalized.currency)}</b>`],
        ['Line items sum', money(normalized.line_items_sum)],
        ['Invoice date', `${plain(normalized.invoice_date)} <span class="hint" style="display:inline">
            (printed as ${plain(normalized.invoice_date_raw)})</span>`],
        ['Extraction confidence', confBar],
      ])
      + sub('What the code changed, and why', transformTable)
      + sub('Provenance - every value traced back to the document',
          payload.text_layer_available
            ? `<table class="mini"><thead><tr><th>Field</th><th class="num">Page</th>
               <th>Quoted text</th><th>Check</th><th class="num">Score</th></tr></thead>
               <tbody>${provRows}</tbody></table>`
            : `<div class="note note-warn">This document has no text layer, so no quoted value can be
               checked against the file itself. The quotes below are what the model reported reading;
               none of them is verifiable, which is why confidence is reduced and R9 will require a
               human.</div><table class="mini"><thead><tr><th>Field</th><th class="num">Page</th>
               <th>Reported</th><th>Check</th><th class="num">Score</th></tr></thead>
               <tbody>${provRows}</tbody></table>`)
      + `<div class="note">Confidence ${num(confidence.final, 2)} = ${esc(confidence.explanation || '')}.
          ${ratio != null ? `Verified ${confidence.provenance_fields_verified} of
          ${confidence.provenance_fields_total} decision-critical fields.` : ''}</div>`;
    },

    RESOLVE_VENDOR(payload) {
      const vendor = payload.vendor || {};
      const rows = (vendor.considered || []).map((candidate) => `<tr>
        <td><code>${esc(candidate.vendor_id)}</code></td>
        <td>${esc(candidate.name)}</td>
        <td>${esc(candidate.via || '')}</td>
        <td><div class="consumed"><div class="bar ${candidate.score >= payload.floor ? 'is-ok' : ''}">
          <span style="width:${Math.round(candidate.score * 100)}%"></span></div>
          <span class="pct">${num(candidate.score, 2)}</span></div></td></tr>`).join('');

      const status = vendor.status === 'matched'
        ? `<span class="badge badge-approve">${esc(vendor.vendor_name)} (${esc(vendor.vendor_id)})</span>`
        : vendor.status === 'blocked'
          ? `<span class="badge badge-reject">${esc(vendor.vendor_name)} — blocked for payment</span>`
          : '<span class="badge badge-reject">not on the approved list</span>';

      return kv([
        ['Result', status],
        ['Score', num(vendor.score, 2)],
        ['Match floor', num(payload.floor, 2)],
        ['Matched on', plain(payload.matched_on)],
        ['Vendors searched', plain(payload.vendors_in_master)],
      ])
      + sub('Candidates considered', `<table class="mini">
          <thead><tr><th>ID</th><th>Name</th><th>Best alias</th><th>Score</th></tr></thead>
          <tbody>${rows}</tbody></table>`);
    },

    MATCH_PO(payload) {
      const po = payload.po || {};
      const cards = (po.candidates || []).map((candidate, index) => {
        const isWinner = po.matched && candidate.po_number === po.po_number;
        const parts = Object.entries(candidate.score_breakdown || {})
          .map(([key, value]) => `<span class="part">${esc(key)}
             <div class="bar"><span style="width:${Math.round(Math.min(1, value) * 100)}%"></span></div>
             ${num(value, 2)}</span>`).join('');
        return `<div class="cand${isWinner ? ' is-win' : ''}">
          <div class="cand-head">
            <span class="sample-n">${index + 1}</span>
            <span class="po">${esc(candidate.po_number)}</span>
            ${isWinner ? '<span class="badge badge-approve">matched</span>' : ''}
            <span class="score">${num(candidate.score, 2)}</span>
          </div>
          <div class="cand-desc">${esc(candidate.description || '')} ·
            value ${money(candidate.po_value)} · billed ${money(candidate.already_billed)} ·
            remaining ${money(candidate.remaining)}</div>
          <div class="cand-parts">${parts}
            <span class="part" style="color:var(--ink-4)">${esc(candidate.weights || '')}</span></div>
          <div class="cand-reason">${esc(candidate.reason || '')}</div>
        </div>`;
      }).join('');

      return kv([
        ['Reference on invoice', payload.invoice_po_reference
          ? `<code>${esc(payload.invoice_po_reference)}</code>`
          : '<span class="badge badge-flag">none stated</span>'],
        ['Result', po.matched
          ? `<span class="badge badge-approve">${esc(po.po_number)} at ${num(po.score, 2)}</span>`
          : '<span class="badge badge-flag">no PO cleared the floor</span>'],
        ['Strategy', esc((po.strategy || '').replace(/_/g, ' '))],
        ['Match floor', num(payload.floor, 2)],
      ])
      + `<div class="note">${esc(po.note || '')}</div>`
      + sub('Candidates, ranked', cards || '<p class="hint">No candidate purchase orders.</p>');
    },

    VALIDATE(payload) {
      const rules = payload.rules || [];
      const rows = rules.map((rule) => {
        const cls = rule.skipped ? 'is-skip' : rule.passed ? '' : 'is-fail';
        const badge = rule.skipped
          ? '<span class="badge">not applicable</span>'
          : rule.passed
            ? '<span class="badge badge-approve">pass</span>'
            : `<span class="badge badge-${decisionClass(rule.severity)}">${esc(rule.severity)}</span>`;
        return `<div class="rule-row ${cls}">
          <div class="rule-id">${esc(rule.rule_id)}</div>
          <div>
            <div class="rule-title">${esc(rule.title)}</div>
            <div class="rule-msg">${esc(rule.message)}</div>
            ${rule.note ? `<div class="rule-note">${esc(rule.note)}</div>` : ''}
            ${rule.skipped ? '' : `<div class="rule-ea">
              <span>expected <b>${esc(rule.expected)}</b></span>
              <span>actual <b>${esc(rule.actual)}</b></span></div>`}
          </div>
          <div>${badge}</div>
        </div>`;
      }).join('');

      const ledger = payload.po_ledger;
      return `<div class="rules-list">${rows}</div>`
        + (ledger ? sub('PO ledger used by R5 and R6', kv([
            ['Purchase order', `<code>${esc(ledger.po_number)}</code> — ${esc(ledger.description || '')}`],
            ['PO value', money(ledger.po_value)],
            ['Already billed', money(ledger.already_billed)],
            ['Remaining', money(ledger.remaining)],
          ])) : '')
        + sub('Thresholds applied', `<table class="mini"><tbody>${
            Object.entries(payload.config || {}).map(([key, value]) =>
              `<tr><td><code>${esc(key)}</code></td><td class="num">${esc(value)}</td></tr>`).join('')
          }</tbody></table>`);
    },

    DECIDE(payload) {
      const decision = payload.decision || {};
      const booked = payload.po_consumption_booked;
      return kv([
        ['Decision', decisionBadge(decision.decision)],
        ['Failed rules', decision.failed_rules && decision.failed_rules.length
          ? decision.failed_rules.map((id) => `<code>${esc(id)}</code>`).join(' ')
          : 'none'],
        ['Requires a human', decision.requires_human ? 'yes' : 'no'],
      ])
      + `<div class="note">${esc(payload.logic || '')}</div>`
      + `<div class="note">${esc(payload.confidence_note || '')}</div>`
      + (booked ? `<div class="note note-info">Approved, so ${money(booked.amount)} was booked
          against <code>${esc(booked.po_number)}</code>. That consumption is what rule R6 will see
          on the next invoice billed to the same PO.</div>` : '');
    },
  };

  function renderStageBody(stage) {
    if (stage.status === 'skipped') {
      return `<p class="hint">${esc(stage.summary || 'Not applicable.')}</p>`;
    }
    if (stage.status === 'failed') {
      return `<div class="note note-warn">${esc(stage.error || 'Stage failed.')}</div>`;
    }
    const payload = stage.payload || {};
    if (!Object.keys(payload).length) return '';
    const renderer = RENDERERS[stage.stage];
    if (!renderer) return `<pre class="note">${esc(JSON.stringify(payload, null, 2))}</pre>`;
    try {
      return renderer(payload);
    } catch (err) {
      return `<div class="note note-warn">Could not render this stage: ${esc(err.message)}</div>`;
    }
  }

  // --------------------------------------------------------------- decision

  /** Swap between the intake hero and the result view. */
  function showResultView(on) {
    $('#run-intake').hidden = !!on;
    $('#run-result').hidden = !on;
    if (!on) { state.intent = null; renderIntent(); }
  }

  // Short names for the tracker, plus what each stage is actually doing. The
  // stage rows keep the precise engineering labels; these are what a reviewer
  // reads while waiting, and they are the difference between a progress bar
  // and understanding what the system is busy with.
  const PHASES = [
    {
      name: 'Read file',
      will: 'Will open the PDF, fingerprint it, and check whether it has real text or is a scan.',
      doing: 'Opening the PDF, fingerprinting it, and checking whether it has real text or is a scan.',
      done: 'Opened the file and checked whether it has a text layer.',
    },
    {
      name: 'Extract',
      will: 'Will send the page to a model to pull out the vendor, dates, amounts and PO reference.',
      doing: 'A model is reading the page and pulling out the vendor, dates, amounts and PO reference.',
      done: 'Read the fields off the page.',
    },
    {
      name: 'Verify',
      will: 'Will check each extracted figure against the text actually printed in the document.',
      doing: 'Checking each extracted figure against the text actually printed in the document.',
      done: 'Checked every figure against the document itself.',
    },
    {
      name: 'Vendor',
      will: 'Will look the supplier up on the approved vendor list.',
      doing: 'Looking the supplier up on the approved vendor list.',
      done: 'Looked the supplier up on the approved list.',
    },
    {
      name: 'Match PO',
      will: 'Will score every plausible purchase order to find which one this bills against.',
      doing: 'Scoring every plausible purchase order to find which one this bills against.',
      done: 'Found the purchase order this bills against.',
    },
    {
      name: 'Check rules',
      will: 'Will run all nine checks - totals, tolerances, duplicates, dates, confidence.',
      doing: 'Running all nine checks - totals, tolerances, duplicates, dates, confidence.',
      done: 'Ran all nine checks.',
    },
    {
      name: 'Decide',
      will: 'Will combine the results: the most serious failure sets the outcome.',
      doing: 'Combining the results: the most serious failure sets the outcome.',
      done: 'Reached a decision.',
    },
  ];

  /** The phase tracker.
   *
   *  Upload UIs that show only a spinner get read as stalled the moment one
   *  step takes longer than the others - and extraction here takes 4-40s
   *  depending on provider, far longer than its neighbours. Naming the phases
   *  and marking which one is live makes a slow step legible instead of
   *  worrying.
   */
  function updateWorking() {
    const node = $('#tracker');
    if (!node) return;
    const bySeq = new Map(state.stages.map((s) => [s.seq, s]));
    node.innerHTML = PHASES.map((phase, i) => {
      const stage = bySeq.get(i + 1);
      const status = stage ? stage.status : 'pending';
      const mark = status === 'done' ? '✓'
        : status === 'failed' ? '✕'
        : status === 'skipped' ? '–'
        : status === 'running' ? '' : i + 1;
      // Hovering a phase shows what that step actually did on *this* invoice -
      // the stage's own summary and timing - rather than a generic description.
      // Before it runs there is nothing to report, so it explains what it will
      // do instead.
      const tip = stage && stage.summary
        ? `<div class="tt-head">${esc(phase.name)}${
            stage.duration_ms != null ? ` · ${duration(stage.duration_ms)}` : ''
          }</div>${esc(stage.summary)}`
        : `<div class="tt-head">${esc(phase.name)}</div>${esc(phase.doing)}`;
      return `<li class="ph is-${esc(status)}" data-tip="${esc(tip)}">
        <span class="ph-dot">${mark}</span>
        <span class="ph-name">${esc(phase.name)}</span>
      </li>`;
    }).join('');
    bindTooltips(node);

    // Narrate the live stage. Once everything is terminal this clears, because
    // the verdict below it is then the thing to read.
    const now = $('#now-doing');
    if (now) {
      const running = state.stages.find((x) => x.status === 'running');
      if (running && PHASES[running.seq - 1]) {
        now.hidden = false;
        now.innerHTML = `<span class="now-dot"></span>${esc(PHASES[running.seq - 1].doing)}`;
      } else {
        now.hidden = true;
      }
    }
  }

  /** Turn record identifiers in rule text into links to the record itself.
   *
   *  A rule saying "byte-identical to run RUN-20261101-023136-099" is precise
   *  and useless to act on: the reviewer's next question is always "show me
   *  that one". PO numbers and run ids are the two things the rules name, and
   *  both have a record in this app, so both become one click.
   *
   *  Escapes first, then substitutes - the input is rule text, but it reaches
   *  the DOM as HTML and nothing here should ever be exempt from escaping.
   */
  function linkify(text) {

    let out = esc(text == null ? '' : String(text));
    out = out.replace(/(RUN-\d{8}-\d{6}-\d{3})/g,
      '<a class="reclink" href="#" data-run="$1" title="Open this run">$1</a>');
    out = out.replace(/(PO-\d+)/g,
      '<a class="reclink" href="#" data-po="$1" title="Open this purchase order">$1</a>');
    return out;
  }

  /** Record links are rendered into innerHTML all over the result view, so bind
   *  them once at the document level rather than after every render. */
  document.addEventListener('click', (event) => {
    const goto = event.target.closest('[data-goto]');
    if (goto) { event.preventDefault(); showTab(goto.dataset.goto); return; }
    const link = event.target.closest('.reclink');
    if (!link) return;
    event.preventDefault();
    if (link.dataset.run) loadRun(link.dataset.run);
    else if (link.dataset.po) showPo(link.dataset.po);
  });

  /** A purchase order, with what has been billed against it. */
  async function showPo(poNumber) {
    try {
      const po = await api(`/api/pos/${encodeURIComponent(poNumber)}`);
      const rows = (po.consumption || []).map((c) => `
        <tr>
          <td><a class="reclink" href="#" data-run="${esc(c.run_id)}">${esc(c.invoice_number || c.run_id)}</a></td>
          <td class="num">${money(c.amount, po.currency || 'INR')}</td>
          <td>${when(c.booked_at)}</td>
        </tr>`).join('');
      openSheet(`Purchase order ${esc(po.po_number)}`, `
        <div class="facts facts-sheet">
          <div class="fact"><div class="fact-k">Vendor</div><div class="fact-v">${esc(po.vendor_name || '—')}</div></div>
          <div class="fact"><div class="fact-k">PO value</div><div class="fact-v">${money(po.po_value, 'INR')}</div></div>
          <div class="fact"><div class="fact-k">Already billed</div><div class="fact-v">${money(po.already_billed, 'INR')}</div></div>
          <div class="fact"><div class="fact-k">Remaining</div><div class="fact-v">${money(po.remaining, 'INR')}</div></div>
        </div>
        ${po.description ? `<p class="sheet-note">${esc(po.description)}</p>` : ''}
        ${rows ? `<table class="grid sheet-grid"><thead><tr>
            <th>Billed by</th><th class="num">Amount</th><th>When</th>
          </tr></thead><tbody>${rows}</tbody></table>`
          : '<p class="sheet-note">Nothing has been billed against this PO yet.</p>'}`);
    } catch (err) {
      toast(`Could not open ${poNumber}: ${err.message}`, true);
    }
  }

  /** A light slide-over for a referenced record, so following a link never
   *  loses the run you were reading. */
  function openSheet(title, html) {
    let sheet = $('#sheet');
    if (!sheet) {
      sheet = document.createElement('div');
      sheet.id = 'sheet';
      sheet.className = 'sheet';
      sheet.innerHTML = `<div class="sheet-back"></div>
        <div class="sheet-panel" role="dialog" aria-modal="true">
          <div class="sheet-head"><h3 id="sheet-title"></h3>
            <button class="btn btn-ghost btn-sm" id="sheet-close" type="button">Close</button></div>
          <div class="sheet-body" id="sheet-body"></div>
        </div>`;
      document.body.appendChild(sheet);
      const close = () => { sheet.hidden = true; };
      $('.sheet-back', sheet).addEventListener('click', close);
      $('#sheet-close', sheet).addEventListener('click', close);
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') close();
      });
    }
    $('#sheet-title', sheet).textContent = title.replace(/<[^>]*>/g, '');
    $('#sheet-body', sheet).innerHTML = html;
    sheet.hidden = false;
  }

  /** The model's plain-English read of the problem, fetched after the verdict.
   *
   *  Deliberately loaded separately and after the fact: the decision is already
   *  on screen and correct without it, so a slow or unavailable model delays
   *  nothing that matters. It is labelled as model-written because it is the
   *  one piece of prose on this screen that a model produced.
   */
  async function loadSummary(run) {
    const node = $('#ai-summary');
    if (!node || !run || !run.decision) return;
    node.hidden = false;
    node.className = 'aisum is-loading';
    node.innerHTML = `<div class="ai-badge">AI summary</div>
      <div class="ai-text"><span class="ai-skel"></span><span class="ai-skel"></span></div>`;
    try {
      const out = await api(`/api/runs/${encodeURIComponent(run.run_id)}/explain`, { method: 'POST' });
      node.className = 'aisum';
      node.innerHTML = `
        <div class="ai-badge">${out.source === 'model' ? 'AI summary' : 'Summary'}</div>
        <p class="ai-text">${esc(out.summary)}</p>
        <div class="ai-foot">${out.source === 'model'
          ? `Written by ${esc(out.model || out.provider || 'a model')} from the rule results. The decision above was made by the rules, not by this.`
          : 'Composed from the rule output - no model was available.'}</div>`;
    } catch (err) {
      // An explanation is a convenience; its absence must not read as an error.
      node.hidden = true;
    }
  }

  /** The document itself, beside what was read off it. */
  function renderDocView(run) {
    const node = $('#docview');
    if (!node || !run || !run.run_id) return;
    const url = `/api/runs/${encodeURIComponent(run.run_id)}/file`;
    node.innerHTML = `<iframe src="${esc(url)}#toolbar=0&navpanes=0&view=FitH"
                        title="The invoice PDF" loading="lazy"></iframe>`;
  }

  const VERDICT_COPY = {
    APPROVE: {
      icon: '✓',
      line: 'This invoice can be paid.',
      sub: 'Every check passed. No one needs to open it.',
    },
    FLAG: {
      icon: '!',
      line: 'A person should look at this.',
      sub: 'Nothing here is necessarily wrong - but it is not safe to pay automatically.',
    },
    REJECT: {
      icon: '✕',
      line: 'Do not pay this invoice.',
      sub: 'A blocking check failed.',
    },
  };

  function renderDecision(run) {
    const node = $('#decision');
    const decision = run.decision;

    if (!decision) {
      if (run.status === 'failed') {
        node.hidden = false;
        node.className = 'verdict is-reject';
        node.innerHTML = `
          <div class="v-mark">✕</div>
          <div class="v-text">
            <div class="v-line">The run could not finish.</div>
            <div class="v-sub">${esc(run.error || 'No further detail was recorded.')}</div>
          </div>`;
        return;
      }
      node.hidden = true;
      return;
    }

    const verdict = decision.decision;
    const copy = VERDICT_COPY[verdict] || { icon: '?', line: verdict, sub: '' };
    // The headline reason is the one that decided it. The rest are detail and
    // live under the checks list, so the verdict is readable in one glance.
    const headline = decision.reasons && decision.reasons.length ? decision.reasons[0] : copy.sub;

    node.hidden = false;
    node.className = `verdict is-${decisionClass(verdict)}`;
    node.innerHTML = `
      <div class="v-mark" aria-hidden="true">${copy.icon}</div>
      <div class="v-text">
        <div class="v-word">${esc(verdict)}</div>
        <div class="v-line">${esc(copy.line)}</div>
        <div class="v-sub">${esc(headline)}</div>
      </div>
      ${decision.requires_human ? '<div class="v-tag">needs a human</div>' : ''}`;

    renderFacts(run);
    renderCheckedAgainst(run);
    renderChecks(run);
    renderDocView(run);
    $('#run-panes').hidden = false;
    $('#run-detail').hidden = false;
    loadSummary(run);
  }

  /** The reference records this invoice was actually measured against.
   *
   *  The facts panel says the invoice names PO-4471; this says what PO-4471
   *  *is* - its value, what was already billed against it, what that leaves -
   *  which is the half of the comparison the document cannot tell you. Without
   *  it "exceeds the PO" is a claim; with it, it is arithmetic you can check.
   *
   *  Everything here comes from the run's own stored record, so it is what the
   *  rules actually read at the time, not a fresh lookup that may since have
   *  moved.
   */
  function renderCheckedAgainst(run) {
    const node = $('#checked-against');
    if (!node) return;
    const vendor = run.vendor || {};
    const po = run.po || {};
    const winner = (po.candidates || []).find((c) => c.po_number === po.po_number);

    const vendorBlock = `
      <div class="ca-item">
        <div class="ca-k">Vendor master</div>
        ${vendor.vendor_id ? `
          <div class="ca-v">${esc(vendor.vendor_name)}</div>
          <dl class="ca-dl">
            <div><dt>Record</dt><dd><code>${esc(vendor.vendor_id)}</code></dd></div>
            <div><dt>Status</dt><dd>${vendor.status === 'matched'
              ? '<span class="ca-ok">on the approved list</span>'
              : `<span class="ca-bad">${esc(vendor.status)}</span>`}</dd></div>
            <div><dt>Matched</dt><dd>${esc((vendor.considered || [])[0]?.via || '')} ·
                 score ${num(vendor.score, 2)}</dd></div>
          </dl>`
          : `<div class="ca-v ca-none">No vendor on the master matched</div>
             <p class="ca-note">Best score ${num(vendor.score, 2)}, below the floor. This is
                what rule R3 rejected on.</p>`}
      </div>`;

    const poBlock = `
      <div class="ca-item">
        <div class="ca-k">Purchase order</div>
        ${winner ? `
          <div class="ca-v"><a class="reclink" href="#" data-po="${esc(winner.po_number)}">${esc(winner.po_number)}</a>
            <span class="ca-sub">${esc(winner.description || '')}</span></div>
          <dl class="ca-dl">
            <div><dt>PO value</dt><dd>${money(winner.po_value, 'INR')}</dd></div>
            <div><dt>Already billed</dt><dd>${money(winner.already_billed, 'INR')}</dd></div>
            <div><dt>Left before this</dt><dd><b>${money(winner.remaining, 'INR')}</b></dd></div>
            <div><dt>This invoice</dt><dd>${money((run.normalized || {}).total_amount, 'INR')}</dd></div>
            <div><dt>Matched by</dt><dd>${esc((winner.reason || '').split(';')[0])}</dd></div>
          </dl>`
          : `<div class="ca-v ca-none">No purchase order matched</div>
             <p class="ca-note">${esc(po.note || 'Nothing scored above the match floor.')}</p>`}
      </div>`;

    const otherCands = (po.candidates || []).filter((c) => c.po_number !== po.po_number).slice(0, 3);
    const alsoBlock = otherCands.length ? `
      <details class="ca-also">
        <summary>${otherCands.length} other purchase order${otherCands.length === 1 ? '' : 's'} were scored</summary>
        <ul class="ca-cands">${otherCands.map((c) => `
          <li><a class="reclink" href="#" data-po="${esc(c.po_number)}">${esc(c.po_number)}</a>
              <span>${esc(c.description || '')}</span>
              <b>${num(c.score, 2)}</b></li>`).join('')}</ul>
      </details>` : '';

    node.hidden = false;
    node.innerHTML = `
      <div class="pane-head">What it was checked against</div>
      <div class="ca-body">${vendorBlock}${poBlock}${alsoBlock}</div>`;
  }

  /** The four things a person actually wants off an invoice. */
  function renderFacts(run) {
    const node = $('#run-facts');
    const inv = run.normalized || {};
    const po = run.po || {};
    const money = inv.total_amount != null
      ? `${esc(inv.currency || 'INR')} ${num(inv.total_amount, 2)}`
      : '—';
    const cells = [
      ['Vendor', inv.vendor_name_raw || inv.vendor_name_canonical || '—'],
      ['Invoice no.', inv.invoice_number || 'not printed'],
      ['Amount', money],
      ['Purchase order', po.matched
        ? `<a class="reclink" href="#" data-po="${esc(po.po_number)}">${esc(po.po_number)}</a>`
        : 'none matched'],
    ];
    // Every cell but the PO is plain text and is escaped; the PO cell is built
    // as markup above so it can link to the record it was checked against.
    node.innerHTML = cells.map(([k, v], i) => `
      <div class="fact">
        <div class="fact-k">${esc(k)}</div>
        <div class="fact-v">${i === 3 ? v : esc(String(v))}</div>
      </div>`).join('');
  }

  /** What was checked - failures first, the passes folded away. */
  function renderChecks(run) {
    const node = $('#run-checks');
    const rules = run.rules || [];
    if (!rules.length) { node.innerHTML = ''; return; }

    // A rule is skipped, passed, or failed - in that order of precedence,
    // because a skipped rule also reports passed=true and must not be counted
    // as a check that actually ran.
    const kind = (r) => (r.skipped ? 'skip' : r.passed ? 'pass' : 'fail');
    const failed = rules.filter((r) => kind(r) === 'fail');
    const passed = rules.filter((r) => kind(r) === 'pass');
    const skipped = rules.filter((r) => kind(r) === 'skip');

    const row = (r) => {
      const k = kind(r);
      return `
      <li class="check is-${esc(k)}">
        <span class="check-mark">${k === 'pass' ? '✓' : k === 'fail' ? '✕' : '–'}</span>
        <span class="check-body">
          <span class="check-name">${esc(r.title || r.rule_id)}</span>
          ${r.message ? `<span class="check-detail">${linkify(r.message)}</span>` : ''}
          ${k === 'fail' && r.expected ? `<span class="check-cmp">
             <span class="cmp-row"><b>Looked for</b> ${linkify(r.expected)}</span>
             <span class="cmp-row"><b>Found</b> ${linkify(r.actual || '—')}</span>
           </span>` : ''}
        </span>
        <span class="check-id">${esc(r.rule_id)}</span>
      </li>`;
    };

    node.innerHTML = `
      <div class="checks-head">
        <h3>${failed.length ? 'What went wrong' : 'What was checked'}</h3>
        <span class="checks-count">${passed.length} of ${rules.length - skipped.length} checks passed</span>
      </div>
      ${failed.length ? `
        <p class="checks-lead">${esc(
          failed.length === 1
            ? 'One check did not pass. Here is what it looked for and what it found instead.'
            : `${failed.length} checks did not pass. Each one shows what it looked for and what it found instead.`
        )}</p>
        <ul class="check-list">${failed.map(row).join('')}</ul>` : ''}
      ${passed.length || skipped.length ? `
        <details class="checks-rest">
          <summary>${failed.length ? `Show the ${passed.length} checks that passed` : 'Show every check'}</summary>
          <ul class="check-list">${passed.concat(skipped).map(row).join('')}</ul>
        </details>` : ''}`;
  }




  /** Who we are allowed to pay. Small, but the question "which vendors are on
   *  the master?" was previously only answerable three clicks away. */
  async function renderVendorStrip() {
    const node = $('#vendors-strip');
    if (!node) return;
    try {
      const vendors = await api('/api/vendors');
      const blocked = vendors.filter((v) => v.status !== 'approved').length;
      node.innerHTML = `
        <div class="vs-head">
          <h3>Approved vendors</h3>
          <span class="vs-note">${vendors.length} on the master${
            blocked ? ` · ${blocked} blocked` : ''}. Only these can be paid - rule R3.</span>
        </div>
        <div class="vs-list">${vendors.map((v) => `
          <span class="vs-item ${v.status !== 'approved' ? 'is-blocked' : ''}"
                data-tip="${esc(`<div class="tt-head">${v.name}</div>${v.vendor_id} · GSTIN ${v.gstin || '—'}`
                  + (v.status !== 'approved' ? '<br>Blocked: invoices from this vendor are rejected.' : ''))}">
            <i class="vs-dot"></i>${esc(v.name)}${
              v.status !== 'approved' ? '<b class="vs-tag">blocked</b>' : ''}
          </span>`).join('')}</div>`;
      bindTooltips(node);
    } catch (_) { node.innerHTML = ''; }
  }

  // ----------------------------------------------------------------- layouts

  /* Why this tab exists: "it extracts invoices" means little until you see that
   * the five documents genuinely differ. Each vendor hides the same facts in a
   * different place, so the extraction step is reading a document rather than
   * filling in a known form. */
  const LAYOUT_NOTES = {
    meridian: {
      vendor: 'Meridian Office Supplies',
      shape: 'Clean itemised table',
      tax: 'CGST + SGST broken out separately',
      po: 'A labelled field in the header',
      hard: 'The easy case, and the baseline the others are measured against.',
    },
    sharma: {
      vendor: 'Sharma Steel Works',
      shape: 'Traditional bordered, serif, dense',
      tax: 'IGST as one line',
      po: 'Header block on some invoices, a footnote on others',
      hard: 'The PO moves between invoices from the same vendor, so its position cannot be learned.',
    },
    nalanda: {
      vendor: 'Nalanda Print & Packaging',
      shape: 'One bundled line, no itemisation',
      tax: 'Embedded in the rate and never stated',
      po: 'Buried mid-sentence in a remarks paragraph',
      hard: 'Nothing to total up and no tax line to find - the model has to read prose to get the PO.',
    },
    kaveri: {
      vendor: 'Kaveri Logistics',
      shape: 'Dense freight table',
      tax: 'GST at 5%, the goods-transport rate',
      po: 'A numbered footnote at the foot of the page',
      hard: 'Also the scanned one: an image with no text layer at all.',
    },
    orbit: {
      vendor: 'Orbit Facility Services',
      shape: 'Services table, man-day units',
      tax: 'CGST + SGST broken out',
      po: 'Sometimes nowhere - the document names no PO',
      hard: 'Tests what happens when the thing being looked for does not exist.',
    },
  };

  async function loadLayouts() {
    const node = $('#layouts');
    if (!node || node.dataset.loaded) return;
    const samples = state.samples.length ? state.samples : await api('/api/samples').catch(() => []);
    const byLayout = new Map();
    samples.forEach((s) => {
      const key = Object.keys(LAYOUT_NOTES).find((k) => (s.file || '').toLowerCase().includes(k));
      if (!key) return;
      if (!byLayout.has(key)) byLayout.set(key, []);
      byLayout.get(key).push(s);
    });

    node.innerHTML = Array.from(byLayout.entries()).map(([key, items]) => {
      const note = LAYOUT_NOTES[key];
      const first = items[0];
      return `
      <article class="lay">
        <button class="lay-doc" type="button" data-sample="${esc(first.file)}"
                title="Run ${esc(first.title)}">
          <img src="/api/thumbs/${esc(key)}.png" alt="Top of a ${esc(note.vendor)} invoice"
               loading="lazy" width="794" height="560">
        </button>
        <div class="lay-body">
          <h3 class="lay-vendor">${esc(note.vendor)}</h3>
          <p class="lay-shape">${esc(note.shape)}</p>
          <p class="lay-hard">${esc(note.hard)}</p>
          <dl class="lay-dl">
            <div><dt>Tax</dt><dd>${esc(note.tax)}</dd></div>
            <div><dt>PO</dt><dd>${esc(note.po)}</dd></div>
          </dl>
          <div class="lay-runs">
            ${items.map((s) => `<button class="chip chip-run" type="button"
                 data-sample="${esc(s.file)}">${esc(s.title)} ${decisionBadge(s.expected)}</button>`).join('')}
          </div>
        </div>
      </article>`;
    }).join('');

    $$('[data-sample]', node).forEach((b) => b.addEventListener('click', () => runSample(b.dataset.sample)));
    node.dataset.loaded = '1';
  }

  // ------------------------------------------------------------------ charts

  /* Two charts, both earning their place.
   *
   * Decisions are a *status* quantity, not a categorical one - good / warning /
   * critical - so they use the reserved status colours rather than a series
   * palette, and every segment is direct-labelled because status must never be
   * carried by colour alone. The hues were checked with a CVD validator rather
   * than picked by eye: at deuteranopia the amber/red pair separates by dE 10.3,
   * comfortably past the 8 threshold where a chart stops being readable for the
   * ~8% of men with red-green colour blindness. The amber sits below 3:1 against
   * the page, which is why the labels are not optional and why the table below
   * is part of the answer.
   *
   * The rules chart is one series, so it takes one hue and no legend - the
   * heading names it.
   */
  const CHART = {
    APPROVE: { fill: '#1f8a63', label: 'Approved' },
    FLAG:    { fill: '#d8a008', label: 'Flagged' },
    REJECT:  { fill: '#ef4a4a', label: 'Rejected' },
  };

  function renderCharts(runs) {
    const node = $('#charts');
    if (!node) return;
    const done = runs.filter((r) => r.decision);
    if (!done.length) { node.innerHTML = ''; return; }

    // Two columns. The left one stacks the outcome split above the purchase-order
    // ledger, which is both the natural pairing - what happened, against what it
    // was billed to - and what stops the column running short beside the rule
    // chart.
    node.innerHTML = `
      <div class="chart-col">
        <section class="chart">
          <h3 class="chart-h">Where invoices ended up</h3>
          <p class="chart-sub">${done.length} processed. The approved ones are invoices nobody had to open.</p>
          ${decisionBar(done)}
        </section>
        <section class="chart" id="po-chart">
          <h3 class="chart-h">Purchase orders, and how much is left</h3>
          <p class="chart-sub">The budget each invoice is billed against. Rule R6 reads this ledger.</p>
          <div id="po-bars"><p class="chart-empty">Loading…</p></div>
        </section>
      </div>
      <section class="chart">
        <h3 class="chart-h">Which checks caught something</h3>
        <p class="chart-sub">Counts every time a check failed, across all ${done.length} invoices.</p>
        ${ruleBars(done)}
      </section>`;
    renderPoBars();
  }

  /** The PO ledger as a chart: how much of each budget is spoken for.
   *
   *  This is the data R6 decides on, so showing it here is not decoration - a
   *  reviewer looking at "this over-consumes the PO" can see the whole ledger
   *  in one place. Over-consumption is a *state*, so it takes a status colour
   *  and a word, never colour alone.
   */
  async function renderPoBars() {
    const node = $('#po-bars');
    if (!node) return;
    try {
      const pos = await api('/api/pos');
      const rows = pos
        .map((po) => ({ ...po, pct: po.po_value ? (po.already_billed / po.po_value) * 100 : 0 }))
        .sort((a, b) => b.pct - a.pct)
        .slice(0, 7);
      if (!rows.length) { node.innerHTML = '<p class="chart-empty">No purchase orders.</p>'; return; }

      node.innerHTML = `<ul class="pos">${rows.map((po) => {
        const over = po.pct >= 100;
        const near = !over && po.pct >= 80;
        return `
        <li class="po-row ${over ? 'is-over' : near ? 'is-near' : ''}"
            data-tip="${esc(`<div class="tt-head">${po.po_number}</div>`
              + `${po.vendor_name || ''}<br>${po.description || ''}<br>`
              + `billed ${money(po.already_billed, 'INR')} of ${money(po.po_value, 'INR')}`)}">
          <a class="reclink po-ref" href="#" data-po="${esc(po.po_number)}">${esc(po.po_number)}</a>
          <span class="po-track"><span class="po-fill" style="width:${Math.min(po.pct, 100)}%"></span></span>
          <span class="po-pct">${po.pct.toFixed(0)}%</span>
          <span class="po-vendor">${esc((po.vendor_name || '').replace(/ (Private Limited|LLP|Services|and Packaging)$/, ''))}
            ${over ? '<b class="po-tag">fully billed</b>' : ''}</span>
        </li>`;
      }).join('')}</ul>
      <p class="chart-foot">Showing ${rows.length} of ${pos.length}. Open any PO for the invoices
         that consumed it, or see
         <button class="linkish" data-goto="pos" type="button">the full purchase-order master</button>.</p>`;
      bindTooltips(node);
    } catch (err) {
      node.innerHTML = '<p class="chart-empty">Could not load purchase orders.</p>';
    }
  }

  /** One 100% stacked bar: the whole queue, split by outcome. */
  function decisionBar(runs) {
    const order = ['APPROVE', 'FLAG', 'REJECT'];
    const counts = order.map((d) => ({
      key: d, ...CHART[d], n: runs.filter((r) => r.decision === d).length,
    })).filter((c) => c.n > 0);
    const total = runs.length;

    const segs = counts.map((c) => {
      const pct = (c.n / total) * 100;
      return `<div class="seg" style="flex:${c.n};background:${c.fill}"
                   title="${esc(c.label)}: ${c.n} of ${total} (${pct.toFixed(0)}%)"
                   role="img" aria-label="${esc(c.label)} ${c.n}">
                ${pct >= 11 ? `<span class="seg-n">${c.n}</span>` : ''}
              </div>`;
    }).join('');

    // Legend is always present; each entry carries its own count so identity
    // never rests on the colour.
    const legend = counts.map((c) => `
      <span class="lg">
        <i class="lg-dot" style="background:${c.fill}"></i>
        ${esc(c.label)} <b>${c.n}</b>
        <span class="lg-pct">${((c.n / total) * 100).toFixed(0)}%</span>
      </span>`).join('');

    return `<div class="stack">${segs}</div><div class="legend">${legend}</div>`;
  }

  /** Horizontal bars: how often each rule actually caught something. */
  function ruleBars(runs) {
    const tally = new Map();
    runs.forEach((r) => (r.failed_rules || []).forEach((id) => {
      tally.set(id, (tally.get(id) || 0) + 1);
    }));
    if (!tally.size) {
      return '<p class="chart-empty">No check has failed yet - every invoice passed all nine.</p>';
    }
    const rows = Array.from(tally.entries()).sort((a, b) => b[1] - a[1]);
    const max = rows[0][1];
    const names = state.ruleNames || {};

    return `<ul class="bars">${rows.map(([id, n]) => `
      <li class="bar-row" title="${esc(names[id] || id)}: caught ${n}">
        <span class="bar-id">${esc(id)}</span>
        <span class="bar-track">
          <span class="bar-fill" style="width:${(n / max) * 100}%"></span>
        </span>
        <span class="bar-n">${n}</span>
        <span class="bar-name">${esc(names[id] || '')}</span>
      </li>`).join('')}</ul>`;
  }

  // -------------------------------------------------------------- dashboard

  async function loadDashboard() {
    try {
      const [stats, runs] = await Promise.all([api('/api/stats'), api('/api/runs')]);
      if (!state.ruleNames) await loadRuleNames();
      state.runs = runs;
      renderTiles(stats);
      renderCharts(runs);
      renderVendorStrip();
      renderRunsTable();
    } catch (err) {
      toast(`Dashboard failed to load: ${err.message}`, true);
    }
  }

  function renderTiles(stats) {
    const tiles = [
      { label: 'Invoices processed', value: stats.total_runs },
      { label: 'Auto-approved', value: `${num(stats.auto_approved_pct, 1)}%`,
        sub: `${stats.approved} of ${stats.total_runs}`, cls: 'is-approve' },
      { label: 'Flagged', value: stats.flagged, sub: 'need a human', cls: 'is-flag' },
      { label: 'Rejected', value: stats.rejected, sub: 'blocked from payment', cls: 'is-reject' },
      { label: 'Avg handling', value: duration(stats.avg_duration_ms), sub: 'per invoice' },
      { label: 'Hours saved', value: stats.hours_saved, cls: 'is-hero',
        sub: 'click for the basis', tip: `<div class="tt-head">How this is calculated</div>${esc(stats.hours_saved_basis)}` },
    ];
    $('#tiles').innerHTML = tiles.map((tile) => `
      <div class="tile ${tile.cls || ''}" ${tile.tip ? `data-tip="${esc(tile.tip)}"` : ''}>
        <div class="tile-label">${esc(tile.label)}</div>
        <div class="tile-value">${esc(tile.value)}</div>
        ${tile.sub ? `<div class="tile-sub">${esc(tile.sub)}</div>` : ''}
      </div>`).join('');
    bindTooltips($('#tiles'));
  }

  function renderRunsTable() {
    const body = $('#runs-table tbody');
    let rows = state.runs.slice();
    if (state.filter !== 'ALL') rows = rows.filter((row) => row.decision === state.filter);

    const { key, dir } = state.sort;
    rows.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      const cmp = typeof x === 'number' && typeof y === 'number'
        ? x - y : String(x).localeCompare(String(y));
      return dir === 'asc' ? cmp : -cmp;
    });

    $('#runs-empty').hidden = rows.length > 0;
    body.innerHTML = rows.map((row) => `
      <tr class="clickable" data-run="${esc(row.run_id)}">
        <td>${when(row.started_at)}</td>
        <td class="wrap">${esc(row.filename)}
          ${row.pdf_kind === 'scanned' ? '<span class="badge badge-flag">scanned</span>' : ''}</td>
        <td>${plain(row.vendor_name)}</td>
        <td>${plain(row.invoice_number)}</td>
        <td class="num">${money(row.total_amount)}</td>
        <td>${row.matched_po ? `<code>${esc(row.matched_po)}</code>` : '—'}</td>
        <td>${decisionBadge(row.decision)}
          ${row.failed_rules && row.failed_rules.length
            ? `<span class="badge">${esc(row.failed_rules.join(' '))}</span>` : ''}</td>
        <td class="num">${num(row.confidence, 2)}</td>
        <td class="num">${duration(row.duration_ms)}</td>
      </tr>`).join('');

    $$('#runs-table th[data-sort]').forEach((th) => {
      const arrow = th.dataset.sort === key ? `<span class="arrow">${dir === 'asc' ? '▲' : '▼'}</span>` : '';
      th.innerHTML = th.textContent.replace(/[▲▼]/g, '').trim() + arrow;
    });

    $$('tr.clickable', body).forEach((tr) => {
      tr.addEventListener('click', () => loadRun(tr.dataset.run));
    });
  }

  // ------------------------------------------------------------------ admin

  async function loadAdmin() {
    try {
      const [rules, config, vendors, telemetry] = await Promise.all([
        api('/api/rules'), api('/api/config'),
        api('/api/vendors'), api('/api/admin/telemetry'),
      ]);
      state.config = config.config;
      state.defaults = config.defaults;
      renderRules(rules);
      renderConfigForm(config);
      renderVendors(vendors);
      renderTelemetry(telemetry);
      buildSectionNav($('#panel-admin'));
    } catch (err) {
      toast(`Admin failed to load: ${err.message}`, true);
    }
  }

  /** The nine rules, as cards.
   *
   *  A reviewer's first question about a rule engine is not "what are the
   *  rules" but "what does this one actually stop?". So each card leads with
   *  that question in the words someone outside the team would use, then the
   *  precise check, then what happens when it fails and the live numbers it
   *  uses. Every value here is served from `/api/rules` - the same definitions
   *  the engine applies - so the page cannot describe a rule the code does not
   *  have.
   */
  const SEVERITY_SHAPE = [
    { test: /REJECT.*FLAG|FLAG.*REJECT/i, cls: 'is-both-chip', chip: 'FLAG, then REJECT' },
    { test: /REJECT/i, cls: 'is-reject-chip', chip: 'REJECT' },
    { test: /FLAG/i, cls: 'is-flag-chip', chip: 'FLAG' },
  ];

  const THRESHOLD_WORDS = {
    po_tolerance_pct: 'how much over the PO is still acceptable, in percent',
    po_over_tolerance_reject_pct: 'how far over the PO turns into a rejection',
    duplicate_window_days: 'how many days either side count as a near duplicate',
    confidence_floor: 'the least certainty a reading may have',
    arithmetic_tolerance: 'rounding slack, in rupees',
    stale_invoice_days: 'how old an invoice may be',
    po_match_min_score: 'the score a PO match must reach',
    vendor_match_min_score: 'the score a vendor match must reach',
  };

  function renderRules(payload) {
    const rules = payload.rules || [];

    // Grouped by what each rule is compared against, which is the same shape
    // the diagram on How it works uses. Nine cards in one grid is a list to be
    // read through; four groups is a structure to be understood, and the
    // grouping is the answer to the question a reviewer actually has - "what
    // does this system know that lets it say that?".
    const GROUPS = [
      { source: 'approved vendors', title: 'Against the approved vendor list',
        blurb: 'Who we are allowed to pay at all.', tab: 'admin' },
      { source: 'purchase orders', title: 'Against the purchase orders',
        blurb: 'What was ordered, and how much of each budget is left.', tab: 'pos' },
      { source: 'processed history', title: 'Against every invoice seen before',
        blurb: 'By file, and by vendor, amount and date.', tab: 'dashboard' },
      { source: 'the document itself', title: 'Against the document itself',
        blurb: 'Its own printed text, re-read to check what was taken off it.', tab: null },
    ];

    const counts = { REJECT: 0, FLAG: 0 };
    rules.forEach((rule) => {
      const shape = severityShape(rule.severity);
      if (shape.chip.includes('REJECT')) counts.REJECT += 1; else counts.FLAG += 1;
    });

    const legend = `
      <div class="rules-legend">
        <div class="rl-item">
          <span class="rcard-sev is-reject-chip">REJECT</span>
          <span>stops the payment outright &mdash; nothing a reviewer can do makes it payable</span>
        </div>
        <div class="rl-item">
          <span class="rcard-sev is-flag-chip">FLAG</span>
          <span>holds it for a person, with the finding and the figures already on screen</span>
        </div>
        <div class="rl-item rl-rule">
          <span>Worst result wins. ${rules.length} checks, every invoice, in order.</span>
        </div>
      </div>`;

    const groups = GROUPS.map((group) => {
      const items = rules.filter((rule) => rule.source === group.source);
      if (!items.length) return '';
      return `
        <section class="rgroup">
          <header class="rgroup-head">
            <h3>${esc(group.title)}</h3>
            <p>${esc(group.blurb)}</p>
            ${group.tab
              ? `<button class="linkish" type="button" data-goto="${esc(group.tab)}">See the data</button>`
              : ''}
            <span class="rgroup-n">${items.length} check${items.length === 1 ? '' : 's'}</span>
          </header>
          <div class="rules-grid">${items.map(ruleCard).join('')}</div>
        </section>`;
    }).join('');

    $('#rules-list').innerHTML = legend + groups
      + `<div class="note note-info">${esc(payload.decision_logic)}</div>`;
  }

  function severityShape(severity) {
    return SEVERITY_SHAPE.find((entry) => entry.test.test(severity || '')) || SEVERITY_SHAPE[2];
  }

  /** One rule. The question it answers leads, because that is what a reviewer
   *  arrives wanting; the formal name, the precise check and the live number
   *  follow in decreasing order of how often they are needed. */
  function ruleCard(rule) {
    const shape = severityShape(rule.severity);
    const thresholds = Object.entries(rule.active_thresholds || {});
    return `
      <article class="rcard">
        <header class="rcard-top">
          <span class="rcard-id">${esc(rule.id)}</span>
          <span class="rcard-sev ${shape.cls}">${esc(shape.chip)}</span>
        </header>

        <h4 class="rcard-q">${esc(rule.plain || rule.title)}</h4>
        ${rule.plain ? `<p class="rcard-title">${esc(rule.title)}</p>` : ''}

        <p class="rcard-checks"><span>Checks</span>${esc(rule.checks)}</p>

        ${thresholds.length ? `<ul class="rcard-th">${thresholds.map(([key, value]) => `
          <li>
            <code>${esc(key)}</code>
            <b>${esc(value)}</b>
            <span>${esc(THRESHOLD_WORDS[key] || '')}</span>
          </li>`).join('')}</ul>`
          : '<p class="rcard-th-none">No number to tune &mdash; it either holds or it does not.</p>'}
      </article>`;
  }

  const CONFIG_HINTS = {
    po_tolerance_pct: 'R5 — percent over the PO value still acceptable',
    po_over_tolerance_reject_pct: 'R5 — percent over at which it becomes a rejection',
    duplicate_window_days: 'R7 — days either side for a near-duplicate',
    confidence_floor: 'R9 — minimum extraction confidence',
    arithmetic_tolerance: 'R2 — rounding slack in currency units',
    stale_invoice_days: 'R8 — age at which an invoice is too old',
    po_match_min_score: 'R4 — score a PO match must reach',
    vendor_match_min_score: 'R3 — score a vendor match must reach',
  };

  function renderConfigForm(payload) {
    const config = payload.config, defaults = payload.defaults;
    $('#config-form').innerHTML = Object.entries(config).map(([key, value]) => `
      <div class="field${value !== defaults[key] ? ' is-changed' : ''}">
        <label for="cfg-${esc(key)}">${esc(key)}</label>
        <input id="cfg-${esc(key)}" name="${esc(key)}" type="number" step="any" value="${esc(value)}">
        <div class="fhint">${esc(CONFIG_HINTS[key] || '')}${
          value !== defaults[key] ? ` · default ${esc(defaults[key])}` : ''}</div>
      </div>`).join('');
  }

  async function saveConfig() {
    const patch = {};
    $$('#config-form input').forEach((input) => {
      const value = Number(input.value);
      if (isFinite(value)) patch[input.name] = value;
    });
    const note = $('#config-note');
    try {
      const result = await api('/api/config', { method: 'PATCH', body: JSON.stringify(patch) });
      state.config = result.config;
      note.classList.remove('is-err');
      note.textContent = result.changed.length
        ? `Saved: ${result.changed.join(', ')}. Re-run an invoice to see the effect.`
        : 'No change.';
      renderConfigForm({ config: result.config, defaults: state.defaults });
      const rules = await api('/api/rules');
      renderRules(rules);
    } catch (err) {
      note.classList.add('is-err');
      note.textContent = err.message;
    }
  }

  async function resetConfig() {
    try {
      const result = await api('/api/config/reset', { method: 'POST' });
      state.config = result.config;
      renderConfigForm({ config: result.config, defaults: state.defaults });
      renderRules(await api('/api/rules'));
      $('#config-note').classList.remove('is-err');
      $('#config-note').textContent = 'Restored to defaults.';
    } catch (err) {
      toast(err.message, true);
    }
  }

  // --------------------------------------------------------- purchase orders

  /** The purchase-order master, as its own page.
   *
   *  This is the source of truth behind rules R4 (was a PO matched), R5 (is the
   *  invoice inside its tolerance) and R6 (does cumulative billing exceed it),
   *  so it is laid out to be analysed rather than merely displayed: every figure
   *  that decides a payment is on the row, headroom is stated in money as well
   *  as percent, and each row expands to the individual invoices that consumed
   *  it. A number that blocks a payment should be traceable to the invoices
   *  that produced it.
   */

  const DAY_MS = 24 * 60 * 60 * 1000;

  /** Decorate a raw PO with everything the page sorts, filters and colours on. */
  function decoratePo(po) {
    const value = Number(po.po_value) || 0;
    const billed = Number(po.already_billed) || 0;
    const remaining = po.remaining != null ? Number(po.remaining) : value - billed;
    const pct = value ? (billed / value) * 100 : 0;
    const expiry = po.expiry_date ? new Date(`${po.expiry_date}T00:00:00`) : null;
    const daysLeft = expiry && !isNaN(expiry)
      ? Math.round((expiry - new Date()) / DAY_MS)
      : null;
    // "unused" is a distinct state from "ok": it is the case R6 explicitly
    // stands aside on, so it is worth being able to see and filter.
    const health = pct >= 100 ? 'over' : pct >= 80 ? 'near' : billed === 0 ? 'unused' : 'ok';
    return {
      ...po, value, billed, remaining, pct, daysLeft, health,
      mine: po.source === 'user',
      expired: daysLeft != null && daysLeft < 0,
    };
  }

  const PO_HEALTH = {
    over:   { cls: 'is-over',   tag: 'fully billed'  },
    near:   { cls: 'is-near',   tag: 'nearly spent'  },
    unused: { cls: 'is-unused', tag: 'untouched'     },
    ok:     { cls: 'is-ok',     tag: ''              },
  };

  const PO_FILTERS = {
    ALL:       () => true,
    MINE:      (po) => po.source === 'user',
    SEED:      (po) => po.source !== 'user',
    AVAILABLE: (po) => po.health === 'ok' || po.health === 'unused',
    NEAR:      (po) => po.health === 'near',
    FULL:      (po) => po.health === 'over',
    UNUSED:    (po) => po.health === 'unused',
  };

  async function loadPos() {
    const body = $('#pos-table tbody');
    try {
      state.pos = (await api('/api/pos')).map(decoratePo);
    } catch (err) {
      body.innerHTML = '';
      $('#pos-budget').innerHTML = '';
      $('#po-board').innerHTML = '';
      const empty = $('#pos-empty');
      empty.hidden = false;
      empty.textContent = `Could not load purchase orders: ${err.message}`;
      return;
    }
    renderBudget();
    renderPoBoard();
    renderPosTable();
  }

  /** Indian short-scale money. "INR 43,80,000.00" is unreadable at a glance and
   *  wraps in any sensible column width; a controller reading this page thinks
   *  in lakhs and crores. The exact figure stays on the title attribute. */
  function compactInr(value) {
    const n = Number(value) || 0;
    const sign = n < 0 ? '-' : '';
    const a = Math.abs(n);
    if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
    if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
    if (a >= 1e3) return `${sign}₹${(a / 1e3).toFixed(1)}K`;
    return `${sign}₹${a.toFixed(0)}`;
  }

  /** The commitment position, as one panel rather than six boxes.
   *
   *  Six equal tiles gave six numbers equal weight, which is wrong: five of
   *  them are context for one question - how much of what we committed is
   *  already spent. So that single ratio is the ring, the money that composes
   *  it sits beside it on hairline-divided rows, and the per-PO counts are
   *  pills underneath. Money is shown in lakhs and crores because the full
   *  figure wrapped and nobody reads "INR 43,80,000.00" at a glance.
   */
  function renderBudget() {
    const pos = state.pos;
    const t = pos.reduce((acc, po) => ({
      value: acc.value + po.value,
      billed: acc.billed + po.billed,
      over: acc.over + (po.health === 'over' ? 1 : 0),
      near: acc.near + (po.health === 'near' ? 1 : 0),
      unused: acc.unused + (po.health === 'unused' ? 1 : 0),
      mine: acc.mine + (po.mine ? 1 : 0),
      mineValue: acc.mineValue + (po.mine ? po.value : 0),
    }), { value: 0, billed: 0, over: 0, near: 0, unused: 0, mine: 0, mineValue: 0 });
    const available = t.value - t.billed;
    const pct = t.value ? (t.billed / t.value) * 100 : 0;
    const healthy = pos.length - t.over - t.near;

    // A stroked circle, drawn from the top. r=52 in a 120 box leaves room for
    // the 13px stroke without clipping.
    const R = 52;
    const circ = 2 * Math.PI * R;
    const dash = (Math.min(pct, 100) / 100) * circ;

    const fig = (label, value, exact, sub) => `
      <div class="bfig">
        <div class="bfig-k">${label}</div>
        <div class="bfig-v" title="${esc(exact)}">${value}</div>
        <div class="bfig-s">${sub}</div>
      </div>`;

    $('#pos-budget').innerHTML = `
      <div class="budget-ring">
        <svg viewBox="0 0 120 120" role="img"
             aria-label="${pct.toFixed(1)}% of committed value billed">
          <circle class="ring-track" cx="60" cy="60" r="${R}"></circle>
          <circle class="ring-fill" cx="60" cy="60" r="${R}"
                  stroke-dasharray="${dash.toFixed(2)} ${(circ - dash).toFixed(2)}"
                  transform="rotate(-90 60 60)"></circle>
        </svg>
        <div class="ring-mid">
          <b>${pct.toFixed(1)}<span>%</span></b>
          <span class="ring-cap">billed</span>
        </div>
      </div>

      <div class="budget-figs">
        ${fig('Committed', compactInr(t.value), money(t.value, 'INR'),
              t.mine
                ? `${pos.length} purchase orders · ${t.mine} yours`
                : `${pos.length} purchase orders`)}
        ${fig('Billed to date', compactInr(t.billed), money(t.billed, 'INR'),
              `${pct.toFixed(1)}% of committed`)}
        ${fig('Still available', compactInr(available), money(available, 'INR'),
              'headroom left')}
      </div>

      <div class="budget-flags">
        ${t.mine ? `<span class="bflag is-mine" data-pofilter-go="MINE" role="button"
            tabindex="0" title="Show only the purchase orders added here"><b>${t.mine}</b>
            added by you · ${compactInr(t.mineValue)}</span>` : ''}
        ${t.over ? `<span class="bflag is-over"><b>${t.over}</b> over-consumed</span>` : ''}
        ${t.near ? `<span class="bflag is-near"><b>${t.near}</b> nearly spent</span>` : ''}
        <span class="bflag is-ok"><b>${healthy}</b> with headroom</span>
        ${t.unused ? `<span class="bflag"><b>${t.unused}</b> untouched</span>` : ''}
      </div>`;
  }

  /** The POs a human should look at, pulled out of the table so they are not
   *  something you have to notice by scanning. */
  function renderPoBoard() {
    const risky = state.pos
      .filter((po) => po.health === 'over' || po.health === 'near' || po.expired)
      .sort((a, b) => b.pct - a.pct);
    const node = $('#po-board');
    if (!risky.length) {
      node.innerHTML = `<p class="po-board-none">Every purchase order has headroom and none
        has expired. Nothing here needs a decision.</p>`;
      return;
    }
    node.innerHTML = `
      <div class="po-board-head">Needs attention</div>
      <div class="po-board-grid">${risky.map((po) => `
        <article class="po-alert ${PO_HEALTH[po.health].cls}" data-pogo="${esc(po.po_number)}"
                 tabindex="0" role="button">
          <div class="po-alert-top">
            <code class="po-code">${esc(po.po_number)}</code>
            <span class="po-tag">${po.expired ? 'expired' : PO_HEALTH[po.health].tag}</span>
          </div>
          <div class="po-alert-vendor">${esc(po.vendor_name || po.vendor_id)}</div>
          <div class="po-meter">
            <span class="po-meter-fill" style="width:${Math.min(po.pct, 100)}%"></span>
          </div>
          <div class="po-alert-foot">
            <span><b>${po.pct.toFixed(1)}%</b> consumed</span>
            <span>${po.remaining < 0
              ? `${money(Math.abs(po.remaining), 'INR')} over`
              : `${money(po.remaining, 'INR')} left`}</span>
          </div>
        </article>`).join('')}</div>`;

    $$('#po-board [data-pogo]').forEach((card) => {
      const go = () => {
        state.posFilter = 'ALL';
        state.posQuery = card.dataset.pogo;
        $('#po-search').value = card.dataset.pogo;
        $$('#po-filters .chip').forEach((c) =>
          c.classList.toggle('is-active', c.dataset.pofilter === 'ALL'));
        renderPosTable();
        const row = $(`#pos-table [data-po="${CSS.escape(card.dataset.pogo)}"]`);
        if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
      };
      card.addEventListener('click', go);
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      });
    });
  }

  function renderPosTable() {
    const q = state.posQuery.trim().toLowerCase();
    const rows = state.pos
      .filter(PO_FILTERS[state.posFilter] || PO_FILTERS.ALL)
      .filter((po) => !q || [po.po_number, po.vendor_name, po.vendor_id, po.description]
        .some((f) => String(f || '').toLowerCase().includes(q)));

    const { key, dir } = state.posSort;
    const sign = dir === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
      const x = a[key]; const y = b[key];
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === 'number' && typeof y === 'number') return (x - y) * sign;
      return String(x).localeCompare(String(y)) * sign;
    });

    $$('#pos-table th[data-posort]').forEach((th) => {
      th.classList.toggle('is-sorted', th.dataset.posort === key);
      th.dataset.dir = th.dataset.posort === key ? dir : '';
    });

    // The expanded rows are pinned to the visible width of the scroll area
    // rather than to the table's own width - see `.po-detail-body`. Measured
    // here and on resize, because only the browser knows it.
    sizePoDetails();

    $('#pos-empty').hidden = rows.length > 0;
    $('#pos-table tbody').innerHTML = rows.map((po) => {
      const h = PO_HEALTH[po.health];
      const expiryNote = po.daysLeft == null ? ''
        : po.daysLeft < 0 ? `<span class="po-exp is-bad">${Math.abs(po.daysLeft)}d ago</span>`
        : po.daysLeft <= 30 ? `<span class="po-exp is-soon">in ${po.daysLeft}d</span>`
        : `<span class="po-exp">in ${po.daysLeft}d</span>`;
      return `<tr class="po-main ${h.cls} ${po.mine ? 'is-mine' : ''}"
                  data-po="${esc(po.po_number)}" tabindex="0"
                  role="button" aria-expanded="false">
        <td><code class="po-code">${esc(po.po_number)}</code>
            ${po.mine ? '<span class="po-mine">added by you</span>' : ''}</td>
        <td class="po-vendor-cell">${esc(po.vendor_name || po.vendor_id)}
            <div class="po-desc">${esc(po.description || '')}</div>
            <div class="po-vid"><code>${esc(po.vendor_id)}</code></div></td>
        <td class="num">${money(po.value)}</td>
        <td class="num">${money(po.billed)}</td>
        <td class="num"><b class="${po.remaining < 0 ? 'po-neg' : ''}">${money(po.remaining)}</b></td>
        <td class="po-consumed">
          <div class="po-cell">
            <div class="po-meter">
              <span class="po-meter-fill" style="width:${Math.min(po.pct, 100)}%"></span>
            </div>
            <span class="po-meter-pct">${po.pct.toFixed(1)}%</span>
            ${h.tag ? `<span class="po-tag">${h.tag}</span>` : ''}
          </div>
        </td>
        <td class="po-when">${esc(po.issued_date || '—')}</td>
        <td class="po-when">${esc(po.expiry_date || '—')}${expiryNote ? `<br>${expiryNote}` : ''}</td>
        <td><span class="po-status">${esc(po.status || '—')}</span>
            <span class="po-ccy">${esc(po.currency || '')}</span></td>
      </tr>
      <tr class="po-detail" data-detail="${esc(po.po_number)}" hidden>
        <td colspan="9"><div class="po-detail-body">Loading…</div></td>
      </tr>`;
    }).join('');

    wirePoRows();
  }

  /** Give the expanded rows the width of the part of the table you can see.
   *
   *  The panel starts a little inside the scroll container's left edge - the
   *  table's own cell geometry - so the visible width is the container minus
   *  that inset, or the panel runs under the right-hand edge by exactly that
   *  much. The inset is measured rather than assumed, and only while the table
   *  is scrolled home: once it is scrolled, the panel is pinned to the edge
   *  and the measurement would read as zero.
   */
  let poDetailInset = null;

  function sizePoDetails() {
    const table = $('#pos-table');
    const wrap = table && table.closest('.table-wrap');
    if (!wrap) return;
    const body = $('.po-detail:not([hidden]) .po-detail-body', table);
    if (body && wrap.scrollLeft === 0) {
      poDetailInset = Math.max(
        0, Math.round(body.getBoundingClientRect().left - wrap.getBoundingClientRect().left));
    }
    table.style.setProperty('--po-visible', `${wrap.clientWidth - (poDetailInset || 0) - 2}px`);
  }

  window.addEventListener('resize', sizePoDetails);

  /** Expand a row to the invoices that actually consumed it. */
  function wirePoRows() {
    $$('#pos-table .po-main').forEach((tr) => {
      const open = async () => {
        const detail = $(`#pos-table [data-detail="${CSS.escape(tr.dataset.po)}"]`);
        if (!detail) return;
        detail.hidden = !detail.hidden;
        tr.classList.toggle('is-open', !detail.hidden);
        tr.setAttribute('aria-expanded', String(!detail.hidden));
        // Measured once the panel is on the page: a hidden element has no box.
        if (!detail.hidden) sizePoDetails();
        if (detail.hidden || detail.dataset.loaded) return;
        try {
          const po = await api(`/api/pos/${encodeURIComponent(tr.dataset.po)}`);
          const used = po.consumption || [];
          const total = used.reduce((sum, c) => sum + (Number(c.amount) || 0), 0);
          const own = (state.pos.find((x) => x.po_number === tr.dataset.po) || {}).mine;
          $('.po-detail-body', detail).innerHTML = (own ? testInvoiceStrip(po) : '') + (used.length
            ? `<table class="po-sub"><thead><tr>
                 <th>Invoice</th><th class="num">Amount</th><th>Source</th>
                 <th>Booked</th><th>Run</th>
               </tr></thead><tbody>${used.map((c) => `<tr>
                 <td>${esc(c.invoice_number || '—')}</td>
                 <td class="num">${money(c.amount, 'INR')}</td>
                 <td>${c.source === 'history'
                   ? '<span class="po-src">billed before this system</span>'
                   : '<span class="po-src is-live">booked by a run</span>'}</td>
                 <td>${c.booked_at ? when(c.booked_at) : '—'}</td>
                 <td>${c.run_id
                   ? `<a class="reclink" href="#" data-run="${esc(c.run_id)}">${esc(c.run_id)}</a>`
                   : '—'}</td>
               </tr>`).join('')}</tbody>
               <tfoot><tr><td>${used.length} invoice${used.length === 1 ? '' : 's'}</td>
                 <td class="num"><b>${money(total, 'INR')}</b></td>
                 <td colspan="3">is what R6 compares the next invoice against</td></tr></tfoot>
               </table>`
            : '<p class="hint">Nothing has been billed against this PO yet, so rule R6 has no ledger to check and stands aside.</p>');
          wireTestInvoice(detail);
          detail.dataset.loaded = '1';
        } catch (err) {
          $('.po-detail-body', detail).textContent = `Could not load: ${err.message}`;
        }
      };
      tr.addEventListener('click', open);
      tr.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
    });
  }

  // ------------------------------------------- raising a purchase order

  /** Everything in this block exists to make one loop visible end to end:
   *  raise a purchase order, generate an invoice against it, watch the seven
   *  stages decide it. A PO master you can only read makes the interesting
   *  half of the system undemonstrable on anything but the twelve seeded rows.
   *
   *  The separation between the two halves is deliberate and shown in three
   *  places - a tag on the row, a filter beside the health filters, and a
   *  count in the budget panel - because a figure on this page is a figure a
   *  payment gets decided on, and "is this real procurement data or something
   *  I typed in a demo?" must never be a question.
   */

  const DEFAULT_PO_WINDOW_DAYS = 90;

  /** Suggest the next free number in the user's own range.
   *
   *  The seeded masters occupy PO-4471 to PO-4482, so anything raised here
   *  starts at PO-9001: the number itself says which half of the table a row
   *  came from, even in a screenshot with no styling.
   */
  function suggestPoNumber() {
    const mine = state.pos
      .map((po) => /^PO-9(\d{3})$/.exec(po.po_number))
      .filter(Boolean)
      .map((m) => Number(m[1]));
    return `PO-9${String((mine.length ? Math.max(...mine) : 0) + 1).padStart(3, '0')}`;
  }

  const isoDay = (date) => date.toISOString().slice(0, 10);

  let poFormReady = false;

  async function loadPoForm() {
    if (!state.vendors.length) {
      try { state.vendors = await api('/api/vendors'); } catch (err) { state.vendors = []; }
    }
    if (!state.variants.length) {
      try { state.variants = await api('/api/pos/variants'); } catch (err) { state.variants = []; }
    }

    // Blocked vendors are listed but disabled rather than hidden: the point of
    // the control is to show that the approved list is what a PO may name, and
    // silently dropping a vendor would just raise the question of where they
    // went. Same reasoning as the provider switch.
    //
    // Only the first visit resets the fields. `showTab` runs this every time
    // the tab is activated, and resetting here wiped a half-filled form for
    // anyone who stepped over to Rules & data to look a vendor up - including
    // the vendor they had just added and selected.
    if (poFormReady) {
      renderVendorOptions($('#np-vendor') ? $('#np-vendor').value : '');
      return;
    }
    poFormReady = true;
    renderVendorOptions();
    resetPoForm();
  }

  /** The vendor picker: a listbox of our own, not a native select.
   *
   *  The native control's drop-down is drawn by the operating system. It can
   *  show one line of text per option and nothing else - so the vendor code,
   *  the tax number, a blocked supplier and one you added yourself all look
   *  identical, and the list looks like nothing else on the page. Those are
   *  exactly the distinctions that decide whether a PO can ever pay an
   *  invoice, so they belong inside the control.
   *
   *  The chosen value lives in a hidden input, which is what the rest of the
   *  form reads - so this is a change to how the choice is presented, not to
   *  how a purchase order is submitted.
   */
  const VENDOR_PLACEHOLDER = 'Choose an approved vendor\u2026';

  function renderVendorOptions(selected = '') {
    const pop = $('#np-vendor-pop');
    if (!pop) return;

    const mine = state.vendors.filter((v) => v.source === 'user');
    const seeded = state.vendors.filter((v) => v.source !== 'user');

    const row = (vendor) => {
      const blocked = vendor.status !== 'approved';
      // The name, and nothing else. A code and a tax number are what the row
      // in the master is for; in a picker they are three lines of noise around
      // the one word someone is looking for.
      return `
        <button class="pick-opt${blocked ? ' is-blocked' : ''}" type="button" role="option"
                aria-selected="${vendor.vendor_id === selected}"
                aria-disabled="${blocked}" data-value="${esc(vendor.vendor_id)}"
                title="${esc(vendor.vendor_id)}${vendor.gstin ? ' · GSTIN ' + esc(vendor.gstin) : ''}">
          <span class="pick-name">${esc(vendor.name)}</span>
          ${vendor.source === 'user' ? '<span class="po-mine">added by you</span>' : ''}
          ${blocked ? `<span class="pick-flag">${esc(vendor.status)}</span>` : ''}
          <span class="pick-check" aria-hidden="true">&#10003;</span>
        </button>`;
    };

    const group = (label, rows) =>
      rows.length ? `<div class="pick-group">${esc(label)}</div>${rows.map(row).join('')}` : '';

    pop.innerHTML = group('Approved vendors', seeded)
      + group('Added by you', mine)
      + `<div class="pick-sep"></div>
         <button class="pick-opt is-add" type="button" role="option"
                 aria-selected="false" data-value="__new__">
           <span class="pick-plus" aria-hidden="true">+</span>
           <span class="pick-name">Add a new vendor</span>
         </button>`;

    $('#np-vendor').value = selected;
    paintVendorButton();
    wireVendorOptions();
  }

  function wireVendorOptions() {
    $$('#np-vendor-pop .pick-opt').forEach((option) => {
      option.addEventListener('click', () => {
        if (option.getAttribute('aria-disabled') === 'true') return;
        chooseVendor(option.dataset.value);
      });
    });
  }

  function chooseVendor(value) {
    $('#np-vendor').value = value;
    $$('#np-vendor-pop .pick-opt').forEach((option) =>
      option.setAttribute('aria-selected', String(option.dataset.value === value)));
    paintVendorButton();
    openVendorPicker(false);
    if (value === '__new__') {
      openVendorModal('picker');
      return;
    }
    const button = $('#np-vendor-btn');
    if (button) button.focus();
  }

  /** Closed, the control shows what a row of the list showed: the name, with
   *  the code beside it. Nothing chosen yet is a prompt rather than a value,
   *  so it is set in the ink a placeholder gets. */
  function paintVendorButton() {
    const button = $('#np-vendor-btn');
    const field = $('#np-vendor');
    if (!button || !field) return;
    const label = $('.pick-val', button);
    const vendor = state.vendors.find((v) => v.vendor_id === field.value);
    button.classList.toggle('is-adding', field.value === '__new__');
    label.classList.toggle('is-empty', !field.value);
    if (field.value === '__new__') {
      label.textContent = 'Adding a new vendor';
    } else if (vendor) {
      label.textContent = vendor.name;
    } else {
      label.textContent = VENDOR_PLACEHOLDER;
    }
  }

  function openVendorPicker(on) {
    const pop = $('#np-vendor-pop');
    const button = $('#np-vendor-btn');
    if (!pop || !button) return;
    pop.hidden = !on;
    button.setAttribute('aria-expanded', String(!!on));
    button.classList.toggle('is-open', !!on);
    if (on) {
      const current = $('#np-vendor-pop .pick-opt[aria-selected="true"]')
        || $('#np-vendor-pop .pick-opt');
      if (current) current.focus();
    }
  }

  /** Arrow keys walk the list, Enter picks, Escape closes, a click outside
   *  closes - the four things a native select does for free and a custom one
   *  has to be given back. */
  function wireVendorPicker() {
    const button = $('#np-vendor-btn');
    const pop = $('#np-vendor-pop');
    if (!button || !pop) return;

    button.addEventListener('click', () => openVendorPicker(pop.hidden));
    button.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); openVendorPicker(true); }
    });

    pop.addEventListener('keydown', (event) => {
      const options = $$('#np-vendor-pop .pick-opt')
        .filter((o) => o.getAttribute('aria-disabled') !== 'true');
      const here = options.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = event.key === 'ArrowDown'
          ? Math.min(here + 1, options.length - 1)
          : Math.max(here - 1, 0);
        (options[next] || options[0]).focus();
      } else if (event.key === 'Escape') {
        openVendorPicker(false);
        button.focus();
      }
    });

    document.addEventListener('click', (event) => {
      if (!pop.hidden && !event.target.closest('#np-vendor-pick')) openVendorPicker(false);
    });
  }

  /** Adding a vendor, as a dialog over the page.
   *
   *  It was a panel inside the purchase-order form, which was wrong twice: it
   *  pushed the rest of the form down under whoever was filling it in, and it
   *  made a short self-contained errand look like part of a longer one. A
   *  dialog states the one question it needs answered - what is this supplier
   *  called - and puts everything else behind a disclosure.
   *
   *  `returnTo` is where the new vendor should land. From the PO form it is
   *  selected in the picker; from the vendor master it just appears in the
   *  table.
   */
  let vendorReturnTo = null;

  function openVendorModal(returnTo) {
    const modal = $('#nv-modal');
    if (!modal) return;
    vendorReturnTo = returnTo || null;
    modal.hidden = false;
    document.body.classList.add('is-modal');
    $('#nv-note').textContent = '';
    $('#nv-note').className = 'save-note';
    const name = $('#nv-name');
    name.value = '';
    name.focus();
  }

  function closeVendorModal() {
    const modal = $('#nv-modal');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove('is-modal');
    // The picker still reads "Adding a new vendor" if that is what opened it,
    // and nothing was added - so put it back to nothing chosen.
    if (vendorReturnTo === 'picker' && $('#np-vendor') && $('#np-vendor').value === '__new__') {
      chooseVendor('');
    }
    vendorReturnTo = null;
  }

  function wireVendorModal() {
    const modal = $('#nv-modal');
    if (!modal) return;

    $$('[data-nv-close]', modal).forEach((node) =>
      node.addEventListener('click', closeVendorModal));

    $('#nv-save').addEventListener('click', createVendor);
    $('#nv-form').addEventListener('submit', (event) => {
      event.preventDefault();
      createVendor();
    });

    // Escape closes it, and focus is kept inside while it is open - the two
    // things a dialog owes anyone not using a mouse.
    document.addEventListener('keydown', (event) => {
      if (modal.hidden) return;
      if (event.key === 'Escape') { closeVendorModal(); return; }
      if (event.key !== 'Tab') return;
      const focusable = $$('button, input, summary, [href]', modal)
        .filter((node) => node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    });

    // Every way into the dialog, wherever the button happens to be.
    document.addEventListener('click', (event) => {
      if (event.target.closest('[data-add-vendor]')) openVendorModal('master');
    });
  }

  function resetPoForm() {
    const today = new Date();
    const later = new Date(today.getTime() + DEFAULT_PO_WINDOW_DAYS * DAY_MS);
    if ($('#np-number')) $('#np-number').value = suggestPoNumber();
    if ($('#np-desc')) $('#np-desc').value = '';
    if ($('#np-value')) $('#np-value').value = '';
    if ($('#np-vendor')) renderVendorOptions('');
    if ($('#np-issued')) $('#np-issued').value = isoDay(today);
    if ($('#np-expiry')) $('#np-expiry').value = isoDay(later);
    if ($('#np-note')) $('#np-note').textContent = '';
  }

  async function createPo() {
    const note = $('#np-note');
    const payload = {
      po_number: $('#np-number').value.trim().toUpperCase(),
      vendor_id: $('#np-vendor').value,
      description: $('#np-desc').value.trim(),
      po_value: Number($('#np-value').value),
      issued_date: $('#np-issued').value,
      expiry_date: $('#np-expiry').value || null,
    };

    // The three mistakes worth catching without a round trip. Everything else
    // is validated server-side, where it has to be anyway.
    if (!payload.po_number) return fail(note, 'Give the purchase order a number.');
    if (!payload.vendor_id) return fail(note, 'Choose the vendor this PO is raised on.');
    if (payload.vendor_id === '__new__') {
      return fail(note, 'Add the new vendor first, then raise the PO against it.');
    }
    if (!payload.po_value || payload.po_value <= 0) {
      return fail(note, 'Enter the PO value in rupees.');
    }

    const button = $('#np-save');
    button.disabled = true;
    note.className = 'save-note';
    note.textContent = 'Writing it to the purchase-order master\u2026';
    try {
      const created = await api('/api/pos', { method: 'POST', body: JSON.stringify(payload) });
      note.textContent = '';
      await loadPos();

      state.posFilter = 'MINE';
      $$('#po-filters .chip').forEach((chip) =>
        chip.classList.toggle('is-active', chip.dataset.pofilter === 'MINE'));
      state.posQuery = '';
      $('#po-search').value = '';
      renderPosTable();
      renderPoCreated(created);
      resetPoForm();
      toast(`${created.po_number} added to the purchase-order master`);
    } catch (err) {
      fail(note, err.message);
    } finally {
      button.disabled = false;
    }
  }

  /** Put a message under a form and mark it as a problem. Returns null so a
   *  caller can `return fail(...)` in one line. */
  function fail(note, message) {
    note.className = 'save-note is-err';
    note.textContent = message;
    return null;
  }

  async function createVendor() {
    const note = $('#nv-note');
    const button = $('#nv-save');
    const payload = {
      name: $('#nv-name').value.trim(),
      aliases: $('#nv-aliases').value.trim(),
      gstin: $('#nv-gstin').value.trim() || null,
      payment_terms_days: Number($('#nv-terms').value || 30),
    };
    if (payload.name.length < 3) return fail(note, 'Give the vendor its registered name.');

    button.disabled = true;
    note.className = 'save-note';
    note.textContent = 'Adding it to the approved master…';
    try {
      const created = await api('/api/vendors', { method: 'POST', body: JSON.stringify(payload) });
      state.vendors = await api('/api/vendors');
      const landed = vendorReturnTo;

      // Leave the dialog clean for next time. Guarded, because the vendor is
      // already created by this point - a missing field here must not throw
      // and leave the dialog open over work that succeeded.
      ['#nv-name', '#nv-aliases', '#nv-gstin'].forEach((sel) => {
        const field = $(sel);
        if (field) field.value = '';
      });
      if ($('#nv-terms')) $('#nv-terms').value = 30;
      const more = $('#nv-form .modal-more');
      if (more) more.open = false;

      vendorReturnTo = null;
      $('#nv-modal').hidden = true;
      document.body.classList.remove('is-modal');

      if (landed === 'picker') {
        renderVendorOptions(created.vendor_id);
        toast(`${created.name} added as ${created.vendor_id}, and selected`);
      } else {
        renderVendorOptions($('#np-vendor') ? $('#np-vendor').value : '');
        renderVendors(state.vendors);
        toast(`${created.name} added as ${created.vendor_id}`);
      }
    } catch (err) {
      fail(note, err.message);
    } finally {
      button.disabled = false;
    }
  }

  /** The panel that appears after an insert.
   *
   *  It is not a success message. A PO on its own decides nothing, so this is
   *  where the generated invoices live: one click per variant, each one a real
   *  PDF through the real pipeline, and each one stating the verdict it should
   *  earn *before* it runs. A demo that announces its expectation first is a
   *  demo you can catch being wrong.
   */
  function renderPoCreated(po) {
    const node = $('#np-done');
    if (!node) return;
    node.hidden = false;
    // The header stays; everything under it folds. Raising several POs in a
    // row otherwise pushes the form further down the page each time, and once
    // you know what the three buttons do you want the row, not the briefing.
    node.innerHTML = `
      <button class="npo-done-head" type="button" aria-expanded="true" aria-controls="npo-done-body">
        <svg class="npo-caret" viewBox="0 0 12 8" width="11" height="7" aria-hidden="true">
          <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor"
                stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <code class="po-code">${esc(po.po_number)}</code>
        <span class="po-mine">added by you</span>
        <span class="npo-done-sum">${esc(po.vendor_name || po.vendor_id)} ·
          ${money(po.po_value, 'INR')} · open until ${esc(po.expiry_date || 'further notice')}</span>
      </button>
      <div class="npo-done-body" id="npo-done-body">
        <p class="npo-done-lead">Live in the master now &mdash; stage 5 scores invoices against
           it like any other PO. Generate one against it:</p>
        ${variantButtons(po.po_number)}
        <p class="hint">A real single-page PDF from <code>backend/invoice_pdf.py</code>,
           submitted through the same <code>POST /api/runs</code> path as a dragged-in file.</p>
      </div>`;

    const head = $('.npo-done-head', node);
    head.addEventListener('click', () => {
      const open = node.classList.toggle('is-folded');
      head.setAttribute('aria-expanded', String(!open));
    });

    wireTestInvoice(node);
  }

  const EXPECT_COPY = {
    APPROVE: 'should be approved',
    FLAG: 'should be flagged for a human',
    REJECT: 'should be rejected',
  };

  function variantButtons(poNumber) {
    if (!state.variants.length) {
      return '<p class="hint">The generated-invoice variants could not be loaded.</p>';
    }
    return `<div class="tiv" data-tiv-po="${esc(poNumber)}">${state.variants.map((v) => `
      <button class="tiv-btn" type="button" data-variant="${esc(v.id)}">
        <span class="tiv-top">
          <span class="tiv-label">${esc(v.label)}</span>
          ${decisionBadge(v.expect)}
        </span>
        <span class="tiv-why">${esc(v.why)}</span>
        <span class="tiv-go">Generate it and run it &rarr;
          <em>${esc(EXPECT_COPY[v.expect] || '')}</em></span>
      </button>`).join('')}</div>`;
  }

  /** The same three actions, on the expanded row of any PO you raised. */
  function testInvoiceStrip(po) {
    return `
      <div class="tiv-wrap">
        <div class="tiv-head">
          <span>Check an invoice against ${esc(po.po_number)}</span>
          <button class="btn btn-ghost btn-sm" type="button"
                  data-po-delete="${esc(po.po_number)}">Delete this PO</button>
        </div>
        ${variantButtons(po.po_number)}
      </div>`;
  }

  function wireTestInvoice(root) {
    $$('.tiv', root).forEach((group) => {
      $$('.tiv-btn', group).forEach((button) => {
        button.addEventListener('click', (event) => {
          event.stopPropagation();          // the PO row is itself a button
          runTestInvoice(group.dataset.tivPo, button.dataset.variant, button);
        });
      });
    });
    $$('[data-po-delete]', root).forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        deletePo(button.dataset.poDelete);
      });
    });
  }

  /** Generate an invoice for a PO and attach the run view to it.
   *
   *  `attachRun` already switches to the Check-an-invoice tab, so the click
   *  lands on the live tracker with stage 1 already running. The declared
   *  expectation travels with it so the verdict can be read against what was
   *  promised.
   */
  async function runTestInvoice(poNumber, variant, button) {
    if (button) button.disabled = true;
    try {
      const started = await api(
        `/api/pos/${encodeURIComponent(poNumber)}/test-invoice`,
        { method: 'POST', body: JSON.stringify({ variant }) },
      );
      attachRun(started.run_id, started.filename, started);
    } catch (err) {
      toast(`Could not generate an invoice: ${err.message}`, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function deletePo(poNumber) {
    if (!window.confirm(
      `Delete ${poNumber}, and forget the generated test invoices run against it?`)) return;
    try {
      const out = await api(`/api/pos/${encodeURIComponent(poNumber)}`, { method: 'DELETE' });
      $('#np-done').hidden = true;
      await loadPos();
      toast(out.test_runs_cleared
        ? `${poNumber} deleted, with ${out.test_runs_cleared} generated run(s)`
        : `${poNumber} deleted`);
    } catch (err) {
      toast(`Could not delete ${poNumber}: ${err.message}`, true);
    }
  }

  /** What a generated invoice was built to demonstrate, above the tracker.
   *
   *  While it runs this states the expectation; once the decision lands it
   *  says whether the pipeline agreed. The honest case matters more than the
   *  happy one: if the rules reach a different verdict, this says so rather
   *  than quietly keeping the label.
   */
  function renderIntent() {
    const node = $('#run-intent');
    if (!node) return;
    const intent = state.intent;
    if (!intent) { node.hidden = true; node.innerHTML = ''; return; }

    const actual = state.run && state.run.decision ? state.run.decision.decision : null;
    const agreed = actual ? actual === intent.expect : null;
    node.hidden = false;
    node.className = `intent ${agreed === null ? '' : agreed ? 'is-agreed' : 'is-diverged'}`;
    node.innerHTML = `
      <div class="intent-k">Generated for ${esc(intent.po_number)}</div>
      <div class="intent-body">
        <p class="intent-lead">${esc(intent.label)} &mdash; ${money(intent.total, 'INR')}
           against a ${money(intent.po_value, 'INR')} purchase order
           (<b>${intent.variance_pct > 0 ? '+' : ''}${esc(intent.variance_pct)}%</b>).</p>
        <p class="intent-why">${esc(intent.why)}</p>
      </div>
      <div class="intent-verdict">
        <div class="intent-row"><span>Expected</span>${decisionBadge(intent.expect)}</div>
        <div class="intent-row"><span>Decided</span>${
          actual ? decisionBadge(actual) : '<span class="badge badge-live">running…</span>'}</div>
        ${agreed === false
          ? `<p class="intent-note">The rules reached a different verdict. That is the record,
               not a bug in the label - open the checks below to see which rule decided it.</p>`
          : ''}
      </div>`;
  }

  function renderVendors(vendors) {
    $('#vendors-table tbody').innerHTML = vendors.map((vendor) => `<tr>
      <td><code>${esc(vendor.vendor_id)}</code>${vendor.source === 'user'
        ? ' <span class="po-mine">added by you</span>' : ''}</td>
      <td>${esc(vendor.name)}</td>
      <td class="wrap">${esc((vendor.aliases || []).join(', '))}</td>
      <td><code>${esc(vendor.gstin || '—')}</code></td>
      <td>${vendor.status === 'approved'
        ? '<span class="badge badge-approve">approved</span>'
        : '<span class="badge badge-reject">blocked</span>'}</td></tr>`).join('');
  }

  function renderTelemetry(telemetry) {
    const tiles = [
      { label: 'API calls', value: telemetry.total_llm_calls },
      { label: 'Cache hits', value: telemetry.cache_hits,
        sub: `${num(telemetry.cache_hit_rate * 100, 0)}% of calls avoided` },
      { label: 'Input tokens', value: telemetry.input_tokens.toLocaleString() },
      { label: 'Output tokens', value: telemetry.output_tokens.toLocaleString() },
      { label: 'Estimated cost', value: `$${num(telemetry.estimated_cost_usd, 4)}` },
      { label: 'p50 latency', value: duration(telemetry.p50_latency_ms) },
      { label: 'p95 latency', value: duration(telemetry.p95_latency_ms) },
    ];
    $('#telemetry-tiles').innerHTML = tiles.map((tile) => `
      <div class="tile">
        <div class="tile-label">${esc(tile.label)}</div>
        <div class="tile-value">${esc(tile.value)}</div>
        ${tile.sub ? `<div class="tile-sub">${esc(tile.sub)}</div>` : ''}
      </div>`).join('');

    $('#telemetry-models').innerHTML = telemetry.by_model && telemetry.by_model.length
      ? `<table class="mini"><thead><tr><th>Model</th><th class="num">Calls</th>
         <th class="num">In</th><th class="num">Out</th><th class="num">Rate in/out per MTok</th>
         <th class="num">Cost</th></tr></thead><tbody>${
          telemetry.by_model.map((row) => `<tr>
            <td><code>${esc(row.model)}</code></td>
            <td class="num">${row.calls}</td>
            <td class="num">${row.input_tokens.toLocaleString()}</td>
            <td class="num">${row.output_tokens.toLocaleString()}</td>
            <td class="num">$${num(row.input_rate_per_mtok, 2)} / $${num(row.output_rate_per_mtok, 2)}</td>
            <td class="num">$${num(row.estimated_cost_usd, 4)}</td></tr>`).join('')
         }</tbody></table>`
      : '<p class="hint">No live API calls recorded yet.</p>';

    $('#telemetry-note').textContent = telemetry.stage_delay_note || '';
  }

  async function reseed(clearRuns) {
    const note = $('#reseed-note');
    if (clearRuns && !window.confirm('Delete every run and reset the purchase orders?')) return;
    try {
      const result = await api('/api/admin/reseed', {
        method: 'POST', body: JSON.stringify({ clear_runs: !!clearRuns }),
      });
      note.classList.remove('is-err');
      note.textContent = `Reset: ${result.vendors} vendors, ${result.purchase_orders} POs, `
        + `${result.runs} run(s) remaining.`;
      loadAdmin();
      loadHealth();
      if (clearRuns) {
        // The run that was on screen no longer exists, so go back to the
        // intake rather than leaving a record that cannot be re-opened.
        if (state.source) { state.source.close(); state.source = null; }
        state.runs = [];
        state.run = null;
        state.stages = [];
        showResultView(false);
      }
    } catch (err) {
      note.classList.add('is-err');
      note.textContent = err.message;
    }
  }

  // ------------------------------------------------------------------- wire

  function wire() {
    const dropzone = $('#dropzone');
    const input = $('#file-input');

    dropzone.addEventListener('click', () => input.click());
    dropzone.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); }
    });
    $('#browse-btn').addEventListener('click', (event) => { event.stopPropagation(); input.click(); });
    input.addEventListener('change', () => {
      uploadFile(input.files[0]);
      input.value = '';
    });

    ['dragenter', 'dragover'].forEach((type) =>
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.add('is-over');
      }));
    ['dragleave', 'drop'].forEach((type) =>
      dropzone.addEventListener(type, () => dropzone.classList.remove('is-over')));
    dropzone.addEventListener('drop', (event) => {
      event.preventDefault();
      uploadFile(event.dataTransfer.files[0]);
    });

    const envBtn = $('#env-btn');
    if (envBtn) {
      envBtn.addEventListener('click', () => {
        const pop = $('#env-strip');
        pop.hidden = !pop.hidden;
        envBtn.setAttribute('aria-expanded', String(!pop.hidden));
      });
    }
    $('#back-btn').addEventListener('click', () => {
      if (state.source) { state.source.close(); state.source = null; }
      showResultView(false);
    });
    $('#replay-btn').addEventListener('click', replayRun);
    $('#view-pdf-btn').addEventListener('click', () => {
      if (state.run) window.open(`/api/runs/${encodeURIComponent(state.run.run_id)}/file`, '_blank');
    });
    $('#expand-btn').addEventListener('click', () => {
      const total = state.stages.length || 7;
      if (state.openStages.size >= total) state.openStages.clear();
      else state.stages.forEach((stage) => {
        if (stage.status !== 'pending') state.openStages.add(stage.seq);
      });
      renderTimeline();
    });

    $$('#decision-filters .chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        state.filter = chip.dataset.filter;
        $$('#decision-filters .chip').forEach((other) =>
          other.classList.toggle('is-active', other === chip));
        renderRunsTable();
      });
    });

    $$('#po-filters .chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        state.posFilter = chip.dataset.pofilter;
        $$('#po-filters .chip').forEach((other) =>
          other.classList.toggle('is-active', other === chip));
        renderPosTable();
      });
    });

    $('#po-search').addEventListener('input', (e) => {
      state.posQuery = e.target.value;
      renderPosTable();
    });

    $$('#pos-table th[data-posort]').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.posort;
        state.posSort = state.posSort.key === key
          ? { key, dir: state.posSort.dir === 'asc' ? 'desc' : 'asc' }
          : { key, dir: 'desc' };
        renderPosTable();
      });
    });

    $$('#runs-table th[data-sort]').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        state.sort = state.sort.key === key
          ? { key, dir: state.sort.dir === 'asc' ? 'desc' : 'asc' }
          : { key, dir: 'desc' };
        renderRunsTable();
      });
    });

    const save = $('#np-save');
    if (save) {
      $('#np-clear').addEventListener('click', () => { resetPoForm(); $('#np-done').hidden = true; });
      // One handler, not two. `#np-save` sits outside the form in the card's
      // action row and is tied back to it with the `form` attribute, so a
      // click and an Enter keypress both arrive here as the same submit.
      $('#new-po-form').addEventListener('submit', (event) => {
        event.preventDefault();
        createPo();
      });

      wireVendorPicker();
    }

    // The "N added by you" pill in the budget panel is also the filter for it.
    document.addEventListener('click', (event) => {
      const go = event.target.closest('[data-pofilter-go]');
      if (!go) return;
      state.posFilter = go.dataset.pofilterGo;
      $$('#po-filters .chip').forEach((chip) =>
        chip.classList.toggle('is-active', chip.dataset.pofilter === state.posFilter));
      renderPosTable();
      $('#pos-table').scrollIntoView({ block: 'start', behavior: 'smooth' });
    });

    $('#config-save').addEventListener('click', saveConfig);
    $('#config-reset').addEventListener('click', resetConfig);
    $('#reseed-btn').addEventListener('click', () => reseed(false));
    $('#reseed-all-btn').addEventListener('click', () => reseed(true));
  }

  /** Fold the whole run story away.
   *
   *  Separate from "Expand all", which opens the payload under each step: this
   *  is the section, that is what is inside it. Someone who has read the steps
   *  once wants the verdict and the document side by side without scrolling
   *  past seven of them, and someone watching a run happen wants the opposite.
   *  The state is remembered for the session, because whichever of those two
   *  you are, you are that for more than one invoice.
   */
  let storyFolded = false;

  function setStoryFolded(folded) {
    const section = $('#run-detail');
    const toggle = $('#story-toggle');
    if (!section || !toggle) return;
    storyFolded = !!folded;
    section.classList.toggle('is-folded', storyFolded);
    toggle.setAttribute('aria-expanded', String(!storyFolded));
    const expand = $('#expand-btn');
    // Expanding a step's payload inside a panel that is folded shut would do
    // nothing visible, so that control goes with it.
    if (expand) expand.hidden = storyFolded;
  }

  function wireStoryToggle() {
    const toggle = $('#story-toggle');
    if (!toggle) return;
    toggle.addEventListener('click', () => setStoryFolded(!storyFolded));
    setStoryFolded(storyFolded);
  }

  /** Back to top.
   *
   *  Four of the six pages are several screens tall - the rule set, the PO
   *  master, a finished run, and How it works - and the navigation lives in a
   *  bar at the very top of the document. Appears once scrolling has gone far
   *  enough for the header to be properly gone.
   */
  function wireToTop() {
    const button = $('#to-top');
    if (!button) return;
    const update = () => button.classList.toggle('is-on', window.scrollY > 520);
    window.addEventListener('scroll', update, { passive: true });
    button.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    update();
  }

  wire();
  wireToTop();
  wireStoryToggle();
  wireVendorModal();
  loadHealth();
  loadSamples();
  // The dashboard is the front page: what the system has decided so far is the
  // first question anyone has, and checking a new invoice is one click from it.
  showTab('dashboard');
})();
