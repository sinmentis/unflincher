"""FastAPI application entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from diary.config import load_settings
from diary.db import get_connection, init_schema
from diary.routes import timeline

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_connection(settings.db_path)
    init_schema(conn)
    app.state.db = conn
    yield
    conn.close()


app = FastAPI(title="diary", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="src/diary/static"), name="static")
app.include_router(timeline.router)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})
