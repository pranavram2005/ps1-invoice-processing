"""The resolution agent: what happens to an invoice after it is flagged.

A flag is not an answer. It means "a person has to look at this", and in a real
AP team that person then spends twenty minutes doing the same three things:
pulling up the purchase order's history, checking what this vendor has billed
before, and writing the vendor an email asking for whatever is missing. That is
the work this agent does, so the human starts from a prepared case instead of a
blank screen.

**What this agent is not allowed to do.**

It does not decide anything. The nine rules in `rules.py` produced the verdict
before this module is ever called, and nothing here can change it - there is no
tool that writes, books consumption, re-runs the pipeline or alters a decision.
Every tool below is a read. The agent's output is a *recommendation plus a
draft*, both clearly attributed to a model in the UI, and a human either acts
on them or does not.

That boundary is the same one the whole system is built on, applied one level
up. The model is good at gathering scattered context and writing a decent first
draft of an email. It is not good at being accountable for a payment, and an
agent that could approve its own findings would hand back exactly the
unauditable "the model said so" that `DECISIONS.md` §1 refuses.

**Why an agent here and not a single call.** The questions worth asking depend
on why the invoice flagged. A duplicate needs the original submission pulled up
and compared; a tolerance breach needs the PO's billing history; a missing PO
needs the vendor's recent invoices to guess which PO was intended. A fixed
prompt would have to fetch everything every time and mostly waste it. Letting
the model choose which of four read-only tools to call, and how many times, is
the part that genuinely benefits from a loop - and it is bounded, because there
are four tools and a hard cap on turns.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import db
from .config import active_provider, model_for
from .models import RunResult

log = logging.getLogger("agent")

# A flagged invoice needs a handful of lookups, not an open-ended session. If
# the model has not finished after this many tool rounds it is looping, and the
# right thing is to stop and hand over what it has.
MAX_TURNS = 6


class AgentError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Tools - every one a read. None of them can change a decision or the ledger.
# --------------------------------------------------------------------------- #

def tool_po_history(po_number: str) -> dict:
    """The PO, what has been billed against it, and by which invoices."""
    po = db.get_po(po_number)
    if po is None:
        return {"error": f"No purchase order {po_number} exists on the master."}
    return {
        "po_number": po.get("po_number"),
        "vendor_name": po.get("vendor_name"),
        "description": po.get("description"),
        "po_value": po.get("po_value"),
        "already_billed": po.get("already_billed"),
        "remaining": po.get("remaining"),
        "status": po.get("status"),
        "expiry_date": po.get("expiry_date"),
        "billed_by": db.po_consumption_detail(po_number),
    }


def tool_vendor_history(vendor_name: str, limit: int = 12) -> dict:
    """Recent invoices from a vendor, with how each one was decided."""
    needle = (vendor_name or "").strip().lower()
    rows = []
    for run in db.list_runs(limit=300):
        name = (run.vendor_name or "").lower()
        if needle and needle not in name and name not in needle:
            continue
        rows.append({
            "run_id": run.run_id,
            "filename": run.filename,
            "invoice_number": run.invoice_number,
            "invoice_date": run.invoice_date,
            "total_amount": run.total_amount,
            "matched_po": run.matched_po,
            "decision": run.decision,
            "failed_rules": list(run.failed_rules or []),
        })
        if len(rows) >= limit:
            break
    return {"vendor": vendor_name, "invoice_count": len(rows), "invoices": rows}


def tool_get_run(run_id: str) -> dict:
    """Another run's record - used to compare against a suspected duplicate."""
    run = db.get_run(run_id)
    if run is None:
        return {"error": f"No run {run_id}."}
    inv = run.normalized
    return {
        "run_id": run.run_id,
        "filename": run.filename,
        "decision": run.decision.decision if run.decision else None,
        "vendor_name": inv.vendor_name_raw if inv else None,
        "invoice_number": inv.invoice_number if inv else None,
        "invoice_date": inv.invoice_date if inv else None,
        "total_amount": inv.total_amount if inv else None,
        "po_reference": inv.po_reference if inv else None,
        "line_items": [i.model_dump() for i in (inv.line_items if inv else [])],
    }


def tool_open_purchase_orders(vendor_name: str) -> dict:
    """Open POs for a vendor - for an invoice that named no PO, or a wrong one."""
    needle = (vendor_name or "").strip().lower()
    out = []
    for po in db.list_pos():
        name = (po.get("vendor_name") or "").lower()
        if needle and needle not in name and name not in needle:
            continue
        out.append({
            "po_number": po.get("po_number"),
            "description": po.get("description"),
            "po_value": po.get("po_value"),
            "already_billed": po.get("already_billed"),
            "remaining": po.get("remaining"),
            "status": po.get("status"),
        })
    return {"vendor": vendor_name, "open_pos": out}


TOOLS: dict[str, Callable[..., dict]] = {
    "po_history": tool_po_history,
    "vendor_history": tool_vendor_history,
    "get_run": tool_get_run,
    "open_purchase_orders": tool_open_purchase_orders,
}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "po_history",
        "description": (
            "Look up a purchase order: its value, how much has already been billed "
            "against it, how much remains, and which invoices consumed it. Use this "
            "whenever the flag involves a PO - a tolerance breach, cumulative "
            "over-consumption, or a PO that may be the wrong one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "po_number": {"type": "string", "description": "e.g. PO-4471"},
            },
            "required": ["po_number"],
        },
    },
    {
        "name": "vendor_history",
        "description": (
            "Recent invoices from this vendor and how each was decided. Use it to "
            "judge whether a problem is a one-off or a pattern, and to see what this "
            "vendor normally bills."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vendor_name": {"type": "string"},
                "limit": {"type": "integer", "description": "Default 12."},
            },
            "required": ["vendor_name"],
        },
    },
    {
        "name": "get_run",
        "description": (
            "Fetch another processed invoice by run id, including its line items. "
            "Use this on a suspected duplicate to compare the two documents field by "
            "field and say what actually differs."
        ),
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "open_purchase_orders",
        "description": (
            "All open purchase orders for a vendor. Use it when an invoice named no "
            "PO, or named one that does not fit, to identify which PO it most likely "
            "belongs against."
        ),
        "parameters": {
            "type": "object",
            "properties": {"vendor_name": {"type": "string"}},
            "required": ["vendor_name"],
        },
    },
]


SYSTEM_PROMPT = """You prepare flagged supplier invoices for a human reviewer in an
accounts-payable team. An automated rule engine has already decided this invoice
needs human attention, and has told you exactly which checks failed and why.

Your job is to save the reviewer the twenty minutes of lookups they would
otherwise do, and to draft the email they would otherwise write.

You do not decide anything. The verdict is already made and you cannot change
it. Never state or imply that the invoice should be paid, approved, rejected or
released - say what you found and what the reviewer's options are, and let them
choose.

How to work:

- Use the tools to gather the context that actually bears on the failed checks.
  A duplicate flag needs the original submission fetched and compared field by
  field. A PO tolerance breach needs that PO's billing history. A missing PO
  reference needs the vendor's open POs. Do not fetch things that have no
  bearing on why this invoice flagged.
- Ground every claim in a tool result or in the rule output you were given. If
  you could not establish something, say so plainly. Never invent an invoice
  number, an amount, a date or a PO.
- Indian invoices: amounts are in rupees and lakh-grouped, dates are day-first.

When you have what you need, stop calling tools and reply with a JSON object:

{
  "summary": "2-3 sentences: what is wrong, and what you established by looking.",
  "findings": ["each a specific, checkable statement grounded in a tool result"],
  "recommended_action": "the single concrete next step you would suggest to the reviewer",
  "vendor_email": {
    "send": true or false,
    "subject": "...",
    "body": "..."
  },
  "open_questions": ["anything a human must confirm that you could not"]
}

The email is a draft for a human to approve, edit or discard - never sent
automatically. Write it as a professional AP clerk would: short, specific,
naming the invoice number and the exact discrepancy, asking for one clear thing.
Set "send" to false when no vendor contact is warranted (for example when the
problem is internal, such as a scan quality issue or a missing PO on our side),
and leave subject and body empty strings in that case."""


def build_brief(run: RunResult) -> str:
    """What the agent is told about the invoice, drawn from the run record."""
    inv = run.normalized
    failed = [r for r in run.rules if not r.passed and not r.skipped]
    lines = [
        f"Run: {run.run_id}",
        f"File: {run.filename}",
        f"Verdict: {run.decision.decision if run.decision else 'unknown'}"
        f" (extraction confidence {run.decision.confidence:.2f})" if run.decision else "",
        "",
        "Invoice as extracted:",
        f"  vendor:         {inv.vendor_name_raw if inv else '—'}",
        f"  invoice number: {inv.invoice_number if inv else '—'}",
        f"  invoice date:   {inv.invoice_date if inv else '—'}",
        f"  currency:       {inv.currency if inv else '—'}",
        f"  total:          {inv.total_amount if inv else '—'}",
        f"  PO referenced:  {inv.po_reference if inv else '—'}",
    ]
    if run.vendor:
        lines.append(
            f"  vendor master:  {run.vendor.status} "
            f"({run.vendor.vendor_name or 'no match'}, score {run.vendor.score:.2f})"
        )
    if run.po:
        lines.append(
            f"  PO matched:     {run.po.po_number or 'none'} "
            f"(score {run.po.score:.2f}, {run.po.strategy})"
        )
    if run.duplicate_of:
        lines.append(f"  possible duplicate of run: {run.duplicate_of}")

    lines += ["", "Checks that failed:"]
    for r in failed:
        lines.append(f"  [{r.rule_id}] {r.title} - severity {r.severity}")
        lines.append(f"      expected: {r.expected}")
        lines.append(f"      found:    {r.actual}")
        lines.append(f"      meaning:  {r.message}")
    if not failed:
        lines.append("  (none - this invoice passed every check)")
    return "\n".join(line for line in lines if line != "")


def run_agent(run: RunResult, *, provider: str | None = None) -> dict:
    """Gather context for a flagged invoice and draft the vendor email.

    Returns the agent's findings plus a transcript of every tool call it made,
    so a reviewer can see what it actually looked at rather than trusting the
    summary. Raises AgentError if the provider cannot run a tool loop.
    """
    provider = (provider or active_provider()).lower()
    model = model_for(provider)

    if provider == "groq":
        from .agent_groq import loop as provider_loop
    elif provider == "gemini":
        from .agent_gemini import loop as provider_loop
    else:
        raise AgentError(
            f"The review agent is not wired up for provider {provider!r}. "
            f"Switch to Groq or Gemini."
        )

    brief = build_brief(run)
    log.info("review agent starting for %s on %s/%s", run.run_id, provider, model)
    result = provider_loop(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        brief=brief,
        tool_schemas=TOOL_SCHEMAS,
        dispatch=dispatch_tool,
        max_turns=MAX_TURNS,
    )
    result["provider"] = provider
    result["model"] = model
    return result


def dispatch_tool(name: str, arguments: dict) -> dict:
    """Run one tool by name. Never raises into the loop - a tool error is a
    result the model should see and work around, not a crash."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"No such tool: {name}"}
    try:
        return fn(**(arguments or {}))
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:                       # pragma: no cover - defensive
        log.exception("tool %s failed", name)
        return {"error": f"{name} failed: {exc}"}


def parse_output(text: str) -> dict:
    """The agent's final JSON, tolerant of a fenced code block around it."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Better to hand back prose the reviewer can read than to fail the
        # whole thing on a formatting slip.
        return {
            "summary": cleaned[:1200],
            "findings": [],
            "recommended_action": "",
            "vendor_email": {"send": False, "subject": "", "body": ""},
            "open_questions": [],
            "unstructured": True,
        }
    if not isinstance(data, dict):
        return {"summary": str(data), "findings": [], "unstructured": True}
    email = data.get("vendor_email")
    if not isinstance(email, dict):
        data["vendor_email"] = {"send": False, "subject": "", "body": ""}
    return data


def coerce_arguments(raw: Any) -> dict:
    """Tool arguments arrive as a JSON string from some providers, a dict from
    others. Normalise before dispatch."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
