import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gbpw.storage import connect  # noqa: E402
from gbpw.web import auth  # noqa: E402
from gbpw.web.app import create_app  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("gbpw.web.app.start_background_refresh", lambda *a, **k: __import__("threading").Event())
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    auth._failures.clear()
    db = tmp_path / "gbpw.db"
    conn = connect(db)
    auth.upsert_user(conn, "Client@Example.com", PASSWORD)
    conn.close()
    c = TestClient(create_app(db), follow_redirects=False)
    c.db = db
    return c


def _login(c, email="client@example.com", password=PASSWORD, next_="/risk/"):
    return c.post("/login", content=f"email={email}&password={password}&next={next_}",
                  headers={"content-type": "application/x-www-form-urlencoded"})


def test_login_page_renders_a_real_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'type="password"' in r.text and 'action="/login"' in r.text


def test_check_is_401_without_a_session_and_204_with_one(client):
    assert client.get("/auth/check").status_code == 401
    r = _login(client)
    assert r.status_code == 303 and r.headers["location"] == "/risk/"
    assert "httponly" in r.headers["set-cookie"].lower()
    ok = client.get("/auth/check")
    assert ok.status_code == 204 and ok.headers["x-auth-user"] == "client@example.com"


def test_wrong_password_and_unknown_email_get_the_same_error_and_no_session(client):
    for email, pw in (("client@example.com", "wrong-password-1"), ("nobody@example.com", PASSWORD)):
        r = _login(client, email, pw)
        assert r.status_code == 401 and "Incorrect email or password." in r.text
    assert client.get("/auth/check").status_code == 401


def test_repeated_failures_are_rate_limited_even_for_the_right_password(client):
    for _ in range(auth.MAX_FAILURES):
        _login(client, password="wrong-password-1")
    assert _login(client).status_code == 429


def test_logout_ends_the_session(client):
    _login(client)
    assert client.get("/auth/check").status_code == 204
    r = client.get("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/auth/check").status_code == 401


def test_changing_a_password_or_deleting_the_user_revokes_existing_sessions(client):
    _login(client)
    conn = connect(client.db)
    auth.upsert_user(conn, "client@example.com", "a-brand-new-password")
    assert client.get("/auth/check").status_code == 401
    conn.close()
    r = _login(client, password="a-brand-new-password")
    assert r.status_code == 303 and client.get("/auth/check").status_code == 204
    conn = connect(client.db)
    auth.delete_user(conn, "client@example.com")
    conn.close()
    assert client.get("/auth/check").status_code == 401


def test_forged_tokens_are_rejected(client):
    conn = connect(client.db)
    secret = client.app.state.session_secret
    good = auth.make_token(conn, secret, "client@example.com")
    assert auth.user_from_token(conn, secret, good) == "client@example.com"
    assert auth.user_from_token(conn, b"other-secret", good) is None
    assert auth.user_from_token(conn, secret, good[:-1] + ("0" if good[-1] != "0" else "1")) is None
    assert auth.user_from_token(conn, secret, "garbage") is None
    assert auth.user_from_token(conn, secret, None) is None
    conn.close()


T0 = 1_800_000_000.0  # an arbitrary fixed "now" for the timeout tests


def _session(client, monkeypatch, idle=None, max_hours=None):
    for key, value in (("SESSION_IDLE_MINUTES", idle), ("SESSION_MAX_HOURS", max_hours)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    conn = connect(client.db)
    secret = client.app.state.session_secret
    return conn, secret, auth.make_token(conn, secret, "client@example.com", now=T0)


def test_a_session_ends_after_the_idle_timeout(client, monkeypatch):
    conn, secret, token = _session(client, monkeypatch)  # defaults: 60 min idle, 12 h max
    assert auth.user_from_token(conn, secret, token, now=T0 + 59 * 60) == "client@example.com"
    # 59 min later again is still within the idle window of that last activity...
    assert auth.user_from_token(conn, secret, token, now=T0 + 59 * 60 + 59 * 60) == "client@example.com"
    # ...but a gap of over 60 minutes with no activity ends it, and the row is gone for good.
    assert auth.user_from_token(conn, secret, token, now=T0 + 59 * 60 + 59 * 60 + 61 * 60) is None
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert auth.user_from_token(conn, secret, token, now=T0 + 60) is None  # cannot be revived
    conn.close()


def test_a_session_ends_after_the_absolute_maximum_even_while_active(client, monkeypatch):
    conn, secret, token = _session(client, monkeypatch)
    t = T0
    while t < T0 + 11.5 * 3600:  # busy the whole time, a request every 30 minutes
        t += 30 * 60
        assert auth.user_from_token(conn, secret, token, now=t) == "client@example.com"
    assert auth.user_from_token(conn, secret, token, now=T0 + 12 * 3600 + 30) is None
    conn.close()


def test_the_limits_can_be_changed_with_settings(client, monkeypatch):
    conn, secret, token = _session(client, monkeypatch, idle=5, max_hours=1)
    assert auth.user_from_token(conn, secret, token, now=T0 + 4 * 60) == "client@example.com"
    assert auth.user_from_token(conn, secret, token, now=T0 + 4 * 60 + 6 * 60) is None  # 5 min idle limit
    token2 = auth.make_token(conn, secret, "client@example.com", now=T0)
    for minute in range(4, 68, 4):
        user = auth.user_from_token(conn, secret, token2, now=T0 + minute * 60)
    assert user is None  # past the 1 hour maximum although never idle
    conn.close()


def test_signing_out_ends_the_session_on_the_server_so_a_copied_cookie_stops_working(client):
    c = TestClient(client.app, follow_redirects=False)
    _login(c)
    stolen = c.cookies.get(auth.COOKIE_NAME)
    assert c.get("/auth/check").status_code == 204
    c.get("/logout")
    replay = TestClient(client.app, follow_redirects=False)
    replay.cookies.set(auth.COOKIE_NAME, stolen)
    assert replay.get("/auth/check").status_code == 401


def test_every_login_is_its_own_session_and_the_cookie_lifetime_matches_the_limit(client):
    first, second = TestClient(client.app, follow_redirects=False), TestClient(client.app, follow_redirects=False)
    r = _login(first)
    _login(second)
    assert first.cookies.get(auth.COOKIE_NAME) != second.cookies.get(auth.COOKIE_NAME)
    assert f"Max-Age={12 * 3600}" in r.headers["set-cookie"]
    first.get("/logout")
    assert second.get("/auth/check").status_code == 204  # signing one device out leaves the other alone


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example", "/\\evil", "javascript:alert(1)", ""])
def test_next_only_ever_redirects_within_the_site(client, bad):
    assert auth.safe_next(bad) == auth.DEFAULT_NEXT
    r = _login(client, next_=bad)
    assert r.headers["location"] == "/risk/"


def test_next_keeps_a_local_path_and_signed_in_users_skip_the_form(client):
    r = _login(client, next_="/risk/some/page")
    assert r.headers["location"] == "/risk/some/page"
    again = client.get("/login?next=/risk/x")
    assert again.status_code == 303 and again.headers["location"] == "/risk/x"


def test_password_rules_and_stored_hash(tmp_path):
    conn = connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        auth.upsert_user(conn, "a@b.com", "short")
    with pytest.raises(ValueError):
        auth.upsert_user(conn, "not-an-email", PASSWORD)
    assert auth.upsert_user(conn, "a@b.com", PASSWORD) is True
    assert auth.upsert_user(conn, "A@B.com", PASSWORD) is False  # same account, case-insensitive
    stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert auth.verify_password(PASSWORD, stored) and not auth.verify_password("nope-nope-nope", stored)


def test_the_rest_of_the_site_stays_public(client):
    assert client.get("/static/app.css").status_code == 200
