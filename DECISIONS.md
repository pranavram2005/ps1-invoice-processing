# Design decisions

The judgment calls behind this build, each with what it cost and what the
alternative would have been. Ordered roughly by how much they shaped the
system.

---

## 1. The model extracts, the code decides

**Decision.** Exactly one step in the pipeline consults a model: stage 2 turns
a PDF into structured fields. Every subsequent step — normalisation, vendor
resolution, PO matching, all nine rules, the verdict — is deterministic Python
over published thresholds.

**Why.** The two halves of this problem have opposite requirements. Reading a
vendor's invoice is genuinely fuzzy: layouts vary, the PO reference hides in a
footnote, half the amounts are formatted three different ways. That needs a
model. Deciding whether to pay has the opposite requirement — the same input
must produce the same decision every time, a finance team must be able to be
told *why*, and the answer has to survive an audit. A model is the wrong tool
for that, not because it would often be wrong, but because "often right" is not
the standard and "because the model said so" is not a reason.

**What it buys.** The decision is reproducible by construction. The rules are
readable by a non-programmer. A threshold change provably changes outcomes
(`tools/check_threshold_flip.py`). And when something is wrong, it is wrong in
one identifiable place.

**What it costs.** Rules cannot handle a case nobody anticipated; they escalate
instead. That is the right failure mode for accounts payable, but it does mean
the flag rate is a function of how well the rule set covers reality.

**The alternative.** Hand the model the invoice, the PO list and the policy and
ask for a verdict. It would work on the happy path, be far less code, and be
indefensible the first time a vendor asked why an invoice was rejected.

---

## 2. Provenance: a schema field checked in code, not API citations

**Decision.** The extraction schema requires the model to report, for each
field, the page and the verbatim text it read the value from. The code then
**verifies** each quote against the PDF's own text layer with a fuzzy match and
shows the result per field.

**Why not the API's `citations` feature.** It was the first choice, and the plan
for this build specified it: set `citations: {enabled: true}` on the document
block and every value comes back with a `page_location` and the `cited_text`.
Server-side, no prompt engineering.

It cannot be used here. **Citations are incompatible with structured outputs** —
enabling both on the same request returns a 400. The choice is therefore between
a schema guarantee and server-formatted citations, and on a financial extraction
the schema guarantee wins easily: a malformed field is a pipeline failure, while
provenance is a presentation and audit feature. Mapping a citation back to a
*specific field* also requires reasoning about which text block produced which
fragment of the JSON, which is fragile.

**The unexpected payoff.** Self-reported provenance would, on its own, just be a
second thing the model asserts. Verifying the quotes against the extracted text
turns an assertion into evidence — and it made the confidence number
decomposable:

```
confidence = ½ · model's own confidence
           + ½ · fraction of decision-critical fields whose quote was found
```

Then the scanned-document case falls out of the same mechanism for free. No text
layer means nothing can be verified, which means a documented penalty, which
means R9 flags it. A behaviour I would otherwise have had to special-case became
a consequence of a check that already existed.

**What it costs.** The quotes are only as good as the model's willingness to
copy text verbatim, and PDF text extraction mangles whitespace and glyphs — so
the match is fuzzy at a 0.88 threshold, not exact. A correct value with a
sloppily-quoted source shows as unverified and pulls confidence down. That is
the conservative direction, but it is a real false-negative.

---

## 3. Native PDF input, no OCR stack

**Decision.** The PDF goes to the API as a base64 `document` block. Claude reads
the text layer and the page images. One code path handles a clean digital PDF
and an image-only scan.

**Why.** The alternative is Tesseract plus poppler plus pdf2image, a branch for
"does this have text", and a container that is an order of magnitude larger. The
build machine does not even have poppler installed — `pdftoppm` is missing — so
avoiding an OCR stack was a practical constraint, not a preference.

**What it costs.** Scanned pages are read as images with no local fallback, so
there is no offline degradation path. And pypdf is still a dependency: it is
needed to probe for a text layer and to verify provenance quotes, so this
removes OCR, not PDF parsing entirely.

---

## 4. R6 skips when the PO has no prior billing

**Decision.** The cumulative-consumption rule reports "not applicable" when
nothing has yet been billed against the matched PO.

**Why.** This was a bug found while building the fixtures. R5 judges this
invoice's variance against the PO (FLAG up to 10% over, REJECT beyond). R6
judges everything billed against the PO including this invoice. With no prior
billing the two compare the *same number* to the same ceiling — but R6's
severity is REJECT. The result was that **R5's entire FLAG band was
unreachable**: any invoice over tolerance was rejected by R6 before R5's
gentler verdict could stand.

The fix is also the clearer statement of intent. R6 exists to catch what
per-document logic cannot see. With no prior billing there is nothing
cross-document to see, so it has nothing to say and says so.

**How it shows up.** The split-invoice case (₹6L against a ₹10L PO already
carrying ₹5L) is rejected by R6 alone. The 7.5%-over case is flagged by R5
alone. Before the fix, both were rejections and the demo would have claimed a
tolerance band the code did not actually have.

---

## 5. An exact PO reference settles identity on its own

**Decision.** If the invoice names a PO and that PO exists, the match scores
1.0. The amount and date are computed and displayed, but they are not blended
into the score.

**Why.** This was also a bug, and the dashboard surfaced it: re-running the
happy-path invoice produced an `R4` failure — "no PO matched with confidence" —
for an invoice that names its PO explicitly and correctly.

The cause was scoring the amount against the PO's *remaining* balance. Once the
first invoice had been approved and consumed ₹2.41L of a ₹2.5L PO, a second
invoice for the same amount looked like a terrible fit, dragging the match score
below the floor.

The deeper error was conflating two questions. "Which PO is this?" is stage 5's
job. "Is this amount acceptable against that PO?" is R5's and R6's, and they
answer it explicitly, with the right severity, and say so in the audit trail.
Letting the amount veto the identity meant a legitimate second invoice was
reported as an unidentifiable one — the wrong diagnosis entirely.

**What it costs.** A vendor who quotes a valid PO number on an unrelated invoice
gets a confident match. The amount and date rules then have to catch it, which
they do, and the candidate card still shows the poor amount fit.

---

## 6. An inferred PO can never clear the match floor

**Decision.** When the invoice names no PO, the system still ranks candidates by
vendor, amount and date — but the score is capped at **0.78**, below the 0.80
match floor. An inferred PO therefore always fails R4 and always reaches a
human.

**Why.** Guessing which PO to bill against is precisely the decision an AP
system should not make silently. But refusing to try is also wrong: the useful
behaviour is to escalate *with the work already done*, so the human gets a
ranked shortlist with scores and reasons rather than an empty field.

The cap makes that policy explicit and auditable rather than an emergent
property of some weights. It is in the code as a named constant with the
reasoning attached.

**What it costs.** A cap is a blunt instrument. A genuinely unambiguous
inference — one open PO, exact amount, inside the window — still goes to a
human. That is the intended trade, but it is a trade.

---

## 7. SQLite, and `run_stages` is the audit trail *and* the UI

**Decision.** One SQLite file. The pipeline writes a row per stage transition as
it happens, and that table is simultaneously what the browser streams live, what
the dashboard replays, and what an auditor reads.

**Why.** The alternative — logging for humans and separately persisting results
for the UI — guarantees the two drift. Making the audit record the only source
means what you watched happen and what you can prove happened are the same rows.
Data volume here is tens of rows; Postgres would be ceremony.

**What it costs.** Single-writer. The SSE broker is in-process, so a
multi-replica deployment needs a shared bus. Both are fine at this scale and
neither is hidden.

---

## 8. Server-sent events, not polling

**Decision.** `GET /api/runs/{id}/events` streams stage transitions; the client
is ~40 lines of `EventSource`.

**Why.** The run view is explicitly graded and polling would make it feel like
what it is — a UI asking "are we there yet" four times a second.

**The parts that actually mattered** were not the streaming:

- The stream **subscribes before reading the snapshot**, so an event firing in
  the gap is queued rather than lost.
- A **late subscriber gets a full replay** of current stage states before live
  events. Reloading the page mid-run shows the real state, not an empty
  timeline. Asserted in `tools/check_sse.py`.
- The run row and its seven pending stages are created **synchronously in the
  request handler** before the work is scheduled. Without that the client races
  the background task: a new run 404s and a replay briefly reads as the previous
  completed attempt. That second failure was real and is covered in §9.
- A 15-second keepalive comment keeps proxies from closing an idle stream, and
  `X-Accel-Buffering: no` stops nginx-style buffering from delivering all seven
  events at once at the end.

**Fallback.** Polling `GET /api/runs/{id}` every 400 ms is equivalent if SSE
misbehaves behind a proxy; the endpoint already returns the full stage list.

---

## 9. A replay clears its own derived state

**Decision.** Re-running an existing run under the same id wipes the stored
result blob and every derived column first, and the row — not the stored blob —
is authoritative for status.

**Why.** Found while testing the live-threshold demo, which appeared not to
work: change the tolerance, replay, and the decision stubbornly did not move.
The config was being applied correctly the whole time. `create_run` reset the
row's status to `running` but left the previous `result_json` in place, and
`get_run` rebuilt the response from that blob — including its embedded
`"status": "complete"`. The API served the *old* decision for the entire
duration of the replay.

A test caught it because it polled until status was not `running` and got a
stale answer instantly. In the browser it would have looked like a threshold
change having no effect — which is precisely the claim the demo is built to
prove, so it would have discredited the one move that most needed to land.

**Also.** A replay reverses any PO consumption its previous attempt booked, so
re-running an approved invoice does not double-count against the PO.

---

## 10. The extraction cache and the duplicate rule draw opposite conclusions

**Decision.** Extraction is cached by SHA-256 of the file, so the same bytes are
never read twice. Rule R7 independently treats a resubmission of those same
bytes as a duplicate and rejects it.

**Why this is not a contradiction.** Reusing the extraction is an efficiency —
the same document always contains the same fields. Recognising the resubmission
is a control — the same invoice should not be paid twice. Both conclusions are
correct, drawn from the same fact, in different layers. The UI says so on the
cached stage, because it reads like a contradiction otherwise.

**A consequence worth knowing before a demo.** Dropping the same PDF twice
yields APPROVE then REJECT, which is correct. To show the same invoice again
without that, use **Re-run** on the existing row — a replay, not a resubmission.

---

## 11. No agent framework

**Decision.** No LangChain, no agent loop, no tool-calling orchestration. A
seven-step function.

**Why.** The pipeline is a fixed, ordered sequence with one model call in it.
An agent framework would add a dependency, an abstraction layer and a source of
non-determinism to a process whose defining property is that it is
deterministic. There is nothing for an agent to decide: the steps do not branch
on model output, they branch on rule results.

**Where an agent would earn its place:** resolving the flags this system
produces — chasing a missing PO, emailing a vendor about a mismatch,
negotiating a correction. That is open-ended work with a human in the loop. The
decision itself is not.

---

## 12. No CDN, no build step

**Decision.** Hand-written CSS and vanilla JS. The plan called for Tailwind via
CDN, following a previous project.

**Why the change.** A graded live demo should have zero external dependencies at
render time. A CDN is one more thing that can be slow, blocked by a corporate
network, or version-bumped between rehearsal and recording. Hand-written CSS is
~700 lines, loads instantly, and cannot fail independently of the app.

**What it costs.** More CSS to write and maintain than utility classes would
need. At one application's scale that is a fine trade; at ten it would not be.

---

## 13. An offline mode that is honest about what it proves

**Decision.** `EXTRACTION_MODE=offline` serves stage 2 from ground-truth
fixtures. Runs that use it are badged in the UI, excluded from cost and latency
telemetry, and fail loudly on any PDF without a fixture.

**Why it exists.** Partly insurance — "what if the API is down during the
interview" has a real answer now (`auto` mode falls back automatically). Mostly
testing: it isolates the deterministic half of the system from model variance,
so when a rule test fails it failed because of the rule.

**The honesty requirement.** A fixture run proves nothing about whether the
model can read an invoice. If the UI let an offline run look like a live one,
the dashboard would be quietly lying about the thing the system most needs to be
trusted on. Hence the badge, the exclusion from telemetry, and the loud failure
on unknown files.

**The reason it was built now:** the API key available during this build
returned `credit balance is too low`, so no live extraction call has ever
succeeded here. Rather than stop, everything downstream was built and verified
against fixtures. The live path is written against the documented API and
remains the one genuinely unverified part of the system — stated in the README
under Known limits rather than left to be discovered.

---

## 14. Decisions get no confidence score

**Decision.** The number shown next to the verdict is **extraction
confidence** — how sure the system is that it read the document correctly. The
decision itself carries no probability.

**Why.** It would be easy, and wrong, to print "87% confident this should be
rejected". Given the extracted fields, the decision is deterministic: the same
fields always produce the same verdict. Attaching a probability to it would
imply a model is weighing the outcome, which is the exact impression this design
exists to avoid.

The only genuine uncertainty is in reading the document — so that is the only
thing with a number on it, and the UI prints the arithmetic behind it.
