from __future__ import annotations

import os

import pytest

from production_hub.core.config.defaults import build_default_config


DEPLOYED_BASE = "https://icas-clicker.work"
PAGES = [item.path for item in build_default_config().remote_pages]
TRANSPARENT_DATA_OVERLAYS = {
    "displays/current-audio.html",
    "scoreboard/large.html",
    "scoreboard/dynamic.html",
    "scoreboard/top-right.html",
    "scoreboard/bottom-right.html",
}


@pytest.mark.browser
@pytest.mark.parametrize("page_path", PAGES)
def test_deployed_page_safe_smoke(page, page_path: str) -> None:
    if os.environ.get("PRODUCTION_HUB_DEPLOYED_SMOKE") != "1":
        pytest.skip("enable with PRODUCTION_HUB_DEPLOYED_SMOKE=1")
    attempted_mutations: list[str] = []

    def protect_production(route, request) -> None:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            attempted_mutations.append(f"{request.method} {request.url}")
            route.fulfill(status=204, body="")
            return
        route.continue_()

    page.route("**/*", protect_production)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.goto(f"{DEPLOYED_BASE}/{page_path}", wait_until="domcontentloaded", timeout=30000)
    assert response is not None and response.status < 400, (page_path, response.status if response else None)
    if page_path == "picker.html":
        page.wait_for_url(f"{DEPLOYED_BASE}/index.html", timeout=30000)
        page.wait_for_load_state("domcontentloaded")
    assert page.locator("body").count() == 1
    if page_path not in TRANSPARENT_DATA_OVERLAYS:
        assert page.locator("body").inner_text().strip()
    assert not errors, (page_path, errors)
    # Mutations, if a page performs one during startup, were fulfilled locally
    # and never reached the production tunnel.
    assert all(item.split(" ", 1)[0] not in {"GET", "HEAD", "OPTIONS"} for item in attempted_mutations)
