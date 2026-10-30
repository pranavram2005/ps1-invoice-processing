# Demo script

A run order for the ≤5 minute video, and the live-interview moves worth having
ready. Record against the deployed URL, not localhost.

---

## Before recording

```bash
# 1. Clean slate: masters restored, run history wiped, all nine reprocessed
python -m backend.seed --runs --clear

# 2. Confirm every scenario still lands where it should
python tools/check_scenarios.py

# 3. Confirm the threshold move works (this is the one that must not fail)
python tools/check_threshold_flip.py
```

Then, in the Admin tab, **Reset everything, including run history** so the
dashboard starts empty and fills up on camera.

Check the env strip reads `extraction live`. If it reads `offline`, the
extraction step is coming from fixtures and the demo should say so.

`STAGE_DELAY_MS` defaults to 350 ms, which makes the run view watchable. If the
stages still go by too fast on camera, raise it to 600.

---

## The 5-minute run

| Time | What happens | What to say |
|---|---|---|
| 0:00–0:30 | Title screen / the Run tab, empty | The problem in a buyer's words: an AP clerk with hundreds of PDFs a week, a spreadsheet of POs, and an expensive Friday-afternoon mistake waiting to happen. |
| 0:30–2:00 | **Scenario 1 — clean invoice.** Narrate the stages as they light up. Stop on stage 2, hover a provenance marker. Expand stage 6. Land on APPROVE. | "The model reads the document. It doesn't decide anything." On the hover: *"this total came from page 1, and here's the exact text it was read from — and the system checked that text is really in the file."* On stage 6: nine checks, each with expected versus actual. |
| 2:00–2:50 | **Scenario 2 — split invoice against one PO.** Land on REJECT, read the R6 reason. | "Six lakh against a ten lakh PO. Perfectly reasonable — except five lakh was already billed against it in September. No per-document check can see that. This one has state." |
| 2:50–3:30 | **Scenario 3, then 4 — the Nalanda pair.** Original approves. Re-issue flags on R7, linked to the original. | "Same vendor, same amount, same date, different invoice number. A hash check sees two different files. Duplicate payment is the single most expensive thing that goes wrong in accounts payable." |
| 3:30–4:10 | **Scenario 5 — the scanned PDF.** Point at stage 1 (`no text layer`), stage 3 (nothing verifiable), stage 6 (R9 fires). | "Nothing broke. Every other check passed. It flagged *because confidence dropped* — there's no text in this file to check anything against. The right answer on a worse input is to be less certain, not to fail." |
| 4:10–4:40 | **Dashboard.** The stat tiles, then the table. | "Across these runs: N auto-approved, N flagged, N rejected. The approved ones are invoices a human never had to open." |
| 4:40–5:00 | Back to the decision banner, or a still of the Admin rule list | The one design decision, stated plainly: **the model extracts, the code decides** — because a finance team needs the same input to produce the same decision every time, and needs to be told why. |

**If you are over time**, cut scenario 4 (missing PO) — it is already not in the
list above — and shorten the dashboard beat. Never cut the stage timeline or the
provenance hover.

---

## Live interview moves

Each of these is 15–30 seconds and each proves something a recording cannot.

### 1. The threshold flip — the strongest one

1. Run **Tolerance boundary: 7.5% over**. It FLAGs on R5.
2. Admin → Thresholds → change `po_tolerance_pct` from `2` to `10` → **Save**.
3. Back to the run → **Re-run**.
4. It now APPROVEs, and R5's "expected" line reads `within 10.0%`.
5. Put it back to `2` and re-run to show it flip back.

Say: *"the rules aren't hardcoded output — they're configuration, and that's the
same invoice and the same code."*

### 2. The duplicate, live

Drop a PDF that has already been processed. It REJECTs on R7 with a link to the
original run, and stage 2 shows `extraction cached — zero LLM calls`.

Worth explaining, because it reads like a contradiction: the cache reuses the
*extraction* because the same bytes contain the same fields; R7 rejects the
*submission* because the same invoice shouldn't be paid twice. Same fact, two
layers, both right.

### 3. Upload something unexpected

Drop any invoice-like PDF you have. If it is from an unknown vendor it REJECTs
on R3, which is the correct and boring answer — and a good moment to point out
that the system escalates rather than guesses.

### 4. "What if the API is down?"

`EXTRACTION_MODE=auto` falls back to bundled fixtures if the call fails, and the
UI badges the run `offline fixture`. Point at the badge: the fallback never
pretends to be a live read.

---

## Questions worth having an answer ready for

**"Why not just ask the model for the decision?"**
Because "often right" isn't the standard for payments, and "the model said so"
isn't a reason you can give a vendor or an auditor. Same input, same decision,
every time — and the reason is readable by a non-programmer.
([DECISIONS.md §1](DECISIONS.md#1-the-model-extracts-the-code-decides))

**"How do you know the extraction is right?"**
Partly you don't, which is why confidence is decomposed rather than asserted:
half the model's own confidence, half the fraction of decision-critical fields
whose quoted source text was actually found in the document. Below 0.75 it goes
to a human regardless of everything else.

**"What breaks first at volume?"**
The in-process SSE broker and single-writer SQLite — both fine at this scale,
both named in the README. The rule engine itself is stateless apart from the PO
ledger.

**"What's missing for production?"**
Three-way matching against goods-receipt notes, email intake, a real ERP behind
the repository functions, and a human review queue — the flag has to go
*somewhere*. The first and last are the ones a finance team would ask for first.

**"What's the honest weak spot?"**
The live extraction path is the least-tested part of the system: the key
available during the build had no credit, so everything downstream was verified
against fixtures instead. It's written against the documented API and it's one
command to check with a funded key — but it hasn't been, and the README says so.
