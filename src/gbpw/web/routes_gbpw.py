"""
GB Power Weekly: re-renders the standalone report from stored facts/narrative
on every request. No filesystem coupling -- reflects exactly what's in the
`reports` table, so a `gbpw build --regenerate` changes what this route
serves immediately.

The document itself (render_week()'s output) is NOT wrapped in the app-shell
-- it's a clean, printable, "send to clients" artifact, and `build.py`'s CLI
path writes that exact same string straight to a `.html` file for that
purpose. But someone reaching this page by clicking through the web app
needs the same navigation the rest of the app has, so this route injects
the full app nav bar (same markup/CSS as base.html's .appnav, inlined here
rather than linked, so it can't collide with or be affected by the report's
own stylesheet) right after <body>, wrapped in @media print so it's absent
from anything printed/exported from this page and from the CLI-generated
file (the report's own stylesheet already has an @media print block).
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
  @media print { .webnav-appnav { display:none; } }
  .webnav-appnav { background:#10294A; font:13.5px -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; }
  .webnav-appnav .webnav-wrap { display:flex; align-items:center; gap:28px; padding:0 28px; max-width:1060px; margin:0 auto; }
  .webnav-appnav .webnav-brand { font-weight:700; color:#fff; letter-spacing:.01em; font-size:15px; padding:14px 0; }
  .webnav-appnav .webnav-brand span { font-weight:400; color:#B9C6DA; }
  .webnav-appnav nav { display:flex; gap:2px; }
  .webnav-appnav nav a { display:block; padding:16px 14px; color:#B9C6DA; text-decoration:none; font-size:13.5px;
    border-bottom:2px solid transparent; }
  .webnav-appnav nav a.on { color:#fff; border-bottom-color:#B04A39; font-weight:600; }
  .webnav-appnav nav a.soon { color:#5E7291; cursor:default; }
  .webnav-appnav nav a.soon span { font-size:10.5px; margin-left:5px; border:1px solid #45577A; padding:1px 5px;
    border-radius:8px; color:#8FA0BC; }
  .webnav-appnav nav a:not(.soon):not(.on):hover { color:#fff; }
</style>
<div class="webnav-appnav">
  <div class="webnav-wrap">
    <div class="webnav-brand">Mazao Consulting <span>/ Energy Data Analytics</span></div>
    <nav>
      <a href="/gbpw" class="on">GB Power Weekly</a>
      <a href="/bess">BESS Analytics</a>
      <a class="soon">Live market<span>soon</span></a>
      <a class="soon">PPA tools<span>soon</span></a>
    </nav>
  </div>
</div>
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
