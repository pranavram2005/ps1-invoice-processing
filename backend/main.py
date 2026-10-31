"""FastAPI entrypoint: JSON API, SSE run stream, and the static frontend.

One container serves the API and the UI from the same origin. That is a
deployment decision, not laziness: a single service means one URL, no CORS
story, and nothing to go wrong during a live demo.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import config as config_mod, db, pipeline, rules, seed, state
from .config import (
    EXTRACTION_EFFORT,
    EXTRACTION_MODE,
    FRONTEND_DIR,
    PROVIDERS,
    SAMPLE_INVOICE_DIR,
    STAGE_DELAY_MS,
    active_model,
    active_provider,
    provider_key_present,
)
from .events import broker
from .models import PROVIDER_LABELS, RunResult, RunSummary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
)
log = logging.getLogger("api")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # the API accepts 32 MB; stay well inside it

app = FastAPI(title="PS-1 Invoice Processing", version="1.0.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Hold references to in-flight pipeline tasks so they are not garbage collected
# mid-run (asyncio only keeps weak references to tasks).
_tasks: set[asyncio.Task] = set()


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    seed.seed_masters_if_empty()
    state.load_from_db()
    log.info(
        "ready - %d vendors, %d POs, %d runs, model=%s",
        len(db.list_vendors()), len(db.list_pos()), db.count_runs(), active_model(),
    )


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #

def _provider_options() -> list[dict]:
    """Every provider the UI can offer, and whether it is usable right now.

    A provider with no key is listed but disabled rather than hidden: the point
    of the control is to show the seam exists, and silently dropping an option
    would just raise the question of where it went.
    """
    return [
        {
            "id": name,
            "label": PROVIDER_LABELS.get(name, name),
            "available": provider_key_present(name),
            "active": name == active_provider(),
            # Groq serves text-only models, so a scanned PDF has nothing for it
            # to read. The UI says so before someone picks it and is surprised.
            "reads_images": name != "groq",
        }
        for name in PROVIDERS
    ]


@app.post("/api/provider")
def set_provider(payload: dict = Body(...)) -> dict:
    """Switch which model provider stage 2 calls.

    Process-local and not persisted: this is a demo control that makes the
    provider seam visible, not a deployment setting. Runs already in the
    database keep whatever read them - the switch applies to the next run, and
    each run's stage 2 records the model that actually ran.
    """
    name = str(payload.get("provider", "")).strip().lower()
    try:
        active = config_mod.set_active_provider(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not provider_key_present(active):
        log.warning("switched to %s, which has no API key configured", active)
    log.info("extraction provider switched to %s (%s)", active, active_model())
    return {
        "provider": active,
        "model": active_model(),
        "providers": _provider_options(),
        "key_present": provider_key_present(active),
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": active_model(),
        "provider": active_provider(),
        "providers": _provider_options(),
        "effort": EXTRACTION_EFFORT,
        "extraction_mode": EXTRACTION_MODE,
        "api_key_present": provider_key_present(active_provider()),
        "runs": db.count_runs(),
        "vendors": len(db.list_vendors()),
        "purchase_orders": len(db.list_pos()),
        "stage_delay_ms": STAGE_DELAY_MS,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


@app.get("/api/rules")
def get_rules() -> dict:
    """The rule set as data, with the thresholds currently in force spliced in.

    The UI renders this verbatim, so the documented rules and the applied rules
    cannot drift apart.
    """
    return {
        "rules": rules.catalog_with_thresholds(state.current()),
        "config": state.current().to_dict(),
        "decision_logic": (
            "Worst severity wins. Any REJECT-severity failure rejects; otherwise any "
            "FLAG-severity failure flags; otherwise approve."
        ),
    }


@app.get("/api/config")
def get_config() -> dict:
    return {"config": state.current().to_dict(), "defaults": rules.RuleConfig().to_dict()}


class ConfigPatch(BaseModel):
    po_tolerance_pct: float | None = None
    po_over_tolerance_reject_pct: float | None = None
    duplicate_window_days: int | None = None
    confidence_floor: float | None = None
    arithmetic_tolerance: float | None = None
    stale_invoice_days: int | None = None
    po_match_min_score: float | None = None
    vendor_match_min_score: float | None = None


@app.patch("/api/config")
def patch_config(patch: ConfigPatch) -> dict:
    """Change a threshold at runtime. Re-run an invoice afterwards and the
    decision changes - which is the point: the rules are configuration, not
    hardcoded output."""
    config, changed = state.update(patch.model_dump(exclude_none=True))
    return {"config": config.to_dict(), "changed": changed}


@app.post("/api/config/reset")
def reset_config() -> dict:
    return {"config": state.reset().to_dict(), "changed": ["reset to defaults"]}


# --------------------------------------------------------------------------- #
# Procurement masters
# --------------------------------------------------------------------------- #

@app.get("/api/vendors")
def get_vendors() -> list[dict]:
    return db.list_vendors()


@app.get("/api/pos")
def get_pos() -> list[dict]:
    return db.list_pos()


@app.get("/api/pos/{po_number}")
def get_po(po_number: str) -> dict:
    po = db.get_po(po_number)
    if po is None:
        raise HTTPException(404, f"PO {po_number} not found")
    return {**po, "consumption": db.po_consumption_detail(po_number)}


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def _schedule(run_id: str, filename: str, data: bytes, *, replay: bool) -> None:
    """Register the run synchronously, then run the stages in the background.

    The two-step matters: `start()` writes the run row and its pending stages
    before this function returns, so the client that just got a run id can
    immediately fetch it or attach to its event stream without racing.
    """
    run = pipeline.start(run_id, filename, data, state.current(), replay=replay)
    task = asyncio.create_task(run.run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _validate_pdf(filename: str, data: bytes) -> None:
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"File is {len(data) / 1e6:.1f} MB; the limit is {MAX_UPLOAD_BYTES / 1e6:.0f} MB."
        )
    if not data.startswith(b"%PDF"):
        raise HTTPException(
            400,
            f"'{filename}' is not a PDF. This pipeline reads PDFs natively; "
            f"images and Office files are out of scope.",
        )


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...)) -> dict:
    """Accept an invoice PDF and start the pipeline.

    Returns immediately with a run id; the client then opens the SSE stream to
    watch the stages. The upload response deliberately does not wait for the
    decision - the whole point of the run view is that you see the work happen.
    """
    data = await file.read()
    filename = file.filename or "upload.pdf"
    _validate_pdf(filename, data)

    run_id = pipeline.new_run_id()
    _schedule(run_id, filename, data, replay=False)
    return {"run_id": run_id, "filename": filename, "bytes": len(data)}


@app.post("/api/runs/{run_id}/replay")
async def replay_run(run_id: str) -> dict:
    """Re-run an existing run from its stored PDF, reusing the same run id.

    This is how to demonstrate a threshold change: edit the PO tolerance, replay
    the same invoice, watch the decision flip. Because the run id is reused, the
    duplicate rule correctly treats it as a replay rather than a resubmission,
    and any PO consumption the previous attempt booked is reversed first.
    """
    existing = db.get_run(run_id)
    if existing is None:
        raise HTTPException(404, f"run {run_id} not found")
    path = pipeline.blob_path(existing.file_sha256)
    if not path.exists():
        raise HTTPException(410, "The stored PDF for this run is no longer on disk.")
    _schedule(run_id, existing.filename, path.read_bytes(), replay=True)
    return {"run_id": run_id, "filename": existing.filename, "replay": True}


@app.get("/api/runs")
def list_runs(decision: str | None = None) -> list[RunSummary]:
    runs = db.list_runs()
    if decision:
        wanted = decision.upper()
        runs = [r for r in runs if (r.decision or "").upper() == wanted]
    return runs


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> RunResult:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    if not db.delete_run(run_id):
        raise HTTPException(404, f"run {run_id} not found")
    return {"ok": True, "run_id": run_id}


@app.get("/api/runs/{run_id}/file")
def get_run_file(run_id: str, download: bool = False) -> FileResponse:
    """The stored PDF.

    Served **inline** by default so the UI can embed it beside the extracted
    fields. Passing `filename=` to FileResponse would set
    `Content-Disposition: attachment`, which makes a browser download the file
    rather than render it - fine for a download link, fatal for an <iframe>.
    `?download=1` asks for that behaviour explicitly.
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    path = pipeline.blob_path(run.file_sha256)
    if not path.exists():
        raise HTTPException(404, "stored PDF not found")
    if download:
        return FileResponse(str(path), media_type="application/pdf", filename=run.filename)
    safe = run.filename.replace('"', "")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe}"'},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events for the live run view.

    Subscribes *before* reading the snapshot so an event that fires in between
    is queued rather than lost. The client applies stage updates by sequence
    number, so a replayed-then-streamed duplicate is harmless.
    """
    async def generator():
        queue = broker.subscribe(run_id)
        try:
            run = db.get_run(run_id)
            if run is None:
                yield _sse("error", {"message": f"run {run_id} not found"})
                return

            yield _sse("snapshot", {
                "run_id": run_id,
                "filename": run.filename,
                "status": run.status,
                "stages": [s.model_dump() for s in run.stages],
                "decision": run.decision.model_dump() if run.decision else None,
            })

            if run.status != "running":
                yield _sse("done", {"run_id": run_id, "status": run.status})
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    if await request.is_disconnected():
                        return
                    continue

                yield _sse(event.get("type", "message"), event)
                if event.get("type") == "done":
                    return
        finally:
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tell nginx-style proxies (Render's included) not to buffer, or the
            # stage events arrive all at once at the end.
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Bundled sample invoices - one-click runs, so a demo never fumbles a file picker
# --------------------------------------------------------------------------- #

@app.get("/api/samples")
def list_samples() -> list[dict]:
    return seed.describe_samples()


@app.post("/api/samples/{name}/run")
async def run_sample(name: str) -> dict:
    safe = Path(name).name
    path = SAMPLE_INVOICE_DIR / safe
    if path.suffix.lower() != ".pdf" or not path.exists():
        raise HTTPException(404, f"sample '{safe}' not found")
    run_id = pipeline.new_run_id()
    _schedule(run_id, safe, path.read_bytes(), replay=False)
    return {"run_id": run_id, "filename": safe}


# --------------------------------------------------------------------------- #
# Dashboard + admin
# --------------------------------------------------------------------------- #

@app.get("/api/stats")
def stats() -> dict:
    return db.dashboard_stats()


@app.get("/api/admin/telemetry")
def telemetry() -> dict:
    return {
        **db.telemetry_summary(),
        "model": active_model(),
        "effort": EXTRACTION_EFFORT,
        "stage_delay_ms": STAGE_DELAY_MS,
        "stage_delay_note": (
            "Artificial per-stage pacing so the live run view is watchable. It is added "
            "latency, not work - set STAGE_DELAY_MS=0 for true pipeline timings."
        ),
    }


class ReseedRequest(BaseModel):
    clear_runs: bool = False


@app.post("/api/admin/reseed")
def reseed(req: ReseedRequest) -> dict:
    """Restore the procurement masters to their seeded state.

    Useful between demo takes: approving an invoice books consumption against a
    PO, which changes what the next run of the same invoice decides.
    """
    summary = seed.reseed(clear_runs=req.clear_runs)
    return summary


# --------------------------------------------------------------------------- #
# Static frontend - mounted last so /api routes win
# --------------------------------------------------------------------------- #

if FRONTEND_DIR.exists():

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        target = FRONTEND_DIR / path
        if target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
