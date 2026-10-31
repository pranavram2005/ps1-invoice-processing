"""The seven-stage pipeline.

    PDF in
      |
   1. INTAKE          hash the file, store the blob, probe the text layer,
      |               check the hash against previous submissions
   2. EXTRACT         one Claude call: PDF -> schema-guaranteed JSON, with the
      |               page and verbatim quote behind every field
   3. NORMALIZE       dates -> ISO, amounts -> numbers, currency -> ISO 4217,
      |               vendor name canonicalised, quoted provenance verified
      |               against the PDF's own text layer -> confidence
   4. RESOLVE VENDOR  fuzzy match against the approved vendor master
      |
   5. MATCH PO        score every plausible PO; pick a winner or escalate with
      |               a ranked shortlist
   6. VALIDATE        run all nine rules; each emits expected vs actual
      |
   7. DECIDE          worst severity wins -> APPROVE / FLAG / REJECT, with
      |               plain-English reasons
    decision out

Every stage writes a row to `run_stages` with its status, duration and payload
*as it happens*. That table is simultaneously the live view the browser streams,
the audit trail a reviewer reads afterwards, and the replay the dashboard
renders. There is no second, prettier version of events.

Stages are intentionally paced by `STAGE_DELAY_MS` so a human can watch them.
That is presentation, and it is labelled as such in the Admin tab - set it to 0
for real throughput numbers.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import AsyncIterator

from . import db, extract as extract_mod, matching, normalize as normalize_mod, provenance as prov
from .config import STAGE_DELAY_MS, UPLOAD_DIR, active_model
from .events import broker
from .models import (
    ConfidenceBreakdown,
    Decision,
    NormalizedInvoice,
    POMatch,
    RawExtraction,
    RunResult,
    stages,
    StageRecord,
    VendorMatch,
)
from .rules import RuleConfig, RuleContext, decide, evaluate

log = logging.getLogger("pipeline")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_run_id() -> str:
    return f"RUN-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}"


def store_upload(file_sha256: str, data: bytes) -> str:
    """Content-addressed blob store so a run can re-serve its own PDF to the
    UI (and so a re-run never needs the file uploaded again)."""
    path = UPLOAD_DIR / f"{file_sha256}.pdf"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def blob_path(file_sha256: str):
    return UPLOAD_DIR / f"{file_sha256}.pdf"


class PipelineRun:
    """One invoice through the seven stages."""

    def __init__(
        self, run_id: str, filename: str, pdf_bytes: bytes, config: RuleConfig,
        *, replay: bool = False,
    ) -> None:
        self.run_id = run_id
        self.filename = filename
        self.pdf_bytes = pdf_bytes
        self.config = config
        self.replay = replay
        self.stages = [
            StageRecord(seq=i + 1, stage=stage, label=label)
            for i, (stage, label) in enumerate(stages())
        ]
        self.result = RunResult(
            run_id=run_id,
            filename=filename,
            file_sha256=sha256_of(pdf_bytes),
            file_bytes=len(pdf_bytes),
            status="running",
            started_at=_now(),
            stages=self.stages,
            rule_config=config.to_dict(),
        )
        self._t0 = time.perf_counter()

    # -- plumbing ---------------------------------------------------------- #

    def _publish(self, event_type: str, stage: StageRecord | None = None, **extra) -> None:
        payload = {"type": event_type, "run_id": self.run_id, **extra}
        if stage is not None:
            payload["stage"] = stage.model_dump()
        broker.publish(self.run_id, payload)

    @asynccontextmanager
    async def stage(self, seq: int) -> AsyncIterator[StageRecord]:
        record = self.stages[seq - 1]
        record.status = "running"
        record.started_at = _now()
        db.save_stage(self.run_id, record)
        self._publish("stage", record)

        # Pacing so the run view is legible; see module docstring.
        if STAGE_DELAY_MS:
            await asyncio.sleep(STAGE_DELAY_MS / 1000.0)

        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            record.finished_at = _now()
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            db.save_stage(self.run_id, record)
            self._publish("stage", record)
            raise
        record.status = "done"
        record.finished_at = _now()
        record.duration_ms = int((time.perf_counter() - started) * 1000)
        db.save_stage(self.run_id, record)
        self._publish("stage", record)

    def _skip_remaining(self, from_seq: int, reason: str) -> None:
        for record in self.stages[from_seq - 1:]:
            if record.status in ("pending",):
                record.status = "skipped"
                record.summary = reason
                db.save_stage(self.run_id, record)
                self._publish("stage", record)

    # -- the stages -------------------------------------------------------- #

    def prepare(self) -> None:
        """Create the run row and its seven pending stages, synchronously.

        Called from the request handler *before* the work is scheduled, so by
        the time the HTTP response returns, `GET /api/runs/{id}` already reports
        a running run with a full timeline. Without this the client races the
        background task: a replay would briefly still read as the previous
        completed attempt, and a new run would 404.
        """
        db.create_run(
            self.run_id, self.filename, self.result.file_sha256,
            self.result.file_bytes, self.stages,
        )
        # A replay reuses its own run id, so clear anything the previous
        # attempt booked against a PO before re-deciding.
        db.unbook_run(self.run_id)
        self._publish("run_started", filename=self.filename, run_id=self.run_id)

    async def run(self) -> RunResult:
        try:
            probe = await self._stage_intake()
            extraction = await self._stage_extract()
            invoice, confidence = await self._stage_normalize(extraction, probe)
            vendor = await self._stage_resolve_vendor(invoice)
            po_match = await self._stage_match_po(invoice, vendor)
            rule_results = await self._stage_validate(invoice, vendor, po_match, confidence, probe)
            await self._stage_decide(rule_results, confidence, po_match, invoice)
            self.result.status = "complete"
        except Exception as exc:
            log.exception("run %s failed: %s", self.run_id, exc)
            self.result.status = "failed"
            self.result.error = str(exc)
            failed_at = next((s.seq for s in self.stages if s.status == "failed"), len(self.stages))
            self._skip_remaining(failed_at + 1, "Not reached - an earlier stage failed.")

        self.result.finished_at = _now()
        self.result.duration_ms = int((time.perf_counter() - self._t0) * 1000)
        self.result.stages = self.stages
        db.finish_run(self.result)
        self._publish(
            "run_finished",
            status=self.result.status,
            decision=self.result.decision.model_dump() if self.result.decision else None,
            duration_ms=self.result.duration_ms,
            error=self.result.error,
        )
        broker.mark_finished(self.run_id)
        return self.result

    async def _stage_intake(self) -> prov.PdfProbe:
        async with self.stage(1) as record:
            store_upload(self.result.file_sha256, self.pdf_bytes)
            probe = prov.probe_pdf(self.pdf_bytes)
            self.result.pdf_kind = probe.kind  # type: ignore[assignment]
            self.result.page_count = probe.page_count

            prior = db.find_run_by_hash(
                self.result.file_sha256,
                exclude_run_id=self.run_id if self.replay else None,
            )
            if prior:
                self.result.duplicate_of = prior["run_id"]

            record.summary = (
                f"{self.result.file_bytes / 1024:.0f} KB, {probe.page_count} page(s), "
                f"{'text layer present' if probe.has_text_layer else 'image-only (scanned)'}"
            )
            record.payload = {
                "filename": self.filename,
                "file_sha256": self.result.file_sha256,
                "sha256_short": self.result.file_sha256[:16],
                "file_bytes": self.result.file_bytes,
                "page_count": probe.page_count,
                "pdf_kind": probe.kind,
                "text_layer_chars": probe.total_chars,
                "text_layer_note": probe.note,
                "prior_submission": prior,
                "replay": self.replay,
            }
            return probe

    async def _stage_extract(self) -> RawExtraction:
        async with self.stage(2) as record:
            cached = db.find_extraction_by_hash(
                self.result.file_sha256,
                exclude_run_id=self.run_id,
            )
            # A cached *fixture* must never be served into a live run. The bytes
            # match, so the cache is technically valid - but these values were
            # never read by a model, and a run labelled anything other than
            # `fixture` implies they were. Re-reading costs one call and keeps
            # "live means live" true, which is the whole basis of the claim that
            # an offline run can never be mistaken for a real one. Offline and
            # auto modes may reuse it: there, a fixture is what was asked for.
            if (
                cached
                and cached.get("origin") == "fixture"
                and extract_mod.EXTRACTION_MODE == "live"
            ):
                log.info(
                    "ignoring cached extraction from %s: it came from a fixture and this "
                    "run is live", cached["run_id"],
                )
                cached = None
            if cached:
                extraction = RawExtraction.model_validate(cached["extraction"])
                self.result.cached = True
                self.result.extraction_source = "cache"
                self.result.extraction_origin = cached.get("origin") or "live"
                self._model_iso = None
                db.record_llm_event(
                    run_id=self.run_id, model=active_model(),
                    latency_ms=0, input_tokens=0, output_tokens=0, cached=True,
                )
                record.summary = (
                    f"Reused the extraction from run {cached['run_id']} - identical file, "
                    f"zero LLM calls"
                )
                record.payload = {
                    "cached": True,
                    "source": "cache",
                    "origin": self.result.extraction_origin,
                    "cache_source_run": cached["run_id"],
                    "extraction": extraction.model_dump(),
                    "note": (
                        "Extraction is content-addressed by SHA-256, so the same bytes never "
                        "cost a second API call. Rule R7 still treats the resubmission as a "
                        "duplicate - caching is an efficiency, not a bypass."
                    ),
                }
                self.result.extraction = extraction
                return extraction

            extraction, telemetry = await asyncio.to_thread(extract_mod.extract, self.pdf_bytes)
            self._model_iso = telemetry.get("invoice_date_iso")
            source = telemetry.get("source", "live")
            self.result.extraction = extraction
            self.result.extraction_source = source
            self.result.extraction_origin = source

            # Only a real API call belongs in the telemetry table. Recording a
            # fixture run there would put a zero-latency, zero-token row into
            # the latency percentiles and the cost estimate.
            if source == "live":
                db.record_llm_event(
                    run_id=self.run_id, model=telemetry["model"],
                    latency_ms=telemetry["latency_ms"],
                    input_tokens=telemetry["input_tokens"],
                    output_tokens=telemetry["output_tokens"],
                    cached=False,
                )

            fields_found = sum(
                1 for value in (
                    extraction.vendor_name, extraction.invoice_number,
                    extraction.invoice_date, extraction.total_amount,
                    extraction.po_reference,
                ) if value not in (None, "")
            )
            provenance = (
                f"offline fixture ({telemetry.get('fixture_file')})" if source == "fixture"
                else f"{telemetry['model']} in {telemetry['latency_ms']} ms"
            )
            record.summary = (
                f"{fields_found}/5 key fields, {len(extraction.line_items)} line item(s), "
                f"model confidence {extraction.model_confidence:.2f} - {provenance}"
            )
            record.payload = {
                "cached": False,
                "source": source,
                "model": telemetry["model"],
                "effort": telemetry["effort"],
                "latency_ms": telemetry["latency_ms"],
                "input_tokens": telemetry["input_tokens"],
                "output_tokens": telemetry["output_tokens"],
                "extraction": extraction.model_dump(),
            }
            if source == "fixture":
                record.payload["note"] = (
                    "EXTRACTION_MODE is not 'live', so this run used the bundled ground-truth "
                    "fixture for this file instead of calling the model. Everything downstream "
                    "- normalisation, provenance verification, matching, the rules and the "
                    "decision - is the real code path. Only the reading step was substituted."
                )
                if telemetry.get("fallback_from_error"):
                    record.payload["fallback_from_error"] = telemetry["fallback_from_error"]
            return extraction

    async def _stage_normalize(
        self, extraction: RawExtraction, probe: prov.PdfProbe,
    ) -> tuple[NormalizedInvoice, ConfidenceBreakdown]:
        async with self.stage(3) as record:
            invoice = normalize_mod.normalize(
                extraction, model_iso=getattr(self, "_model_iso", None)
            )
            verified = prov.verify_provenance(list(extraction.provenance), probe)
            confidence = prov.compute_confidence(
                model_confidence=extraction.model_confidence,
                items=verified, probe=probe,
            )
            self.result.normalized = invoice
            self.result.provenance = verified
            self.result.confidence_breakdown = confidence

            verified_count = sum(1 for item in verified if item.verified)
            checkable = sum(1 for item in verified if item.verified is not None)
            record.summary = (
                f"{invoice.currency} {invoice.total_amount:,.2f} dated {invoice.invoice_date}; "
                f"{verified_count}/{checkable} quoted fields verified against the document; "
                f"confidence {confidence.final:.2f}"
                if invoice.total_amount is not None else
                f"no total could be read; confidence {confidence.final:.2f}"
            )
            record.payload = {
                "normalized": invoice.model_dump(),
                "transformations": [
                    {
                        "field": "invoice_date",
                        "before": invoice.invoice_date_raw,
                        "after": invoice.invoice_date,
                        "note": "day-first parsing (Indian invoice convention)",
                    },
                    {
                        "field": "currency",
                        "before": extraction.currency,
                        "after": invoice.currency,
                        "note": "mapped to ISO 4217",
                    },
                    {
                        "field": "vendor_name",
                        "before": invoice.vendor_name_raw,
                        "after": invoice.vendor_name_canonical,
                        "note": "canonical form used for matching only",
                    },
                    {
                        "field": "line_items_sum",
                        "before": None,
                        "after": invoice.line_items_sum,
                        "note": "computed by code, never by the model, so R2 has an independent figure",
                    },
                ],
                "provenance": [item.model_dump() for item in verified],
                "confidence": confidence.model_dump(),
                "text_layer_available": probe.has_text_layer,
            }
            return invoice, confidence

    async def _stage_resolve_vendor(self, invoice: NormalizedInvoice) -> VendorMatch:
        async with self.stage(4) as record:
            vendors = db.list_vendors()
            vendor = matching.resolve_vendor(invoice, vendors, self.config)
            self.result.vendor = vendor

            if vendor.status == "matched":
                record.summary = f"{vendor.vendor_name} ({vendor.vendor_id}) at {vendor.score:.2f}"
            elif vendor.status == "blocked":
                record.summary = f"{vendor.vendor_name} is on the master but BLOCKED"
            else:
                record.summary = (
                    f"'{invoice.vendor_name_raw or 'unknown'}' did not resolve "
                    f"(best {vendor.score:.2f}, floor {self.config.vendor_match_min_score:.2f})"
                )
            record.payload = {
                "vendor": vendor.model_dump(),
                "floor": self.config.vendor_match_min_score,
                "vendors_in_master": len(vendors),
                "matched_on": "GSTIN" if vendor.score == 1.0 and invoice.vendor_gstin else "name similarity",
            }
            return vendor

    async def _stage_match_po(self, invoice: NormalizedInvoice, vendor: VendorMatch) -> POMatch:
        async with self.stage(5) as record:
            pos = db.list_pos()
            po_match = matching.match_po(invoice, vendor, pos, self.config)
            self.result.po = po_match

            record.summary = (
                f"{po_match.po_number} at {po_match.score:.2f}"
                if po_match.matched else
                f"no PO cleared {self.config.po_match_min_score:.2f} "
                f"({len(po_match.candidates)} candidate(s) ranked)"
            )
            record.payload = {
                "po": po_match.model_dump(),
                "floor": self.config.po_match_min_score,
                "invoice_po_reference": invoice.po_reference,
                "pos_considered": len(po_match.candidates),
                "inferred_ceiling": matching.INFERRED_PO_SCORE_CEILING,
            }
            return po_match

    async def _stage_validate(
        self, invoice: NormalizedInvoice, vendor: VendorMatch, po_match: POMatch,
        confidence: ConfidenceBreakdown, probe: prov.PdfProbe,
    ) -> list:
        async with self.stage(6) as record:
            # Duplicate inputs are resolved here rather than inside a rule, so
            # the rules stay pure functions of their context and remain
            # unit-testable without a database.
            exact = db.find_run_by_hash(
                self.result.file_sha256,
                exclude_run_id=self.run_id,
            )
            near = db.find_near_duplicate(
                vendor_id=vendor.vendor_id,
                total_amount=invoice.total_amount,
                invoice_date=invoice.invoice_date,
                window_days=self.config.duplicate_window_days,
                amount_tolerance=self.config.arithmetic_tolerance,
                exclude_run_id=self.run_id,
            )
            near_detail = ""
            if near and exact and near["run_id"] == exact["run_id"]:
                near = None  # the exact match already covers it
            if near:
                near_detail = (
                    f"invoice {near['invoice_number']} dated {near['invoice_date']} for the same "
                    f"vendor and the same amount ({invoice.currency} {near['total_amount']:,.2f})"
                )

            po_record = db.get_po(po_match.po_number) if po_match.po_number else None
            context = RuleContext(
                invoice=invoice, vendor=vendor, po=po_match, confidence=confidence,
                config=self.config, pdf_kind=probe.kind,
                exact_duplicate_run_id=exact["run_id"] if exact else None,
                near_duplicate_run_id=near["run_id"] if near else None,
                near_duplicate_detail=near_detail,
                po_value=po_record["po_value"] if po_record else None,
                po_already_billed=po_record["already_billed"] if po_record else 0.0,
                today=date.today(),
            )
            results = evaluate(context)
            self.result.rules = results
            if exact:
                self.result.duplicate_of = exact["run_id"]
            elif near:
                self.result.duplicate_of = near["run_id"]

            failed = [r for r in results if not r.passed]
            checked = [r for r in results if not r.skipped]
            record.summary = (
                f"{len(checked) - len(failed)}/{len(checked)} checks passed"
                + (f"; failed: {', '.join(r.rule_id for r in failed)}" if failed else "")
            )
            record.payload = {
                "rules": [r.model_dump() for r in results],
                "config": self.config.to_dict(),
                "duplicate_inputs": {"exact": exact, "near": near, "near_detail": near_detail},
                "po_ledger": po_record,
            }
            return results

    async def _stage_decide(
        self, rule_results: list, confidence: ConfidenceBreakdown,
        po_match: POMatch, invoice: NormalizedInvoice,
    ) -> Decision:
        async with self.stage(7) as record:
            decision = decide(rule_results, confidence)
            self.result.decision = decision

            # Only an approved invoice consumes PO budget. A flagged or
            # rejected invoice has not been accepted, so booking it would
            # corrupt the ledger that rule R6 depends on.
            booked = None
            if decision.decision == "APPROVE" and po_match.po_number and invoice.total_amount:
                db.book_consumption(
                    po_match.po_number, invoice.total_amount,
                    run_id=self.run_id, invoice_number=invoice.invoice_number, source="run",
                )
                booked = {"po_number": po_match.po_number, "amount": invoice.total_amount}

            severities = {r.rule_id: r.severity for r in rule_results if not r.passed}
            record.summary = f"{decision.decision} (confidence {decision.confidence:.2f})"
            record.payload = {
                "decision": decision.model_dump(),
                "severity_by_failed_rule": severities,
                "logic": (
                    "Worst severity wins: any REJECT-severity failure rejects, otherwise any "
                    "FLAG-severity failure flags, otherwise approve. The decision is a pure "
                    "function of the rule results above - the model is not consulted here."
                ),
                "confidence_note": (
                    "This is extraction confidence: how sure the system is that it read the "
                    "document correctly. The decision itself is deterministic, so it carries no "
                    "separate probability - the same fields always produce the same verdict."
                ),
                "po_consumption_booked": booked,
            }
            return decision


def start(
    run_id: str, filename: str, pdf_bytes: bytes, config: RuleConfig, *, replay: bool = False,
) -> PipelineRun:
    """Register a run synchronously and hand back the object to await.

    Split from `execute` so an HTTP handler can guarantee the run is visible
    before it responds, then schedule `run()` on the event loop.
    """
    run = PipelineRun(run_id, filename, pdf_bytes, config, replay=replay)
    run.prepare()
    return run


async def execute(
    run_id: str, filename: str, pdf_bytes: bytes, config: RuleConfig, *, replay: bool = False,
) -> RunResult:
    """Prepare and run to completion. Used by the seeder and the CLI tools."""
    return await start(run_id, filename, pdf_bytes, config, replay=replay).run()
