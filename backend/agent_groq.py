"""The review agent's tool loop on Groq (OpenAI-compatible tool calling).

Groq is the better fit for this particular job even though Gemini is the
extraction default: the loop makes several sequential round trips, and Groq
answers in a few seconds where Gemini takes tens. Latency compounds in a loop
in a way it does not in a single extraction call.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .agent import coerce_arguments, parse_output
from .config import GROQ_API_BASE
from .groq import _post

log = logging.getLogger("agent.groq")


def loop(
    *,
    model: str,
    system_prompt: str,
    brief: str,
    tool_schemas: list[dict],
    dispatch: Callable[[str, dict], dict],
    max_turns: int,
) -> dict:
    tools = [{"type": "function", "function": schema} for schema in tool_schemas]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "A flagged invoice needs preparing for review. Here is what the rule "
                "engine recorded.\n\n"
                f"{brief}\n\n"
                "Gather what bears on the failed checks, then reply with the JSON object."
            ),
        },
    ]

    transcript: list[dict] = []
    tokens_in = tokens_out = 0

    for turn in range(max_turns):
        payload = _post(f"{GROQ_API_BASE}/chat/completions", {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_completion_tokens": 4000,
        })

        usage = payload.get("usage") or {}
        tokens_in += int(usage.get("prompt_tokens") or 0)
        tokens_out += int(usage.get("completion_tokens") or 0)

        choices = payload.get("choices") or []
        if not choices:
            break
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []

        if not calls:
            # No more tools wanted: this is the answer.
            result = parse_output(message.get("content") or "")
            result.update({
                "transcript": transcript,
                "turns": turn + 1,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "stopped": "complete",
            })
            return result

        # Echo the assistant turn back verbatim - the API requires the tool_calls
        # it issued to be present before the matching tool results.
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        })

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = coerce_arguments(fn.get("arguments"))
            output = dispatch(name, args)
            transcript.append({"tool": name, "arguments": args, "result": output})
            log.info("tool %s(%s)", name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": name,
                "content": json.dumps(output, default=str)[:12000],
            })

    # Out of turns. Hand back the tool results anyway - a reviewer can still use
    # them, and saying so is better than pretending the agent finished.
    return {
        "summary": (
            "The agent reached its tool-call limit before writing a conclusion. "
            "What it gathered is below."
        ),
        "findings": [],
        "recommended_action": "",
        "vendor_email": {"send": False, "subject": "", "body": ""},
        "open_questions": [],
        "transcript": transcript,
        "turns": max_turns,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "stopped": "max_turns",
    }
