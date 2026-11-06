"""
Sign-in for the private part of the site (today: /risk, the Portfolio Risk
KPI tool). A normal login page, accounts in this app's own SQLite database,
a signed session cookie -- no third-party service and no new dependency.

Nginx protects /risk with `auth_request /auth/check` (see deploy/setup.sh):
every request under /risk makes a subrequest to GET /auth/check, which
answers 204 (signed in) or 401, and a 401 sends the browser to /login.
The rest of the site stays public.

Security choices, so they are not re-litigated:
- Passwords: scrypt with a per-user salt (stdlib), compared in constant time.
- Sessions are server-side (table `sessions`): the cookie is "<random id>.<hmac-sha256>", HttpOnly,
  SameSite=Lax, Secure behind HTTPS. A session ends after SESSION_IDLE_MINUTES (default 60) without
  activity, or SESSION_MAX_HOURS (default 12) after sign-in, whichever comes first; also on sign-out,
  a password change, or deleting the user. Every request is checked against the database.
- The signing secret is SESSION_SECRET, or else generated once into
  data/.session_secret (the data volume survives container rebuilds).
- Failed logins are rate-limited per client address (in memory).
- `next` redirects are only ever same-site paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .assets import make_templates
from ..storage import connect
from .deps import get_db

COOKIE_NAME = "mazao_session"
DEFAULT_IDLE_MINUTES = 60   # signed out after this long without any activity
DEFAULT_MAX_HOURS = 12      # signed out this long after signing in, however active
TOUCH_EVERY_SECONDS = 60    # how often an active session's last-seen time is written back


def idle_seconds() -> float:
    return float(os.environ.get("SESSION_IDLE_MINUTES") or DEFAULT_IDLE_MINUTES) * 60


def max_seconds() -> float:
    return float(os.environ.get("SESSION_MAX_HOURS") or DEFAULT_MAX_HOURS) * 3600
DEFAULT_NEXT = "/risk/"
MIN_PASSWORD_LENGTH = 10
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

templates = make_templates(Path(__file__).resolve().parent / "templates")
router = APIRouter()


# ---- passwords and users ---------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


# Verified against when the email is unknown, so that path costs the same time as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password")


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _validate_new(email: str, password: str) -> str:
    email = normalise_email(email)
    if "@" not in email or " " in email:
        raise ValueError("That does not look like an email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return email


def upsert_user(conn: sqlite3.Connection, email: str, password: str, admin: bool | None = None) -> bool:
    """Creates the user or resets their password. `admin` None leaves an existing user's admin flag
    alone (a new user is not an admin). Returns True if newly created."""
    email = _validate_new(email, password)
    existed = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None
    conn.execute(
        """
        INSERT INTO users (email, password_hash, created_at, is_admin) VALUES (?, ?, ?, ?)
        ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash
        """,
        (email, hash_password(password), datetime.now(timezone.utc).isoformat(), int(bool(admin))),
    )
    if admin is not None and existed:
        conn.execute("UPDATE users SET is_admin = ? WHERE email = ?", (int(admin), email))
    if existed:  # a changed password signs that user out everywhere
        conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    conn.commit()
    return not existed


def set_admin(conn: sqlite3.Connection, email: str, admin: bool) -> bool:
    cur = conn.execute("UPDATE users SET is_admin = ? WHERE email = ?", (int(admin), normalise_email(email)))
    conn.commit()
    return cur.rowcount > 0


def config_admins() -> set[str]:
    """Emails that are always admins, from the ADMIN_EMAILS setting (comma-separated). A safety net
    as well as a bootstrap: whatever happens to the database, these accounts keep admin access."""
    return {normalise_email(e) for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(conn: sqlite3.Connection, email: str) -> bool:
    email = normalise_email(email)
    if email in config_admins():
        return True
    row = conn.execute("SELECT is_admin FROM users WHERE email = ?", (email,)).fetchone()
    return bool(row and row[0])


def delete_user(conn: sqlite3.Connection, email: str) -> bool:
    email = normalise_email(email)
    cur = conn.execute("DELETE FROM users WHERE email = ?", (email,))
    conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    conn.commit()
    return cur.rowcount > 0


def list_users(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute("SELECT email, created_at FROM users ORDER BY email").fetchall()


def list_users_detailed(conn: sqlite3.Connection) -> list[dict]:
    admins = config_admins()
    return [
        {"email": email, "created": created[:10], "admin": bool(flag) or email in admins, "config_admin": email in admins}
        for email, created, flag in conn.execute("SELECT email, created_at, is_admin FROM users ORDER BY email")
    ]


# ---- session cookie --------------------------------------------------------

def load_secret(db_path: Path) -> bytes:
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    path = Path(db_path).parent / ".session_secret"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        path.write_bytes(secret)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return secret


def _sign(secret: bytes, sid: str) -> str:
    return hmac.new(secret, sid.encode(), hashlib.sha256).hexdigest()


def make_token(conn: sqlite3.Connection, secret: bytes, email: str, now: float | None = None) -> str:
    """Starts a session for `email` and returns the cookie value: a random session id plus its signature."""
    now = now if now is not None else time.time()
    conn.execute("DELETE FROM sessions WHERE ? - last_seen > ? OR ? - created > ?", (now, idle_seconds(), now, max_seconds()))
    sid = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO sessions (id, email, created, last_seen) VALUES (?, ?, ?, ?)", (sid, email, now, now))
    conn.commit()
    return f"{sid}.{_sign(secret, sid)}"


def end_session(conn: sqlite3.Connection, secret: bytes, token: str | None) -> None:
    sid, _, sig = (token or "").partition(".")
    if sid and hmac.compare_digest(sig, _sign(secret, sid)):
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        conn.commit()


def user_from_token(
    conn: sqlite3.Connection, secret: bytes, token: str | None, now: float | None = None
) -> str | None:
    """The signed-in email, or None for a missing, forged, idle-expired, too-old or revoked session.
    A valid request counts as activity and keeps the idle clock from running out."""
    if not token or "." not in token:
        return None
    sid, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(secret, sid)):
        return None
    now = now if now is not None else time.time()
    row = conn.execute("SELECT email, created, last_seen FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        return None
    email, created, last_seen = row
    if now - last_seen > idle_seconds() or now - created > max_seconds() or conn.execute(
        "SELECT 1 FROM users WHERE email = ?", (email,)
    ).fetchone() is None:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        conn.commit()
        return None
    if now - last_seen >= TOUCH_EVERY_SECONDS:
        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, sid))
        conn.commit()
    return email


# ---- helpers ---------------------------------------------------------------

def safe_next(value: str | None) -> str:
    """Only same-site absolute paths; anything else falls back to the default."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT
    return value


def client_address(request: Request) -> str:
    # Behind Nginx the socket peer is always loopback; Nginx sets X-Real-IP.
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        return request.headers.get("x-real-ip", peer)
    return peer


_failures: dict[str, list[float]] = {}


def _recent_failures(addr: str, now: float) -> list[float]:
    kept = [t for t in _failures.get(addr, []) if now - t < FAILURE_WINDOW_SECONDS]
    _failures[addr] = kept
    return kept


async def read_form(request: Request) -> dict[str, str]:
    raw = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in raw.items()}


def csrf_token(secret: bytes, session_token: str | None) -> str:
    """Tied to the session cookie, so a form from one session cannot be replayed in another."""
    return hmac.new(secret, f"csrf|{session_token or ''}".encode(), hashlib.sha256).hexdigest()


def session_context(request: Request) -> dict:
    """Template context for every page: who is signed in (None if nobody), and whether an admin."""
    token = request.cookies.get(COOKIE_NAME)
    state = getattr(request.app, "state", None)
    if not token or state is None or not hasattr(state, "db_path"):
        return {"auth_user": None, "auth_is_admin": False}
    conn = connect(state.db_path)
    try:
        email = user_from_token(conn, state.session_secret, token)
        return {"auth_user": email, "auth_is_admin": bool(email and is_admin(conn, email))}
    finally:
        conn.close()


def _current_user(request: Request, conn: sqlite3.Connection) -> str | None:
    return user_from_token(conn, request.app.state.session_secret, request.cookies.get(COOKIE_NAME))


def _page(request: Request, status: int, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", ctx, status_code=status)


# ---- routes ----------------------------------------------------------------

@router.get("/auth/check")
def auth_check(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """Nginx auth_request target: 204 + X-Auth-User when signed in, else 401."""
    email = _current_user(request, conn)
    if email is None:
        return Response(status_code=401)
    return Response(status_code=204, headers={"X-Auth-User": email})


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    target = safe_next(next)
    if _current_user(request, conn):
        return RedirectResponse(target, status_code=303)
    return _page(request, 200, next=target, error=None, email="")


@router.post("/login")
async def login_submit(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    email = normalise_email(form.get("email", [""])[0])
    password = form.get("password", [""])[0]
    target = safe_next(form.get("next", [""])[0])

    addr, now = client_address(request), time.time()
    if len(_recent_failures(addr, now)) >= MAX_FAILURES:
        return _page(request, 429, next=target, email=email,
                     error="Too many failed attempts. Please wait 15 minutes and try again.")

    row = conn.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
    # Always run a hash so an unknown email costs the same time as a wrong password.
    ok = verify_password(password, row[0] if row else _DUMMY_HASH)
    if not row or not ok:
        _failures.setdefault(addr, []).append(now)
        return _page(request, 401, next=target, email=email, error="Incorrect email or password.")

    _failures.pop(addr, None)
    response = RedirectResponse(target, status_code=303)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        COOKIE_NAME, make_token(conn, request.app.state.session_secret, email),
        max_age=int(max_seconds()), httponly=True, samesite="lax", secure=secure, path="/",
    )
    return response


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    end_session(conn, request.app.state.session_secret, request.cookies.get(COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
