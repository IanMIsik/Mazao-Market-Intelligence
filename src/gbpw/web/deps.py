"""
Per-request DB connection, injected via FastAPI's Depends(). get_db() is a
placeholder that create_app() always overrides via app.dependency_overrides
-- tests use the exact same mechanism to point at a seeded temp DB.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from ..storage import connect


def get_db() -> Iterator[sqlite3.Connection]:
    raise RuntimeError("get_db must be overridden via app.dependency_overrides")


def make_get_db(db_path: Path):
    def _get_db() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    return _get_db
