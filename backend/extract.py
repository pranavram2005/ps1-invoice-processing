"""Stage 2: one model call turns a PDF into schema-guaranteed JSON.

This is the only place in the pipeline that talks to a model, and the only
fuzzy step in the system. Two providers fill the same seam - Gemini (default,
in `gemini.py`) and Anthropic (below) - selected by `EXTRACTION_PROVIDER`.
Both are handed the same schema and prompts and both return the same tuple, so
nothing downstream of stage 2 knows which one ran.

Three decisions are worth reading the rationale for
(all expanded in DECISIONS.md):

1. **Native PDF input.** The file goes to the API as a base64 `document`
   content block. Claude reads the text layer *and* the page images, so one
   code path handles a clean digital PDF and an image-only scan identically -
   no Tesseract, no poppler, no pdf2image, nothing extra in the container.
   (The build machine does not even have poppler; avoiding an OCR stack was a
   real constraint, not a theoretical preference.)

2. **Structured outputs, not citations.** The API's `citations` feature would
   return page-level provenance from the server, but it is incompatible with
   `output_config.format` - enabling both returns a 400. A schema guarantee on
   a financial extraction is worth more than server-formatted citations, so
   provenance is a field *in* the schema and `provenance.py` verifies the
   quoted snippets against the PDF's text layer instead. The result is
   provenance that has been checked rather than merely asserted.

3. **The model is told not to be helpful.** It must report the total exactly as
   printed and must not recompute or correct it. If the model silently fixed a
   vendor's arithmetic, rule R2 could never fire and a real class of invoice
   error would become invisible. Extraction reports; the rules judge.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import gemini, groq
from .config import (
    ANTHROPIC_API_KEY,
    EXTRACTION_CHAIN,
    EXTRACTION_EFFORT,
    EXTRACTION_MODE,
    FIXTURES_PATH,
    active_model,
    active_provider,
    keys_for,
    model_for,
)
from .models import LineItem, Provenance, RawExtraction

log = logging.getLogger("extract")

_clients: dict[str, anthropic.Anthropic] = {}


def client(api_key: str = "") -> anthropic.Anthropic:
    """An SDK client per credential, so a fallback key gets its own client."""
    api_key = api_key or ANTHROPIC_API_KEY
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set - copy .env.example to .env and add a key."
        )
    if api_key not in _clients:
        _clients[api_key] = anthropic.Anthropic(api_key=api_key, max_retries=3)
    return _clients[api_key]


PROVENANCE_FIELDS = [
    "vendor_name", "vendor_gstin", "invoice_number", "invoice_date",
    "currency", "po_reference", "subtotal", "tax_amount", "total_amount",
]

INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "vendor_name", "vendor_gstin", "invoice_number", "invoice_date",
        "invoice_date_iso", "currency", "po_reference", "line_items",
        "subtotal", "tax_amount", "total_amount", "notes",
        "model_confidence", "provenance",
    ],
    "properties": {
        "vendor_name": {
            "type": ["string", "null"],
            "description": "The supplier issuing the invoice, exactly as printed. Not the buyer.",
        },
        "vendor_gstin": {
            "type": ["string", "null"],
            "description": "The supplier's 15-character GSTIN if printed, else null.",
        },
        "invoice_number": {
            "type": ["string", "null"],
            "description": "The vendor's own invoice/bill number, as printed.",
        },
        "invoice_date": {
            "type": ["string", "null"],
            "description": "The invoice date exactly as printed, e.g. '14/03/2026' or '14 Mar 2026'.",
        },
        "invoice_date_iso": {
            "type": ["string", "null"],
            "description": (
                "The same date as YYYY-MM-DD. Indian invoices are day-first, so "
                "'03/04/2026' is 3 April. Null if no date is printed."
            ),
        },
        "currency": {
            "type": ["string", "null"],
            "description": "ISO 4217 code. The Rupee symbol, 'Rs.' and 'INR' all mean INR.",
        },
        "po_reference": {
            "type": ["string", "null"],
            "description": (
                "The purchase order number this invoice bills against, if stated anywhere - "
                "including in a footnote, a remarks line or the subject line. Null if absent. "
                "Do not invent one and do not return the invoice number here."
            ),
        },
        "line_items": {
            "type": "array",
            "description": "One entry per billed line. Empty array if the invoice is not itemised.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "quantity", "unit_price", "line_total"],
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "line_total": {
                        "type": ["number", "null"],
                        "description": "The line amount as printed, before invoice-level tax.",
                    },
                },
            },
        },
        "subtotal": {
            "type": ["number", "null"],
            "description": "Pre-tax subtotal as printed, else null. Never computed by you.",
        },
        "tax_amount": {
            "type": ["number", "null"],
            "description": (
                "Total tax as printed. Sum the GST components (CGST + SGST, or IGST) only when "
                "they are printed as separate lines of the same invoice-level tax. If tax is "
                "embedded in the line items and never stated separately, return null."
            ),
        },
        "total_amount": {
            "type": ["number", "null"],
            "description": (
                "The grand total the vendor is asking to be paid, exactly as printed. "
                "Do NOT recompute it, and do NOT correct it if it disagrees with the line "
                "items - reporting the printed figure faithfully is what lets a downstream "
                "rule catch the vendor's arithmetic error."
            ),
        },
        "notes": {
            "type": ["string", "null"],
            "description": (
                "Anything a human reviewer should know: an unusual layout, a figure you had "
                "to read from a poor-quality image, an ambiguity you resolved. Else null."
            ),
        },
        "model_confidence": {
            "type": "number",
            "description": (
                "Your own confidence that the fields above are correct, 0 to 1. Lower it when "
                "the page is an image, the print is unclear, or the layout forced a guess."
            ),
        },
        "provenance": {
            "type": "array",
            "description": (
                "One entry per field you populated, giving the page and the verbatim text you "
                "read it from. The snippet is checked against the document, so copy it exactly "
                "as it appears - do not paraphrase, reformat numbers, or stitch together text "
                "from different parts of the page."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "page", "quote"],
                "properties": {
                    "field": {"type": "string", "enum": PROVENANCE_FIELDS},
                    "page": {"type": ["integer", "null"], "description": "1-indexed page number."},
                    "quote": {
                        "type": ["string", "null"],
                        "description": "3 to 15 words copied verbatim, enough to locate the value.",
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You extract fields from vendor invoices for an accounts-payable system.

You are the reading step of a pipeline. A deterministic rule engine downstream
decides whether to approve, flag or reject the invoice. Your job is to report
what the document says - accurately, completely, and without improving it.

Rules you must follow:

- Report figures exactly as printed. Never recompute a total, never silently
  correct arithmetic that does not add up, and never reconcile a discrepancy.
  Discrepancies are signal: the rule engine is specifically looking for them,
  and a corrected total destroys that signal.
- Use null for anything genuinely absent. A null is a useful, actionable answer.
  An invented value is a payment error. Never guess a PO number or an invoice
  number.
- The vendor is the party issuing the invoice and asking to be paid, not the
  party being billed. Invoices print both; do not confuse them.
- Amounts are plain numbers: no currency symbols, no thousands separators,
  no words. "Rs. 11,80,000.00" is 1180000.00 - note that Indian digit grouping
  is lakh-based, so that figure is eleven lakh eighty thousand, not 11,800.
- For every field you populate, add a provenance entry quoting the exact text
  you read it from and the page it was on. The quote is automatically checked
  against the document's text, so verbatim accuracy matters more than tidiness.
- If the page is a scan or photograph rather than digital text, read it as
  carefully as you can and lower model_confidence to reflect the real
  uncertainty. Under-confidence routes the invoice to a human, which is a good
  outcome. Overconfidence on a misread figure is not.
"""

USER_PROMPT = """Extract the fields from this invoice.

Work through the whole document before answering - the PO reference in
particular is often not in an obvious place: check headers, footers, remarks
lines, narrow columns and any small print.
"""


class ExtractionError(RuntimeError):
    pass


# Retry the transient failures and a malformed response; let a 400 (bad request,
# unsupported file, oversized PDF) fail fast - retrying it just wastes the
# reviewer's time while the UI spins.
RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    ExtractionError,
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type(RETRYABLE),
    reraise=True,
)
def _call_anthropic(pdf_b64: str, model: str, api_key: str = "") -> tuple[dict, int, int, int]:
    """One request. Returns (parsed json, latency_ms, input_tokens, output_tokens)."""
    t0 = time.perf_counter()
    response = client(api_key).messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": EXTRACTION_EFFORT,
            "format": {"type": "json_schema", "schema": INVOICE_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": USER_PROMPT},
            ],
        }],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # Always check stop_reason before reading content: a safety classifier can
    # decline a request with HTTP 200 and no usable content block.
    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise ExtractionError(
            f"The model declined to process this document "
            f"(category: {getattr(detail, 'category', 'unknown')})."
        )
    if response.stop_reason == "max_tokens":
        raise ExtractionError("Extraction was truncated at max_tokens.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ExtractionError("Model returned no text block to parse.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # output_config.format guarantees valid JSON, so this means something
        # unusual happened; retry once rather than failing the run outright.
        log.error("extraction returned unparseable JSON: %s | %s", exc, text[:400])
        raise ExtractionError(f"Model returned invalid JSON: {exc}") from exc

    usage = response.usage
    return (
        data,
        latency_ms,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _call_model(
    pdf_b64: str, model: str, provider: str, pdf_bytes: bytes | None = None,
    api_key: str = "",
) -> tuple[dict, int, int, int]:
    """Dispatch one extraction to one provider with one credential.

    Both providers are handed the same schema and the same two prompts, and both
    return the same four-tuple. That is the whole provider seam - nothing below
    this line knows or cares which model read the PDF, which is what makes the
    rule engine's determinism claim independent of the extraction vendor.
    """
    if provider == "anthropic":
        return _call_anthropic(pdf_b64, model, api_key=api_key)
    if provider == "gemini":
        return gemini.call(
            pdf_b64,
            model,
            schema=INVOICE_SCHEMA,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            api_key=api_key,
        )
    if provider == "groq":
        # Groq serves text-only models, so this one reads the PDF's text layer
        # rather than the file. It is handed the bytes to avoid a pointless
        # base64 round trip.
        return groq.call(
            pdf_b64,
            model,
            schema=INVOICE_SCHEMA,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            pdf_bytes=pdf_bytes,
            api_key=api_key,
        )
    raise ExtractionError(
        f"Unknown extraction provider {provider!r} - expected one of "
        f"gemini, groq, anthropic."
    )


class AllProvidersExhausted(ExtractionError):
    """Every configured credential on every provider in the chain failed.

    Carries the per-attempt reasons so the run view can say *which* provider
    gave out and why, rather than just reporting that extraction failed.
    """

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        quota = [a for a in attempts if a.get("quota")]
        if quota and len(quota) == len(attempts):
            headline = (
                "Extraction limit reached - every configured API key is out of quota."
            )
        else:
            headline = "Extraction failed on every configured provider."
        parts = []
        for a in attempts:
            label = a["provider"]
            if a["key_count"] > 1:
                label += " key {}".format(a["key_index"])
            parts.append("{}: {}".format(label, a["error"]))
        super().__init__("{} {}".format(headline, "; ".join(parts)))


def _is_quota_error(exc: Exception) -> bool:
    """Whether a failure means "this credential is spent" rather than "broken".

    Worth separating: a 429 or an explicit quota message should move on to the
    next key quietly, while a 400 usually means the request itself is wrong and
    the next key will fail identically.
    """
    text = str(exc).lower()
    return (
        "429" in text
        or "quota" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "resource_exhausted" in text
        or "credit balance is too low" in text
        or "insufficient_quota" in text
    )


def _attempt_plan(first_provider: str) -> list[tuple[str, str, int, int]]:
    """(provider, key, index, count) for every credential worth trying, in order.

    The provider the caller asked for goes first even if the configured chain
    lists it later - an explicit choice in the UI should be honoured before the
    fallback order kicks in.
    """
    ordered = [first_provider] + [p for p in EXTRACTION_CHAIN if p != first_provider]
    plan: list[tuple[str, str, int, int]] = []
    for provider in ordered:
        keys = keys_for(provider)
        for i, key in enumerate(keys, start=1):
            plan.append((provider, key, i, len(keys)))
    return plan


def _coerce_number(value: Any) -> float | None:
    """Accept a number, or a string the model slipped a symbol into."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").replace("₹", "").replace("Rs.", "")
    cleaned = cleaned.replace("Rs", "").replace("INR", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_payload(data: dict) -> RawExtraction:
    """Turn the model's JSON into a RawExtraction. The schema already guarantees
    the shape; this guards the value types and drops useless provenance rows."""
    items: list[LineItem] = []
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        items.append(LineItem(
            description=str(raw.get("description") or "").strip(),
            quantity=_coerce_number(raw.get("quantity")),
            unit_price=_coerce_number(raw.get("unit_price")),
            line_total=_coerce_number(raw.get("line_total")),
        ))

    seen: set[str] = set()
    provenance: list[Provenance] = []
    for raw in data.get("provenance") or []:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or "").strip()
        quote = raw.get("quote")
        if not field or field in seen or not quote:
            continue
        seen.add(field)
        page = raw.get("page")
        provenance.append(Provenance(
            field=field,
            page=int(page) if isinstance(page, int) else None,
            quote=str(quote).strip(),
        ))

    def text(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    # Keep the model's ISO reading alongside the printed form; normalise.py
    # decides which to trust.
    iso = text("invoice_date_iso")
    return RawExtraction(
        vendor_name=text("vendor_name"),
        vendor_gstin=text("vendor_gstin"),
        invoice_number=text("invoice_number"),
        invoice_date=text("invoice_date") or iso,
        currency=text("currency"),
        po_reference=text("po_reference"),
        line_items=items,
        subtotal=_coerce_number(data.get("subtotal")),
        tax_amount=_coerce_number(data.get("tax_amount")),
        total_amount=_coerce_number(data.get("total_amount")),
        notes=text("notes"),
        model_confidence=_coerce_number(data.get("model_confidence")) or 0.0,
        provenance=provenance,
    )


# --------------------------------------------------------------------------- #
# Offline mode
# --------------------------------------------------------------------------- #

_fixtures: dict[str, dict] | None = None


def _load_fixtures() -> dict[str, dict]:
    global _fixtures
    if _fixtures is None:
        if not FIXTURES_PATH.exists():
            _fixtures = {}
        else:
            try:
                payload = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
                _fixtures = payload.get("fixtures", {})
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("could not read extraction fixtures: %s", exc)
                _fixtures = {}
    return _fixtures


def _extract_from_fixture(pdf_bytes: bytes) -> tuple[RawExtraction, dict]:
    """Serve a ground-truth fixture instead of calling the API.

    Only the bundled sample invoices have fixtures. An unknown file raises
    rather than guessing: a pipeline that quietly fabricated an extraction would
    be worse than one that stopped.
    """
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    fixture = _load_fixtures().get(digest)
    if fixture is None:
        raise ExtractionError(
            "Offline mode has no fixture for this file. Offline runs only cover the nine "
            "bundled sample invoices; to process an arbitrary PDF set EXTRACTION_MODE=live "
            "and supply a funded key for whichever EXTRACTION_PROVIDER is configured."
        )
    data = fixture["extraction"]
    return parse_payload(data), {
        "model": "offline-fixture",
        "provider": "fixture",
        "effort": "n/a",
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "invoice_date_iso": data.get("invoice_date_iso"),
        "source": "fixture",
        "fixture_file": fixture.get("file"),
    }


def extract(
    pdf_bytes: bytes,
    *,
    model: str | None = None,
    mode: str | None = None,
    provider: str | None = None,
) -> tuple[RawExtraction, dict]:
    """Extract one invoice. Returns the extraction plus a telemetry dict.

    `telemetry["source"]` is either "live" or "fixture" and is surfaced in the
    run view, so an offline run is never mistaken for a real one.
    """
    mode = (mode or EXTRACTION_MODE).lower()
    provider = (provider or active_provider()).lower()
    # Resolve the model against the provider actually being used, not the one
    # that happens to be active - otherwise an explicit provider= override gets
    # the wrong provider's model name.
    model = model or model_for(provider)

    if mode == "offline":
        return _extract_from_fixture(pdf_bytes)

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    plan = _attempt_plan(provider)
    if not plan:
        raise ExtractionError(
            "No API key is configured for any extraction provider. Set "
            "GEMINI_API_KEY (or GROQ_API_KEY / ANTHROPIC_API_KEY) in the "
            "environment."
        )

    attempts: list[dict] = []
    for used_provider, api_key, key_index, key_count in plan:
        # The model is resolved per provider: falling back from Gemini to Groq
        # must not carry the Gemini model name across.
        used_model = model if used_provider == provider else model_for(used_provider)
        try:
            data, latency_ms, in_tok, out_tok = _call_model(
                pdf_b64, used_model, used_provider, pdf_bytes, api_key=api_key,
            )
        except Exception as exc:
            quota = _is_quota_error(exc)
            attempts.append({
                "provider": used_provider,
                "model": used_model,
                "key_index": key_index,
                "key_count": key_count,
                "quota": quota,
                "error": str(exc)[:400],
            })
            log.warning(
                "extraction failed on %s (key %d/%d, %s) - %s",
                used_provider, key_index, key_count,
                "quota exhausted" if quota else "error",
                str(exc)[:200],
            )
            continue

        extraction = parse_payload(data)
        # The model's ISO date reading is not part of RawExtraction's public
        # shape, so hand it to the caller separately for normalisation.
        return extraction, {
            "model": used_model,
            "provider": used_provider,
            "effort": EXTRACTION_EFFORT if used_provider == "anthropic" else "n/a",
            "latency_ms": latency_ms,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "invoice_date_iso": data.get("invoice_date_iso") or None,
            "source": "live",
            # Present only when the first choice did not answer, so the run view
            # can say the chain was walked rather than silently showing a
            # provider the operator did not pick.
            "failover_from": [a["provider"] for a in attempts] or None,
            "attempts": attempts or None,
        }

    exhausted = AllProvidersExhausted(attempts)
    if mode == "auto":
        log.warning("%s - falling back to fixture", exhausted)
        extraction, telemetry = _extract_from_fixture(pdf_bytes)
        telemetry["fallback_from_error"] = str(exhausted)
        telemetry["attempts"] = attempts
        return extraction, telemetry
    raise exhausted
