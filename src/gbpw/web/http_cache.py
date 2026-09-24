"""
Conditional GET (ETag / If-None-Match) for pages whose content is driven
by background_refresh.py's periodic re-ingest -- Live Market and BESS
Analytics, both the kind of page someone leaves open and repeatedly
reloads, but which genuinely only change on a 5-minute cadence
(background_refresh.DEFAULT_INTERVAL_SECONDS). Without this, every reload
reruns every query and rebuilds every chart from scratch just to produce
HTML that's byte-for-byte identical to what the client already has --
fine at low traffic, wasteful once real concurrent traffic shows up.

Standard HTTP conditional GET, not a second cache store (Redis etc.):
storage.latest_fetch_ts() already tracks the one signal that actually
matters here (has ANY series changed since the client's copy), so no new
dependency, and it composes for free with any reverse proxy/CDN put in
front later -- they understand ETag/Cache-Control natively.
"""

from __future__ import annotations

from fastapi import Request, Response


def etag_for(*parts: object) -> str:
    """Opaque ETag identifying this exact response -- last_updated alone
    for a page with no query params, or last_updated plus every param
    that changes the rendered output (e.g. BESS Analytics' window/
    auction_day). An ETag must capture the full response identity, not
    just "is the underlying data fresh", or a client could 304 into
    stale content left over from a different combination of params.
    """
    return '"' + "-".join(str(p) for p in parts) + '"'


def not_modified(request: Request, etag: str) -> Response | None:
    """A 304 if the client's cached copy (via If-None-Match) already
    matches this exact ETag -- callers check this first and return
    immediately, before running any of the real queries/chart-building
    the page needs. None means render as normal, then call
    apply_cache_headers() on the real response before returning it.
    """
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return None


def apply_cache_headers(response: Response, etag: str) -> None:
    """Cache-Control: no-cache (not no-store) -- the client/a proxy may
    keep the body, but must always revalidate with the server first,
    since this page can change at any moment background_refresh.py's
    loop lands new data. That revalidation is exactly what makes the
    304 path above possible on the next request.
    """
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
