"""FastAPI application entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from diary.config import load_settings
from diary.db import get_connection, init_schema
from diary.llm import ensure_default_persona_prompt
from diary.routes import chat, entry, new_entry, report, timeline, workshop


def create_app() -> FastAPI:
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = get_connection(settings.db_path)
        init_schema(conn)
        ensure_default_persona_prompt(conn)
        app.state.db = conn
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
