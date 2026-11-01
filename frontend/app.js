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
        if (payload && payload.detail) detail = payload.detail;
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
  };

  // -------------------------------------------------------------------- tabs

  function showTab(name) {
    $$('.nav-link').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === name));
    $$('.panel').forEach((panel) => panel.classList.toggle('is-active', panel.id === `panel-${name}`));
    // A tab is a different page; land at the top of it rather than wherever the
    // last one happened to be scrolled to.
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    if (name === 'dashboard') loadDashboard();
    if (name === 'admin') loadAdmin();
    if (name === 'layouts') loadLayouts();
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
    // One <select> rather than a wall of cards. Fourteen scenarios is a useful
    // amount of test coverage and a terrible amount of landing page: it buried
    // the actual task under a menu most visitors never need.
    const select = $('#sample-select');
    const why = $('#sample-why');
    if (!state.samples.length) {
      select.innerHTML = '<option>No bundled samples found</option>';
      select.disabled = true;
      $('#sample-run').disabled = true;
      why.textContent = 'Generate them with: node tools/invoice_gen/generate.js';
      return;
    }
    select.innerHTML = state.samples
      .map((s) => `<option value="${esc(s.file)}">${esc(s.title)} — expects ${esc(s.expected)}</option>`)
      .join('');

    const describe = () => {
      const picked = state.samples[select.selectedIndex];
      why.textContent = picked ? picked.why : '';
    };
    select.addEventListener('change', describe);
    describe();
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
  function attachRun(runId, filename) {
    if (state.source) { state.source.close(); state.source = null; }
    state.openStages.clear();
    state.run = { run_id: runId, filename, status: 'running' };
    state.stages = [];
    showResultView(true);
    $('#run-filename').textContent = filename || runId;
    $('#run-meta').innerHTML = '';
    $('#decision').hidden = true;
    $('#run-panes').hidden = true;
    $('#run-detail').hidden = true;
    $('#ai-summary').hidden = true;
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

  function renderTimeline() {
    const container = $('#timeline');
    const stages = state.stages.length
      ? state.stages
      : Array.from({ length: 7 }, (_, i) => ({ seq: i + 1, label: '…', status: 'pending', payload: {} }));

    container.innerHTML = stages.map((stage) => {
      const open = state.openStages.has(stage.seq);
      const interactive = stage.status !== 'pending';
      const mark = stage.status === 'running'
        ? '<span class="spinner"></span>'
        : (STATUS_MARK[stage.status] || stage.seq);
      const body = interactive ? renderStageBody(stage) : '';
      return `
        <div class="stage is-${esc(stage.status)}${open ? ' is-open' : ''}" data-seq="${stage.seq}">
          <button class="stage-row" type="button" ${interactive ? '' : 'disabled'}>
            <span class="stage-mark">${mark}</span>
            <span class="stage-main">
              <span class="stage-label">${stage.seq}. ${esc(stage.label)}</span>
              <span class="stage-summary">${stage.error ? esc(stage.error) : esc(stage.summary || '')}</span>
            </span>
            <span class="stage-right">
              <span class="stage-time">${stage.duration_ms != null ? duration(stage.duration_ms) : ''}</span>
              ${interactive && body ? '<span class="caret">▶</span>' : ''}
            </span>
          </button>
          ${body ? `<div class="stage-body">${body}</div>` : ''}
        </div>`;
    }).join('');

    $$('.stage', container).forEach((node) => {
      const seq = Number(node.dataset.seq);
      const row = $('.stage-row', node);
      if (!row || row.disabled) return;
      row.addEventListener('click', () => {
        if (state.openStages.has(seq)) state.openStages.delete(seq);
        else state.openStages.add(seq);
        node.classList.toggle('is-open');
      });
    });

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
  }

  // Short names for the tracker, plus what each stage is actually doing. The
  // stage rows keep the precise engineering labels; these are what a reviewer
  // reads while waiting, and they are the difference between a progress bar
  // and understanding what the system is busy with.
  const PHASES = [
    {
      name: 'Read file',
      doing: 'Opening the PDF, fingerprinting it, and checking whether it has real text or is a scan.',
      done: 'Opened the file and checked whether it has a text layer.',
    },
    {
      name: 'Extract',
      doing: 'A model is reading the page and pulling out the vendor, dates, amounts and PO reference.',
      done: 'Read the fields off the page.',
    },
    {
      name: 'Verify',
      doing: 'Checking each extracted figure against the text actually printed in the document.',
      done: 'Checked every figure against the document itself.',
    },
    {
      name: 'Vendor',
      doing: 'Looking the supplier up on the approved vendor list.',
      done: 'Looked the supplier up on the approved list.',
    },
    {
      name: 'Match PO',
      doing: 'Scoring every plausible purchase order to find which one this bills against.',
      done: 'Found the purchase order this bills against.',
    },
    {
      name: 'Check rules',
      doing: 'Running all nine checks - totals, tolerances, duplicates, dates, confidence.',
      done: 'Ran all nine checks.',
    },
    {
      name: 'Decide',
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
      <p class="chart-foot">Showing ${rows.length} of ${pos.length}. Open any PO for the invoices that consumed it.</p>`;
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
      const [rules, config, pos, vendors, telemetry] = await Promise.all([
        api('/api/rules'), api('/api/config'), api('/api/pos'),
        api('/api/vendors'), api('/api/admin/telemetry'),
      ]);
      state.config = config.config;
      state.defaults = config.defaults;
      renderRules(rules);
      renderConfigForm(config);
      renderPos(pos);
      renderVendors(vendors);
      renderTelemetry(telemetry);
    } catch (err) {
      toast(`Admin failed to load: ${err.message}`, true);
    }
  }

  function renderRules(payload) {
    $('#rules-list').innerHTML = payload.rules.map((rule) => `
      <div class="rule-def">
        <div class="rule-id">${esc(rule.id)}</div>
        <div>
          <div class="rd-title">${esc(rule.title)}</div>
          <div class="rd-checks">${esc(rule.checks)}</div>
          <div class="rd-meta">
            <span class="badge">${esc(rule.severity)}</span>
            ${Object.entries(rule.active_thresholds || {}).map(([key, value]) =>
              `<span class="badge badge-accent"><code>${esc(key)}</code> = ${esc(value)}</span>`).join('')}
          </div>
        </div>
      </div>`).join('')
      + `<div class="note note-info">${esc(payload.decision_logic)}</div>`;
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

  /** The purchase-order master - the data every PO check is decided against.
   *
   *  This is the source of truth behind rules R4 (was a PO matched), R5 (is the
   *  invoice inside its tolerance) and R6 (does cumulative billing exceed it),
   *  so it is laid out to be analysed rather than merely displayed: sorted by
   *  how consumed each PO is, with the headroom stated in money as well as
   *  percent, and every row expandable to the individual invoices that consumed
   *  it. A number that decides a payment should be traceable to the invoices
   *  that produced it.
   */
  function renderPos(pos) {
    const rows = pos
      .map((po) => ({ ...po, pct: po.po_value ? (po.already_billed / po.po_value) * 100 : 0 }))
      .sort((a, b) => b.pct - a.pct);

    const totals = rows.reduce((acc, po) => ({
      value: acc.value + (po.po_value || 0),
      billed: acc.billed + (po.already_billed || 0),
    }), { value: 0, billed: 0 });

    $('#pos-summary').innerHTML = `
      <span><b>${rows.length}</b> purchase orders</span>
      <span><b>${money(totals.value, 'INR')}</b> committed</span>
      <span><b>${money(totals.billed, 'INR')}</b> billed</span>
      <span><b>${money(totals.value - totals.billed, 'INR')}</b> still available</span>`;

    $('#pos-table tbody').innerHTML = rows.map((po) => {
      const pct = po.pct;
      const state = pct >= 100 ? 'is-over' : pct >= 80 ? 'is-near' : 'is-ok';
      const tag = pct >= 100 ? 'fully billed' : pct >= 80 ? 'nearly spent' : '';
      return `<tr class="po-main ${state}" data-po="${esc(po.po_number)}" tabindex="0">
        <td><code class="po-code">${esc(po.po_number)}</code></td>
        <td>${esc(po.vendor_name || po.vendor_id)}
            <div class="po-desc">${esc(po.description || '')}</div></td>
        <td class="num">${money(po.po_value)}</td>
        <td class="num">${money(po.already_billed)}</td>
        <td class="num"><b>${money(po.remaining)}</b></td>
        <td class="po-cell">
          <div class="po-meter">
            <span class="po-meter-fill" style="width:${Math.min(pct, 100)}%"></span>
          </div>
          <span class="po-meter-pct">${pct.toFixed(0)}%</span>
          ${tag ? `<span class="po-tag">${tag}</span>` : ''}
        </td>
        <td class="po-when">${esc(po.issued_date)}<br><span>to ${esc(po.expiry_date || '—')}</span></td>
      </tr>
      <tr class="po-detail" data-detail="${esc(po.po_number)}" hidden>
        <td colspan="7"><div class="po-detail-body">Loading…</div></td>
      </tr>`;
    }).join('');

    // Expand a row to the invoices that actually consumed it.
    $$('#pos-table .po-main').forEach((tr) => {
      const open = async () => {
        const detail = $(`#pos-table [data-detail="${CSS.escape(tr.dataset.po)}"]`);
        if (!detail) return;
        detail.hidden = !detail.hidden;
        tr.classList.toggle('is-open', !detail.hidden);
        if (detail.hidden || detail.dataset.loaded) return;
        try {
          const po = await api(`/api/pos/${encodeURIComponent(tr.dataset.po)}`);
          const used = po.consumption || [];
          $('.po-detail-body', detail).innerHTML = used.length
            ? `<table class="po-sub"><thead><tr>
                 <th>Invoice</th><th class="num">Amount</th><th>Booked</th><th>Run</th>
               </tr></thead><tbody>${used.map((c) => `<tr>
                 <td>${esc(c.invoice_number || '—')}</td>
                 <td class="num">${money(c.amount, 'INR')}</td>
                 <td>${when(c.booked_at)}</td>
                 <td><a class="reclink" href="#" data-run="${esc(c.run_id)}">${esc(c.run_id)}</a></td>
               </tr>`).join('')}</tbody></table>`
            : '<p class="hint">Nothing has been billed against this PO yet, so rule R6 has no ledger to check and stands aside.</p>';
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

  function renderVendors(vendors) {
    $('#vendors-table tbody').innerHTML = vendors.map((vendor) => `<tr>
      <td><code>${esc(vendor.vendor_id)}</code></td>
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
        state.runs = [];
        state.run = null;
        $('#run-live').hidden = true;
        $('#run-empty').hidden = false;
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
    $('#sample-run').addEventListener('click', () => {
      const select = $('#sample-select');
      if (select && select.value) runSample(select.value);
    });
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

    $$('#runs-table th[data-sort]').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        state.sort = state.sort.key === key
          ? { key, dir: state.sort.dir === 'asc' ? 'desc' : 'asc' }
          : { key, dir: 'desc' };
        renderRunsTable();
      });
    });

    $('#config-save').addEventListener('click', saveConfig);
    $('#config-reset').addEventListener('click', resetConfig);
    $('#reseed-btn').addEventListener('click', () => reseed(false));
    $('#reseed-all-btn').addEventListener('click', () => reseed(true));
  }

  wire();
  loadHealth();
  loadSamples();
})();
