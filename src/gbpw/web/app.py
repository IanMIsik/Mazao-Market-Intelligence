"""
FastAPI app factory. Two pages today (GB Power Weekly, BESS Analytics);
adding another follows the same pattern -- a data-layer module at package
root, an APIRouter under web/routes_<page>.py, include_router() here, a
template extending base.html, and a nav-link flip in base.html itself.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..storage import DEFAULT_DB_PATH
from . import routes_bess, routes_gbpw
from .background_refresh import start_background_refresh
from .deps import get_db, make_get_db

STATIC_DIR = Path(__file__).resolve().parent / "static"

DB_PATH_ENV_VAR = "GBPW_DB_PATH"


def create_app(db_path: Path = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="Mazao Energy Data Analytics")
    app.dependency_overrides[get_db] = make_get_db(db_path)
    # Routes that need to fan work out across threads (e.g. /gbpw/build's
    # parallel ingest) need the raw path, not just a Connection -- each
    # worker thread opens its own. Stashed on app.state rather than a
    # second Depends() target since it's a plain value, not a per-request
    # resource with its own lifecycle.
    app.state.db_path = db_path
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_gbpw.router)
    app.include_router(routes_bess.router)
    # BESS Analytics' own meta refresh (bess_analytics.html) only shows
    # fresh data if something is re-fetching it -- this is that something.
    app.state.background_refresh_stop = start_background_refresh(db_path)

    @app.get("/")
    def index():
        return RedirectResponse(url="/bess")

    return app


def create_app_from_env() -> FastAPI:
    """Factory entrypoint for uvicorn's --reload mode, which spawns a fresh
    subprocess that re-imports this module and can't take Python arguments
    directly -- it needs an "module:factory" import string instead (see
    cli.py's `serve` command). The db path crosses that boundary via an env
    var, set by `serve` before invoking uvicorn.
    """
    db_path = os.environ.get(DB_PATH_ENV_VAR)
    return create_app(Path(db_path) if db_path else DEFAULT_DB_PATH)
