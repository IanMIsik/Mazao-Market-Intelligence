"""
Admin page for managing sign-in accounts (/admin/users): create a user, reset a password, grant or
remove admin, delete a user. Only admins can open it (see auth.is_admin: a flag in the database,
or an address listed in the ADMIN_EMAILS setting, which always stays admin).

Every change is a POST carrying a CSRF token tied to the admin's own session cookie. Guards against
locking yourself out: you cannot delete or demote your own account, and an account made admin by the
ADMIN_EMAILS setting cannot be demoted here (change the setting instead).
"""

from __future__ import annotations

import hmac
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth
from .assets import make_templates
from .deps import get_db

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = make_templates(TEMPLATES_DIR)
router = APIRouter(prefix="/admin")

PAGE = "/admin/users"


def _gate(request: Request, conn: sqlite3.Connection) -> tuple[str | None, Response | None]:
    """(admin's email, None) if allowed, else (None, the response to send instead)."""
    email = auth._current_user(request, conn)
    if email is None:
        return None, RedirectResponse(f"/login?next={PAGE}", status_code=303)
    if not auth.is_admin(conn, email):
        return None, HTMLResponse("<h1>403</h1><p>Admins only.</p>", status_code=403)
    return email, None


def _csrf_ok(request: Request, form: dict[str, str]) -> bool:
    expected = auth.csrf_token(request.app.state.session_secret, request.cookies.get(auth.COOKIE_NAME))
    return hmac.compare_digest(form.get("csrf", ""), expected)


def _back(msg: str | None = None, err: str | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{PAGE}?{query}" if query else PAGE, status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str | None = None, err: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    me, denied = _gate(request, conn)
    if denied:
        return denied
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "users": auth.list_users_detailed(conn),
            "me": me,
            "csrf": auth.csrf_token(request.app.state.session_secret, request.cookies.get(auth.COOKIE_NAME)),
            "msg": msg,
            "err": err,
            "min_length": auth.MIN_PASSWORD_LENGTH,
        },
    )


async def _post(request: Request, conn: sqlite3.Connection):
    """Common checks for every change: admin, valid CSRF. Returns (me, form, early_response)."""
    me, denied = _gate(request, conn)
    if denied:
        return None, {}, denied
    form = await auth.read_form(request)
    if not _csrf_ok(request, form):
        return None, {}, _back(err="Your session form expired. Please try again.")
    return me, form, None


@router.post("/users/create")
async def create_user(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    me, form, early = await _post(request, conn)
    if early:
        return early
    email = auth.normalise_email(form.get("email", ""))
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return _back(err=f"{email} already has an account. Use Reset password instead.")
    try:
        auth.upsert_user(conn, email, form.get("password", ""), admin=form.get("is_admin") == "on")
    except ValueError as e:
        return _back(err=str(e))
    return _back(msg=f"Created {email}.")


@router.post("/users/password")
async def reset_password(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    me, form, early = await _post(request, conn)
    if early:
        return early
    email = auth.normalise_email(form.get("email", ""))
    if not conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return _back(err="No such user.")
    try:
        auth.upsert_user(conn, email, form.get("password", ""))
    except ValueError as e:
        return _back(err=str(e))
    return _back(msg=f"Password reset for {email}. Any session they had is signed out.")


@router.post("/users/admin")
async def change_admin(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    me, form, early = await _post(request, conn)
    if early:
        return early
    email = auth.normalise_email(form.get("email", ""))
    make = form.get("make_admin") == "1"
    if not make and email == me:
        return _back(err="You cannot remove your own admin access.")
    if not make and email in auth.config_admins():
        return _back(err=f"{email} is an admin through the server's ADMIN_EMAILS setting; change that setting instead.")
    if not auth.set_admin(conn, email, make):
        return _back(err="No such user.")
    return _back(msg=f"{email} is {'now an admin' if make else 'no longer an admin'}.")


@router.post("/users/delete")
async def delete_user(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    me, form, early = await _post(request, conn)
    if early:
        return early
    email = auth.normalise_email(form.get("email", ""))
    if email == me:
        return _back(err="You cannot delete your own account.")
    if not auth.delete_user(conn, email):
        return _back(err="No such user.")
    return _back(msg=f"Deleted {email}. Their sessions stop working immediately.")
