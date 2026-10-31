"""Runtime config loaded from environment / .env file.

Everything a reviewer might want to change without touching code lives here or
in `rules.py`'s RuleConfig. Nothing about the decision logic is hidden deeper
than those two places.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Which model provider runs stage 2. The rest of the pipeline is unaffected:
# both providers fill the same seam and return the same schema.
EXTRACTION_PROVIDER = os.getenv("EXTRACTION_PROVIDER", "gemini").strip().lower()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Gemini. The credential is sent as an API key (`x-goog-api-key`) unless
# GEMINI_AUTH_MODE is "oauth", in which case it is sent as a bearer token.
# Sniffing the prefix does not work: AI Studio issues keys shaped like
# `AQ.Ab8...` that look like OAuth tokens but authenticate only as API keys.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_AUTH_MODE = os.getenv("GEMINI_AUTH_MODE", "api_key").strip().lower()
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta"
).strip().rstrip("/")
# Only needed when authenticating with an OAuth token rather than an API key.
GEMINI_PROJECT = os.getenv("GEMINI_PROJECT", "").strip()

# Gemini's equivalent of a reasoning-effort dial. Left unset by default: not
# every model accepts it, and an unsupported field is a 400 rather than a
# warning. -1 asks for dynamic thinking, 0 disables it where that is allowed.
_thinking = os.getenv("GEMINI_THINKING_BUDGET", "").strip()
GEMINI_THINKING_BUDGET: int | None = int(_thinking) if _thinking else None

# Groq. OpenAI-compatible endpoint; the models it serves are text-only, so that
# provider reads the PDF's text layer rather than the file. See `groq.py`.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_API_BASE = os.getenv(
    "GROQ_API_BASE", "https://api.groq.com/openai/v1"
).strip().rstrip("/")

# Per-provider defaults, so switching provider does not also require remembering
# to change the model name.
#
# The Gemini default is a flash model, which is a deliberate choice rather than
# a cost compromise: the `*-pro` models are not on the free tier at all (they
# return 429 with `limit: 0`, not a throttle), and `gemini-2.5-pro` now 404s for
# keys that did not already have access. A flash model is the one that actually
# runs on a fresh key, and it reads these invoices correctly - verified against
# all nine samples. Override with EXTRACTION_MODEL on a billed key.
_DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-3.5-flash",
    # gpt-oss-120b is the strongest instruction-follower Groq serves that also
    # honours a strict json_schema, which this extraction depends on.
    "groq": "openai/gpt-oss-120b",
}
EXTRACTION_MODEL = (
    os.getenv("EXTRACTION_MODEL", "").strip()
    or _DEFAULT_MODELS.get(EXTRACTION_PROVIDER, "gemini-3.5-flash")
)

# An explicit EXTRACTION_MODEL overrides the per-provider default, which is the
# right precedence but makes one mistake easy: pin a model, later switch
# provider, and you get the new provider pointed at the old provider's model
# name. That surfaces as a confusing 404 from the API rather than a config
# error, so say so at import time. A warning, not an exception - an unrecognised
# name may simply be a model newer than this table.
_EXPECTED_PREFIX = {"anthropic": "claude", "gemini": "gemini"}.get(EXTRACTION_PROVIDER)
MODEL_PROVIDER_MISMATCH = bool(
    _EXPECTED_PREFIX and not EXTRACTION_MODEL.startswith(_EXPECTED_PREFIX)
)
if MODEL_PROVIDER_MISMATCH:
    logging.getLogger("config").warning(
        "EXTRACTION_MODEL=%r does not look like an %s model - check that "
        "EXTRACTION_MODEL and EXTRACTION_PROVIDER agree.",
        EXTRACTION_MODEL, EXTRACTION_PROVIDER,
    )
EXTRACTION_EFFORT = os.getenv("EXTRACTION_EFFORT", "medium").strip()

# Ceiling on one extraction HTTP call. A big scanned PDF plus thinking can take
# a while; past this something is wrong and the run should fail rather than hang.
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_S", "180"))

# live    - always call the API (the real thing)
# offline - never call the API; use the bundled ground-truth fixtures. Only the
#           nine sample invoices are known, so an arbitrary upload fails loudly
#           rather than silently inventing data.
# auto     - prefer live, fall back to a fixture if the call fails. Useful
#           insurance for a live demo; the UI always says which path ran.
EXTRACTION_MODE = os.getenv("EXTRACTION_MODE", "live").strip().lower()

DB_PATH = Path(os.getenv("DB_PATH", "data/invoices.db"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH

DATA_DIR = ROOT / "data"
FIXTURES_PATH = DATA_DIR / "fixtures.json"
UPLOAD_DIR = DATA_DIR / "uploads"
SAMPLE_INVOICE_DIR = DATA_DIR / "invoices"
FRONTEND_DIR = ROOT / "frontend"

PORT = int(os.getenv("PORT", "8000"))

# Artificial per-stage pacing so the live run view is watchable. This is
# presentation, not work - it is surfaced in the Admin tab and in the README so
# nobody mistakes it for pipeline latency.
STAGE_DELAY_MS = int(os.getenv("STAGE_DELAY_MS", "350"))

# Published list price per million tokens, for the Admin cost estimate.
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Gemini list prices for prompts under the 200k-token tier, which every
    # single-page invoice is comfortably inside.
    #
    # The 2.5-series rates are published list prices. The 3.x rates are NOT
    # confirmed - they are the flash-tier rate carried forward so the Admin cost
    # tile shows an order of magnitude rather than nothing. Treat any 3.x cost
    # figure in this app as indicative until these are checked against the
    # current pricing page.
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),          # unconfirmed
    "gemini-3.1-pro-preview": (1.25, 10.00),   # unconfirmed
    # Groq list prices.
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "qwen/qwen3.8-27b": (0.15, 0.75),          # unconfirmed
}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_INVOICE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# The live provider choice
#
# EXTRACTION_PROVIDER above is the *startup* value. The UI can switch provider
# at runtime, so the current choice lives here rather than in a module constant
# that callers would capture at import time and never see change. Everything
# that dispatches a model call reads `active_provider()`.
#
# This is process-local and deliberately not persisted: it is a demo control
# for showing that the provider is a detail, not a deployment setting. A
# restart returns to whatever the environment says.
# --------------------------------------------------------------------------- #

PROVIDERS = ("gemini", "groq", "anthropic")

_active_provider = EXTRACTION_PROVIDER if EXTRACTION_PROVIDER in PROVIDERS else "gemini"


def active_provider() -> str:
    return _active_provider


def model_for(provider: str) -> str:
    """The model to use for a given provider.

    An explicit EXTRACTION_MODEL pins the model only for the provider it
    actually belongs to. Without that check, switching provider would carry the
    previous provider's model name across and produce a confusing 404 from an
    API that has never heard of it.
    """
    pinned = os.getenv("EXTRACTION_MODEL", "").strip()
    if pinned:
        prefix = {"anthropic": "claude", "gemini": "gemini"}.get(provider)
        # Groq model ids are vendor-prefixed (openai/..., qwen/...), so there is
        # no single prefix to test; accept any pin for it.
        if prefix is None or pinned.startswith(prefix):
            return pinned
    return _DEFAULT_MODELS.get(provider, "gemini-3.5-flash")


def active_model() -> str:
    """The model for whichever provider is live."""
    return model_for(_active_provider)


def set_active_provider(name: str) -> str:
    global _active_provider
    name = (name or "").strip().lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r} - expected one of {', '.join(PROVIDERS)}")
    _active_provider = name
    return _active_provider


def provider_key_present(name: str) -> bool:
    return bool({
        "gemini": GEMINI_API_KEY,
        "groq": GROQ_API_KEY,
        "anthropic": ANTHROPIC_API_KEY,
    }.get(name, ""))


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens.

    An unlisted model falls back to the priciest tier of whichever family its
    name suggests, so a cost estimate for a model we have no rate card for errs
    high rather than flattering the system.
    """
    if model in MODEL_PRICING_USD_PER_MTOK:
        return MODEL_PRICING_USD_PER_MTOK[model]
    if model.startswith("gemini"):
        return (1.25, 10.00)
    return (5.00, 25.00)
