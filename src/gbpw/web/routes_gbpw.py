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

The "Download PDF" button in that same nav bar is plain window.print() --
not a server-rendered file. The report's stylesheet already has a real
@media print block (built for exactly this "send to clients" use case), and
every browser's print dialog offers "Save as PDF" as a destination, so this
gets a genuine PDF with zero new dependencies. A server-side renderer
(WeasyPrint/Playwright) would need either native system libraries or a
bundled browser download -- not worth it while print-to-PDF already covers
the same output faithfully (same stylesheet, same @media print rules).

/gbpw/new and /gbpw/build (an on-demand "ingest + build this past week"
pair, backing the "build a report for a week that isn't in the reports
table yet" flow) are registered ahead of /gbpw/{week_ending} -- FastAPI
matches path params as plain strings before validating them as a `date`,
so if {week_ending} were registered first it would swallow "new"/"build"
as literal path segments and 422 rather than ever reaching these routes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..build import build_report
from ..ingest import history_range, ingest_week_parallel
from ..metrics import IncompleteWeekError
from ..render.render import render_week
from ..settlement import most_recent_sunday
from ..storage import get_report, latest_report_week, list_report_weeks
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_WEB_NAV_CSS = """
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
  .webnav-appnav nav a:not(.on):hover { color:#fff; }
  .webnav-tools { margin-left:auto; display:flex; align-items:center; gap:14px; }
  .webnav-weekpick { display:flex; align-items:center; gap:8px; }
  .webnav-weekpick label { color:#8FA0BC; font-size:12px; }
  .webnav-weekpick select { background:#16305A; color:#fff; border:1px solid #2B4B73; border-radius:4px;
    font:13px -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; padding:5px 8px; }
  .webnav-newlink { color:#B9C6DA; text-decoration:none; font-size:13px; white-space:nowrap; }
  .webnav-newlink:hover { color:#fff; }
  .webnav-pdfbtn { background:#16305A; color:#fff; border:1px solid #2B4B73; border-radius:4px;
    font:13px -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; padding:5px 10px; cursor:pointer; }
  .webnav-pdfbtn:hover { background:#2B4B73; }
  /* Same overflow problem as base.html's .appnav (see app.css's own
     comment on its mobile fix) -- confirmed live, brand + 5 nav links
     + "Build report for another week" + a PDF button + a week-picker
     select all crammed into one un-wrapping flex row overflows a phone
     viewport even worse here, since there's more in this row than the
     plain nav has. Same fix, duplicated rather than shared (this
     stylesheet is inlined specifically to stay independent of app.css,
     see the module docstring) -- stack brand above nav, drop the
     decorative subtitle, let nav and the tools row each wrap. */
  @media (max-width:600px) {
    .webnav-appnav .webnav-wrap { flex-direction:column; align-items:flex-start; gap:0; padding:10px 16px; }
    .webnav-appnav .webnav-brand { padding:4px 0; }
    .webnav-appnav .webnav-brand span { display:none; }
    .webnav-appnav nav { flex-wrap:wrap; gap:0 4px; width:100%; }
    .webnav-appnav nav a { padding:8px 8px; font-size:12.5px; }
    .webnav-tools { margin-left:0; width:100%; flex-wrap:wrap; gap:8px 14px; padding:8px 0 4px; }
  }
</style>
"""


def _week_nav_bar(weeks: list[dict], current: date) -> str:
    options = []
    for w in weeks:
        label = w["week_ending"].strftime("Week ending %a %d %b %Y")
        if not w["published"]:
            label += " (draft)"
        selected = " selected" if w["week_ending"] == current else ""
        options.append(f'<option value="{w["week_ending"].isoformat()}"{selected}>{label}</option>')
    picker = ""
    if len(weeks) > 1:
        picker = f"""
      <div class="webnav-weekpick">
        <label for="webnav-week-select">Report</label>
        <select id="webnav-week-select" onchange="location.href='/gbpw/' + this.value">
          {"".join(options)}
        </select>
      </div>"""
    return f"""{_WEB_NAV_CSS}
<div class="webnav-appnav">
  <div class="webnav-wrap">
    <div class="webnav-brand">Mazao Consulting <span>/ Energy Data Analytics</span></div>
    <nav>
      <a href="/gbpw" class="on">GB Power Weekly</a>
      <a href="/bess">BESS Analytics</a>
      <a href="/live">Live market</a>
      <a href="/forecasts">Forecasts</a>
      <a href="/ppa">PPA tools</a>
    </nav>
    <div class="webnav-tools">
      <a class="webnav-newlink" href="/gbpw/new">+ Build report for another week</a>
      <button class="webnav-pdfbtn" type="button" onclick="window.print()">Download PDF</button>{picker}
    </div>
  </div>
</div>
"""


def _with_web_nav(html: str, weeks: list[dict], current: date) -> str:
    return html.replace("<body>", "<body>" + _week_nav_bar(weeks, current), 1)


@router.get("/gbpw")
def latest(db: sqlite3.Connection = Depends(get_db)):
    week = latest_report_week(db)
    if week is None:
        raise HTTPException(404, "No GB Power Weekly report has been built yet. Visit /gbpw/new to build one.")
    return RedirectResponse(url=f"/gbpw/{week.isoformat()}")


def _new_report_form(request: Request, db: sqlite3.Connection, error: str | None, week_ending_str: str | None):
    return templates.TemplateResponse(
        request,
        "gbpw_new.html",
        {
            "request": request,
            "active_nav": "gbpw",
            "latest_available": latest_report_week(db),
            "max_date": most_recent_sunday(date.today()),
            "error": error,
            "week_ending_str": week_ending_str,
        },
    )


@router.get("/gbpw/new", response_class=HTMLResponse)
def new_report_form(request: Request, week_ending: str | None = None, db: sqlite3.Connection = Depends(get_db)):
    return _new_report_form(request, db, error=None, week_ending_str=week_ending)


@router.get("/gbpw/build", response_class=HTMLResponse)
def build_report_route(request: Request, week_ending: str, db: sqlite3.Connection = Depends(get_db)):
    """Ingests the target week (plus 37 days of trailing history) live from
    Elexon and builds the report, exactly like `gbpw run --week-ending
    <date>` -- synchronous, so this request blocks for as long as the real
    Elexon fetches take. Ingest itself is parallelized (ingest_week_parallel,
    a ThreadPoolExecutor across dates) so this is now on the order of a
    handful of seconds rather than minutes for a fresh week.
    """
    try:
        parsed = date.fromisoformat(week_ending)
    except ValueError:
        return _new_report_form(request, db, f"'{week_ending}' isn't a valid date (expected YYYY-MM-DD).", week_ending)

    max_date = most_recent_sunday(date.today())
    if parsed > max_date:
        return _new_report_form(
            request, db,
            f"{parsed.strftime('%a %d %b %Y')} hasn't finished yet -- the most recently completed "
            f"week ends {max_date.strftime('%a %d %b %Y')}.",
            week_ending,
        )

    try:
        dates = history_range(parsed, 37)
    except ValueError as e:
        return _new_report_form(request, db, str(e), week_ending)

    ingest_week_parallel(request.app.state.db_path, dates)
    out_path = Path("out") / f"gbpw-{parsed.isoformat()}.html"
    try:
        build_report(db, parsed, out_path)
    except IncompleteWeekError as e:
        return _new_report_form(
            request, db,
            f"Couldn't build week ending {parsed.isoformat()} -- Elexon's data for it looks incomplete:\n{e}",
            week_ending,
        )

    return RedirectResponse(url=f"/gbpw/{parsed.isoformat()}", status_code=303)


@router.get("/gbpw/{week_ending}")
def weekly(week_ending: date, db: sqlite3.Connection = Depends(get_db)):
    report = get_report(db, week_ending)
    if report is None:
        raise HTTPException(
            404,
            f"No report on file for week ending {week_ending.isoformat()}. "
            f"Visit /gbpw/new?week_ending={week_ending.isoformat()} to build it.",
        )
    html = render_week(
        json.loads(report["facts_json"]),
        json.loads(report["narrative"]),
        datetime.fromisoformat(report["built_at"]),
    )
    weeks = list_report_weeks(db)
    return HTMLResponse(content=_with_web_nav(html, weeks, week_ending))
