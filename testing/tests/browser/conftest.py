from __future__ import annotations

import pytest

from testing.harness.web_server import safe_page_server


@pytest.fixture(scope="session")
def safe_web_base_url():
    with safe_page_server() as base_url:
        yield base_url

