# Raising a purchase order, and checking an invoice against it

A record of what was built on 2026-11-02, why it is shaped the way it is, and
what was verified before it was called done.

The system could already check an invoice against the twelve seeded purchase
orders. It could not check one against a purchase order you raised — because
there was no way to raise one, and because a PO with no invoice against it
decides nothing. This closes both halves of that loop:

1. **Raise a purchase order from the UI**, written to the same `pos` table the
   pipeline matches against, and kept visually separate from the seeded demo
   data everywhere it appears.
2. **Generate a real invoice against it** and put that invoice through the
   ordinary seven-stage pipeline, so the whole process is something you watch
   rather than take on trust.

The dashboard is now the landing page, and a four-diagram **How it works**
page explains the system end to end.

---

## What a user does

| Step | Where | What happens |
|---|---|---|
| 1 | **Purchase orders** → *Raise a purchase order* | Number, vendor, description, value, dates. The vendor is picked from the approved master, or added inline. |
| 2 | The row appears | Tagged **added by you**, under a new *Added by you* filter, counted in the budget panel. |
| 3 | *Generate it and run it* | Three variants, each stating the verdict it should earn **before** it runs. |
| 4 | Lands on **Check an invoice** | The live tracker, all seven stages, then the decision — with *Expected* beside *Decided*. |
| 5 | Back on the PO row | The ledger has moved: *Billed* and *Consumed* now include that invoice, and the row expands to the invoice that spent it. |

The three variants, and why three:

| Variant | Bills | Earns | Decided by |
|---|---|---|---|
| `match` | ~60% of the PO | **APPROVE** | nothing fails |
| `over` | ~7.5% over the PO | **FLAG** | `R5` — past the 2% tolerance, inside the 10% reject threshold |
| `reject` | ~35% over the PO | **REJECT** | `R5` — past the reject threshold |

One variant would only ever show the happy path. Three show the thing that
actually matters: the same document shape, three different amounts, three
different verdicts, each with the rule that produced it.

---

## What was added

### Backend

| File | Change |
|---|---|
| `backend/db.py` | `pos.source` (`seed`/`user`) and `pos.created_at`; a `MIGRATIONS` table and `_migrate()` run from `init_db`; `insert_po`, `delete_po`, `delete_runs_by_filename_prefix`. |
| `backend/invoice_pdf.py` | **New.** A pure-Python invoice PDF writer, and the three variant definitions. |
| `backend/main.py` | `POST /api/pos`, `DELETE /api/pos/{po}`, `POST /api/pos/{po}/test-invoice`, `GET /api/pos/variants`, `POST /api/vendors`; the `NewPO` and `NewVendor` validation models. |

### Frontend

| File | Change |
|---|---|
| `frontend/index.html` | Dashboard is the active panel; nav reordered with a **How it works** tab; the PO entry form and its inline new-vendor sub-form; the `Added by you` / `Demo data` filter chips; the expectation banner in the run view; the four-section How it works page. |
| `frontend/app.js` | `loadPoForm`, `createPo`, `runTestInvoice`, `deletePo`, `renderIntent`, `testInvoiceStrip`; the `MINE`/`SEED` PO filters; `attachRun` carries a declared expectation; `showTab('dashboard')` on load; FastAPI 422 field errors are now readable. |
| `frontend/uml.js` | **New.** The SVG vocabulary the diagrams are built from — a box, an arrow, an elbow, a hairline, a numbered marker, and a captioned figure. |
| `frontend/how.js` | **New.** The four figures, built from those primitives on first open. |
| `frontend/styles.css` | The entry form and vendor picker, the `added by you` marker in its three places, the generated-invoice buttons, the expectation banner, the run story, the rule cards, and the diagram vocabulary. |

### Tests

`tests/test_po_entry.py` — 34 tests: the PDF is a real one-page PDF, its text
layer carries every field the rules read, the figures reconcile to within a
rupee, each variant lands in the band that earns its verdict at four PO sizes,
two POs of the same value do not bill the same amount, the `NewPO` validators
refuse eight kinds of unpayable PO, every GSTIN the app itself ships passes
the check that guards new vendors, and the migration adds `source` to a `pos`
table that already exists and is a no-op the second time.

Total suite: **63 passing**.

---

## The five decisions worth not re-deriving

**1. The migration is not optional.** `CREATE TABLE IF NOT EXISTS` is a no-op
on a table that already exists, and this repository *ships* a populated
`data/invoices.db` which Render uses as-is. Without a real `ALTER TABLE` the
feature works locally and 500s in production. `db.MIGRATIONS` is a list of
`(table, column, ddl)` run on every startup, each guarded by a
`PRAGMA table_info` check, so it is idempotent and the next column is one line.

**2. The vendor is chosen, never typed.** A PO naming a supplier the system
does not know could never pay an invoice — `R3` rejects the vendor, `R4` finds
no PO. Free text would let someone create a purchase order that silently cannot
work. Blocked vendors are *listed but disabled*, for the same reason the
provider switch lists a provider with no key: hiding the option just raises the
question of where it went. Posting one anyway is refused with the reason.

**3. The generated PDF is written by hand, and has a real text layer.**
`reportlab` would have to go into the image for one page of output; the
Dockerfile is deliberately a thin Python container. So `invoice_pdf.py` emits
PDF 1.4 syntax over the base-14 Helvetica — catalog, pages, one content stream,
an xref table — in about 200 lines and with no new dependency. A rasterised
page would have demonstrated the *scanned* path instead, which the bundled
Kaveri sample already covers. And the figures are derived from the line items
upward rather than back-filled from a round total, because `R2` adds them up
again and an invoice that does not reconcile is a different scenario the repo
already has a sample for.

**4. The button clears its own history, and only its own.** A second generated
invoice would otherwise trip two rules correctly: `R7` sees a near duplicate
(same vendor, same amount, same day) and `R6` reads the consumption the
previous attempt booked. Both are correct readings of a history the button
created, and they would land on whichever variant was run second. So
`POST /api/pos/{po}/test-invoice` deletes runs matching
`test-invoice-<PO>-%` first — scoped by filename prefix, so an uploaded
invoice is never touched and a real duplicate is still caught. The same
reasoning, and the same precedent, as `delete_runs_by_hash` for the prepared
scenarios.

**5. Each invoice gets its own small, deterministic amount.** Two POs that hold
the same value, for the same vendor, billed on the same day at the same ratio
produce the same total — and `R7`'s near-duplicate tolerance is one rupee, so
the second one flags. Clearing *per PO* does not help, because the collision is
across POs. The fix is a nudge of `max(₹2, po_value × salt / 100,000)` where
`salt` is derived from the generation second and the PO number: proportional, so
a small PO cannot be pushed out of its band; floored at ₹2, so it always clears
the tolerance; salted by PO number, so two POs generated in the same second
still differ. It moves the billed fraction by at most a tenth of a percent, and
`tests/test_po_entry.py` asserts the bands hold from ₹1,000 to ₹1 crore.

This was found by the browser check, not by reasoning: two POs raised at
₹2,50,000 each, both billed at 60%, and the second run came back **FLAG** on
`R7` with the expectation banner reading *diverged*. The banner working as
designed is how the bug surfaced.

---

## Added after the first pass

Four things came out of looking at it in a browser rather than at the code:

- **The navbar wrapped onto two lines.** An absolutely-centred block is
  shrink-to-fit against the distance from its `left` to the edge of its
  containing block, so once six labels needed more than half the bar, every
  label wrapped. It is a flex track now, with the labels marked `nowrap`, the
  provider switch dropping below 1040px, and the links scrolling sideways below
  860px. One line at every width from 1512 down to 860.
- **The prepared invoices are cards again.** Each scenario has to carry a
  title, why it is worth running, the verdict it should earn and its own PDF
  download; the hairline list gave those four things one line to share.
- **A vendor can be added from inside the PO form.** A supplier who is not on
  the master is the ordinary reason a PO cannot be raised, and sending someone
  to another page to fix it loses the PO they were typing. `POST /api/vendors`
  adds an approved vendor with the next free id in the `V-2xxx` range, refusing
  a name or GSTIN that already resolves to an existing one — two vendors the
  matcher cannot tell apart is the one state this master must never be in.
- **The run view leads with the story.** The seven stages were a collapsed
  section at the bottom called "How this was processed". They are now the first
  thing under the tracker, always open, one step per row: what the step is for
  in a sentence that changes tense as the run reaches it, what it actually
  found on this invoice underneath, and everything it produced one click away.
  Watching the work is the point of the run view - a reviewer who cannot see
  what a system did has to take its verdict on trust.

And a second round, all of it about being understood rather than being correct:

- **The grey text was too light to read.** The ink scale came from a marketing
  site, where grey body copy under big type is fine; this is a finance tool
  read in long sittings, and at 11&ndash;12px the lighter two steps were not
  comfortably legible. All four steps are darker (`--ink-2` #454545 &rarr;
  #333, `--ink-3` #6e6e6e &rarr; #4e4e4e, `--ink-4` #9a9a9a &rarr; #6b6b6b),
  the hierarchy kept. One change, every page.
- **The rule set is nine cards.** A hairline list answered "what are the
  rules"; the question a reviewer arrives with is "what does this one stop?".
  Each card now leads with that in plain words &mdash; *"Is the vendor charging
  more than we agreed to?"* &mdash; then the formal title, the precise check,
  the record it is compared against (a link to that page), and the live
  threshold with what it means. The plain sentence and the source are served
  from `/api/rules` beside the rule itself, so a card cannot describe a rule
  the engine does not have.
- **How it works went from eight diagrams to four.** Documenting a system in
  every notation available teaches nobody anything. The four that survive
  answer the four questions somebody actually arrives with &mdash; what it
  decides, how it gets there, what it compares against, what happens when you
  raise a PO &mdash; and each is a shape readable at a glance: a flow, a list
  of steps, a hub, a numbered sequence. Diagram type is black and a point
  larger, strokes heavier. The section index moved out of the content column
  into a rail floating in the left margin.
- **The vendor dropdown is drawn, not inherited.** A native select brings its
  own font metrics, height and chevron, so next to three hand-styled fields it
  read as a control from a different form. `appearance: none`, an inline-SVG
  chevron, and every other value copied from `.field input`.
- **A back-to-top button**, which four pages tall enough to need one had been
  missing.

A third round, after looking at the pages rather than the code:

- **Everything grey became near-black.** The scale stepped down again
  (`--ink-2` #1c1c1c, `--ink-3` #2b2b2b, `--ink-4` #434343). The hierarchy is
  now carried by weight and size rather than by fading the ink out, which is
  the right trade for 11&ndash;13px text in a tool read in long sittings.
- **The vendor control is a listbox of our own.** A native `<select>` has its
  drop-down drawn by the operating system: one line of text per option and
  nothing else, so a vendor's code, tax number, blocked status and
  *added by you* tag all look identical &mdash; and those are exactly the
  distinctions that decide whether a PO could ever pay an invoice. The value
  still lives in a hidden input, so nothing that reads the form changed.
  Arrow keys, Enter, Escape and click-outside are given back by hand.
- **The rule cards lost their coloured left edge** for an even border all the
  way round. A stripe read as decoration, and worse, as a severity *scale* the
  three tones do not form &mdash; the chip in the corner says it in words. The
  same stripe came off `.note` and the three verdict boxes on How it works.
- **The diagrams were rebuilt on a tighter vocabulary.** The first pass had
  eight sprawling figures; this has four compact ones, modelled on the
  diagramming in the Kivi project &mdash; small boxes, a title and one line of
  detail, short arrows, near-black on two greys with colour reserved for a
  verdict. `frontend/uml.js` is now about a hundred lines instead of three
  hundred and fifty.
- **Box alignment is asserted, not eyeballed.** A browser check walks every
  figure and fails on anything drawn outside its own `viewBox`, any two boxes
  overlapping, any text string wider than the box it sits in, any arrow
  starting or ending inside a box, and any loose text running past the right
  edge. It caught two labels in figure 4 that were two pixels too wide.
- **The section rail is gone** from How it works. Four sections of prose and
  pictures are read top to bottom; the back-to-top button covers the rest.

A fourth round, on reading rather than correctness:

- **Black, strictly.** All four ink tokens are now `#000000`. They are kept as
  four names because the hierarchy is real and every rule already calls the
  right one, but the hierarchy is carried entirely by size, weight, letter
  spacing and background. Placeholders are black and italic, so an empty field
  is still told apart from a filled one without fading it out.
- **The vendor row is just the name.** Code, tax number and payment terms were
  three lines of noise around the one word anybody is looking for; they are on
  the row's `title` instead, and in full on the vendor master.
- **Adding a vendor is a dialog.** It was a panel inside the purchase-order
  form, which pushed the rest of the form down under whoever was filling it in
  and made a one-field errand look like part of a longer one. It asks for the
  name and puts the tax number and aliases behind a disclosure. It is reachable
  from the picker *and* from a button on the vendor master, and it closes on
  Escape, on the scrim, and on Cancel.
- **The rule set is grouped by what each check is held against** — the same
  four groups the diagram on How it works uses, so the two reinforce each other
  instead of repeating. Nine cards in one grid was a list to read through; four
  groups is a structure to understand. A legend above says what REJECT and FLAG
  mean before the first card.
- **How it works is a document.** Seven numbered sections in the order the
  system itself runs — the problem, the seven steps, reading the page, what it
  checks against, what gets written down, raising a PO, and the limits — each
  with its explanation and the picture of the mechanism it describes. Two more
  figures were added for provenance and for the audit trail, so there are six.
  The index is built from the headings actually present and floats beside the
  text in its own grid column; a fixed overlay in the viewport margin
  overlapped the text at 1512px, which a column cannot do.

## What a code review found

A full review of the diff on 2026-11-03 raised nine findings. All nine are
fixed, and the five worth remembering are below.

**One generated invoice per PO at a time (the serious one).** The three variant
buttons share one filename prefix, and the clear-my-own-history step had no
status filter — so starting a second variant while the first was still in the
pipeline deleted the first's `runs` and `run_stages` rows underneath it.
Deleting a run does not stop it: it went on to stage 7 and booked consumption
against a `run_id` with no row, and `po_consumption` has no foreign key on
`run_id`, so nothing afterwards could find that ledger entry to undo it. The
result was a permanent phantom charge against the PO, and every later invoice
judged by `R6` against budget nobody spent. Now the prefix delete skips
anything still running, and the endpoint returns 409 naming the run in flight.

**Deleting a PO under a live run.** Same shape from the other side:
`po_consumption.po_number` *is* a foreign key with `PRAGMA foreign_keys = ON`,
so deleting the PO turned the running invoice's decision stage into an
`IntegrityError` the user saw as a raw database message. `DELETE /api/pos/{po}`
now refuses while any run has matched it or is generating against it.

**A minimum purchase-order value.** The generated invoices add a small absolute
amount so two POs of the same value do not produce near-duplicate invoices that
`R7` correctly flags. That amount is a rounding error against a real PO and
dominates a trivial one: below about ₹80 it pushed the 7.5%-over variant past
the reject threshold, so the invoice advertised as FLAG came back REJECT. The
form now starts at ₹1,000, and a test asserts the bands hold at exactly that
floor.

**A vendor's own aliases were never checked for clashes.** Stage 4 scores the
printed name against the registered name *and* every alias, so adding "Acme
Fabricators" with the alias "Sharma Steel" produced two candidates a hair apart
with the winner decided by sort stability — the one state this master must
never be in. Every name the new vendor could be recognised by is now checked.
A name that is only a company suffix ("Private Limited") is refused too: it
canonicalises to nothing, so stage 4 could never resolve it.

**The expectation banner followed you to another run.** `loadRun` cleared
`state.intent` but never re-rendered, so clicking any history row after a
generated invoice left "Generated for PO-9001 · Expected FLAG" above an
unrelated invoice.

The rest were small: `#pos-tiles` and `#run-live` were ids from earlier
versions of those pages, so the two error paths that referenced them threw
instead of reporting (a failed PO load, and reset-everything); the PO value
field had `min="1" step="1000"`, which made its own placeholder step-invalid;
the form's `submit` listener could never fire because both buttons sat outside
the form, so Enter did nothing; and `loadPoForm` reset the fields on *every*
activation of the tab, wiping a half-filled form for anyone who stepped away to
check a vendor.

---

Two alignment bugs went with them: the expanded PO row was as wide as the
table, which scrolls sideways, so its right-hand card and the delete button sat
outside the visible area (it is now pinned to the scroll container and given
the visible width, measured, because CSS cannot know it); and the commitment
panel had a band of dead space between the figures and the flags.

---

## Two restrictions, on purpose

**Generated invoices are only offered for POs you raised.** Generating one
against a seeded PO would book consumption the prepared scenarios are scored
against, and `tools/check_scenarios.py` would start failing for a reason nobody
would connect to a button in the UI. The API returns 400 with that explanation.

**Deleting a PO is only possible for POs you raised.** Same reason, said from
the other side. Deleting one also forgets the generated runs against it, since
their ledger rows would otherwise point at a PO that no longer exists.

### Reset behaviour

`POST /api/admin/reseed` — *Reset purchase orders* in **Rules & data** —
restores the twelve seeded masters and clears what runs have billed. **It does
not delete POs you raised, and never did:** nothing in `seed.reseed()` deletes
from `pos`, and the seeded rows are re-upserted by number. So a reset wipes the
demo data's billing and leaves your own purchase orders standing, which was the
recommendation. Per-PO deletion is the deliberate, one-row way to remove one.

---

## What was verified

Live, against Gemini `gemini-3.5-flash`, on 2026-11-02:

```
PO-9001  raised via POST /api/pos          → 201, source=user
  match  → total 1,50,000  (-40.00%)  → APPROVE   all nine rules pass, confidence 0.99
  over   → total 2,68,750  (+7.50%)   → FLAG      R5, over tolerance, inside reject
  reject → total 3,37,500  (+35.00%)  → REJECT    R5, past the reject threshold
PO-9101 + PO-9102, same vendor, same ₹1,80,000 value, both billed at 60%
  → APPROVE and APPROVE   (totals ₹1,08,064.78 and ₹1,08,016.20)
```

API guards: duplicate number → 409; blocked vendor → 400 naming the vendor;
unknown vendor → 400; `po_number: "x!"` → 422 with the field named; generated
invoice on a seeded PO → 400; delete of a seeded PO → 400.

Browser (headless Chrome, 1440×1000): the dashboard is the landing panel, all
eight figures render with no SVG spilling its frame, the form suggests the next
free number in the `PO-9xxx` range, the new row carries the **added by you**
tag, the budget pill reads *1 added by you · ₹2.50 L*, the generated invoice
drives all seven phases to `done`, the verdict is **APPROVE** with the banner
marked *agreed*, the PO row then reads *Billed ₹1,50,000 · 60.0% consumed*, and
the run appears at the top of the dashboard. No console errors, no failed
requests, no horizontal overflow.

`python -m pytest tests -q` → **53 passed**.
`python tools/check_scenarios.py` → **all 14 scenarios reached the intended
decision via the intended rules**, unchanged.

---

## What this does not do

- **No editing a PO.** Amending a purchase order after invoices have been
  matched against it makes the earlier decisions unexplainable; a real system
  wants an amendment record, not an `UPDATE`. Delete and raise a new one.
- **No PO upload or ERP sync.** `db.py` is the seam where a procurement API
  would plug in; this adds a row to the same table rather than pretending to be
  that integration.
- **INR only**, like the rest of the build. Every threshold comparison in
  `rules.py` is single-currency, and `NewPO` says so rather than accepting a
  currency it would then compare wrongly.
- **The generated invoice is one layout**, deliberately plain. The five bundled
  vendor samples are the ones that hide the same facts in five different
  places; this document exists to be read correctly, not to test the
  extractor's patience.
