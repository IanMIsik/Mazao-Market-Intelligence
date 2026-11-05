"""
Email alerts when something goes wrong. A watchdog thread, separate from the refresh and weekly
threads on purpose: on 2026-11-04 the refresh loop hung for 30+ hours and nothing noticed, and a
check that lives inside the thing that hung would have hung with it.

What it watches (each is an "issue" with a key):
  refresh_stale  the 5-minute refresh has not completed a cycle for 20 minutes (hung or failing)
  data_stale     the newest FUELINST reading is over 90 minutes old (data not arriving)
  weekly_missing last week's GB Power Weekly report is still unpublished 24h after it was due
  disk_low       free disk under 1.5 GB (the server filled up once already)

Per issue: one email when it starts, a reminder every 6 hours while it lasts, one "resolved"
email when it clears. Nothing is sent while an issue stays quiet.

Email is plain SMTP from environment variables (nothing is stored in the repo):
  ALERT_EMAIL_TO   comma-separated recipients (required to send)
  SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = SSL), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
Not configured -> issues are still logged at ERROR level, no email is attempted.
Test the setup with `gbpw test-alert`.

This cannot notice the whole container or server being down (it dies with it); use an outside
uptime monitor for that.
"""

from __future__ import annotations

import logging
import os
import shutil
import smtplib
import socket
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from ..settlement import most_recent_sunday
from ..storage import connect, get_report

logger = logging.getLogger("gbpw.web.alerts")

CHECK_INTERVAL_SECONDS = 60
REFRESH_STALE_AFTER = timedelta(minutes=20)
DATA_STALE_AFTER = timedelta(minutes=90)
WEEKLY_GRACE = timedelta(hours=24)
WEEKLY_READY_HOUR = 6  # London time, same as background_refresh.WEEKLY_REPORT_READY_HOUR
DISK_LOW_BYTES = int(1.5 * 1024**3)
REMINDER_EVERY = timedelta(hours=6)

_STARTED = datetime.now(timezone.utc)


# ---- email -----------------------------------------------------------------

def _smtp_settings() -> dict | None:
    to = [a.strip() for a in os.environ.get("ALERT_EMAIL_TO", "").split(",") if a.strip()]
    host = os.environ.get("SMTP_HOST", "").strip()
    if not to or not host:
        return None
    user = os.environ.get("SMTP_USER", "").strip()
    return {
        "to": to,
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": os.environ.get("SMTP_FROM", "").strip() or user,
    }


def send_email(subject: str, body: str) -> bool:
    """True if handed to the SMTP server. Never raises: an alert must not crash the watchdog."""
    cfg = _smtp_settings()
    if cfg is None:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content(body)
    try:
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        with server:
            if cfg["port"] != 465:
                server.starttls()
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("alerts: could not send email %r", subject)
        return False


# ---- checks ----------------------------------------------------------------

def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def check_all(db_path: Path, health: dict, now: datetime | None = None) -> dict[str, str]:
    """The issues open right now: {key: human-readable message}."""
    now = now or datetime.now(timezone.utc)
    issues: dict[str, str] = {}

    # 1. refresh loop alive?
    last_ok = health.get("last_ok") or _STARTED
    if now - last_ok > REFRESH_STALE_AFTER:
        msg = f"The 5-minute data refresh has not completed a cycle for {_ago(now - last_ok)} (last success {last_ok:%Y-%m-%d %H:%M} UTC)."
        started = health.get("cycle_started")
        if started and now - started > REFRESH_STALE_AFTER:
            msg += f" The current cycle has been running since {started:%H:%M} UTC and looks stuck."
        if health.get("last_error"):
            msg += f" Last error: {health['last_error']}"
        issues["refresh_stale"] = msg + " Live Market is showing old data."

    conn = connect(db_path)
    try:
        # 2. data arriving?
        row = conn.execute("SELECT MAX(start_time) FROM fuelinst_generation").fetchone()
        if row and row[0]:
            newest = datetime.fromisoformat(row[0])
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            if now - newest > DATA_STALE_AFTER:
                issues["data_stale"] = (
                    f"The newest generation reading (FUELINST) is {_ago(now - newest)} old ({newest:%Y-%m-%d %H:%M} UTC). "
                    "Either the refresh is failing or the upstream feed has stopped."
                )

        # 3. last week's report out?
        london = now.astimezone(ZoneInfo("Europe/London"))
        week_ending = most_recent_sunday(london.date() - timedelta(days=1))
        due = datetime.combine(week_ending + timedelta(days=1), datetime.min.time().replace(hour=WEEKLY_READY_HOUR), tzinfo=london.tzinfo)
        if london > due + WEEKLY_GRACE:
            report = get_report(conn, week_ending)
            if not (report and report["published"]):
                issues["weekly_missing"] = (
                    f"The GB Power Weekly report for the week ending {week_ending} was due on {due:%a %d %b} "
                    f"and is still not published ({'built but unpublished' if report else 'not built'})."
                )
    finally:
        conn.close()

    # 4. disk
    free = shutil.disk_usage(Path(db_path).parent).free
    if free < DISK_LOW_BYTES:
        issues["disk_low"] = f"Only {free / 1024**3:.1f} GB of disk is free on the server; the database and Docker builds need room."

    return issues


# ---- state machine: email on start, remind, resolve -------------------------

class Alerter:
    def __init__(self, send=send_email) -> None:
        self._send = send
        self._open: dict[str, tuple[str, datetime]] = {}  # key -> (message, last emailed)
        self._host = socket.gethostname()

    def process(self, issues: dict[str, str], now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for key, message in issues.items():
            seen = self._open.get(key)
            if seen is None:
                logger.error("ALERT %s: %s", key, message)
                self._send(f"[Mazao] ISSUE: {key}", self._body(message, now))
                self._open[key] = (message, now)
            elif now - seen[1] >= REMINDER_EVERY:
                logger.error("ALERT (still open) %s: %s", key, message)
                self._send(f"[Mazao] STILL OPEN: {key}", self._body(message, now, reminder=True))
                self._open[key] = (message, now)
        for key in [k for k in self._open if k not in issues]:
            logger.warning("ALERT %s resolved", key)
            self._send(f"[Mazao] RESOLVED: {key}", self._body(f"This issue has cleared. It was: {self._open[key][0]}", now))
            del self._open[key]

    def _body(self, message: str, now: datetime, reminder: bool = False) -> str:
        return (
            f"{message}\n\n"
            f"{'Reminder: this has not cleared yet.' + chr(10) if reminder else ''}"
            f"Server: {self._host}\nTime: {now:%Y-%m-%d %H:%M:%S} UTC\n"
            "Site: https://mazao-intelligence.duckdns.org\n"
            "Logs: sudo docker compose logs --since 1h gbpw"
        )


def start_watchdog(db_path: Path, stop: threading.Event, health: dict, interval_seconds: float = CHECK_INTERVAL_SECONDS) -> threading.Thread:
    if _smtp_settings() is None:
        logger.warning("alerts: ALERT_EMAIL_TO / SMTP_HOST not set -- issues will be logged but not emailed")
    alerter = Alerter()

    def _loop() -> None:
        while not stop.wait(interval_seconds):  # first check after one interval, not at startup
            try:
                alerter.process(check_all(db_path, health))
            except Exception:  # noqa: BLE001 -- the watchdog itself must not die
                logger.exception("alerts: watchdog check failed")

    thread = threading.Thread(target=_loop, name="alert-watchdog", daemon=True)
    thread.start()
    return thread
