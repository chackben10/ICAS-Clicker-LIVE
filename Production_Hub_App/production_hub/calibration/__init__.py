"""Read-only camera calibration primitives used before motion automation."""

from production_hub.calibration.camera_alignment import (
    AlignmentError,
    AlignmentResult,
    AlignmentSettings,
    consolidate_alignments,
    estimate_alignment,
    render_alignment_diagnostics,
)

__all__ = [
    "AlignmentError",
    "AlignmentResult",
    "AlignmentSettings",
    "consolidate_alignments",
    "estimate_alignment",
    "render_alignment_diagnostics",
]
