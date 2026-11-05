"""
Static-file caching. /static/app.css used to be served with no cache headers and a URL that never
changed, so browsers kept an old copy for hours or days after a deploy: new pages arrived with the
new HTML but the old stylesheet (a deployed login page and link card showed up unstyled).

Two fixes together:
- templates link `/static/app.css?v={{ asset_v }}`, a hash of the file's contents, so a changed
  stylesheet is a new URL and every browser fetches it immediately;
- static files are served `Cache-Control: no-cache`, so browsers always revalidate with the ETag
  (a cheap 304 when unchanged) instead of guessing how long a file stays fresh.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    try:
        return hashlib.md5((STATIC_DIR / "app.css").read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


ASSET_VERSION = _asset_version()


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _auth_context(request) -> dict:
    from . import auth  # lazy: auth imports this module

    return auth.session_context(request)


def make_templates(directory) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(directory), context_processors=[_auth_context])
    templates.env.globals["asset_v"] = ASSET_VERSION
    return templates
