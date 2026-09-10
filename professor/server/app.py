"""HTTP endpoints.

Small on purpose. The library on disk is the state, so most endpoints are a
read of a directory plus a render. The two that do more are `/api/upload`,
which runs a file through ingest and mirrors it, and `/api/chat`, which streams
from Claude.

Everything is bound to localhost. There is no authentication because there is
no remote access -- and the one secret involved never leaves this process.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .. import library
from ..capture.paths import CAPTURES_DIR, ensure_library
from ..capture.topics import TopicRegistry
from ..config import DEFAULT_EFFORT, MODELS, Config
from ..ingest import IngestError, ingest_file
from ..mirror import mirror_capture, prune_stale
from ..professor import Turn, stream_answer

STATIC = Path(__file__).parent / "static"

# Uploads are read fully into memory before ingest, so this is also the ceiling
# on how much a single request can allocate. Comfortably above a long PDF
# chapter, well below anything that would trouble a laptop.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def _root() -> Path:
    return ensure_library(Config.load().library_path())


def create_app() -> FastAPI:
    app = FastAPI(title="my-favorite-professor", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------- the page

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    @app.get("/static/{name}")
    def static_file(name: str) -> FileResponse:
        path = (STATIC / name).resolve()
        try:
            path.relative_to(STATIC.resolve())
        except ValueError:
            raise HTTPException(404)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    # ------------------------------------------------------------ settings

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse(Config.load().public())

    @app.post("/api/settings")
    def set_settings(payload: dict = Body(...)) -> JSONResponse:
        config = Config.load()

        # An empty string means "leave it alone", so clearing the box by
        # accident can't silently wipe a working key. `null` is the explicit
        # "forget it" signal the UI's Remove button sends.
        if "api_key" in payload:
            key = payload["api_key"]
            if key is None:
                config.api_key = ""
            elif str(key).strip():
                config.api_key = str(key).strip()

        if payload.get("model") in MODELS:
            config.model = payload["model"]
        if payload.get("effort"):
            config.effort = str(payload["effort"])
        if "mirror_to_downloads" in payload:
            config.mirror_to_downloads = bool(payload["mirror_to_downloads"])
        if "initials" in payload:
            config.initials = str(payload["initials"] or "")[:3].upper()
        if "library" in payload and str(payload["library"] or "").strip():
            config.library = str(payload["library"]).strip()
        if isinstance(payload.get("goals"), dict):
            config.goals.update({str(k): str(v) for k, v in payload["goals"].items()})

        config.save()
        return JSONResponse(config.public())

    # ------------------------------------------------------------- library

    @app.get("/api/topics")
    def get_topics() -> JSONResponse:
        root = _root()
        return JSONResponse({
            "library": str(root),
            "topics": [t.public() for t in library.list_topics(root)],
        })

    @app.post("/api/topics")
    def create_topic(payload: dict = Body(...)) -> JSONResponse:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "give the subject a name")
        root = _root()
        resolution = TopicRegistry(root).resolve(name)
        return JSONResponse({
            "topic": library.read_topic(resolution.directory,
                                        resolution.label).public(),
            "action": resolution.action,
            "matched": resolution.matched_topic,
        })

    @app.get("/api/note")
    def get_note(id: str = Query(...)) -> JSONResponse:
        note = library.resolve_note(_root(), id)
        if not note:
            raise HTTPException(404, "no such note")
        return JSONResponse(library.note_document(note))

    @app.get("/api/asset/{topic:path}/{slug}/{name}")
    def get_asset(topic: str, slug: str, name: str) -> FileResponse:
        """Serve an image out of a capture's archive.

        Every component is checked against the resolved library path -- these
        come from note text, which for `claude-references-provided` material
        originated on the open web.

        `topic` is a :path parameter because a subtopic's is two segments. The
        converter is greedy but the two segments after it are not, so the match
        backtracks to leave exactly slug and name -- and widening it changes
        nothing about safety here, which rests on the containment check below
        rather than on the shape of the pattern.
        """
        root = _root().resolve()
        path = (root / topic / CAPTURES_DIR / slug / "assets" / name).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise HTTPException(404)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    # -------------------------------------------------------------- upload

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...),
                     topic: str = Form(...)) -> JSONResponse:
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "that file is larger than 64 MB")

        config = Config.load()
        root = ensure_library(config.library_path())
        resolution = TopicRegistry(root).resolve(topic)

        try:
            result = ingest_file(
                data=data,
                filename=file.filename or "upload.txt",
                topic_dir=resolution.directory,
                topic=resolution.label,
            )
        except IngestError as exc:
            raise HTTPException(400, str(exc))

        mirrored: str | None = None
        mirror_error: str | None = None
        if config.mirror_to_downloads:
            prune_stale(title=result.title, capture_dir=result.capture_dir,
                        topic=resolution.label)
            outcome = mirror_capture(
                capture_dir=result.capture_dir, note_path=result.note_path,
                title=result.title, topic=resolution.label,
            )
            mirrored = str(outcome.page or outcome.note or "")
            mirror_error = outcome.error

        note = library.resolve_note(
            root, library.note_id(resolution.label, result.source,
                                  result.note_path.name),
        )
        return JSONResponse({
            "note": note.public() if note else None,
            "topic": resolution.label,
            "title": result.title,
            "words": result.word_count,
            "images": result.assets_kept,
            "updated": result.updated,
            "mirrored": mirrored,
            "mirror_error": mirror_error,
        })

    # ---------------------------------------------------------------- chat

    @app.post("/api/chat")
    def chat(payload: dict = Body(...)) -> StreamingResponse:
        config = Config.load()
        root = _root()

        question = str(payload.get("question", "")).strip()
        if not question:
            raise HTTPException(400, "ask something")

        note = library.resolve_note(root, str(payload.get("note_id", "")))
        if not note:
            raise HTTPException(404, "open something to read first")

        document = library.note_document(note)
        history = [
            Turn(role=str(t.get("role")), content=str(t.get("content", "")))
            for t in payload.get("history", [])
            if t.get("role") in {"user", "assistant"}
        ][-12:]  # recent turns only; the reading is the context that matters

        goal = config.goals.get(note.topic, "")

        def events() -> Iterator[str]:
            try:
                for event in stream_answer(
                    config=config,
                    reading_title=document["title"],
                    reading_text=document["text"],
                    question=question,
                    history=history,
                    goal=goal,
                ):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:  # a crash must not hang the browser
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# ---------------------------------------------------------------------- run

def _free_port(preferred: int) -> int:
    """Use the preferred port if it's free, otherwise let the OS choose."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(*, port: int = 8765, open_browser: bool = True) -> int:
    import uvicorn

    chosen = _free_port(port)
    url = f"http://127.0.0.1:{chosen}"

    root = _root()
    print(f"  my-favorite-professor")
    print(f"  library:  {root}")
    print(f"  open:     {url}")
    if not Config.load().has_key:
        print("  note:     no API key yet -- add one in Settings")
    print("  stop:     ctrl-c\n")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(), host="127.0.0.1", port=chosen, log_level="warning")
    return 0
