#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from PySide6.QtWidgets import QApplication

from production_hub.core.config.models import CameraTrackingConfig, VideoConfig
from production_hub.tracking.models import TrackingState
from production_hub.video.models import VideoSourceKey
from production_hub.video.service import VideoService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run observation-only Apple Vision person detection against an NDI source.",
    )
    parser.add_argument(
        "--source",
        default="Production Hub - Audience Cam",
        help="Configured or discovered NDI source name.",
    )
    parser.add_argument("--frames", type=int, default=20, help="Analysis frames to collect.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Maximum runtime in seconds.")
    parser.add_argument("--fps", type=float, default=4.0, help="Analysis rate per second.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication.instance() or QApplication([])
    video_config = VideoConfig(
        audience_ndi_source_name=args.source,
        audience_auto_connect=True,
        ptz_enabled=False,
        ptz_auto_connect=False,
    )
    tracking_config = CameraTrackingConfig(
        enabled=True,
        analyze_audience=True,
        analyze_ptz=False,
        analysis_fps=args.fps,
    )
    with TemporaryDirectory(prefix="production-hub-phase2-") as directory:
        service = VideoService(
            video_config,
            Path(directory),
            tracking_config=tracking_config,
        )
        service.initialize_qt()
        deadline = time.monotonic() + max(1.0, args.timeout)
        try:
            while time.monotonic() < deadline:
                app.processEvents()
                tracking = service.tracking.snapshot(VideoSourceKey.AUDIENCE)
                if tracking.analyzed_frames >= max(1, args.frames):
                    break
                if tracking.state in {TrackingState.UNAVAILABLE, TrackingState.ERROR}:
                    break
                time.sleep(0.02)
            source = service.snapshot(VideoSourceKey.AUDIENCE)
            tracking = service.tracking.snapshot(VideoSourceKey.AUDIENCE)
            print(
                json.dumps(
                    {
                        "video_state": source.state.value,
                        "video_format": source.negotiated_format,
                        "received_frames": source.received_frames,
                        "published_frames": source.published_frames,
                        "receiver_drops": source.dropped_frames,
                        "tracking_state": tracking.state.value,
                        "backend": tracking.backend,
                        "analyzed_frames": tracking.analyzed_frames,
                        "analysis_fps": round(tracking.analysis_fps, 2),
                        "latest_inference_ms": round(tracking.inference_ms, 2),
                        "visible_subjects": len(tracking.subjects),
                        "last_error": tracking.last_error,
                    },
                    indent=2,
                )
            )
            return 0 if tracking.state == TrackingState.RUNNING else 1
        finally:
            service.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
