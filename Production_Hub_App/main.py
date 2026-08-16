from __future__ import annotations

import os
import sys
from pathlib import Path


# Keep actionable Qt multimedia warnings and errors, but omit the routine
# backend/version banner from source and packaged startup output. Respect an
# operator-provided rule set when one is already configured.
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.multimedia.ffmpeg.debug=false;qt.multimedia.ffmpeg.info=false",
)

from production_hub.app.dev_launcher import (
    run_in_development_app,
    should_use_development_app,
    without_dev_child_argument,
)
from production_hub.app.main import main


CALIBRATION_WORKFLOW_ARGUMENT = "--calibrate-ptz-to-audience"
STRUCTURAL_PLANE_WORKFLOW_ARGUMENT = "--build-structural-planes"


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if should_use_development_app(arguments):
        raise SystemExit(run_in_development_app(arguments, Path(__file__).resolve().parent))
    child_arguments = without_dev_child_argument(arguments)
    if CALIBRATION_WORKFLOW_ARGUMENT in child_arguments:
        from scripts.calibrate_ptz_to_audience import main as calibration_main

        workflow_arguments = [
            item for item in child_arguments if item != CALIBRATION_WORKFLOW_ARGUMENT
        ]
        raise SystemExit(calibration_main(workflow_arguments))
    if STRUCTURAL_PLANE_WORKFLOW_ARGUMENT in child_arguments:
        from scripts.build_structural_planes import main as structural_plane_main

        workflow_arguments = [
            item
            for item in child_arguments
            if item != STRUCTURAL_PLANE_WORKFLOW_ARGUMENT
        ]
        raise SystemExit(structural_plane_main(workflow_arguments))
    raise SystemExit(main(child_arguments))
