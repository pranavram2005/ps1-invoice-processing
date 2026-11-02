/* ==========================================================================
   The diagram vocabulary for "How it works".

   Hand-built SVG rather than a diagram library, for the same reason the rest
   of this frontend is hand-built: DECISIONS.md S12 rules out a CDN dependency
   at render time, and a graded live demo should not be able to break because a
   third-party script was slow. Mermaid and friends are all CDN scripts.

   The geometry is deliberately tight. A diagram earns its place by letting
   someone take in a whole mechanism at a glance, and that only works if the
   whole thing fits in one glance: small boxes, short arrows, a title and at
   most one line of detail. Every visual decision is a CSS class rather than a
   presentation attribute, so one stylesheet keeps the four figures looking
   like one set - and `var()` is not valid in an SVG attribute anyway.
   ========================================================================== */
(function () {
  'use strict';

  const esc = (value) =>
    String(value == null ? '' : value).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* The shared arrowhead: one definition per figure, used by every arrow. */
  const DEFS = `
    <defs>
      <marker id="dg-head" viewBox="0 0 8 8" refX="7.2" refY="4"
              markerWidth="6.5" markerHeight="6.5" orient="auto">
        <path class="dg-head" d="M0 .6 L7.4 4 L0 7.4 z"/>
      </marker>
    </defs>`;

  /** A labelled box.
   *
   *  `tone` picks it out: `in` is what arrives, `step` something the system
   *  does, `ref` a record it reads, `decide` a branch, and the three verdict
   *  tones carry the colours the rest of the app already uses for them. One
   *  line of title and at most one of detail - anything longer belongs in the
   *  caption, not in the picture.
   */
  function box(x, y, w, h, title, opts) {
    const o = opts || {};
    const sub = o.sub || '';
    const cx = x + w / 2;
    return `
      <g class="dg-box dg-box--${o.tone || 'plain'}">
        <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="5"/>
        <text class="dg-t" x="${cx}" y="${sub ? y + h / 2 - 2 : y + h / 2 + 4}">${esc(title)}</text>
        ${sub ? `<text class="dg-s" x="${cx}" y="${y + h / 2 + 12}">${esc(sub)}</text>` : ''}
      </g>`;
  }

  /** A straight arrow, optionally labelled above the line. */
  function arrow(x1, y1, x2, y2, label) {
    return `
      <g class="dg-arrow">
        <path d="M${x1} ${y1} L${x2} ${y2}" marker-end="url(#dg-head)"/>
        ${label ? `<text class="dg-l" x="${(x1 + x2) / 2}" y="${y1 - 6}">${esc(label)}</text>` : ''}
      </g>`;
  }

  /** An arrow that turns a corner - for a branch that leaves a row rather
   *  than continuing along it. */
  function elbow(path, label, lx, ly) {
    return `
      <g class="dg-arrow">
        <path d="${path}" marker-end="url(#dg-head)"/>
        ${label ? `<text class="dg-l" x="${lx}" y="${ly}">${esc(label)}</text>` : ''}
      </g>`;
  }

  /** A hairline, where one point fans to several and an arrowhead at each end
   *  would be noise. */
  function thin(x1, y1, x2, y2) {
    return `<path class="dg-thin" d="M${x1} ${y1} L${x2} ${y2}"/>`;
  }

  function text(x, y, value, cls) {
    return `<text class="${cls || 'dg-s'}" x="${x}" y="${y}">${esc(value)}</text>`;
  }

  /** A numbered marker, for the sequence figure. */
  function num(x, y, n) {
    return `
      <g class="dg-num">
        <circle cx="${x}" cy="${y}" r="9"/>
        <text class="dg-num-t" x="${x}" y="${y + 3.5}">${esc(n)}</text>
      </g>`;
  }

  let count = 0;

  /** One figure: the drawing, then its caption under a hairline.
   *
   *  The caption says what the picture is *for*, which is the part a reader
   *  cannot infer from boxes and arrows. A diagram is the one thing on this
   *  page with a minimum width, so it scrolls inside its own box rather than
   *  widening the page.
   */
  function figure(caption, spec) {
    count += 1;
    const min = spec.min || 680;
    return `
      <figure class="dg">
        <div class="dg-scroll">
          <svg viewBox="0 0 ${spec.width} ${spec.height}" style="min-width:${min}px"
               role="img" aria-label="${esc(caption)}">${DEFS}${spec.svg}</svg>
        </div>
        <figcaption class="dg-cap"><b>Figure ${count}</b> ${caption}</figcaption>
      </figure>`;
  }

  function resetFigures() { count = 0; }

  window.UML = { esc, box, arrow, elbow, thin, text, num, figure, resetFigures, DEFS };
})();
