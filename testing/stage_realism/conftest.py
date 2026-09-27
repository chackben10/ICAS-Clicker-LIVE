"""Choose the build under test before anything imports production_hub.

    STAGE_REALISM_TARGET=installed testing/.venv/bin/python -m pytest testing/stage_realism -m single
    STAGE_REALISM_TARGET=dev       testing/.venv/bin/python -m pytest testing/stage_realism -m guard
"""

from __future__ import annotations

from testing.stage_realism import target

ACTIVE = target.activate()


def pytest_configure(config) -> None:
    for group in ("single", "double", "triple"):
        config.addinivalue_line("markers", f"{group}: stage-realism scenarios with {group} subject(s)")
    config.addinivalue_line("markers", "challenge: the live app fails this; the Dev build should pass it")
    config.addinivalue_line("markers", "guard: the live app passes this; no build may regress it")
    config.addinivalue_line("markers", "new: not yet in the live baseline")
    config.addinivalue_line("markers", "rendered: real stage imagery and real people through the real detector")
    config.addinivalue_line("markers", "moment: a real stream moment replayed frame by frame")


def pytest_report_header(config) -> str:
    return f"stage-realism target: {ACTIVE.description}"
