# PS-1 — Invoice Processing: Build Plan

> ASA Case Study (AI Solutions Analyst, Zamp). One week. Deliverables: a live
> runnable process with a stage-by-stage run view + a dashboard of past runs,
> plus a ≤5 min narrated demo video.

---

## 1. What we're building, in one line

Drop in a vendor invoice PDF → the system extracts it, finds the matching
purchase order, runs a deterministic rule set, and emits an auditable
decision (**Approve / Flag / Reject**) with every intermediate step visible.

**The core design principle:** the LLM *extracts*, the code *decides*.
Extraction is fuzzy and needs a model. The decision must be explainable,
repeatable, and defensible to a finance team — so it's plain Python rules,
never "ask the model what to do." This is the single most important judgment
call in the build and the thing to lead with in the demo.

---

## 2. What we already have (harvested from previous projects)

### 2.1 `D:\Zetta` — Zetta Technologies take-home — **the backbone**

Same deliverable shape as this case study (pipeline + dashboard + deploy +
write-up). Roughly 60% of the scaffolding lifts across directly.

| Zetta file | Reuse for PS-1 | Change needed |
|---|---|---|
| `backend/config.py` | as-is | rename keys |
| `backend/db.py` | as-is pattern (SQLite, JSON blob for results, `llm_events` telemetry table) | new schema: `invoices`, `runs`, `run_stages`, `pos` |
| `backend/main.py` | FastAPI app + static SPA mount + `/api/*` + SPA fallback | new endpoints |
| `backend/models.py` | Pydantic result models | new shapes |
| `backend/pipeline.py` | LLM call + `tenacity` retry + SHA-256 result cache | swap transcript → PDF |
| `backend/seed.py` | bounded-concurrency batch seeding | seed PO dataset + sample invoices |
| `frontend/` (`index.html` + `app.js` 933 LOC + `styles.css`) | tab nav, sortable table, detail drawer, upload flow, progress bar | new run view + columns |
| `Dockerfile`, `docker-compose.yml`, `render.yaml` | as-is | rename service |
| `DECISIONS.md`, `AI_USAGE.md` | **reuse the format** — this is exactly the "explain your judgment" artifact Zamp grades | rewrite content |

**Patterns worth carrying over verbatim:**

- SHA-256 content hash → cache hit → zero LLM calls on re-run. Makes the live
  demo instant and idempotent, and protects against burning free-tier quota.
- `llm_events` telemetry table → an Admin tab showing calls, tokens, cost,
  p50/p95 latency. Cheap to build, reads as very senior.
- Rules defined **once as data** and exposed over an API endpoint so the UI
  renders the exact same rules the engine applied. No drift between
  "documented rules" and "applied rules." Direct transfer: `/api/rules`.
- Single container serving API + static frontend → one deploy, one URL,
  nothing to go wrong live.

**Gap:** Zetta had an upload flow with a progress bar, but *not* a true
stage-by-stage live run view. That is explicitly graded here, so it's the main
piece of new frontend work. See §5.

### 2.2 `D:\Loyalty` — **the test-data generator**

`Loyaldupbackend` has `puppeteer@24.2.1`; `Loyaldup` has `jspdf`,
`jspdf-autotable`, `pdf-lib`, `html2pdf.js`. That's a ready-made invoice-PDF
factory — we need ~8 invoices across 4–5 visually different vendor layouts.

Plan: write 5 HTML invoice templates (different vendors, different layouts —
one itemised, one bundled, one with tax embedded in line items, one with tax
broken out, one with the PO reference buried in a footnote) → render each to
PDF with puppeteer. For the **scanned-invoice edge case**, screenshot the
rendered page with puppeteer and embed the PNG into a text-free PDF with
`pdf-lib` — a genuine image-only PDF with no text layer, no scanner needed.

Also from this project: `nodemailer` (if we want an "email the vendor / notify
AP" step), `multer` (file upload), `joi` (validation), JWT auth.

### 2.3 `D:\Laalgudi_2` — Lalgudi 2026 election voters app

Vite + React 18 + `react-router-dom` + `@supabase/supabase-js` + `leaflet`.
Relevant takeaway: you've shipped a React SPA over hosted Postgres with real
row volumes and real filtering/search UX. Supabase is the fallback if we
outgrow SQLite (we won't — this is a demo with tens of rows). The voter-list
filtering/table UX is the closest thing you've built to the dashboard needed
here.

### 2.4 `D:\AI Agent` — langchain / openai / anthropic / duckduckgo-search

FastAPI agent + tools pattern across `travel-agent`, `stock-market`,
`hospital-management`, each with its own `backend` + `frontend`. Relevant as
prior art for multi-tool orchestration, but **deliberately not used here** —
LangChain adds a dependency and an abstraction layer for zero gain on a
7-stage deterministic pipeline with one LLM call. Worth a sentence in the
video: *"I considered an agent framework and chose not to — here's why."*

### 2.5 `D:\BolnaAI` — the interview-prep playbook

Not code. It's your own prep structure (`00-PLAYBOOK.md`,
`04-test-scenarios.md`, `05-submission-form.md`, `INTERVIEW-PREP.md`). Reuse
that structure for the demo-run script (§8) — rehearsing the live demo is
explicitly part of the grade.

### 2.6 Honest gaps

- **No prior PDF-*parsing* code.** Only PDF *generation* (Loyalty). Mitigated
  by sending PDFs natively to Claude (§3) — no OCR stack to install.
- **No SSE / streaming-progress code.** Closest is the AssemblyAI polling loop
  in `Zetta/frontend/app.js` (~line 593). New work, but small.
- **No fuzzy-matching code.** Needed for vendor-name and PO matching. Use
  `rapidfuzz` — a 10-line helper.

---

## 3. Stack decision

| Layer | Choice | Why |
|---|---|---|
| Backend | **FastAPI + Uvicorn** | lift Zetta wholesale |
| DB | **SQLite** | zero setup, one file, trivially seedable; data volume is tiny |
| Frontend | **Vanilla JS + Tailwind CDN** (Zetta pattern) | no build step → single container → one deploy; the 933-line `app.js` already has the table / detail / upload plumbing |
| Extraction LLM | **Claude (`claude-opus-5`)** via the `anthropic` Python SDK | native PDF input — see below |
| PDF text-layer probe | **`pypdf`** | one call to decide "clean PDF" vs "scanned", for display + routing |
| Fuzzy match | **`rapidfuzz`** | vendor + PO reference matching |
| Retry | **`tenacity`** | already in the Zetta requirements |
| PDF test data | **puppeteer + pdf-lib** (Node, from Loyalty) | one-off generator script, not part of the running app |
| Deploy | **Render free tier via `render.yaml`** | already proven in Zetta |

### Why Claude over Gemini here

Your Gemini code works and you have a key, so Gemini is the safe default — but
Claude has two features that map *exactly* onto what this brief grades:

1. **Native PDF input.** Pass the PDF straight through as a base64 `document`
   content block (no beta header; limits 32 MB / 600 pages). The model reads
   both the text layer *and* the page images. **This means one code path
   handles clean PDFs and scanned images alike — no Tesseract, no poppler, no
   pdf2image, nothing to install in the container.** (Note: this machine
   doesn't even have poppler installed — `pdftoppm` is missing. Avoiding an
   OCR stack is a real win, not a theoretical one.)
2. **Citations.** Set `citations: {enabled: true}` on the document block and
   every extracted value comes back with a `page_location` pointing at the
   exact page plus the `cited_text` it was read from. The brief asks for a
   decision "with everything that happened in between visible" — field-level
   provenance is the strongest possible answer to that. In the UI: hover any
   extracted field → see the snippet and page it came from. This is the detail
   that will get remembered in the debrief.

Structured output: `output_config: {format: {...}}` (or `messages.parse()`)
against a strict schema so the model cannot drift on enum values — same
discipline as `Zetta/backend/prompts.py`.

Pricing, for the Admin tab: Opus 5 `$5 / $25` per MTok in/out; Sonnet 5
`$2 / $10`; Haiku 4.5 `$1 / $5`. A whole week of development is low
single-digit dollars. Keep the model in an env var so it's swappable live.

Keep the Gemini path behind the same interface as a documented fallback — it
costs ~20 lines and it's a good answer to "what if the API is down in the
interview."

---

## 4. The process — 7 stages

Each stage writes a row to `run_stages` with status, duration, and a payload.
That table *is* the live run view and the audit trail.

```
  PDF in
    │
 1. INTAKE            hash file, store blob, probe text layer (pypdf),
    │                 check hash against previous runs
 2. EXTRACT           one Claude call: PDF → strict JSON
    │                 (vendor, invoice no, date, currency, line items,
    │                  tax, total, PO ref) + per-field citations
 3. NORMALIZE         dates → ISO, amounts → Decimal, currency codes,
    │                 vendor name canonicalised
 4. RESOLVE VENDOR    fuzzy match against approved vendor master
    │                 → vendor_id + confidence, or UNKNOWN_VENDOR
 5. MATCH PO          candidate POs by vendor + ref + amount;
    │                 score each; pick best, or escalate if ambiguous
 6. VALIDATE          run every rule; each emits
    │                 {rule_id, passed, expected, actual, severity}
 7. DECIDE            worst severity wins → APPROVE / FLAG / REJECT
    │                 + plain-English reasons + confidence
  decision out
```

### The rule set (deterministic, defined once as data, served at `/api/rules`)

| id | Rule | Severity if failed |
|---|---|---|
| `R1` | Required fields present (invoice no, date, total, vendor) | REJECT if total/vendor missing, else FLAG |
| `R2` | Line items + tax = stated total (±₹1 rounding) | FLAG — arithmetic mismatch |
| `R3` | Vendor is on the approved list | REJECT |
| `R4` | A PO was matched with confidence ≥ 0.8 | FLAG — needs human PO assignment |
| `R5` | Invoice total within PO tolerance (default **2%**) | FLAG ≤10% over, REJECT >10% |
| `R6` | Cumulative billed against this PO ≤ PO value | REJECT — PO over-consumed |
| `R7` | Not a duplicate (exact hash, or same vendor + amount within 30 days) | FLAG (near) / REJECT (exact) |
| `R8` | Invoice date not in the future, not >180 days stale | FLAG |
| `R9` | Extraction confidence above floor (scanned / low-quality) | FLAG — mandatory human review |

Tolerances, the approved-vendor list, and the duplicate window all live in one
config object so you can change `2%` to `5%` live in the interview and re-run
to show the decision flip. **Rehearse that** — it's a 15-second move that
proves the rules are real and not hardcoded output.

---

## 5. The UI (explicitly graded — don't skimp)

Three tabs, Zetta's shell, Tailwind.

**Tab 1 — Run.** Drop zone for the PDF. On submit, a vertical stage timeline:
7 rows, each going `pending → running (spinner) → done (✓ + duration)` or
`→ failed`. Each row expands to show its payload — stage 2 shows extracted
fields with hover-to-see-citation, stage 5 shows candidate POs with match
scores, stage 6 shows every rule as a pass/fail line with expected vs actual.
Ends in a decision banner: colour-coded verdict, confidence, numbered reasons.

**Implementation:** SSE. `GET /api/runs/{id}/events` returns a FastAPI
`StreamingResponse` with `media_type="text/event-stream"`; the pipeline pushes
one event per stage transition. Client-side `EventSource`, ~40 lines. (Polling
every 400 ms is the fallback if SSE misbehaves behind Render's proxy — you
already have the polling pattern in `Zetta/frontend/app.js`.) Make each stage
*visibly* take a moment — if the whole thing finishes in 900 ms the run view
is pointless on video.

**Tab 2 — Dashboard.** Table of all past runs: timestamp, filename, vendor,
invoice no, amount, matched PO, decision badge, confidence, duration. Sortable
and filterable by decision. Click a row → the same stage detail, replayed from
the DB. Top strip of stat tiles: total runs, auto-approved %, flagged,
rejected, avg handling time, estimated hours saved. That last tile is the
number a non-technical buyer actually cares about.

**Tab 3 — Admin.** PO master + approved vendors (read-only table), the live
rule set with current thresholds, and LLM telemetry (calls, tokens, cost,
p50/p95, cache hits) — straight from Zetta's S4.

---

## 6. Edge cases — build 4 of these 5

The brief says don't pick trivial ones, and that the choice itself is graded.
Each needs a purpose-built input PDF and must produce a *different decision
path*, not just "didn't crash."

1. **Split invoice against one PO.** Vendor bills ₹6L against a ₹10L PO,
   having already billed ₹5L on a prior invoice. Each invoice alone looks fine
   against the PO; together they exceed it. Only `R6` (cumulative consumption)
   catches it. → REJECT, *"PO-4471 has ₹5,00,000 already billed; this invoice
   would take consumption to ₹11,00,000 against a ₹10,00,000 PO."*
   **Why it's good:** it proves the system has state, not just per-document
   logic.

2. **Near-duplicate resubmission.** Same vendor, same amount, same date, same
   line items — different invoice number (vendor re-issued it). Exact-hash
   dedupe misses it; the fuzzy rule catches it. → FLAG with a link to the
   original run. **Why it's good:** duplicate payment is the single most
   expensive AP failure mode, and it's the case where naive hashing fails.

3. **Scanned image-only PDF.** No text layer at all. Stage 1 detects it and
   labels the run `scanned`, stage 2 goes through the vision path, extraction
   confidence drops, `R9` fires. → FLAG for mandatory human review *even
   though every other check passed.* **Why it's good:** the correct behaviour
   is to be less confident, not to fail — and it shows deliberate confidence
   handling.

4. **Missing / ambiguous PO reference.** No PO number anywhere on the invoice.
   Stage 5 falls back to vendor + amount + date-window matching, surfaces 2–3
   candidate POs with scores, none clears 0.8. → FLAG with ranked candidates
   for a human to pick. **Why it's good:** it shows the system escalating with
   useful work already done, instead of guessing or giving up.

5. **Tolerance boundary pair.** Two invoices against the same PO: one 1.8%
   over (within the 2% tolerance → APPROVE, with the variance stated) and one
   7.5% over (→ FLAG). Run both back to back. **Why it's good:** it
   demonstrates the rule set is a real threshold engine, and sets up the live
   change-the-tolerance-and-re-run move.

Pick 1, 2, 3, 4. Keep 5 in the back pocket for the live interview — it's the
cheapest one to run and the most convincing.

---

## 7. Day-by-day

| Day | Work | Done when |
|---|---|---|
| **1** | Email coordinator: subject `ASA Case Study — [Your Name] — PS-1`. Scaffold `D:\ZampAI` from Zetta (copy config / db / main / Dockerfile / render.yaml). Write this plan's stage contract as stub functions returning fake data. Deploy the empty shell to Render. | A live URL exists on day 1 |
| **2** | Test data: 5 HTML invoice templates → puppeteer → PDFs; 1 image-only PDF; PO master CSV (~12 POs) + approved-vendor list. Seed the DB. | 8 PDFs + seeded DB |
| **3** | Stages 1–3 for real: intake, hash, text-layer probe, Claude extraction with strict schema + citations, normalisation. Verify extraction on all 5 layouts. | `/api/runs` returns real extracted JSON |
| **4** | Stages 4–7: vendor resolve, PO match scoring, the 9 rules, decision + reasons. Happy path end-to-end. | One clean invoice → APPROVE with reasons |
| **5** | Frontend: run view with SSE stage timeline, decision banner, citation hovers. Dashboard table + stat tiles. | Happy path is watchable |
| **6** | Edge cases 1–4, each verified. Admin tab. `DECISIONS.md` + `AI_USAGE.md` + `README.md`. Redeploy. Rehearse the run order twice. | All 5 scenarios pass on the deployed URL |
| **7** | Record the video (expect 3–4 takes). Submit both links. | Sent |

Hard rule from the brief: a process that runs beats a process that's 80%
built. If day 6 is tight, cut edge case 4 and the Admin tab — never cut the
run view or the dashboard.

---

## 8. The 5-minute video script

| Time | Content |
|---|---|
| 0:00–0:30 | The problem, in a buyer's words — AP clerk, hundreds of PDFs, spreadsheet lookups, expensive Friday-afternoon mistakes |
| 0:30–2:00 | **Happy path.** Drop a clean invoice. Narrate each stage as it lights up. Stop on the extraction stage and hover a citation: *"this total came from page 1, here's the exact text."* Land on APPROVE and read the reasons |
| 2:00–3:30 | **Edge case 1 (split PO)**, then **edge case 2 (near-duplicate).** Say what the naive version would have done and why this one doesn't |
| 3:30–4:15 | **Edge case 3 (scanned).** Make the point that it flags *because* confidence dropped, not because anything broke |
| 4:15–4:45 | Dashboard. The stat tiles. *"Across 12 runs, 7 auto-approved, 4 flagged, 1 rejected — that's the 7 a human never has to open"* |
| 4:45–5:00 | One design decision, stated plainly: **the model extracts, the code decides** — because a finance team needs the same input to produce the same decision every time, and to be told why |

Record on the deployed URL, not localhost. Your own voice, no slides, no
editing.

---

## 9. Assumptions to state out loud (the brief rewards this)

- Invoices arrive as a file upload rather than by live email polling. Email
  intake is an IMAP listener in front of stage 1 — out of scope for a week,
  and it adds demo fragility for zero logic gain.
- The PO master and approved-vendor list stand in for a procurement system /
  ERP. The interface is a repository function, so swapping SQLite for a real
  ERP API touches one module.
- Currency is INR throughout; multi-currency would need an FX table and a
  rate-date rule.
- Three-way match (invoice ↔ PO ↔ goods-receipt note) is the real-world
  standard; this is a two-way match. GRN is the obvious next build, and saying
  so shows you know the domain.
- Tolerance (2%), duplicate window (30 days), and confidence floor are set to
  plausible mid-market values, not derived from a real policy.

---

## 10. First command

Scaffold from Zetta, keep the pieces that transfer, delete the call-specific
logic:

```bash
# from D:\ZampAI
cp -r /d/Zetta/backend ./backend
cp /d/Zetta/Dockerfile /d/Zetta/docker-compose.yml /d/Zetta/render.yaml .
cp -r /d/Zetta/frontend ./frontend
# then: strip transcript / telecaller logic, keep config, db, telemetry,
#       cache, retry, tab shell, sortable table, detail drawer
```

`requirements.txt` for PS-1:

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.2
python-dotenv==1.0.1
anthropic
pypdf
rapidfuzz
tenacity==9.0.0
python-multipart
```
