"""FastAPI application entrypoint."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from unflincher.auth import AccessJWTMiddleware
from unflincher.config import load_settings
from unflincher.csrf import CSRFMiddleware
from unflincher.db import get_connection, init_schema, migrate_chat_session, migrate_persona_prompt_model, resume_sweep
from unflincher import llm as _llm
from unflincher.llm import ensure_default_persona_prompt
from unflincher.routes import chat, entry, new_entry, report, timeline, workshop
from unflincher.templates_env import templates
from unflincher.worker import BatchWorker

logger = logging.getLogger(__name__)


def _accepts_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


async def branded_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and _accepts_html(request):
        request.state.ui_error_page = True
        return templates.TemplateResponse(
            request,
            "errors/404.html",
            {"page_id": "error", "active_nav": None},
            status_code=404,
        )
    return await http_exception_handler(request, exc)


async def branded_server_error(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if _accepts_html(request):
        request.state.ui_error_page = True
        return templates.TemplateResponse(
            request,
            "errors/500.html",
            {"page_id": "error", "active_nav": None},
            status_code=500,
        )
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


def create_app() -> FastAPI:
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = get_connection(settings.db_path)
        init_schema(conn)
        # Must run before anything reads/writes persona_prompt (ensure_default_persona_prompt and
        # the recovery lookup below both do). Idempotent, so safe on every production restart.
        migrate_persona_prompt_model(conn)
        migrate_chat_session(conn)
        ensure_default_persona_prompt(conn)
        resume_sweep(conn)
        app.state.db = conn
        await _llm.warm_up_client()

        # Crash recovery: if a batch job was left 'running' when the process died, relaunch its
        # worker. resume_sweep() above already reset any half-done items back to 'pending', so the
        # worker just re-claims them. complete_job_item() is atomic, so no result is duplicated.
        running_job = conn.execute(
            "SELECT * FROM regen_job WHERE status = 'running'"
        ).fetchone()
        if running_job is not None:
            prompt = conn.execute(
                "SELECT body_text, model FROM persona_prompt WHERE id = ?",
                (running_job["prompt_version_id"],),
            ).fetchone()
            worker = BatchWorker(conn, settings.batch_concurrency)
            # Hold a strong reference on app.state so the task can't be GC'd mid-run (RUF006);
            # it also gives tests a handle to await the relaunched worker deterministically.
            # Resume with the job's OWN persona model, not settings.llm_model — a recovered job
            # must stay consistent with the model its already-generated items used.
            app.state.recovery_task = asyncio.create_task(
                worker.run_job(
                    running_job["id"], prompt["body_text"], prompt["model"]
                )
            )

        yield
        await _llm.shutdown_client()
        conn.close()

    app = FastAPI(title="unflincher", lifespan=lifespan)
    app.add_middleware(AccessJWTMiddleware, settings=settings)
    app.add_middleware(CSRFMiddleware)

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        # StaticFiles serves theme.css/app.js/htmx with only ETag/Last-Modified (no explicit
        # Cache-Control), so both the browser AND Cloudflare's edge cache (this hostname is
        # proxied) are free to serve a stale copy after a deploy indefinitely -- confirmed as the
        # cause of a real "my CSS fix isn't showing up" report. `no-cache` (not `no-store`) still
        # lets both caches keep a copy, it just forces a conditional revalidation (cheap 304) on
        # every request, so a changed file is picked up on the very next load.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory="src/unflincher/static"), name="static")
    app.include_router(timeline.router)
    app.include_router(entry.router)
    app.include_router(report.router)
    app.include_router(chat.router)
    app.include_router(new_entry.router)
    app.include_router(workshop.router)

    app.add_exception_handler(StarletteHTTPException, branded_http_error)
    app.add_exception_handler(Exception, branded_server_error)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
