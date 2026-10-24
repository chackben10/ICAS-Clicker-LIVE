from __future__ import annotations

import pytest
from urllib.parse import urlsplit

from testing.harness.web_server import safe_page_server


@pytest.fixture(scope="session")
def safe_web_base_url():
    with safe_page_server() as base_url:
        yield base_url


@pytest.fixture(autouse=True)
def isolate_browser_network(request, safe_web_base_url):
    """Even pages with legacy hosted defaults must use the local fake API."""
    if "page" not in request.fixturenames:
        yield
        return
    import os
    if os.environ.get("PRODUCTION_HUB_DEPLOYED_SMOKE") == "1":
        yield
        return
    page = request.getfixturevalue("page")
    allowed = urlsplit(safe_web_base_url).netloc

    def local_only(route):
        target = urlsplit(route.request.url)
        if target.netloc in {"fonts.googleapis.com", "fonts.gstatic.com"}:
            # WebKit rejects a stylesheet served with a non-CSS type.
            route.fulfill(body="", content_type="text/css")
            return
        if target.netloc == "cdn.jsdelivr.net" and target.path == "/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js":
            from pathlib import Path
            fixture = Path(__file__).resolve().parents[2] / "fixtures/web/confetti.browser.min.js"
            route.fulfill(path=str(fixture), content_type="application/javascript")
            return
        cors = {
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
            "access-control-allow-headers": "*",
        }
        if route.request.method == "OPTIONS":
            route.fulfill(status=204, headers=cors)
            return
        suffix = target.path + ("?" + target.query if target.query else "")
        response = route.fetch(url=safe_web_base_url + suffix, max_redirects=0)
        route.fulfill(response=response, headers={**response.headers, **cors})

    def external(url):
        # Leave the page's own requests and data:/blob: URLs uninterrupted;
        # WebKit reports intercepted same-origin fetches cancelled by a
        # navigation as access-control failures.
        target = urlsplit(url)
        return target.scheme in {"http", "https"} and target.netloc != allowed

    page.context.route(external, local_only)
    yield
    page.context.unroute(external, local_only)

