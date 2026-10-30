# AI usage

How AI was used to build this, what it got wrong, and what it could not do.

> This document describes the construction of the project. For how the running
> system uses a model — one call, schema-constrained, with everything after it
> deterministic — see [DECISIONS.md](DECISIONS.md) §1.

---

## Summary

The project was built with **Claude Code** (Claude Opus 5) as the primary
implementation tool, working from a human-written build plan (`PLAN.md`) that
fixed the architecture, the stage breakdown, the rule set and the edge cases
before any code existed.

The split in practice:

| Human | AI |
|---|---|
| The problem framing and the architecture: one LLM call, deterministic rules, seven stages, nine rules | Implementing all of it |
| Choosing which edge cases are worth building, and why | Engineering the fixtures so each one lands on a different rule |
| Deciding what the demo must prove | Building the live threshold-change path and the test that asserts it |
| Judging when AI output was wrong | Finding and fixing four real bugs once pointed at the evidence |

The architecture survived implementation essentially intact. The specifics did
not, and the useful parts of this document are where they changed.

---

## Where AI was clearly worth it

**Volume with consistency.** Eleven backend modules, a three-tab frontend, nine
invoice templates in five distinct layouts, and eight verification tools, in one
sitting, with consistent naming and error handling throughout. The nine sample
invoices are the clearest case: hand-writing five HTML layouts and
reverse-engineering amounts so each one trips exactly one intended rule is hours
of fiddly arithmetic, and it was done in minutes and checked automatically.

**Tests nobody would have written by hand under time pressure.** The scenario
checker asserts not just the decision but the *exact set of failing rules*, so a
regression that reaches the right answer for the wrong reason still fails. The
UI check drives a real browser, screenshots every tab, and fails on any console
error or horizontal overflow. Both exist because they were cheap, and both
caught real problems.

**Documentation written while the reasoning was fresh**, including the failures
— which is the half most likely to be lost.

---

## Where AI was wrong

Four bugs reached working code. All four were found by verification rather than
by review, which is the point: the mitigation for confident-but-wrong output is
to make the code prove itself, not to read it harder.

**1. A rule that silently disabled another rule.**
R5 flags an invoice over PO tolerance and rejects it past 10%. R6 rejects
cumulative over-consumption. With no prior billing on a PO, both compared the
same number to the same ceiling — but R6's severity is REJECT, so **R5's entire
FLAG band was unreachable**. The documented tolerance behaviour did not exist.
Found by constructing a fixture that was supposed to land in that band and
noticing it rejected instead. ([DECISIONS.md §4](DECISIONS.md#4-r6-skips-when-the-po-has-no-prior-billing))

**2. A PO matcher that penalised correct matches.**
The match score blended the invoice amount against the PO's *remaining*
balance. Once an approved invoice had consumed most of a PO, a legitimate
follow-up invoice that named that PO explicitly scored below the match floor and
was reported as "no PO matched with confidence" — the wrong diagnosis entirely.
Found by *looking at the dashboard screenshot* and noticing a row that should
not have failed R4. ([DECISIONS.md §5](DECISIONS.md#5-an-exact-po-reference-settles-identity-on-its-own))

**3. A replay that served the previous answer.**
Re-running an invoice reset the run's status but left the stored result blob in
place, and the API rebuilt its response from that blob — including its embedded
status. For the entire duration of a replay the API returned the *old*
decision. This one mattered disproportionately: the symptom was "change a
threshold, re-run, nothing happens", which is exactly the claim the demo is
built to prove. ([DECISIONS.md §9](DECISIONS.md#9-a-replay-clears-its-own-derived-state))

**4. A race between the response and the work.**
`POST /replay` returned before the run row had been reset, so a client that
immediately fetched the run got the previous attempt. Fixed by creating the run
row and its seven pending stages synchronously in the request handler and
scheduling only the work.

A fifth, smaller one: the first attempt at provenance put a descriptive string
into a dict typed as `dict[str, float]`, which pydantic rejected at runtime.
Caught immediately by the first end-to-end run.

---

## Where AI's first answer was right but its source was stale

The build plan called for the Claude API's **`citations`** feature to supply
field-level provenance — a reasonable plan, and the obvious tool for the job.
Checking the current API documentation before writing the call showed that
citations are **incompatible with structured outputs**: enabling both returns a
400.

Had that gone unchecked, it would have been discovered at the first live API
call, late, with the whole provenance story built on it. Instead the design
changed before any code: provenance became a schema field, verified in code
against the PDF's own text layer — which turned out stronger than the original
plan, because a checked quote beats an asserted one, and because the scanned
edge case then fell out of the same mechanism for free.
([DECISIONS.md §2](DECISIONS.md#2-provenance-schema-field-checked-in-code-not-api-citations))

The general lesson: the failure mode was not hallucination, it was confident
recall of an API that had moved. Verifying against current documentation before
writing the call cost minutes and saved a rewrite.

---

## What AI could not do

**Verify the live extraction path.** The API key available during the build
returned `credit balance is too low`, so no real extraction call has ever
succeeded in this repository. Rather than stop, an offline fixture mode was
built so the other six stages, all nine rules, the SSE stream and the entire UI
could be verified — but a fixture run proves nothing about whether the model can
read an invoice. This is stated in the README under Known limits, badged in the
UI, and excluded from the telemetry, because the alternative is a dashboard that
quietly implies something it has not demonstrated.

**Decide what the system should refuse to do.** The two most defensible choices
in the design are both refusals: an inferred PO is capped below the match floor
so it can never auto-approve, and a decision is never given a confidence score
because it is deterministic and a probability would imply otherwise. Both are
judgments about where a finance team's trust should and should not be spent.
AI implemented them crisply once they were decided; it did not propose them.

**Judge which edge cases are worth building.** AI will happily generate twenty
scenarios. Choosing four that each prove something different — state across
documents, the limits of hash-based dedupe, graceful degradation on bad input,
escalating-with-work-done — is the part the brief actually grades, and it came
from the plan, not the model.

---

## How the work was actually organised

1. **Plan first, in prose.** `PLAN.md` fixed the architecture, the stages, the
   rules and the edge cases before implementation. Every later AI prompt was
   scoped by it, which is most of why the output stayed coherent across a
   codebase this size.
2. **Verify against current docs before writing API calls.** This caught the
   citations incompatibility.
3. **Build the checker with the thing.** `check_scenarios.py` was written
   alongside the fixtures, not after. Three of the four bugs were found by
   tooling, not by reading.
4. **Look at the output.** The PO matching bug was invisible in passing tests
   and obvious in a screenshot. Rendering the UI in a real browser and actually
   looking at the images was worth more than another round of code review.
5. **Write the reasoning down while it is fresh**, including what was wrong —
   which is this document and DECISIONS.md.

---

## Honest assessment

AI made this roughly a day's work instead of a week's, and the quality is higher
than a hand-built version would have been in the same time — mainly because the
verification tooling that caught three of the four bugs would not have survived
a time crunch otherwise.

It did not replace judgment. Every decision in DECISIONS.md worth defending in
an interview came from thinking about what an AP team needs to trust, not from
asking a model what to build. And it needed to be checked constantly: four real
bugs in working, plausible-looking code is the honest rate, and the only reason
they were all caught before the demo is that the project was built to prove
itself at every step rather than to look finished.
