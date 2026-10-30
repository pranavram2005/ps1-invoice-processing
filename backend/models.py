"""Pydantic shapes for the invoice pipeline.

Three groups:

1. What the model returns  - `RawExtraction` and friends. This is the only
   fuzzy boundary in the system and it is schema-constrained (strict tool use).
2. What the code derives   - `NormalizedInvoice`, `VendorMatch`, `POMatch`,
   `RuleResult`, `Decision`. All deterministic.
3. What the API serves     - `StageRecord`, `RunResult`, `RunSummary`.

The split matters: group 1 is the model's opinion, group 2 is the system's
judgment, and the UI shows both so a reviewer can see where one ends and the
other begins.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# `model_confidence` is the model's own confidence, not pydantic model config.
# Pydantic reserves the `model_` prefix and warns about it; the field name is
# the clearest one for what it holds, so opt out of the reservation instead.
ALLOW_MODEL_PREFIX = ConfigDict(protected_namespaces=())

Decisions = Literal["APPROVE", "FLAG", "REJECT"]
Severity = Literal["PASS", "FLAG", "REJECT"]
PdfKind = Literal["text", "scanned"]
StageStatus = Literal["pending", "running", "done", "failed", "skipped"]

STAGES: list[tuple[str, str]] = [
    ("INTAKE", "Intake & fingerprint"),
    ("EXTRACT", "Extract fields (Claude)"),
    ("NORMALIZE", "Normalise & verify provenance"),
    ("RESOLVE_VENDOR", "Resolve vendor"),
    ("MATCH_PO", "Match purchase order"),
    ("VALIDATE", "Apply rule set"),
    ("DECIDE", "Decide"),
]


# --------------------------------------------------------------------------- #
# 1. Model output
# --------------------------------------------------------------------------- #

class LineItem(BaseModel):
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    line_total: float | None = None


class Provenance(BaseModel):
    """Where the model says it read a field, and whether we could confirm it.

    `page` and `quote` are the model's claim. `verified` and `match_score` are
    the system's check of that claim against the PDF's own text layer. On a
    scanned PDF there is no text layer, so `verified` stays None - unknown, not
    false. That distinction is what drives the confidence penalty in R9.
    """

    field: str
    page: int | None = None
    quote: str | None = None
    verified: bool | None = None
    match_score: float | None = None


class RawExtraction(BaseModel):
    """Exactly what the extraction call returns, before any normalisation."""

    model_config = ALLOW_MODEL_PREFIX

    vendor_name: str | None = None
    vendor_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    po_reference: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None
    notes: str | None = None
    model_confidence: float = 0.0
    provenance: list[Provenance] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 2. Derived, deterministic
# --------------------------------------------------------------------------- #

class NormalizedInvoice(BaseModel):
    vendor_name_raw: str | None = None
    vendor_name_canonical: str | None = None
    vendor_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None          # ISO-8601 date, or None if unparseable
    invoice_date_raw: str | None = None
    currency: str = "INR"
    po_reference: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    line_items_sum: float | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None
    notes: str | None = None


class VendorMatch(BaseModel):
    vendor_id: str | None = None
    vendor_name: str | None = None
    score: float = 0.0
    status: Literal["matched", "unknown", "blocked"] = "unknown"
    considered: list[dict] = Field(default_factory=list)


class POCandidate(BaseModel):
    po_number: str
    vendor_id: str
    description: str | None = None
    po_value: float
    already_billed: float
    remaining: float
    issued_date: str | None = None
    expiry_date: str | None = None
    score: float
    # Numeric component scores only, so the UI can render them as bars.
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    # Human-readable description of how those components were weighted.
    weights: str = ""
    reason: str = ""


class POMatch(BaseModel):
    matched: bool = False
    po_number: str | None = None
    score: float = 0.0
    strategy: Literal["explicit_reference", "fuzzy_reference", "vendor_amount_window", "none"] = "none"
    candidates: list[POCandidate] = Field(default_factory=list)
    note: str = ""


class RuleResult(BaseModel):
    rule_id: str
    title: str
    passed: bool
    severity: Severity                  # severity actually applied this run
    expected: str
    actual: str
    message: str
    skipped: bool = False


class Decision(BaseModel):
    decision: Decisions
    confidence: float
    reasons: list[str] = Field(default_factory=list)
    failed_rules: list[str] = Field(default_factory=list)
    requires_human: bool = False


class ConfidenceBreakdown(BaseModel):
    """How the extraction confidence number was arrived at - shown in the UI so
    the number is never a bare float a reviewer has to take on faith."""

    model_config = ALLOW_MODEL_PREFIX

    model_confidence: float
    provenance_fields_total: int = 0
    provenance_fields_verified: int = 0
    provenance_verified_ratio: float | None = None
    scanned_penalty_applied: bool = False
    final: float = 0.0
    explanation: str = ""


# --------------------------------------------------------------------------- #
# 3. API surface
# --------------------------------------------------------------------------- #

class StageRecord(BaseModel):
    seq: int
    stage: str
    label: str
    status: StageStatus = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    summary: str = ""
    payload: dict = Field(default_factory=dict)
    error: str | None = None


class RunResult(BaseModel):
    """The full audit record for one invoice. Persisted as a JSON blob and
    replayed verbatim by the dashboard."""

    run_id: str
    filename: str
    file_sha256: str
    file_bytes: int = 0
    pdf_kind: PdfKind | None = None
    page_count: int | None = None
    status: Literal["running", "complete", "failed"] = "running"

    extraction: RawExtraction | None = None
    normalized: NormalizedInvoice | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    confidence_breakdown: ConfidenceBreakdown | None = None
    vendor: VendorMatch | None = None
    po: POMatch | None = None
    rules: list[RuleResult] = Field(default_factory=list)
    decision: Decision | None = None

    duplicate_of: str | None = None
    cached: bool = False
    # "live" (a real API call), "cache" (same bytes read before) or "fixture"
    # (offline mode). Surfaced in the UI so a run's provenance is never unclear.
    extraction_source: str | None = None
    rule_config: dict = Field(default_factory=dict)

    stages: list[StageRecord] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None


class RunSummary(BaseModel):
    """One row in the dashboard table."""

    run_id: str
    filename: str
    status: str
    started_at: str | None = None
    duration_ms: int | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    total_amount: float | None = None
    matched_po: str | None = None
    po_match_score: float | None = None
    decision: str | None = None
    confidence: float | None = None
    pdf_kind: str | None = None
    failed_rules: list[str] = Field(default_factory=list)
    cached: bool = False
