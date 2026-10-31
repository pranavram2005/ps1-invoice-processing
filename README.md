# PS-1 — Invoice Processing

Drop in a vendor invoice PDF. The system extracts it, finds the matching
purchase order, runs a deterministic rule set, and emits an auditable decision —
**Approve / Flag / Reject** — with every intermediate step visible.

**The core design principle: the model extracts, the code decides.**

Extraction is fuzzy and needs a model. A payment decision has to be
explainable, repeatable and defensible to a finance team, so it is plain Python
over published thresholds. Nothing in this system asks a model what to do.

---

## Run it

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env            # then add your GEMINI_API_KEY
uvicorn backend.main:app --reload
```

Open <http://127.0.0.1:8000>.

The repository ships a pre-seeded database, so the dashboard has history from
the first load. To rebuild it from scratch:

```bash
python -m backend.seed --runs --clear
```

### Without an API key

```bash
EXTRACTION_MODE=offline uvicorn backend.main:app
```

Stage 2 is then served from bundled ground-truth fixtures for the nine sample
invoices; every stage after it is the real code path. The UI labels such runs
`offline fixture` so they can never be mistaken for live extraction. See
[Offline mode](#offline-mode) for exactly what that does and does not prove.

---

## The process — seven stages

```
  PDF in
    │
 1. INTAKE          hash the file, store the blob, probe the text layer,
    │               check the hash against previous submissions
 2. EXTRACT         one model call: PDF → schema-guaranteed JSON, with the
    │               page and verbatim quote behind every field
 3. NORMALIZE       dates → ISO, amounts → numbers, currency → ISO 4217,
    │               vendor name canonicalised, quoted provenance verified
    │               against the PDF's own text layer → confidence
 4. RESOLVE VENDOR  fuzzy match against the approved vendor master
    │
 5. MATCH PO        score every plausible PO; pick a winner or escalate with
    │               a ranked shortlist
 6. VALIDATE        run all nine rules; each emits expected vs actual
    │
 7. DECIDE          worst severity wins → APPROVE / FLAG / REJECT, with
    │               plain-English reasons
  decision out
```

Each stage writes a row to `run_stages` with its status, duration and payload
**as it happens**. That table is simultaneously the live view the browser
streams over SSE, the audit trail a reviewer reads afterwards, and the replay
the dashboard renders. There is no second, prettier version of events.

---

## The rule set

Defined once as data in `backend/rules.py` and served at `GET /api/rules`, so
the rules the UI documents cannot drift from the rules the engine applied.

| id | Rule | Severity if it fails |
|---|---|---|
| `R1` | Required fields present (invoice no., date, vendor, total) | REJECT if vendor or total is missing, else FLAG |
| `R2` | Line items + tax reconcile to the stated total (±₹1) | FLAG |
| `R3` | Vendor is on the approved list | REJECT |
| `R4` | A PO was matched at or above the score floor | FLAG |
| `R5` | Invoice total within PO tolerance | FLAG up to 10% over, then REJECT |
| `R6` | Cumulative billing does not over-consume the PO | REJECT |
| `R7` | Not a duplicate submission | REJECT exact, FLAG near |
| `R8` | Invoice date is plausible (not future, not >180 days old) | FLAG |
| `R9` | Extraction confidence clears the floor | FLAG — mandatory human review |

Worst severity wins. The decision is a pure function of the rule results.

**Every threshold is live configuration**, editable in the Admin tab or via
`PATCH /api/config`. Raise the PO tolerance from 2% to 10%, re-run the same
invoice, and the decision flips from FLAG to APPROVE. That round trip is
asserted by a test:

```bash
python tools/check_threshold_flip.py
```

---

## Edge cases

Nine sample invoices across five vendor layouts, each engineered to take a
*different path* through the rules rather than merely to parse. The expected
decision **and the exact rule that produces it** are asserted end to end:

```bash
python -m backend.seed --runs --clear   # process all nine
python tools/check_scenarios.py         # assert decision AND failing rule
```

| Scenario | Decision | Via | Why it is worth building |
|---|---|---|---|
| Clean invoice, matched PO | APPROVE | — | the baseline everything else is measured against |
| **Split invoice against one PO** | REJECT | `R6` | ₹6L against a ₹10L PO that already carries ₹5L. Each invoice is fine alone; only the cumulative ledger sees the problem. Proves the system has **state**, not just per-document logic. |
| **Near-duplicate re-issue** | FLAG | `R7` | Same vendor, amount and date, different invoice number. Exact-hash dedupe sees nothing. Duplicate payment is the most expensive AP failure mode there is. |
| **Scanned, image-only PDF** | FLAG | `R9` | No text layer, so no extracted value can be verified against the file. Confidence drops below the floor *even though every other check passes*. The correct behaviour is to be less certain, not to fail. |
| **No PO reference anywhere** | FLAG | `R4` | Falls back to vendor + amount + date and surfaces ranked candidates with scores. An inferred PO is capped below the match floor **by design**, so it always reaches a human — escalating with the work already done instead of guessing. |
| Tolerance boundary, 1.8% over | APPROVE | — | inside tolerance, approved with the variance stated |
| Tolerance boundary, 7.5% over | FLAG | `R5` | past tolerance, inside the reject threshold — the live threshold demo |
| Invoice that does not add up | FLAG | `R2` | the model is instructed to report the printed total and never to correct it, which is the only reason this is visible at all |

A sixth vendor is on the master but **blocked**, so an invoice from them fails
`R3` differently from an unknown vendor.

---

## Provenance: checked, not asserted

Every extracted field carries the page it came from and the verbatim text it
was read from. The system then **verifies** each quote against the PDF's own
text layer with a fuzzy match, and the UI shows the result per field: hover a
marker to see the quote, the page, and whether it was found.

This is not a server-side citations feature. The Claude API offers one, but it
is **incompatible with structured outputs** — enabling both on one request
returns a 400 — and a schema guarantee on a financial extraction is worth more.
Keeping provenance in the schema also means it survives a change of provider,
and that what gets shown to a reviewer is a quote the code *checked* rather than
a location the API asserted. So provenance is a field *in* the schema and
`backend/provenance.py` checks it. See
[DECISIONS.md](DECISIONS.md#2-provenance-schema-field-checked-in-code-not-api-citations).

The payoff is that confidence becomes decomposable rather than a bare float:

```
confidence = ½ · model's own confidence  +  ½ · fraction of decision-critical
                                              fields whose quote was found
```

and the scanned case falls out of the same mechanism. No text layer means
nothing is verifiable, which means a documented 0.70 penalty, which means R9
fires. The UI prints that arithmetic next to the number.

---

## Offline mode

`EXTRACTION_MODE` is `live` (default), `offline`, or `auto` (prefer live, fall
back to a fixture if the call fails — insurance for a live demo).

The fixtures in `data/fixtures.json` are the **true field values** of the
generated sample invoices, taken from the generator's own definitions, with
each provenance quote lifted verbatim from the PDF's real text layer. They are
not recorded model output and do not pretend to be.

They are good for two things: testing the deterministic half of the system
without model variance, and demoing with no network. They are **not** evidence
about extraction quality — only a live run is that. Offline runs are badged in
the UI, excluded from the cost and latency telemetry, and an unknown PDF in
offline mode fails loudly rather than inventing data.

---

## Project layout

```
backend/
  config.py       environment and pricing
  models.py       pydantic shapes: model output / derived / API surface
  db.py           SQLite; the ERP seam and the audit trail
  rules.py        the nine rules + thresholds + the decision function
  extract.py      the single model call, provider-dispatched (+ fixture path)
  gemini.py       the Gemini provider: schema translation and the REST call
  provenance.py   text-layer probe, quote verification, confidence
  normalize.py    dates, currency, canonical vendor names
  matching.py     vendor resolution and PO scoring
  pipeline.py     the seven stages
  events.py       in-process SSE broker
  main.py         FastAPI: JSON API + SSE + static frontend
  seed.py         vendor/PO masters, sample runs
frontend/         vanilla JS + hand-written CSS, no build step
tools/            development tools (see below), not part of the app
data/invoices/    the nine sample PDFs + ground_truth.json
```

### Tools

| Command | What it does |
|---|---|
| `node tools/invoice_gen/generate.js` | regenerate the sample PDFs and ground truth |
| `python tools/make_fixtures.py` | rebuild the offline extraction fixtures |
| `python tools/probe_samples.py` | assert exactly one fixture has no text layer |
| `python tools/run_one.py <file>` | run one invoice and print every stage |
| `python tools/check_scenarios.py` | assert each scenario fails for the intended rule |
| `python tools/check_sse.py` | assert the live run stream behaves |
| `python tools/check_threshold_flip.py` | assert a threshold change flips the decision |
| `node tools/ui_check.js` | drive the UI in a real browser, screenshot, fail on any console error or layout overflow |

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/runs` | upload a PDF, returns a run id immediately |
| `GET /api/runs/{id}/events` | SSE: snapshot, then live stage transitions |
| `POST /api/runs/{id}/replay` | re-run the same file under current thresholds |
| `GET /api/runs` / `GET /api/runs/{id}` | dashboard list / full audit record |
| `GET /api/runs/{id}/file` | the stored PDF |
| `GET /api/rules`, `GET|PATCH /api/config` | the rule set and its live thresholds |
| `GET /api/vendors`, `GET /api/pos` | the procurement masters |
| `GET /api/stats`, `GET /api/admin/telemetry` | dashboard tiles, token/cost/latency |
| `POST /api/samples/{name}/run` | run a bundled scenario |
| `POST /api/admin/reseed` | restore demo data between takes |

---

## Deploy

```bash
docker compose up --build        # local
```

For Render, `render.yaml` is a one-service Docker deploy on the free tier. Set
`GEMINI_API_KEY` in the dashboard — it is deliberately not synced from the
file. The image ships the seeded database, so a cold start does no model work.

One container serves the API and the UI from the same origin: one URL, no CORS
story, nothing to go wrong during a live demo. For the same reason the frontend
has **no CDN dependency** — the CSS is hand-written and there is no build step.

---

## Assumptions

Stated out loud, because each one is a real limit on what this demonstrates.

- **Invoices arrive as a file upload**, not by email polling. Email intake is an
  IMAP listener in front of stage 1 — out of scope for a week, and it adds demo
  fragility for no logic gain.
- **The PO master and vendor list stand in for a procurement system.** Every
  read goes through functions in `db.py`, so swapping in a real ERP API touches
  one module.
- **Two-way match only** (invoice ↔ PO). The real-world standard is three-way,
  including the goods-receipt note. GRN is the obvious next build.
- **Currency is INR throughout.** Multi-currency would need an FX table and a
  rate-date rule.
- **Thresholds are plausible mid-market values**, not derived from a real
  policy document.
- **Only an approved invoice consumes PO budget.** A flagged or rejected
  invoice has not been accepted, so booking it would corrupt the ledger R6
  depends on.
- **`STAGE_DELAY_MS` adds artificial pacing** so the run view is watchable. It
  is presentation, not work; it is labelled as such in the Admin tab and in the
  env strip. Set it to 0 for true pipeline timings.
- **The sample invoices are synthetic.** Both the vendors and the buyer are
  fictional, and every generated page says so in its footer.

## Known limits

- **The live path is verified on Gemini, not on Anthropic.** All nine scenarios
  have been run end to end through `gemini-3.5-flash` and each reached its
  intended decision via its intended rule, with every provenance quote verified
  against the document. The Anthropic path is still written-but-unrun: the key
  available during the build returned `credit balance is too low`. Switching
  providers is `EXTRACTION_PROVIDER=anthropic`, and
  `python tools/run_one.py INV-MER-0881_meridian_happy.pdf` is the one-command
  check with a funded key.
- **Gemini's free tier does not serve the `*-pro` models.** They return 429 with
  `limit: 0` — a hard refusal, not a throttle — so the default is a flash model.
  Flash read all nine samples correctly, but a harder vendor layout is the kind
  of thing a pro model would be expected to handle better.
- **The free tier also 503s under load.** One of the nine failed that way on the
  first live pass and succeeded on a re-run. Transient 5xx is retried three
  times with backoff; past that the run fails honestly rather than guessing.
- **The Gemini 3.x cost figures in the Admin tab are indicative.** The 2.5-series
  rates in `config.py` are published list prices; the 3.x entries carry the
  flash-tier rate forward and are marked unconfirmed in the source.
- Single-container, single-process. The SSE broker is in-process, so a
  multi-replica deployment would need a shared bus.
- The duplicate window compares vendor, amount and date. It would not catch a
  re-issue whose amount was also altered.
