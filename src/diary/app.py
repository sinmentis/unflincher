"""FastAPI application entrypoint."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from diary.config import load_settings
from diary.db import get_connection, init_schema, resume_sweep
from diary.llm import ensure_default_persona_prompt
from diary.routes import chat, entry, new_entry, report, timeline, workshop
from diary.worker import BatchWorker


def create_app() -> FastAPI:
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = get_connection(settings.db_path)
        init_schema(conn)
        ensure_default_persona_prompt(conn)
        resume_sweep(conn)
        app.state.db = conn

        # Crash recovery: if a batch job was left 'running' when the process died, relaunch its
        # worker. resume_sweep() above already reset any half-done items back to 'pending', so the
        # worker just re-claims them. complete_job_item() is atomic, so no result is duplicated.
        running_job = conn.execute(
            "SELECT * FROM regen_job WHERE status = 'running'"
        ).fetchone()
        if running_job is not None:
            prompt = conn.execute(
                "SELECT body_text FROM persona_prompt WHERE id = ?",
                (running_job["prompt_version_id"],),
            ).fetchone()
            worker = BatchWorker(conn, settings.batch_concurrency)
            # Hold a strong reference on app.state so the task can't be GC'd mid-run (RUF006);
            # it also gives tests a handle to await the relaunched worker deterministically.
            app.state.recovery_task = asyncio.create_task(
                worker.run_job(
                    running_job["id"], prompt["body_text"], settings.llm_model
                )
            )

        yield
        conn.close()

    app = FastAPI(title="diary", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="src/diary/static"), name="static")
    app.include_router(timeline.router)
    app.include_router(entry.router)
    app.include_router(report.router)
    app.include_router(chat.router)
    app.include_router(new_entry.router)
    app.include_router(workshop.router)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
