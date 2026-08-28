from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPOSITORY_ROOT / "Production_Hub_App"
VIDEO_TO_NDI_ROOT = REPOSITORY_ROOT / "Production_Hub_Video_to_NDI_App"
TESTING_ROOT = REPOSITORY_ROOT / "testing"
OUTPUT_ROOT = TESTING_ROOT / "benchmarks" / "outputs"
BASELINE_ROOT = TESTING_ROOT / "benchmarks" / "baselines"
FEATURE_MATRIX = TESTING_ROOT / "feature_matrix.json"


def production_data_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Production Hub"

