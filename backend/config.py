"""Runtime config loaded from environment / .env file.

Everything a reviewer might want to change without touching code lives here or
in `rules.py`'s RuleConfig. Nothing about the decision logic is hidden deeper
than those two places.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "claude-opus-5").strip()
EXTRACTION_EFFORT = os.getenv("EXTRACTION_EFFORT", "medium").strip()

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
}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_INVOICE_DIR.mkdir(parents=True, exist_ok=True)


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens; falls back to Opus-tier rates."""
    return MODEL_PRICING_USD_PER_MTOK.get(model, (5.00, 25.00))
