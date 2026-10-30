"""Sanity check the generated fixtures: which ones have a usable text layer?

    python tools/probe_samples.py

Expects eight digital PDFs classified `text` and exactly one (the Kaveri
freight bill) classified `scanned` with no extractable characters. If that one
ever starts reporting text, the scanned edge case has quietly stopped testing
anything and the generator needs looking at.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import SAMPLE_INVOICE_DIR          # noqa: E402
from backend.provenance import probe_pdf                # noqa: E402


def main() -> int:
    files = sorted(SAMPLE_INVOICE_DIR.glob("*.pdf"))
    if not files:
        print("no sample PDFs found - run: node tools/invoice_gen/generate.js")
        return 2

    scanned = 0
    for path in files:
        probe = probe_pdf(path.read_bytes())
        scanned += probe.kind == "scanned"
        print(f"{probe.kind:8} pages={probe.page_count} chars={probe.total_chars:6}  {path.name}")

    print(f"\n{len(files)} file(s): {len(files) - scanned} with a text layer, {scanned} image-only")
    if scanned != 1:
        print(f"WARNING expected exactly 1 image-only fixture, found {scanned}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
