#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from production_hub.app.dev_launcher import (
    should_use_development_app,
    run_python_entry_in_development_app,
    without_dev_child_argument,
)


def parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read both configured camera views, find natural scene correspondences automatically, "
            "and write a reviewable Audience-to-PTZ alignment. This tool never moves the PTZ."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Production Hub data directory. Defaults to the normal Application Support location.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory. Defaults to Production Hub's calibration directory.",
    )
    parser.add_argument("--audience-image", type=Path, help="Use an Audience still image instead of live video.")
    parser.add_argument("--ptz-image", type=Path, help="Use a PTZ still image instead of live video.")
    parser.add_argument("--samples", type=int, default=5, help="Live frame pairs to evaluate; best valid pair wins.")
    parser.add_argument("--sample-interval", type=float, default=0.6, help="Seconds between live sample pairs.")
    parser.add_argument("--timeout", type=float, default=25.0, help="Maximum seconds to wait for both live feeds.")
    parser.add_argument("--maximum-width", type=int, default=1600, help="Maximum matching width per image.")
    parser.add_argument("--maximum-features", type=int, default=8000, help="Maximum SIFT features per image.")
    parser.add_argument("--ratio-threshold", type=float, default=0.70, help="Nearest-neighbor ambiguity threshold.")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    child_arguments = without_dev_child_argument(raw_arguments)
    offline = "--audience-image" in child_arguments or "--ptz-image" in child_arguments
    if not offline and should_use_development_app(raw_arguments):
        return run_python_entry_in_development_app(raw_arguments, APP_ROOT, Path(__file__))

    args = parse_args(child_arguments)
    if bool(args.audience_image) != bool(args.ptz_image):
        print("Both --audience-image and --ptz-image are required for offline calibration.", file=sys.stderr)
        return 1

    try:
        import cv2
        from production_hub.calibration import (
            AlignmentError,
            AlignmentSettings,
            consolidate_alignments,
            estimate_alignment,
            render_alignment_diagnostics,
        )
    except ImportError as exc:
        print(
            "OpenCV is required. Install Production Hub requirements with "
            "`python3 -m pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1

    settings = AlignmentSettings(
        maximum_width=args.maximum_width,
        maximum_features=args.maximum_features,
        ratio_threshold=args.ratio_threshold,
    )
    data_root = _data_root(args.data_dir)
    output_dir = _output_directory(args.output, data_root)

    live_metadata: dict[str, Any] = {}
    service = None
    try:
        if args.audience_image and args.ptz_image:
            audience = cv2.imread(str(args.audience_image), cv2.IMREAD_COLOR)
            ptz = cv2.imread(str(args.ptz_image), cv2.IMREAD_COLOR)
            if audience is None or ptz is None:
                raise AlignmentError("One or both offline image files could not be read.")
            pairs = [(audience, ptz, {"sample": 1, "mode": "offline"})]
        else:
            pairs, service, live_metadata = _capture_live_pairs(
                data_root,
                samples=max(1, min(30, int(args.samples))),
                interval=max(0.05, float(args.sample_interval)),
                timeout=max(2.0, float(args.timeout)),
            )

        attempts: list[dict[str, Any]] = []
        candidates = []
        for audience, ptz, capture in pairs:
            try:
                result = estimate_alignment(audience, ptz, settings)
                attempts.append(
                    {
                        **capture,
                        "status": result.status,
                        "confidence_score": result.confidence_score,
                        "candidate_matches": result.candidate_matches,
                        "inliers": result.inliers,
                        "inlier_ratio": result.inlier_ratio,
                        "median_error_pixels": result.median_error_pixels,
                        "audience_coverage": result.audience_coverage,
                        "ptz_coverage": result.ptz_coverage,
                        "reasons": list(result.reasons),
                    }
                )
                candidates.append((result, audience, ptz, capture))
            except AlignmentError as exc:
                attempts.append({**capture, "status": "failed", "error": str(exc)})

        if not candidates:
            raise AlignmentError("No sample pair produced an alignment. " + _attempt_errors(attempts))

        result, audience, ptz, capture = _select_reference_candidate(candidates)
        reference_capture = dict(capture)
        stability = _stability_summary(result, candidates, attempts)
        consistent_samples = set(stability.get("consistent_samples", []))
        consistent_results = [
            candidate
            for candidate, _audience, _ptz, candidate_capture in candidates
            if candidate_capture.get("sample") in consistent_samples
        ]
        if len(consistent_results) >= 2:
            result = consolidate_alignments(consistent_results, settings)
            capture = {
                "mode": "multi_sample_consensus",
                "reference_sample": reference_capture.get("sample"),
                "samples": sorted(consistent_samples),
            }
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = render_alignment_diagnostics(audience, ptz, result, output_dir)
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "purpose": "read-only Audience-to-PTZ image alignment",
            "motion_authority": False,
            "selected_sample": capture,
            "source_metadata": live_metadata,
            "settings": asdict(settings),
            "attempts": attempts,
            "stability": stability,
            "alignment": result.to_dict(),
            "artifacts": artifacts,
        }
        _atomic_json(output_dir / "calibration.json", payload)
        print(json.dumps(_console_summary(output_dir, result, attempts, stability), indent=2))
        return 0 if result.accepted else 2
    except (AlignmentError, OSError, RuntimeError) as exc:
        print(f"Calibration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.shutdown()


def _capture_live_pairs(
    data_root: Path,
    *,
    samples: int,
    interval: float,
    timeout: float,
):
    from PySide6.QtWidgets import QApplication

    from production_hub.core.config.models import AppPaths, CameraTrackingConfig, VideoConfig
    from production_hub.core.config.repository import ConfigRepository
    from production_hub.video.models import VideoSourceKey, VideoSourceState
    from production_hub.video.service import VideoService

    app = QApplication.instance() or QApplication([])
    repository = ConfigRepository(AppPaths(data_root))
    app_config = repository.load_app_config()
    video_config = VideoConfig.from_dict(app_config.integrations.video.to_dict())
    video_config.enabled = True
    video_config.audience_enabled = True
    video_config.ptz_enabled = True
    video_config.audience_auto_connect = True
    video_config.ptz_auto_connect = True
    service = VideoService(
        video_config,
        data_root,
        tracking_config=CameraTrackingConfig(enabled=False),
    )
    service.initialize_qt()
    service.set_preview_active(True)

    deadline = time.monotonic() + timeout
    pairs: list[tuple[Any, Any, dict[str, Any]]] = []
    last_sequences = {VideoSourceKey.AUDIENCE: 0, VideoSourceKey.PTZ: 0}
    try:
        while len(pairs) < samples and time.monotonic() < deadline:
            app.processEvents()
            audience_packet = service.frame(VideoSourceKey.AUDIENCE)
            ptz_packet = service.frame(VideoSourceKey.PTZ)
            if (
                audience_packet is not None
                and ptz_packet is not None
                and audience_packet.sequence > last_sequences[VideoSourceKey.AUDIENCE]
                and ptz_packet.sequence > last_sequences[VideoSourceKey.PTZ]
            ):
                captured = time.monotonic()
                audience_age = captured - audience_packet.captured_monotonic
                ptz_age = captured - ptz_packet.captured_monotonic
                if max(audience_age, ptz_age) <= 2.0:
                    pairs.append(
                        (
                            _qimage_to_bgr(audience_packet.image),
                            _qimage_to_bgr(ptz_packet.image),
                            {
                                "sample": len(pairs) + 1,
                                "mode": "live",
                                "audience_sequence": audience_packet.sequence,
                                "ptz_sequence": ptz_packet.sequence,
                                "capture_skew_ms": round(
                                    abs(
                                        audience_packet.captured_monotonic
                                        - ptz_packet.captured_monotonic
                                    )
                                    * 1000.0,
                                    2,
                                ),
                            },
                        )
                    )
                    last_sequences[VideoSourceKey.AUDIENCE] = audience_packet.sequence
                    last_sequences[VideoSourceKey.PTZ] = ptz_packet.sequence
                    target = time.monotonic() + interval
                    while time.monotonic() < target:
                        app.processEvents()
                        time.sleep(0.01)
                    continue
            audience_state = service.snapshot(VideoSourceKey.AUDIENCE)
            ptz_state = service.snapshot(VideoSourceKey.PTZ)
            failed_states = {
                VideoSourceState.ERROR,
                VideoSourceState.MISSING,
                VideoSourceState.BUSY,
                VideoSourceState.PERMISSION_DENIED,
            }
            audience_discovering = (
                audience_state.state == VideoSourceState.MISSING
                and service.source_type(VideoSourceKey.AUDIENCE, video_config) == "ndi"
            )
            ptz_discovering = (
                ptz_state.state == VideoSourceState.MISSING
                and service.source_type(VideoSourceKey.PTZ, video_config) == "ndi"
            )
            audience_failed = (
                audience_state.state in failed_states and not audience_discovering
            )
            ptz_failed = ptz_state.state in failed_states and not ptz_discovering
            if audience_failed or ptz_failed:
                raise RuntimeError(
                    f"Audience: {audience_state.state.value} — {audience_state.message}; "
                    f"PTZ: {ptz_state.state.value} — {ptz_state.message}"
                )
            time.sleep(0.02)
        if not pairs:
            audience_state = service.snapshot(VideoSourceKey.AUDIENCE)
            ptz_state = service.snapshot(VideoSourceKey.PTZ)
            raise RuntimeError(
                f"No fresh frame pair arrived. Audience: {audience_state.state.value} — "
                f"{audience_state.message}; PTZ: {ptz_state.state.value} — {ptz_state.message}"
            )
        metadata = {
            "audience": _source_metadata(service, VideoSourceKey.AUDIENCE, video_config),
            "ptz": _source_metadata(service, VideoSourceKey.PTZ, video_config),
            "requested_samples": samples,
            "captured_samples": len(pairs),
        }
        return pairs, service, metadata
    except Exception:
        service.shutdown()
        raise


def _qimage_to_bgr(image):
    import cv2
    import numpy as np
    from PySide6.QtGui import QImage

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = converted.width()
    height = converted.height()
    stride = converted.bytesPerLine()
    buffer = np.frombuffer(converted.bits(), dtype=np.uint8, count=stride * height)
    rgba = buffer.reshape(height, stride // 4, 4)[:, :width, :]
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR).copy()


def _source_metadata(service, source, config) -> dict[str, Any]:
    snapshot = service.snapshot(source)
    return {
        "type": service.source_type(source, config),
        "name": snapshot.source_name,
        "format": snapshot.negotiated_format,
        "received_frames": snapshot.received_frames,
        "published_frames": snapshot.published_frames,
        "dropped_frames": snapshot.dropped_frames,
    }


def _data_root(override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    from production_hub.core.config.repository import default_app_root

    return default_app_root().resolve()


def _output_directory(override: Path | None, data_root: Path) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return data_root / "calibration" / stamp


def _result_rank(result) -> tuple[int, float, int, float, float]:
    return (
        1 if result.accepted else 0,
        result.confidence_score,
        result.inliers,
        result.inlier_ratio,
        -result.median_error_pixels,
    )


def _select_reference_candidate(candidates):
    """Choose the medoid of the largest transform-agreement cluster."""

    import numpy as np

    if len(candidates) == 1:
        return candidates[0]
    neighbor_counts: list[int] = []
    median_neighbor_deltas: list[float] = []
    for index, first in enumerate(candidates):
        deltas = [
            _pair_transform_delta(first[0], second[0])
            for second in candidates
        ]
        neighbors = [value for value in deltas if value <= 6.0]
        neighbor_counts.append(len(neighbors))
        median_neighbor_deltas.append(
            float(np.median(neighbors)) if neighbors else float("inf")
        )
    selected_index = max(
        range(len(candidates)),
        key=lambda index: (
            neighbor_counts[index],
            -median_neighbor_deltas[index],
            *_result_rank(candidates[index][0]),
        ),
    )
    return candidates[selected_index]


def _pair_transform_delta(first, second) -> float:
    import cv2
    import numpy as np

    points = [
        (item.audience_x, item.audience_y)
        for result in (first, second)
        for item in result.correspondences
    ]
    if len(points) < 8:
        return float("inf")
    source = np.float32(points)[:, None, :]
    first_projection = cv2.perspectiveTransform(
        source,
        np.asarray(first.audience_to_ptz, dtype=np.float64),
    )[:, 0]
    second_projection = cv2.perspectiveTransform(
        source,
        np.asarray(second.audience_to_ptz, dtype=np.float64),
    )[:, 0]
    width, height = first.ptz_size
    margin_x = width * 0.1
    margin_y = height * 0.1
    valid = (
        np.isfinite(first_projection).all(axis=1)
        & np.isfinite(second_projection).all(axis=1)
        & (first_projection[:, 0] >= -margin_x)
        & (first_projection[:, 0] <= width + margin_x)
        & (first_projection[:, 1] >= -margin_y)
        & (first_projection[:, 1] <= height + margin_y)
        & (second_projection[:, 0] >= -margin_x)
        & (second_projection[:, 0] <= width + margin_x)
        & (second_projection[:, 1] >= -margin_y)
        & (second_projection[:, 1] <= height + margin_y)
    )
    if int(valid.sum()) < 8:
        return float("inf")
    errors = np.linalg.norm(first_projection[valid] - second_projection[valid], axis=1)
    return float(np.median(errors))


def _attempt_errors(attempts: list[dict[str, Any]]) -> str:
    errors = [str(item.get("error")) for item in attempts if item.get("error")]
    return "; ".join(errors[:3])


def _stability_summary(result, candidates, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    import cv2
    import numpy as np

    if len(candidates) <= 1:
        return {
            "status": "not_applicable",
            "evaluated_models": len(candidates),
            "consistent_models": len(candidates),
            "maximum_consistent_median_delta_pixels": 6.0,
        }
    reference_points = np.float32(
        [[[item.audience_x, item.audience_y]] for item in result.correspondences]
    )
    reference_matrix = np.asarray(result.audience_to_ptz, dtype=np.float64)
    reference_projection = cv2.perspectiveTransform(reference_points, reference_matrix)[:, 0]
    deltas: list[float] = []
    consistent = 0
    consistent_samples: list[int] = []
    for candidate, _audience, _ptz, capture in candidates:
        candidate_matrix = np.asarray(candidate.audience_to_ptz, dtype=np.float64)
        candidate_projection = cv2.perspectiveTransform(reference_points, candidate_matrix)[:, 0]
        errors = np.linalg.norm(reference_projection - candidate_projection, axis=1)
        median_delta = float(np.median(errors))
        p95_delta = float(np.percentile(errors, 95))
        deltas.append(median_delta)
        if median_delta <= 6.0:
            consistent += 1
            if isinstance(capture.get("sample"), int):
                consistent_samples.append(capture["sample"])
        sample = capture.get("sample")
        attempt = next((item for item in attempts if item.get("sample") == sample), None)
        if attempt is not None:
            attempt["transform_delta_median_pixels"] = median_delta
            attempt["transform_delta_p95_pixels"] = p95_delta
    return {
        "status": "stable" if consistent >= 2 else "unconfirmed",
        "evaluated_models": len(candidates),
        "consistent_models": consistent,
        "consistent_samples": sorted(consistent_samples),
        "maximum_consistent_median_delta_pixels": 6.0,
        "median_model_delta_pixels": float(np.median(deltas)),
        "p95_model_delta_pixels": float(np.percentile(deltas, 95)),
    }


def _console_summary(
    output_dir: Path,
    result,
    attempts: list[dict[str, Any]],
    stability: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": result.status,
        "confidence_score": result.confidence_score,
        "audience_keypoints": result.audience_keypoints,
        "ptz_keypoints": result.ptz_keypoints,
        "candidate_matches": result.candidate_matches,
        "inliers": result.inliers,
        "inlier_ratio": round(result.inlier_ratio, 4),
        "median_error_pixels": round(result.median_error_pixels, 3),
        "audience_coverage": round(result.audience_coverage, 4),
        "ptz_coverage": round(result.ptz_coverage, 4),
        "reasons": list(result.reasons),
        "evaluated_samples": len(attempts),
        "stability": stability,
        "output": str(output_dir),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


if __name__ == "__main__":
    raise SystemExit(main())
