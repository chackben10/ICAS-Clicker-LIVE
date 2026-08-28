from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import os
import re
import statistics
import time

import pytest

from testing.harness.inventory import html_pages
from testing.harness.benchmarks import compare, load_baseline, percentile, write_json_atomic
from testing.harness.paths import REPOSITORY_ROOT


PAGES = sorted(html_pages())


class ControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        actionable = (
            tag == "button"
            or tag == "a" and bool(values.get("href"))
            or tag == "input" and values.get("type", "").lower() in {"button", "submit"}
            or values.get("role") == "button"
        )
        if actionable:
            self.controls.append(values)


@pytest.mark.browser
@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 1024, "height": 1366}], ids=["desktop", "ipad"])
@pytest.mark.parametrize("scenario", ["populated", "empty", "loading", "offline", "reconnect"])
@pytest.mark.parametrize("page_path", PAGES)
def test_every_remote_page_has_a_safe_browser_scenario(
    page,
    safe_web_base_url: str,
    page_path: str,
    scenario: str,
    viewport: dict[str, int],
    tmp_path: Path,
) -> None:
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "console",
        lambda message: console_errors.append(
            f"{message.text} @ {message.location.get('url', '<unknown>')}"
        )
        if message.type == "error"
        else None,
    )

    page.set_viewport_size(viewport)
    run_id = re.sub(r"[^a-zA-Z0-9]", "", tmp_path.name)
    page.context.add_cookies(
        [
            {"name": "ph_test_scenario", "value": scenario, "url": safe_web_base_url},
            {"name": "ph_test_run", "value": run_id, "url": safe_web_base_url},
        ]
    )
    response = page.goto(f"{safe_web_base_url}/remote/{page_path}", wait_until="domcontentloaded")
    assert response is not None and response.ok, page_path
    page.wait_for_timeout(350)
    assert "<body" in page.content().lower()
    if scenario == "populated":
        assert page.locator("body").inner_text().strip(), page_path
    assert not page_errors, f"{page_path}: {page_errors}"
    if scenario == "populated":
        assert not [item for item in console_errors if "favicon" not in item.lower()], f"{page_path}: {console_errors}"
    output_root = Path(os.environ.get("PRODUCTION_HUB_BROWSER_OUTPUT", tmp_path))
    output_root.mkdir(parents=True, exist_ok=True)
    viewport_name = "ipad" if viewport["width"] == 1024 else "desktop"
    screenshot = output_root / f"{page_path.replace('/', '-')}-{scenario}-{viewport_name}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    layout = page.evaluate(
        """() => ({
            clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
        })"""
    )
    assert layout["clientWidth"] == viewport["width"]
    assert layout["scrollWidth"] <= viewport["width"] + 1, (page_path, layout)


@pytest.mark.browser
def test_scoreboard_conflict_is_visible_and_recovers(page, safe_web_base_url: str) -> None:
    page.context.add_cookies(
        [{"name": "ph_test_scenario", "value": "conflict", "url": safe_web_base_url}]
    )
    page.goto(f"{safe_web_base_url}/score.html", wait_until="domcontentloaded")
    page.locator('[data-testid="addRowBtn"]').click()
    page.wait_for_timeout(200)
    assert page.get_by_text("Sync error").count() or page.get_by_text("Conflict").count()


@pytest.mark.browser
def test_transparent_overlays_handle_empty_offline_and_reconnect_states(page, safe_web_base_url: str) -> None:
    for page_path, selector in (
        ("displays/current-audio.html", "#audioText"),
        ("scoreboard/large.html", ".tile"),
        ("scoreboard/dynamic.html", ".team"),
        ("scoreboard/top-right.html", ".team"),
        ("scoreboard/bottom-right.html", ".team"),
    ):
        page.context.add_cookies(
            [
                {"name": "ph_test_scenario", "value": "empty", "url": safe_web_base_url},
                {"name": "ph_test_run", "value": page_path.replace("/", "-"), "url": safe_web_base_url},
            ]
        )
        page.goto(f"{safe_web_base_url}/{page_path}", wait_until="domcontentloaded")
        page.wait_for_timeout(350)
        if page_path == "displays/current-audio.html":
            assert page.locator(selector).inner_text() == ""
        else:
            assert page.locator(selector).count() == 0

        page.context.add_cookies(
            [{"name": "ph_test_scenario", "value": "reconnect", "url": safe_web_base_url}]
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1250)
        if page_path == "displays/current-audio.html":
            assert page.locator(selector).inner_text().strip()
        else:
            assert page.locator(selector).count() > 0


@pytest.mark.browser
def test_actionable_web_controls_are_identifiable_and_mapped() -> None:
    for page_path in PAGES:
        parser = ControlParser()
        parser.feed((REPOSITORY_ROOT / page_path).read_text(encoding="utf-8"))
        identifiers = [item.get("data-testid") for item in parser.controls]
        assert all(identifiers), f"{page_path} has controls without data-testid"
        assert len(identifiers) == len(set(identifiers)), f"{page_path} has duplicate data-testid values"


@pytest.mark.browser
@pytest.mark.parametrize("page_path", ["index.html", "control.html", "pads-control.html", "score.html", "debug.html", "displays/fullscreen-wheel.html"])
def test_each_web_button_dispatches_without_javascript_errors(page, safe_web_base_url: str, page_path: str) -> None:
    page.goto(f"{safe_web_base_url}/{page_path}", wait_until="domcontentloaded")
    test_ids = page.locator("button[data-testid]").evaluate_all(
        "elements => elements.map(element => element.dataset.testid)"
    )
    assert test_ids
    for test_id in test_ids:
        errors: list[str] = []
        page.goto(f"{safe_web_base_url}/{page_path}", wait_until="domcontentloaded")
        page.on("pageerror", lambda error: errors.append(str(error)))
        control = page.locator(f'[data-testid="{test_id}"]')
        if control.count():
            control.first.dispatch_event("click")
        page.wait_for_timeout(20)
        assert not errors, f"{page_path} {test_id}: {errors}"


@pytest.mark.browser
@pytest.mark.parametrize("page_path", PAGES)
def test_runtime_web_control_inventory(page, safe_web_base_url: str, page_path: str) -> None:
    page.goto(f"{safe_web_base_url}/{page_path}", wait_until="domcontentloaded")
    page.wait_for_timeout(400)
    controls = page.locator('button, input[type="button"], input[type="submit"], [role="button"], a[href]')
    identifiers = controls.evaluate_all("items => items.filter(item => !item.disabled).map(item => item.dataset.testid || '')")
    if not identifiers:
        return
    assert all(identifiers), f"{page_path} has enabled runtime controls without data-testid"
    assert len(identifiers) == len(set(identifiers)), f"{page_path} has duplicate runtime data-testid values"


@pytest.mark.browser
@pytest.mark.performance
def test_remote_page_population_benchmark(page, safe_web_base_url: str, browser_name: str) -> None:
    samples: list[float] = []
    for viewport in ({"width": 1440, "height": 900}, {"width": 1024, "height": 1366}):
        page.set_viewport_size(viewport)
        for page_path in PAGES:
            started = time.perf_counter()
            response = page.goto(f"{safe_web_base_url}/remote/{page_path}", wait_until="domcontentloaded")
            assert response is not None and response.ok
            page.wait_for_timeout(100)
            assert "<body" in page.content().casefold()
            samples.append((time.perf_counter() - started) * 1000)
    metric = {
        "name": f"browser_population_{browser_name}",
        "unit": "ms",
        "iterations": len(samples),
        "median": round(statistics.median(samples), 4),
        "p95": round(percentile(samples, 0.95), 4),
        "p99": round(percentile(samples, 0.99), 4),
        "minimum": round(min(samples), 4),
        "maximum": round(max(samples), 4),
    }
    output = os.environ.get("PRODUCTION_HUB_BROWSER_METRICS")
    if output:
        write_json_atomic(Path(output), {"engine": browser_name, "metrics": [metric]})
    comparisons = compare([metric], load_baseline("deterministic"), live=False)
    assert not [item for item in comparisons if item["status"] == "failed"], comparisons
