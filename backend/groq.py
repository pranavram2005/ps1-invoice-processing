"""Stage 2, Groq provider: the PDF's text layer -> schema-guaranteed JSON.

Groq fills the same seam as the other two providers and returns the same tuple,
but it reaches it differently, and the difference is worth stating plainly
rather than hiding behind the abstraction.

**Groq cannot read a PDF.** The models it serves (gpt-oss, qwen) are text-only:
there is no document block and no page image, so the file cannot be handed over
the way it is to Gemini or Claude. This provider therefore extracts the PDF's
own text layer with `pypdf` and sends that text.

Three consequences, all of them real:

1. **A scanned invoice cannot be processed here at all.** No text layer means
   nothing to send. That is not a bug to be smoothed over - it is the honest
   answer, and this module raises rather than submitting an empty prompt and
   letting a model invent an invoice. Gemini and Claude read the page image, so
   the scanned sample only works on those.
2. **Layout is lost.** `pypdf` yields a linear stream of text, so a figure's
   position on the page - which column, which row of the table it belonged to -
   is gone. The model has to infer structure from reading order. For these
   invoices that is usually enough; for a dense multi-column layout it is a
   real disadvantage versus a provider that sees the page.
3. **Provenance still verifies, and verifies against the same source.** Quotes
   are checked against the text layer by `provenance.py`, which is exactly what
   was sent. The check stays meaningful.

The API is OpenAI-compatible (`/openai/v1/chat/completions`), so this is one
POST with `response_format: json_schema`, and stdlib `urllib` again - no SDK.
Groq's edge sits behind Cloudflare, which rejects urllib's default User-Agent
with a 403 (code 1010), so one is set explicitly.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import GROQ_API_BASE, GROQ_API_KEY, REQUEST_TIMEOUT_S
from .provenance import probe_pdf

log = logging.getLogger("extract.groq")

# Cloudflare in front of api.groq.com 403s the default Python User-Agent.
USER_AGENT = "ps1-invoice-processing/1.0"


class GroqError(RuntimeError):
    """A Groq call failed in a way that is not worth retrying."""


class GroqRetryableError(GroqError):
    """Transient: a timeout, a rate limit, or a 5xx."""


class GroqUnreadableError(GroqError):
    """The document has no text layer, so this provider has nothing to read."""


def to_strict_schema(node: Any) -> Any:
    """Normalise our JSON Schema for an OpenAI-style `json_schema` response.

    Strict mode is fussy in two ways our schema does not satisfy as written:
    every object must set `additionalProperties: false`, and every property must
    appear in `required`. Union types (`["string", "null"]`) are fine here - a
    nullable field is expressed exactly that way - so unlike the Gemini
    translation this is a tightening rather than a conversion.
    """
    if not isinstance(node, dict):
        return node

    out = dict(node)
    if "properties" in out:
        out["properties"] = {k: to_strict_schema(v) for k, v in out["properties"].items()}
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    if "items" in out:
        out["items"] = to_strict_schema(out["items"])
    return out


def _post(url: str, body: dict, api_key: str = '') -> dict:
    api_key = api_key or GROQ_API_KEY
    if not api_key:
        raise GroqError(
            "GROQ_API_KEY is not set - add it to .env, or switch provider."
        )
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        if exc.code in (408, 429, 500, 502, 503, 504):
            raise GroqRetryableError(f"Groq HTTP {exc.code}: {detail}") from exc
        if exc.code in (401, 403):
            raise GroqError(
                f"Groq rejected the request (HTTP {exc.code}): {detail}\n"
                "A 403 with code 1010 is Cloudflare blocking the client rather "
                "than Groq rejecting the key."
            ) from exc
        raise GroqError(f"Groq HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GroqRetryableError(f"Groq request failed to complete: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GroqRetryableError(f"Groq returned a non-JSON envelope: {exc}") from exc


def pdf_to_text(pdf_bytes: bytes) -> str:
    """The PDF's text layer, page-tagged so the model can cite a page number.

    Raises if there is no text: a text-only model given an empty document would
    produce a confident, entirely invented invoice, which is the single worst
    outcome this system can have.
    """
    probe = probe_pdf(pdf_bytes)
    if not probe.has_text_layer or not probe.total_chars:
        raise GroqUnreadableError(
            "This PDF has no text layer, and Groq serves text-only models - there "
            "is nothing for them to read. A scanned or photographed invoice needs "
            "a provider that sees the page image: switch to Gemini or Claude. "
            "(Sending an empty prompt would get you an invented invoice, so this "
            "fails instead.)"
        )
    return "\n\n".join(
        f"--- page {i + 1} ---\n{text}" for i, text in enumerate(probe.pages_text)
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type(GroqRetryableError),
    reraise=True,
)
def call(
    pdf_b64: str, model: str, *, schema: dict, system_prompt: str, user_prompt: str,
    pdf_bytes: bytes | None = None,
    api_key: str = '',
) -> tuple[dict, int, int, int]:
    """One request. Returns (parsed json, latency_ms, input_tokens, output_tokens).

    Takes `pdf_b64` to match the other providers' signature and ignores it:
    what actually goes over the wire is the extracted text.
    """
    if pdf_bytes is None:
        import base64
        pdf_bytes = base64.standard_b64decode(pdf_b64)

    document_text = pdf_to_text(pdf_bytes)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"{user_prompt}\n\n"
                    "The document's text layer follows, with page markers. Quote from it "
                    "verbatim in the provenance fields.\n\n"
                    f"{document_text}"
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_extraction",
                "strict": True,
                "schema": to_strict_schema(schema),
            },
        },
        # Extraction must be repeatable: the decision downstream is a pure
        # function of these fields, so sampling here is the one place
        # non-determinism could reach a payment decision.
        "temperature": 0,
        "max_completion_tokens": 16000,
    }

    t0 = time.perf_counter()
    payload = _post(f"{GROQ_API_BASE}/chat/completions", body, api_key)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    choices = payload.get("choices") or []
    if not choices:
        raise GroqRetryableError("Groq returned no choices.")
    choice = choices[0]

    finish = choice.get("finish_reason")
    if finish == "length":
        raise GroqError("Extraction was truncated at max_completion_tokens.")
    if finish == "content_filter":
        raise GroqError("Groq declined to process this document (content filter).")

    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        raise GroqRetryableError("Groq returned an empty message.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("groq returned unparseable JSON: %s | %s", exc, text[:400])
        raise GroqRetryableError(f"Groq returned invalid JSON: {exc}") from exc

    usage = payload.get("usage") or {}
    return (
        data,
        latency_ms,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )
