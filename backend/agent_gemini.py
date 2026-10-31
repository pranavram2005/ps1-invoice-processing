"""The review agent's tool loop on Gemini (function calling).

Same loop as the Groq adapter, different wire format: Gemini carries the
conversation as `contents` with `functionCall` / `functionResponse` parts rather
than OpenAI-style tool messages, and takes the tool list as
`tools[].functionDeclarations`.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .agent import parse_output
from .config import GEMINI_API_BASE
from .gemini import _post, to_gemini_schema

log = logging.getLogger("agent.gemini")


def loop(
    *,
    model: str,
    system_prompt: str,
    brief: str,
    tool_schemas: list[dict],
    dispatch: Callable[[str, dict], dict],
    max_turns: int,
) -> dict:
    declarations = [
        {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": to_gemini_schema(schema["parameters"]),
        }
        for schema in tool_schemas
    ]

    contents: list[dict[str, Any]] = [{
        "role": "user",
        "parts": [{"text": (
            "A flagged invoice needs preparing for review. Here is what the rule "
            f"engine recorded.\n\n{brief}\n\n"
            "Gather what bears on the failed checks, then reply with the JSON object."
        )}],
    }]

    transcript: list[dict] = []
    tokens_in = tokens_out = 0
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"

    for turn in range(max_turns):
        payload = _post(url, {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "tools": [{"functionDeclarations": declarations}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000},
        })

        usage = payload.get("usageMetadata") or {}
        tokens_in += int(usage.get("promptTokenCount") or 0)
        tokens_out += int(usage.get("candidatesTokenCount") or 0)

        candidates = payload.get("candidates") or []
        if not candidates:
            break
        content = (candidates[0].get("content") or {})
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if isinstance(p.get("functionCall"), dict)]

        if not calls:
            text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
            result = parse_output(text)
            result.update({
                "transcript": transcript,
                "turns": turn + 1,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "stopped": "complete",
            })
            return result

        contents.append({"role": "model", "parts": parts})

        responses = []
        for call in calls:
            name = call.get("name") or ""
            args = call.get("args") or {}
            output = dispatch(name, args)
            transcript.append({"tool": name, "arguments": args, "result": output})
            log.info("tool %s(%s)", name, args)
            responses.append({
                "functionResponse": {"name": name, "response": {"result": output}},
            })
        contents.append({"role": "user", "parts": responses})

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
