"""Stage 2, Gemini provider: one Gemini call turns a PDF into JSON.

This is a second implementation of the *same* seam the Anthropic path fills -
`call(pdf_b64, model) -> (parsed json, latency_ms, input_tokens, output_tokens)`.
Everything downstream of that tuple (`extract.parse_payload`, provenance
verification, the rules, the decision) is provider-agnostic and untouched.

Three things are worth reading the rationale for:

1. **One schema, converted, not copied.** The extraction schema is defined once
   in `extract.INVOICE_SCHEMA` as ordinary JSON Schema. Gemini's
   `responseSchema` takes an OpenAPI subset instead - a single `type` rather
   than `["string", "null"]`, an explicit `nullable` flag, and no
   `additionalProperties`. So `to_gemini_schema()` translates it at call time.
   Keeping a hand-written second copy of a 14-field financial schema is exactly
   the kind of drift this project refuses everywhere else.

2. **stdlib HTTP, no new dependency.** The REST endpoint is one POST with a
   JSON body. Pulling in `google-genai` to make that call would add an SDK (and
   an SDK's release churn) to a container that already has everything it needs.
   `urllib` keeps `requirements.txt` unchanged.

3. **Two credential shapes, chosen not guessed.** An API key authenticates with
   the `x-goog-api-key` header; a Google OAuth access token uses
   `Authorization: Bearer`. The obvious move is to sniff the prefix, but that is
   wrong: AI Studio issues keys shaped like `AQ.Ab8...` that look exactly like
   OAuth tokens and are rejected as them. So the API key is the default and
   OAuth is opt-in via `GEMINI_AUTH_MODE`, and the 401 path names which header
   it actually used instead of leaving you to guess.
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

from .config import (
    GEMINI_API_BASE,
    GEMINI_API_KEY,
    GEMINI_AUTH_MODE,
    GEMINI_PROJECT,
    GEMINI_THINKING_BUDGET,
    REQUEST_TIMEOUT_S,
)

log = logging.getLogger("extract.gemini")


class GeminiError(RuntimeError):
    """A Gemini call failed in a way that is not worth retrying."""


class GeminiRetryableError(GeminiError):
    """Transient: a timeout, a rate limit, or a 5xx."""


# --------------------------------------------------------------------------- #
# Schema translation: JSON Schema -> Gemini's OpenAPI subset
# --------------------------------------------------------------------------- #

_TYPE_NAMES = {
    "string": "STRING", "number": "NUMBER", "integer": "INTEGER",
    "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT",
}


def to_gemini_schema(node: Any) -> Any:
    """Convert one JSON Schema node to a Gemini `Schema`.

    The differences that matter for our schema:
      - `"type": ["number", "null"]`  ->  `"type": "NUMBER", "nullable": true`
      - `additionalProperties`        ->  unsupported, dropped
      - property order is not implied ->  stated explicitly via propertyOrdering,
                                          so repeated runs serialise alike
    """
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}

    declared = node.get("type")
    nullable = False
    if isinstance(declared, list):
        concrete = [t for t in declared if t != "null"]
        nullable = "null" in declared
        declared = concrete[0] if concrete else "string"
    if isinstance(declared, str):
        out["type"] = _TYPE_NAMES.get(declared, declared.upper())
    if nullable:
        out["nullable"] = True

    if "description" in node:
        out["description"] = node["description"]
    if "enum" in node:
        # Gemini wants enum values as strings alongside type STRING.
        out["enum"] = [str(v) for v in node["enum"]]

    if "properties" in node:
        props = node["properties"]
        out["properties"] = {k: to_gemini_schema(v) for k, v in props.items()}
        # Explicit ordering is advisory but it makes output stable, which is
        # worth having on a field set this wide.
        out["propertyOrdering"] = list(props.keys())
    if "required" in node:
        out["required"] = list(node["required"])
    if "items" in node:
        out["items"] = to_gemini_schema(node["items"])

    return out


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #

def _auth_headers(api_key: str = "") -> dict[str, str]:
    """Pick the header that matches the credential we were given.

    An API key goes in `x-goog-api-key`; an OAuth access token goes in
    `Authorization: Bearer` and also wants a billing project, which
    `GEMINI_PROJECT` supplies.

    API key is the default because the prefix is not a reliable discriminator:
    AI Studio issues both `AIza...` and `AQ.Ab8...` style keys, and the latter
    looks exactly like an OAuth token but is rejected as one (verified against
    the live endpoint - bearer gives 401, `x-goog-api-key` gives 200). So the
    OAuth path is opt-in via `GEMINI_AUTH_MODE=oauth` rather than guessed.
    """
    api_key = api_key or GEMINI_API_KEY
    if not api_key:
        raise GeminiError(
            "GEMINI_API_KEY is not set - copy .env.example to .env and add a key "
            "(or set EXTRACTION_PROVIDER=anthropic)."
        )
    headers = {"Content-Type": "application/json"}
    if GEMINI_AUTH_MODE == "oauth":
        headers["Authorization"] = f"Bearer {api_key}"
        if GEMINI_PROJECT:
            headers["x-goog-user-project"] = GEMINI_PROJECT
    else:
        headers["x-goog-api-key"] = api_key
    return headers


def _post(url: str, body: dict, api_key: str = "") -> dict:
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers=_auth_headers(api_key),
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        if exc.code in (408, 429, 500, 502, 503, 504):
            raise GeminiRetryableError(f"Gemini HTTP {exc.code}: {detail}") from exc
        if exc.code in (401, 403):
            raise GeminiError(
                f"Gemini rejected the credential (HTTP {exc.code}): {detail}\n"
                f"It was sent as "
                f"{'an OAuth bearer token' if GEMINI_AUTH_MODE == 'oauth' else 'an API key'}. "
                "If the key itself is right, check that the Generative Language API "
                "is enabled for its project. If you meant to authenticate with a "
                "short-lived OAuth token instead, set GEMINI_AUTH_MODE=oauth - note "
                "those expire about an hour after they are issued."
            ) from exc
        raise GeminiError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GeminiRetryableError(f"Gemini request failed to complete: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GeminiRetryableError(f"Gemini returned a non-JSON envelope: {exc}") from exc


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type(GeminiRetryableError),
    reraise=True,
)
def call(
    pdf_b64: str, model: str, *, schema: dict, system_prompt: str, user_prompt: str,
    api_key: str = '',
) -> tuple[dict, int, int, int]:
    """One request. Returns (parsed json, latency_ms, input_tokens, output_tokens)."""
    generation: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseSchema": to_gemini_schema(schema),
        "maxOutputTokens": 16000,
        # Extraction should be repeatable. The decision downstream is a pure
        # function of these fields, so sampling here would be the one place
        # non-determinism could leak into a payment decision.
        "temperature": 0,
    }
    if GEMINI_THINKING_BUDGET is not None:
        generation["thinkingConfig"] = {"thinkingBudget": GEMINI_THINKING_BUDGET}

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
                {"text": user_prompt},
            ],
        }],
        "generationConfig": generation,
    }

    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    t0 = time.perf_counter()
    payload = _post(url, body, api_key)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # A safety filter can block the prompt outright, with HTTP 200 and no
    # candidate at all. Check that before indexing into candidates.
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiError(
            f"Gemini declined to process this document "
            f"(blockReason: {feedback['blockReason']})."
        )

    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiRetryableError("Gemini returned no candidates.")
    candidate = candidates[0]

    finish = candidate.get("finishReason")
    if finish == "MAX_TOKENS":
        raise GeminiError("Extraction was truncated at maxOutputTokens.")
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
        raise GeminiError(
            f"Gemini declined to process this document (finishReason: {finish})."
        )

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
    if not text.strip():
        raise GeminiRetryableError("Gemini returned no text part to parse.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # responseSchema is supposed to guarantee this parses, so a failure here
        # is unusual enough to be worth one retry rather than failing the run.
        log.error("gemini returned unparseable JSON: %s | %s", exc, text[:400])
        raise GeminiRetryableError(f"Gemini returned invalid JSON: {exc}") from exc

    usage = payload.get("usageMetadata") or {}
    return (
        data,
        latency_ms,
        int(usage.get("promptTokenCount") or 0),
        int(usage.get("candidatesTokenCount") or 0),
    )
