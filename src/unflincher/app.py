"""FastAPI application entrypoint."""
import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from unflincher.auth import AccessJWTMiddleware
from unflincher.config import load_settings
from unflincher.csrf import CSRFMiddleware
from unflincher.db import (
    get_connection,
    get_maintenance_locked,
    initialize_database,
    recover_or_cancel_running_jobs,
)
from unflincher import llm as _llm
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
        try:
            # The one deep interface for schema/migrations/Analyst seeding, shared with the CLI
            # import bootstrap (see db.initialize_database's own docstring).
            initialize_database(conn)
            app.state.db = conn
            # One random token per process instance. clear_stale_leases() (inside
            # recover_or_cancel_running_jobs below) removes every lease row from the PREVIOUS
            # process unconditionally; any lease this process itself acquires afterward is tagged
            # with this token so a future restart can tell "this process's leases" apart if ever
            # needed.
            app.state.owner_token = uuid.uuid4().hex
            await _llm.warm_up_client()

            # Crash recovery: a regen_job left 'running' when the process died is resumed ONLY if
            # it has a stored context snapshot (see db.recover_or_cancel_running_jobs) -- a
            # snapshot-less legacy job is cancelled and its unfinished items deleted rather than
            # ever resumed against the live archive. Stale leases from the dead previous process
            # are cleared first.
            recovery_result = recover_or_cancel_running_jobs(conn, app.state.owner_token)
            if recovery_result.cancelled_job_ids:
                logger.warning(
                    "startup recovery: cancelled %d snapshot-less legacy regeneration job(s) "
                    "(%s), deleting %d unfinished item(s) rather than resuming against the live "
                    "archive",
                    len(recovery_result.cancelled_job_ids), recovery_result.cancelled_job_ids,
                    recovery_result.cancelled_item_count,
                )
            for job_id in recovery_result.recovered_job_ids:
                worker = BatchWorker(conn, settings.batch_concurrency)
                # Hold a strong reference on app.state so the task can't be GC'd mid-run (RUF006);
                # it also gives tests a handle to await the relaunched worker deterministically.
                # run_job() reads the job's OWN prompt version and stored snapshot itself — see
                # worker.py's module docstring — so no persona_text/model/entries need to be
                # passed in here.
                app.state.recovery_task = asyncio.create_task(worker.run_job(job_id, recovering=True))

            yield
        finally:
            # Runs for every exit path -- initialization failure, warm-up/recovery failure, an
            # exception crossing `yield`, or normal shutdown -- so the connection (and the
            # Copilot client, if warm-up ever ran) is never left open. A plain `finally` (no
            # `except`) never masks whatever exception is already propagating.

            # Settle any still-running recovery worker BEFORE tearing down the shared Copilot
            # client or closing the database connection -- otherwise a recovered job could still
            # be mid-admission (touching the client) or mid-write (touching the connection) when
            # either is torn down. Cancelling is safe: BatchWorker.run_job's own cancellation
            # handling (see worker.py) cancels/awaits its child per-item tasks and releases their
            # leases before the cancellation propagates here, and never force-marks the job
            # 'cancelled' on a plain cancellation -- the job/items are left exactly as a crash
            # would leave them, so the NEXT startup's recover_or_cancel_running_jobs can resume it
            # again.
            recovery_task = getattr(app.state, "recovery_task", None)
            if recovery_task is not None:
                if not recovery_task.done():
                    recovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recovery_task

            # shutdown_client() is safe even if warm_up_client() never ran (or never fully
            # started a client) -- it is a no-op transition when there is no client to stop.
            await _llm.shutdown_client()
            conn.close()

    app = FastAPI(title="unflincher", lifespan=lifespan)
    app.add_middleware(AccessJWTMiddleware, settings=settings)
    app.add_middleware(CSRFMiddleware)

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        # StaticFiles serves CSS, JavaScript, fonts, and icons with ETag/Last-Modified but no explicit
        # Cache-Control. `no-cache` keeps browser and Cloudflare copies reusable while forcing
        # revalidation after each deploy.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def private_noindex(request, call_next):
        # Defense in depth only. Cloudflare Access remains the primary access boundary; this
        # header just discourages indexing of the private application if edge auth is ever absent.
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
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

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots_txt():
        # Non-sensitive: tells compliant crawlers the private app is not for indexing. Exempt from
        # Access auth in auth.py so it is reachable even without the edge in front.
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/healthz")
    async def healthz():
        return JSONResponse(
            {
                "status": "ok",
                "revision": settings.revision,
                "version": settings.version,
                "generation_locked": get_maintenance_locked(app.state.db),
            }
        )

    return app


app = create_app()
