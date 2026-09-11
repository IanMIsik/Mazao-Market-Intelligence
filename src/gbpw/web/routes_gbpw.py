"""
GB Power Weekly: re-renders the standalone report from stored facts/narrative
on every request. No filesystem coupling -- reflects exactly what's in the
`reports` table, so a `gbpw build --regenerate` changes what this route
serves immediately.

The document itself (render_week()'s output) is NOT wrapped in the app-shell
-- it's a clean, printable, "send to clients" artifact, and `build.py`'s CLI
path writes that exact same string straight to a `.html` file for that
purpose. But someone reaching this page by clicking through the web app has
no way back without one, so this route injects a thin, print-hidden strip
right after <body> -- present when viewed here, gone from the CLI-generated
file and from anything printed/exported from this page (the report's own
stylesheet already has an @media print block).
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

_WEB_NAV_BAR = """
<style>
  .webnav-bar { background:#10294A; color:#B9C6DA; font:13px -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
    padding:8px 28px; }
  .webnav-bar a { color:#fff; text-decoration:none; }
  .webnav-bar a:hover { text-decoration:underline; }
  @media print { .webnav-bar { display:none; } }
</style>
<div class="webnav-bar">&larr; <a href="/bess">Back to BESS Analytics</a></div>
"""


def _with_web_nav(html: str) -> str:
    return html.replace("<body>", "<body>" + _WEB_NAV_BAR, 1)


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
    return HTMLResponse(content=_with_web_nav(html))
