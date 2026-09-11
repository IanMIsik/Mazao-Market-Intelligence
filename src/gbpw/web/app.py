"""
FastAPI app factory. Two pages today (GB Power Weekly, BESS Analytics);
adding another follows the same pattern -- a data-layer module at package
root, an APIRouter under web/routes_<page>.py, include_router() here, a
template extending base.html, and a nav-link flip in base.html itself.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..storage import DEFAULT_DB_PATH
from . import routes_bess, routes_gbpw
from .deps import get_db, make_get_db

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_path: Path = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="Mazao Energy Data Analytics")
    app.dependency_overrides[get_db] = make_get_db(db_path)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_gbpw.router)
    app.include_router(routes_bess.router)

    @app.get("/")
    def index():
        return RedirectResponse(url="/bess")

    return app
