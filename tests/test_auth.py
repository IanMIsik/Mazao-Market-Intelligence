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


def test_forged_and_expired_tokens_are_rejected(client):
    conn = connect(client.db)
    secret = client.app.state.session_secret
    h = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    good = auth.make_token(secret, "client@example.com", h)
    assert auth.user_from_token(conn, secret, good) == "client@example.com"
    assert auth.user_from_token(conn, b"other-secret", good) is None
    assert auth.user_from_token(conn, secret, good[:-1] + ("0" if good[-1] != "0" else "1")) is None
    assert auth.user_from_token(conn, secret, good, now=10**12) is None  # expired
    assert auth.user_from_token(conn, secret, "garbage") is None
    conn.close()


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
