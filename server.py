"""Web server for the computer-use agent demo.

GET  /                              the demo page (index.html); ?embed=1 hides the page header
GET  /api/presets                   the preset tasks
POST /api/runs {"preset"|"prompt"}  starts a live run (or replays a preset's recording); {"id", ...}
POST /api/runs/{id}/stop            stops a live run
GET  /api/runs/{id}/events          server-sent events: steps, actions, the agent's notes, the result
GET  /api/runs/{id}/frame?after=N   the latest desktop frame (JPEG) if newer than N, else 204
GET  /api/runs/{id}/document        the document the agent saved
GET  /api/runs/{id}/preview.pdf     that document rendered to PDF
GET  /api/runs/{id}/preview.png     page one of that PDF

Each live run takes a booted desktop sandbox from a warm pool (or boots one), streams its
screen to the viewer, and runs the agent loop (agent.py) in a worker thread. The sandbox has no
network and no credentials; this server holds the model gateway key. A finished preset run is
recorded (events + frames + document) on a Chalk volume and replayed when the live slots are
busy or the hourly quota is used up.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anthropic
from chalkcompute import Sandbox, Volume, VolumeFileNotFoundError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from agent import DOCUMENTS, AgentConfig, run_agent
from desktop_sandbox import DISPLAY_ENV, boot_desktop, make_desktop_sandbox

PORT = int(os.environ.get("PORT", "8080"))
INDEX_HTML = Path(__file__).with_name("index.html")
MODEL = os.environ.get("AGENT_MODEL", "anthropic/claude-opus-5-5")
EFFORT = os.environ.get("AGENT_EFFORT", "medium")
MAX_CONCURRENT = int(os.environ.get("CU_MAX_CONCURRENT", "2"))
MAX_LIVE_PER_HOUR = int(os.environ.get("CU_MAX_PER_HOUR", "20"))
MAX_PER_CLIENT_PER_HOUR = int(os.environ.get("CU_MAX_PER_CLIENT_PER_HOUR", "3"))
RUN_TIMEOUT_S = int(os.environ.get("CU_RUN_TIMEOUT", "420"))
MAX_TURNS = int(os.environ.get("CU_MAX_TURNS", "45"))
WARM_POOL_SIZE = int(os.environ.get("CU_WARM_POOL", "1"))
WARM_LIFETIME_S = 3600
FRAME_FPS = "3"
MAX_PROMPT_CHARS = 300
RECORDING_VOLUME = os.environ.get("CU_RECORDING_VOLUME", "computer-use-agent-recordings")
REPLAY_MAX_GAP_S = 1.2
POLL_S = 0.1
MAX_DOC_BYTES = 8 * 1024 * 1024
SANDBOX_PREFIX = "cu-desktop-"

PRESETS = [
    {
        "id": "budget",
        "label": "Budget spreadsheet",
        "prompt": "Make a monthly budget spreadsheet for a small bakery: rows for rent, flour and "
        + "ingredients, staff wages, utilities and marketing with realistic amounts, a bold Total "
        + "row that uses a SUM formula, and a bar chart of the expenses. Save it as budget.ods.",
    },
    {
        "id": "memo",
        "label": "Memo → PDF",
        "prompt": "Write a one-page memo from the operations lead to all staff announcing that the "
        + "office moves to 400 Harbor Street on March 3: a bold title, To/From/Date/Subject lines, "
        + "two short paragraphs and a three-item bulleted checklist. Save it as memo.odt and also "
        + "export it as memo.pdf.",
    },
    {
        "id": "invoice",
        "label": "Invoice with formulas",
        "prompt": "Create an invoice spreadsheet for Brightwater Design Studio billing a client for "
        + "four line items (description, hours, rate). Compute each line's amount with a formula, "
        + "a subtotal, 8% tax and a grand total, format money as currency, and save it as "
        + "invoice.ods.",
    },
]
PRESETS_BY_ID = {p["id"]: p for p in PRESETS}

log = logging.getLogger("computer-use-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class RunError(Exception):
    """Something the visitor can act on (busy, quota, bad prompt)."""


class Job:
    """One run's event log and frames. Readers follow it from the start while it grows."""

    def __init__(self, preset: str | None, replay: bool) -> None:
        super().__init__()
        self.id = uuid.uuid4().hex[:12]
        self.preset = preset
        self.replay = replay
        self.events: list[dict[str, Any]] = []
        self.frames: list[tuple[float, bytes]] = []
        self.done = False
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.document: tuple[str, bytes] | None = None
        self.preview_pdf: bytes | None = None
        self.preview_png: bytes | None = None

    def now(self) -> float:
        return round(time.monotonic() - self.started, 2)

    def push(self, event: dict[str, Any]) -> None:
        event.setdefault("at", self.now())
        self.events.append(event)

    def push_frame(self, jpeg: bytes, at: float | None = None) -> None:
        self.frames.append((self.now() if at is None else at, jpeg))

    def finish(self) -> None:
        self.done = True


class WarmPool:
    """Desktops booted ahead of time, so a visitor's run starts in seconds instead of ~20."""

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size
        self.ready: list[tuple[float, Sandbox]] = []
        self.booting = 0
        self.lock = threading.Lock()

    def take(self) -> Sandbox | None:
        with self.lock:
            while self.ready:
                created, sandbox = self.ready.pop(0)
                if time.monotonic() - created < WARM_LIFETIME_S - RUN_TIMEOUT_S - 300:
                    return sandbox
                threading.Thread(target=terminate_quietly, args=(sandbox,), daemon=True).start()
        return None

    def refill(self) -> None:
        with self.lock:
            missing = self.size - len(self.ready) - self.booting
            self.booting += max(missing, 0)
        for _ in range(max(missing, 0)):
            threading.Thread(target=self._boot_one, daemon=True).start()

    def _boot_one(self) -> None:
        try:
            sandbox = make_desktop_sandbox(WARM_LIFETIME_S)
            sandbox.run()
            boot_desktop(sandbox)
            with self.lock:
                self.ready.append((time.monotonic(), sandbox))
            log.info("warm desktop ready: %s", sandbox.id)
        except Exception:
            log.exception("warm desktop boot failed")
        finally:
            with self.lock:
                self.booting -= 1


def terminate_quietly(sandbox: Sandbox) -> None:
    try:
        sandbox.terminate()
    except Exception:  # noqa: BLE001 - already gone; its lifetime ends it anyway
        log.info("terminate skipped for %s", sandbox.id)


state: dict[str, Any] = {}


def router_client() -> anthropic.Anthropic:
    api = os.environ["CHALK_API_SERVER"].rstrip("/")
    return anthropic.Anthropic(
        base_url=os.environ.get("AGENT_ROUTER_BASE_URL", f"{api}/v1/router"),
        api_key=os.environ["ROUTER_API_KEY"],
        default_headers={"X-Chalk-Env-Id": os.environ["CHALK_ENVIRONMENT"]},
        max_retries=3,
        timeout=180.0,
    )


def stream_frames(job: Job, sandbox: Sandbox, loop: asyncio.AbstractEventLoop) -> None:
    """Relays the sandbox's screen as JPEG frames until the run ends or the sandbox goes away."""
    buf = ""
    try:
        for event in sandbox.exec_stream(
            "python", "/opt/desk/deskctl.py", "stream", FRAME_FPS, env=DISPLAY_ENV, timeout_secs=RUN_TIMEOUT_S + 120
        ):
            if job.done:
                return
            if not event.stdout:
                continue
            buf += event.stdout
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.startswith("F "):
                    loop.call_soon_threadsafe(job.push_frame, base64.b64decode(line[2:]))
    except Exception:  # noqa: BLE001 - the stream ends when the sandbox is terminated
        log.info("frame stream ended for %s", job.id)


def collect_document(job: Job, sandbox: Sandbox, loop: asyncio.AbstractEventLoop) -> None:
    """Pulls the newest saved document out of the sandbox and renders a PDF preview of it."""
    sandbox.exec("mkdir", "-p", "/tmp/preview", timeout_secs=10)
    listing = sandbox.exec(
        "bash", "-c", f"cd {DOCUMENTS} 2>/dev/null && ls -1t | grep -v '^\\.~lock' | head -5", timeout_secs=20
    ).stdout_text.split()
    if not listing:
        loop.call_soon_threadsafe(job.push, {"t": "document", "found": False})
        return
    files = []
    for name in listing:
        size = sandbox.exec("stat", "-c", "%s", f"{DOCUMENTS}/{name}", timeout_secs=10).stdout_text.strip()
        files.append({"name": name, "bytes": int(size or 0)})
    # The main document is the newest non-PDF file; a PDF the agent exported is its preview.
    main = next((f for f in files if not f["name"].lower().endswith(".pdf")), files[0])
    if main["bytes"] > MAX_DOC_BYTES:
        loop.call_soon_threadsafe(job.push, {"t": "document", "found": False, "error": "document too large"})
        return
    data = sandbox.fs.read_bytes(f"{DOCUMENTS}/{main['name']}", timeout_secs=60)
    pdf_name = next((f["name"] for f in files if f["name"].lower().endswith(".pdf")), None)
    if pdf_name is None:
        # OpenOffice 4 has no --convert-to; docconv.py drives its UNO API in a separate headless
        # instance, under the Python 2.7 that OpenOffice bundles.
        pdf_path = "/tmp/preview/" + main["name"].rsplit(".", 1)[0] + ".pdf"
        sandbox.exec(
            "/opt/openoffice4/program/python",
            "/opt/desk/docconv.py",
            f"{DOCUMENTS}/{main['name']}",
            pdf_path,
            timeout_secs=120,
        )
    else:
        pdf_path = f"{DOCUMENTS}/{pdf_name}"
    pdf: bytes | None = None
    png: bytes | None = None
    try:
        pdf = sandbox.fs.read_bytes(pdf_path, timeout_secs=60)
        # A PNG of page one previews everywhere, including mobile browsers that won't inline PDFs.
        sandbox.exec(
            "pdftoppm",
            "-png",
            "-r",
            "90",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            pdf_path,
            "/tmp/preview/page1",
            timeout_secs=60,
        )
        png = sandbox.fs.read_bytes("/tmp/preview/page1.png", timeout_secs=60)
    except Exception:
        log.exception("no preview for %s", job.id)

    def publish() -> None:
        job.document = (main["name"], data)
        job.preview_pdf = pdf
        job.preview_png = png
        job.push(
            {
                "t": "document",
                "found": True,
                "name": main["name"],
                "bytes": len(data),
                "files": files,
                "has_pdf": pdf is not None,
                "has_png": png is not None,
            }
        )

    loop.call_soon_threadsafe(publish)


def run_live(job: Job, task: str, loop: asyncio.AbstractEventLoop) -> None:
    """Worker thread for one live run."""

    def push(event: dict) -> None:
        loop.call_soon_threadsafe(job.push, event)

    sandbox: Sandbox | None = None
    try:
        push({"t": "step", "step": "desktop", "status": "running"})
        t0 = time.monotonic()
        sandbox = state["pool"].take()
        warm = sandbox is not None
        if sandbox is None:
            sandbox = make_desktop_sandbox(RUN_TIMEOUT_S + 300)
            sandbox.run()
            boot_desktop(sandbox)
        state["pool"].refill()
        probe = sandbox.exec(
            "python",
            "-c",
            "import urllib.request\ntry:\n urllib.request.urlopen('https://example.com', timeout=4)\n print('reachable')\n"
            + "except Exception as e:\n print('blocked')",
            timeout_secs=20,
        ).stdout_text.strip()
        push(
            {
                "t": "sandbox",
                "id": sandbox.id,
                "seconds": round(time.monotonic() - t0, 1),
                "warm": warm,
                "kernel": "gVisor (restricted)",
                "egress": f"example.com → {probe}",
                "desktop": "GNOME Flashback · Apache OpenOffice 4.1.16 · 1024×768",
            }
        )
        push({"t": "step", "step": "desktop", "status": "done"})
        threading.Thread(target=stream_frames, args=(job, sandbox, loop), daemon=True).start()

        push({"t": "step", "step": "agent", "status": "running"})
        push({"t": "model", "model": MODEL, "effort": EFFORT})
        cfg = AgentConfig(model=MODEL, effort=EFFORT, max_turns=MAX_TURNS, deadline=time.monotonic() + RUN_TIMEOUT_S)
        for event in run_agent(router_client(), sandbox, task, cfg, job.stop.is_set):
            push(event)
        push({"t": "step", "step": "agent", "status": "done"})

        push({"t": "step", "step": "document", "status": "running"})
        collect_document(job, sandbox, loop)
        push({"t": "step", "step": "document", "status": "done"})
    except Exception as exc:
        log.exception("run %s failed", job.id)
        push({"t": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
    finally:
        if sandbox is not None:
            terminate_quietly(sandbox)
        loop.call_soon_threadsafe(job.finish)


# --- recordings -------------------------------------------------------------------------------


def save_recording(job: Job) -> None:
    """Stores a finished preset run on the recordings volume. The index is written last, so its
    presence marks a complete recording; frames are one blob plus offsets, a single write."""
    if job.preset is None or job.document is None:
        return
    vol = Volume(RECORDING_VOLUME)
    base = f"runs/{job.preset}"
    frames, offsets, blob = [], [], bytearray()
    for at, jpeg in job.frames:
        frames.append(at)
        offsets.append(len(blob))
        blob += jpeg
    offsets.append(len(blob))
    vol.put_file(f"{base}/frames.bin", bytes(blob))
    vol.put_file(f"{base}/document", job.document[1])
    if job.preview_pdf is not None:
        vol.put_file(f"{base}/preview.pdf", job.preview_pdf)
    if job.preview_png is not None:
        vol.put_file(f"{base}/preview.png", job.preview_png)
    index = {
        "events": job.events,
        "frames": frames,
        "offsets": offsets,
        "document_name": job.document[0],
        "has_pdf": job.preview_pdf is not None,
        "has_png": job.preview_png is not None,
    }
    vol.put_file(f"{base}/index.json", json.dumps(index))
    state["recordings"][job.preset] = load_recording(job.preset)
    log.info("recorded %s: %d frames, %d bytes", job.preset, len(frames), len(blob))


def load_recording(preset: str) -> dict[str, Any] | None:
    try:
        vol = Volume(RECORDING_VOLUME)
        base = f"runs/{preset}"
        index = json.loads(vol.read_file(f"{base}/index.json"))
        blob = vol.read_file(f"{base}/frames.bin")
        offsets = index["offsets"]
        index["frame_bytes"] = [blob[offsets[i] : offsets[i + 1]] for i in range(len(index["frames"]))]
        index["document"] = vol.read_file(f"{base}/document")
        index["preview_pdf"] = vol.read_file(f"{base}/preview.pdf") if index.get("has_pdf") else None
        index["preview_png"] = vol.read_file(f"{base}/preview.png") if index.get("has_png") else None
        return index
    except (VolumeFileNotFoundError, ValueError):
        return None
    except Exception:
        log.exception("loading recording %s failed", preset)
        return None


async def replay(job: Job, rec: dict[str, Any]) -> None:
    """Plays a recording back on its own timeline, never idling more than REPLAY_MAX_GAP_S."""
    items: list[tuple[float, str, Any]] = [(float(e.get("at", 0)), "e", e) for e in rec["events"]]
    items += [(float(at), "f", jpeg) for at, jpeg in zip(rec["frames"], rec["frame_bytes"], strict=True)]
    items.sort(key=lambda x: (x[0], x[1] == "e"))
    job.document = (rec["document_name"], rec["document"])
    job.preview_pdf = rec["preview_pdf"]
    job.preview_png = rec["preview_png"]
    last_src, clock = 0.0, 0.0
    for at, kind, payload in items:
        if job.stop.is_set():
            break
        gap = min(max(at - last_src, 0.0), REPLAY_MAX_GAP_S)
        await asyncio.sleep(gap)
        clock += gap
        last_src = at
        if kind == "f":
            job.push_frame(payload, at=round(clock, 2))
        else:
            job.push({**{k: v for k, v in payload.items() if k != "at"}, "at": round(clock, 2)})
    job.finish()


# --- run admission ----------------------------------------------------------------------------


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def within(deque_: collections.deque[float], limit: int) -> bool:
    now = time.monotonic()
    while deque_ and now - deque_[0] > 3600:
        deque_.popleft()
    return len(deque_) < limit


async def watch_live(job: Job, slots: asyncio.Semaphore) -> None:
    try:
        while not job.done:
            await asyncio.sleep(POLL_S)
        complete = any(e.get("t") == "agent_end" and e.get("reason") == "done" for e in job.events)
        if complete and job.document is not None and job.preset is not None:
            await asyncio.to_thread(save_recording, job)
    except Exception:
        log.exception("saving recording for %s failed", job.id)
    finally:
        slots.release()


async def start_run(body: dict[str, Any], who: str) -> Job:
    preset_id = body.get("preset")
    if preset_id is not None:
        preset = PRESETS_BY_ID.get(str(preset_id))
        if preset is None:
            raise RunError("Unknown preset.")
        task = preset["prompt"]
    else:
        task = " ".join(str(body.get("prompt", "")).split())
        if len(task) < 12:
            raise RunError("Describe the document in a sentence.")
        if len(task) > MAX_PROMPT_CHARS:
            raise RunError(f"Keep the task under {MAX_PROMPT_CHARS} characters.")
        preset = None

    slots: asyncio.Semaphore = state["slots"]
    per_client: collections.deque[float] = state["per_client"][who]
    live_ok = (
        not slots.locked()
        and within(state["recent"], MAX_LIVE_PER_HOUR)
        and within(per_client, MAX_PER_CLIENT_PER_HOUR)
    )
    if not live_ok or body.get("replay"):
        rec = state["recordings"].get(preset["id"]) if preset else None
        if rec is not None:
            job = Job(preset["id"], replay=True)
            state["jobs"][job.id] = job
            asyncio.create_task(replay(job, rec))
            return job
        if slots.locked():
            raise RunError(
                "Both live desktops are busy. Pick a preset to watch a recorded run, or try again in a few minutes."
            )
        raise RunError("You've reached this hour's limit of live runs. Presets still play their recorded runs.")

    await slots.acquire()
    state["recent"].append(time.monotonic())
    per_client.append(time.monotonic())
    job = Job(preset["id"] if preset else None, replay=False)
    job.push({"t": "task", "preset": job.preset, "task": task if preset else None})
    state["jobs"][job.id] = job
    threading.Thread(target=run_live, args=(job, task, asyncio.get_running_loop()), daemon=True).start()
    asyncio.create_task(watch_live(job, slots))
    return job


def reap_leftover_sandboxes() -> None:
    """Terminates desktops a previous replica left behind; their lifetime would end them too."""
    try:
        leftovers = [i for i in Sandbox.list_all() if (i.name or "").startswith(SANDBOX_PREFIX)]
    except Exception:
        log.exception("listing sandboxes failed")
        return
    for info in leftovers:
        try:
            Sandbox.from_id(info.id).terminate()
        except Exception:  # noqa: BLE001 - already terminating
            log.info("skipped leftover sandbox %s (%s)", info.id, info.status)


async def evict_old_jobs() -> None:
    while True:
        await asyncio.sleep(300)
        cutoff = time.monotonic() - 3600
        for job_id in [j for j, job in state["jobs"].items() if job.done and job.started < cutoff]:
            state["jobs"].pop(job_id, None)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    state.update(
        jobs={},
        slots=asyncio.Semaphore(MAX_CONCURRENT),
        recent=collections.deque(),
        per_client=collections.defaultdict(collections.deque),
        recordings={},
        pool=WarmPool(WARM_POOL_SIZE),
    )
    await asyncio.to_thread(reap_leftover_sandboxes)
    for preset in PRESETS:
        rec = await asyncio.to_thread(load_recording, preset["id"])
        if rec is not None:
            state["recordings"][preset["id"]] = rec
    state["pool"].refill()
    evictor = asyncio.create_task(evict_old_jobs())
    yield
    evictor.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    pool: WarmPool = state["pool"]
    return JSONResponse(
        {
            "ok": True,
            "warm": len(pool.ready),
            "booting": pool.booting,
            "live_slots_free": not state["slots"].locked(),
            "recordings": sorted(state["recordings"]),
        }
    )


@app.get("/api/presets")
async def presets() -> JSONResponse:
    return JSONResponse([{**p, "recorded": p["id"] in state["recordings"]} for p in PRESETS])


@app.post("/api/runs")
async def create_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        job = await start_run(body, client_key(request))
    except RunError as exc:
        return JSONResponse({"error": str(exc)}, status_code=429 if "busy" in str(exc) or "limit" in str(exc) else 400)
    except ValueError:
        return JSONResponse({"error": 'Send JSON like {"preset": "budget"}.'}, status_code=400)
    return JSONResponse({"id": job.id, "replay": job.replay, "preset": job.preset})


@app.post("/api/runs/{job_id}/stop")
async def stop_run(job_id: str) -> JSONResponse:
    job: Job | None = state["jobs"].get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown run."}, status_code=404)
    job.stop.set()
    return JSONResponse({"ok": True})


@app.get("/api/runs/{job_id}/events")
async def run_events(job_id: str) -> StreamingResponse:
    job: Job | None = state["jobs"].get(job_id)

    async def stream() -> AsyncIterator[str]:
        if job is None:
            yield f"data: {json.dumps({'t': 'error', 'message': 'Unknown run.'})}\n\n"
            return
        yield f"data: {json.dumps({'t': 'hello', 'replay': job.replay, 'preset': job.preset})}\n\n"
        sent, idle = 0, 0.0
        while True:
            while sent < len(job.events):
                yield f"data: {json.dumps(job.events[sent])}\n\n"
                sent += 1
                idle = 0.0
            if job.done and sent >= len(job.events):
                yield f"data: {json.dumps({'t': 'end'})}\n\n"
                return
            await asyncio.sleep(POLL_S)
            idle += POLL_S
            if idle >= 15:
                yield ": keepalive\n\n"
                idle = 0.0

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/api/runs/{job_id}/frame")
async def run_frame(job_id: str, after: int = -1) -> Response:
    job: Job | None = state["jobs"].get(job_id)
    if job is None or not job.frames:
        return Response(status_code=204)
    seq = len(job.frames) - 1
    if seq <= after:
        return Response(status_code=204)
    return Response(
        job.frames[seq][1], media_type="image/jpeg", headers={"X-Frame-Seq": str(seq), "Cache-Control": "no-store"}
    )


@app.get("/api/runs/{job_id}/document")
async def run_document(job_id: str) -> Response:
    job: Job | None = state["jobs"].get(job_id)
    if job is None or job.document is None:
        return JSONResponse({"error": "No document."}, status_code=404)
    name, data = job.document
    return Response(
        data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@app.get("/api/runs/{job_id}/preview.png")
async def run_preview_png(job_id: str) -> Response:
    job: Job | None = state["jobs"].get(job_id)
    if job is None or job.preview_png is None:
        return JSONResponse({"error": "No preview."}, status_code=404)
    return Response(job.preview_png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/runs/{job_id}/preview.pdf")
async def run_preview(job_id: str) -> Response:
    job: Job | None = state["jobs"].get(job_id)
    if job is None or job.preview_pdf is None:
        return JSONResponse({"error": "No preview."}, status_code=404)
    return Response(job.preview_pdf, media_type="application/pdf", headers={"Content-Disposition": "inline"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")
