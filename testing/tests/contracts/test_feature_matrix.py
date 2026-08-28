from __future__ import annotations

from testing.harness.inventory import coverage_errors


def test_feature_matrix_covers_runtime_catalogs_and_repository_pages() -> None:
    errors = coverage_errors()
    assert not errors, "\n".join(errors)

