import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gbpw.storage import connect  # noqa: E402
from gbpw.web import auth  # noqa: E402
from gbpw.web.app import create_app  # noqa: E402

PW = "correct-horse-battery"
FORM = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("gbpw.web.app.start_background_refresh", lambda *a, **k: threading.Event())
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    auth._failures.clear()
    db = tmp_path / "gbpw.db"
    conn = connect(db)
    auth.upsert_user(conn, "boss@example.com", PW, admin=True)
    auth.upsert_user(conn, "client@example.com", PW)
    conn.close()
    app = create_app(db)

    def client(email=None, password=PW):
        c = TestClient(app, follow_redirects=False)
        if email:
            r = c.post("/login", content=f"email={email}&password={password}&next=/", headers=FORM)
            assert r.status_code == 303
        return c

    class E:
        pass

    e = E()
    e.db, e.client = db, client
    return e


def _csrf(c):
    token = c.cookies.get(auth.COOKIE_NAME)
    return auth.csrf_token(c.app.state.session_secret, token) if hasattr(c, "app") else None


def _post(c, path, **fields):
    from gbpw.web import routes_admin  # noqa: F401

    html = c.get("/admin/users").text
    import re

    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
    body = "&".join(f"{k}={v}" for k, v in {"csrf": csrf, **fields}.items())
    return c.post(path, content=body, headers=FORM)


def _users(env):
    conn = connect(env.db)
    try:
        return {u["email"]: u for u in auth.list_users_detailed(conn)}
    finally:
        conn.close()


def test_only_signed_in_admins_can_open_the_page(env):
    assert env.client().get("/admin/users").headers["location"].startswith("/login?next=/admin/users")
    assert env.client("client@example.com").get("/admin/users").status_code == 403
    page = env.client("boss@example.com").get("/admin/users")
    assert page.status_code == 200 and "client@example.com" in page.text and "Add a user" in page.text


def test_changes_are_refused_for_non_admins_and_without_a_csrf_token(env):
    user = env.client("client@example.com")
    r = user.post("/admin/users/create", content="email=x@example.com&password=some-long-password&csrf=nope", headers=FORM)
    assert r.status_code == 403 and "x@example.com" not in _users(env)

    admin = env.client("boss@example.com")
    r = admin.post("/admin/users/create", content="email=x@example.com&password=some-long-password&csrf=forged", headers=FORM)
    assert "expired" in r.headers["location"].replace("+", " ") and "x@example.com" not in _users(env)


def test_admin_creates_a_user_who_can_then_sign_in_and_can_make_an_admin(env):
    admin = env.client("boss@example.com")
    r = _post(admin, "/admin/users/create", email="new%40example.com", password="a-fresh-password-1", is_admin="on")
    assert "msg=Created" in r.headers["location"]
    assert _users(env)["new@example.com"]["admin"] is True
    newbie = env.client("new@example.com", password="a-fresh-password-1")  # asserts the login works
    assert newbie.get("/admin/users").status_code == 200


def test_create_rejects_duplicates_bad_emails_and_short_passwords(env):
    admin = env.client("boss@example.com")
    assert "already" in _post(admin, "/admin/users/create", email="client%40example.com", password="a-fresh-password-1").headers["location"]
    assert "err=" in _post(admin, "/admin/users/create", email="nope", password="a-fresh-password-1").headers["location"]
    assert "err=" in _post(admin, "/admin/users/create", email="z%40example.com", password="short").headers["location"]
    assert set(_users(env)) == {"boss@example.com", "client@example.com"}


def test_reset_password_signs_the_user_out_and_the_new_password_works(env):
    client_session = env.client("client@example.com")
    assert client_session.get("/auth/check").status_code == 204
    admin = env.client("boss@example.com")
    _post(admin, "/admin/users/password", email="client%40example.com", password="a-brand-new-password")
    assert client_session.get("/auth/check").status_code == 401
    c = TestClient(admin.app, follow_redirects=False)
    assert c.post("/login", content="email=client@example.com&password=a-brand-new-password&next=/", headers=FORM).status_code == 303


def test_grant_and_remove_admin_and_delete(env):
    admin = env.client("boss@example.com")
    _post(admin, "/admin/users/admin", email="client%40example.com", make_admin="1")
    assert _users(env)["client@example.com"]["admin"] is True
    _post(admin, "/admin/users/admin", email="client%40example.com", make_admin="0")
    assert _users(env)["client@example.com"]["admin"] is False
    victim = env.client("client@example.com")
    _post(admin, "/admin/users/delete", email="client%40example.com")
    assert "client@example.com" not in _users(env) and victim.get("/auth/check").status_code == 401


def test_you_cannot_lock_yourself_out(env):
    admin = env.client("boss@example.com")
    assert "cannot delete" in _post(admin, "/admin/users/delete", email="boss%40example.com").headers["location"].replace("+", " ")
    assert "cannot remove your own" in _post(admin, "/admin/users/admin", email="boss%40example.com", make_admin="0").headers["location"].replace("+", " ")
    assert "boss@example.com" in _users(env) and _users(env)["boss@example.com"]["admin"] is True


def test_admin_emails_setting_grants_admin_and_cannot_be_demoted_in_the_ui(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", " Owner@Example.com , other@example.com")
    conn = connect(env.db)
    auth.upsert_user(conn, "owner@example.com", PW)  # created as an ordinary user...
    conn.close()
    owner = env.client("owner@example.com")           # ...but listed in the setting, so admin
    assert owner.get("/admin/users").status_code == 200
    assert _users(env)["owner@example.com"]["config_admin"] is True
    boss = env.client("boss@example.com")
    assert "ADMIN_EMAILS" in _post(boss, "/admin/users/admin", email="owner%40example.com", make_admin="0").headers["location"].replace("+", " ")


def test_the_top_bar_shows_who_is_signed_in_and_a_users_link_only_to_admins(env):
    assert "/admin/users" not in env.client().get("/login").text
    plain = env.client("client@example.com").get("/ppa").text
    assert "client@example.com" in plain and "/logout" in plain and 'href="/admin/users"' not in plain
    assert 'href="/admin/users"' in env.client("boss@example.com").get("/ppa").text
    assert "who-email" not in env.client().get("/ppa").text


def test_existing_users_table_without_is_admin_is_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE users (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
    old.execute("INSERT INTO users VALUES ('a@b.com', 'x', '2026-11-05')")
    old.commit()
    old.close()
    conn = connect(db)  # runs the migration
    assert conn.execute("SELECT email, is_admin FROM users").fetchall() == [("a@b.com", 0)]
    conn.close()
