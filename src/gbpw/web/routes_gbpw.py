"""
GB Power Weekly: re-renders the standalone report from stored facts/narrative
on every request. No filesystem coupling -- reflects exactly what's in the
`reports` table, so a `gbpw build --regenerate` changes what this route
serves immediately.

Deliberately NOT wrapped in the app-shell (see render/render.py and the
project's design notes): this report is a clean, printable, "send to
clients" artifact and shouldn't carry internal nav chrome.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from ..render.render import render_week
from ..storage import get_report, latest_report_week
from .deps import get_db

router = APIRouter()


@router.get("/gbpw")
def latest(db: sqlite3.Connection = Depends(get_db)):
    week = latest_report_week(db)
    if week is None:
        raise HTTPException(404, "No GB Power Weekly report has been built yet.")
    return RedirectResponse(url=f"/gbpw/{week.isoformat()}")


@router.get("/gbpw/{week_ending}")
def weekly(week_ending: date, db: sqlite3.Connection = Depends(get_db)):
    report = get_report(db, week_ending)
    if report is None:
        raise HTTPException(404, f"No report on file for week ending {week_ending.isoformat()}.")
    html = render_week(
        json.loads(report["facts_json"]),
        json.loads(report["narrative"]),
        datetime.fromisoformat(report["built_at"]),
    )
    return HTMLResponse(content=html)
