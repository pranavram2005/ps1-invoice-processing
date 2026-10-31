"""A two-sentence plain-English summary of a decided invoice.

The rule engine already says precisely what happened - "R6: PO-4471 already has
500,000.00 billed against it; this invoice would take consumption to
1,100,030.00 against a PO value of 1,000,000.00". That is exact, and it is also
the kind of sentence you have to read twice. This module asks a model to say
the same thing the way a colleague would say it out loud.

It decides nothing. It is handed the verdict and the failed rules *after* they
are settled and cannot change either. If the model is unavailable the caller
falls back to a deterministic summary composed from the same rule output, which
is why `fallback()` exists and why the API never hard-fails on this: an
explanation is a convenience, a decision is not.

Groq is preferred regardless of the extraction provider, because this runs
while someone is looking at the screen and Groq answers in two or three seconds
where Gemini takes tens.
"""
from __future__ import annotations

import json
import logging

from .config import GROQ_API_KEY, active_provider, model_for
from .models import RunResult

log = logging.getLogger("explain")

SYSTEM_PROMPT = """You explain the outcome of an automated invoice check to an
accounts-payable clerk who has not read the rule output.

You are told the verdict and exactly which checks failed. The verdict is already
final - you are not reviewing it, questioning it, or deciding anything. Explain
what it means.

Write exactly two sentences:

1. What is wrong with this invoice, in concrete terms, naming the actual figures
   involved. Use plain words, not rule names: say "the purchase order has
   already been billed up to its limit", not "R6 failed".
2. What happens next, or what a person needs to check.

Rules:
- Use the real numbers from the input. Never invent one.
- Indian rupee amounts are lakh-grouped; write them as they are given.
- Never say the invoice should be paid, approved or released - the system has
  already decided and you are only describing.
- No preamble, no rule ids, no bullet points. Two sentences of prose.

Reply as JSON: {"summary": "..."}"""


def _brief(run: RunResult) -> str:
    inv = run.normalized
    failed = [r for r in run.rules if not r.passed and not r.skipped]
    lines = [
        f"Verdict: {run.decision.decision}",
        f"Vendor: {inv.vendor_name_raw if inv else 'unknown'}",
        f"Invoice number: {inv.invoice_number if inv else 'not printed'}",
        f"Amount: {inv.currency if inv else 'INR'} {inv.total_amount if inv else 'unknown'}",
        f"Purchase order matched: {run.po.po_number if run.po and run.po.matched else 'none'}",
        "",
        "Checks that failed:",
    ]
    for r in failed:
        lines.append(f"- {r.title}: {r.message} (expected {r.expected}; found {r.actual})")
    if not failed:
        lines.append("- none; every check passed")
    return "\n".join(lines)


def fallback(run: RunResult) -> dict:
    """A summary composed from the rule output, with no model involved."""
    failed = [r for r in run.rules if not r.passed and not r.skipped]
    verdict = run.decision.decision if run.decision else "UNKNOWN"
    if not failed:
        text = "Every check passed, so no one needs to open this invoice."
    elif len(failed) == 1:
        text = failed[0].message
    else:
        text = (
            f"{len(failed)} checks failed. "
            + " ".join(r.message for r in failed[:2])
        )
    return {"summary": text, "source": "rules", "verdict": verdict}


def summarise(run: RunResult) -> dict:
    """Ask a model for the two-sentence version; fall back to the rules."""
    provider = "groq" if GROQ_API_KEY else active_provider()
    model = model_for(provider)

    try:
        if provider == "groq":
            from .groq import _post
            from .config import GROQ_API_BASE
            payload = _post(f"{GROQ_API_BASE}/chat/completions", {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _brief(run)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                # gpt-oss is a reasoning model: it spends completion tokens
                # thinking before it emits anything. At 400 it hit the ceiling
                # mid-thought and Groq returned a json_validate_failed 400, so
                # give it room and ask it not to deliberate - this is a
                # two-sentence rewrite, not a hard problem.
                "reasoning_effort": "low",
                "max_completion_tokens": 2000,
            })
            choices = payload.get("choices") or []
            text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        else:
            from .gemini import _post
            from .config import GEMINI_API_BASE
            payload = _post(f"{GEMINI_API_BASE}/models/{model}:generateContent", {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": _brief(run)}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                    "maxOutputTokens": 400,
                },
            })
            candidates = payload.get("candidates") or []
            parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
            text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))

        data = json.loads(text)
        summary = str(data.get("summary") or "").strip()
        if not summary:
            raise ValueError("empty summary")
        return {
            "summary": summary,
            "source": "model",
            "provider": provider,
            "model": model,
            "verdict": run.decision.decision if run.decision else None,
        }
    except Exception as exc:
        log.info("explain falling back to rule text: %s", exc)
        return fallback(run)
