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
- Session cookie: "<b64 payload>.<hmac-sha256>", HttpOnly, SameSite=Lax,
  Secure behind HTTPS. The payload carries a tag derived from the password
  hash, so changing a password signs that user out everywhere; deleting a
  user does the same (checked against the database on every request).
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
from .deps import get_db

COOKIE_NAME = "mazao_session"
SESSION_SECONDS = 14 * 24 * 3600
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


def upsert_user(conn: sqlite3.Connection, email: str, password: str) -> bool:
    """Creates the user or resets their password. Returns True if newly created."""
    email = normalise_email(email)
    if "@" not in email or " " in email:
        raise ValueError("That does not look like an email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    existed = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None
    conn.execute(
        """
        INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)
        ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash
        """,
        (email, hash_password(password), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return not existed


def delete_user(conn: sqlite3.Connection, email: str) -> bool:
    cur = conn.execute("DELETE FROM users WHERE email = ?", (normalise_email(email),))
    conn.commit()
    return cur.rowcount > 0


def list_users(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute("SELECT email, created_at FROM users ORDER BY email").fetchall()


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


def _tag(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:12]


def make_token(secret: bytes, email: str, password_hash: str, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    payload = _b64(f"{email}|{expires}|{_tag(password_hash)}".encode())
    return f"{payload}.{hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()}"


def user_from_token(
    conn: sqlite3.Connection, secret: bytes, token: str | None, now: float | None = None
) -> str | None:
    """The signed-in email, or None for a missing, forged, expired or revoked session."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        email, expires, tag = _unb64(payload).decode().split("|")
        if int(expires) < (now if now is not None else time.time()):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    row = conn.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not hmac.compare_digest(_tag(row[0]), tag):
        return None
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
        COOKIE_NAME, make_token(request.app.state.session_secret, email, row[0]),
        max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=secure, path="/",
    )
    return response


@router.api_route("/logout", methods=["GET", "POST"])
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
