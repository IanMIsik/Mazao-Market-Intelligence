import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gbpw.web import assets  # noqa: E402
from gbpw.web.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("gbpw.web.app.start_background_refresh", lambda *a, **k: threading.Event())
    return TestClient(create_app(tmp_path / "gbpw.db"), follow_redirects=False)


def test_static_files_are_always_revalidated_not_cached_blind(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert r.headers.get("etag")  # so a revalidation is a cheap 304


@pytest.mark.parametrize("path", ["/login", "/ppa", "/bess"])
def test_pages_link_the_stylesheet_with_a_content_hash_so_a_new_css_is_a_new_url(client, path):
    html = client.get(path).text
    assert re.search(r'href="/static/app\.css\?v=[0-9a-f]{10}"', html)
    assert f"?v={assets.ASSET_VERSION}" in html


def test_the_version_is_the_hash_of_the_stylesheet_contents():
    import hashlib

    expected = hashlib.md5((assets.STATIC_DIR / "app.css").read_bytes()).hexdigest()[:10]
    assert assets.ASSET_VERSION == expected
