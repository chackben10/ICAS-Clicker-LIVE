from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from production_hub.core.security.request_auth import is_authorized
from production_hub.tracking.models import TrackingSnapshot
from production_hub.video.models import VideoSourceKey


def _snapshot_payload(snapshot: TrackingSnapshot) -> dict:
    return {
        "source": snapshot.source.value,
        "state": snapshot.state.value,
        "message": snapshot.message,
        "backend": snapshot.backend,
        "analyzedSequence": snapshot.analyzed_sequence,
        "analyzedFrames": snapshot.analyzed_frames,
        "analysisFps": snapshot.analysis_fps,
        "inferenceMs": snapshot.inference_ms,
        "analysisAgeSeconds": snapshot.analysis_age_seconds,
        "selectedCount": snapshot.selected_count,
        "rawCandidates": snapshot.raw_candidates,
        "suppressedCandidates": snapshot.suppressed_candidates,
        "activeRegionNames": list(snapshot.active_region_names),
        "lastError": snapshot.last_error,
        "subjects": [
            {
                "id": subject.track_id,
                "confidence": subject.confidence,
                "selected": subject.selected,
                "ageFrames": subject.age_frames,
                "bounds": {
                    "x": subject.bounds.x,
                    "y": subject.bounds.y,
                    "width": subject.bounds.width,
                    "height": subject.bounds.height,
                },
            }
            for subject in snapshot.subjects
        ],
    }


def router(context) -> APIRouter:
    api = APIRouter(prefix="/api/camera", tags=["camera-tracking"])

    @api.get("/tracking")
    async def tracking_state() -> dict:
        return {
            source.value: _snapshot_payload(context.video.tracking.snapshot(source))
            for source in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ)
        }

    @api.get("/regions")
    async def scene_regions() -> dict:
        reference_regions = context.config.integrations.camera_tracking.scene_regions
        live_regions = {
            region.id: region
            for region in context.video.relocalization.stabilized_regions(reference_regions)
        }
        return {
            "calibration": _relocalization_payload(
                context.video.relocalization.snapshot(),
                include_markers=False,
            ),
            "regions": [
                {
                    "id": region.id,
                    "name": region.name,
                    "kind": region.kind,
                    "source": region.source,
                    "color": region.color,
                    "enabled": region.enabled,
                    "suggested": region.suggested,
                    "generationMethod": region.generation_method,
                    "generatedAt": region.generated_at,
                    "supportingPoses": list(region.supporting_poses),
                    "supportPoints": region.support_points,
                    "confidence": region.confidence,
                    "coordinateSpace": region.coordinate_space,
                    "calibrationReference": region.calibration_reference,
                    "points": [
                        {"x": point.x, "y": point.y} for point in region.points
                    ],
                    "livePoints": [
                        {"x": point.x, "y": point.y}
                        for point in live_regions.get(region.id, region).points
                    ] if region.id in live_regions else [],
                }
                for region in reference_regions
            ]
        }

    @api.get("/calibration")
    async def calibration_state() -> dict:
        return _relocalization_payload(context.video.relocalization.snapshot())

    @api.get("/automation")
    async def automation_state() -> dict:
        return _automation_payload(context.ptz_automation.snapshot())

    @api.post("/automation/configure")
    async def configure_automation(request: Request, payload: dict) -> dict:
        _require_privileged(request, context)
        mode = str(payload.get("mode", "")).strip().casefold().replace("+", "_")
        if mode not in {"off", "subject", "stage", "stage_altar", "click"}:
            raise HTTPException(
                status_code=422,
                detail="mode must be off, subject, stage, stage_altar, or click",
            )
        current = context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        config.automation.mode = mode
        if "podiumZoomEnabled" in payload:
            config.automation.podium_zoom_enabled = bool(payload["podiumZoomEnabled"])
        config.automation.__post_init__()
        context.config.integrations.camera_tracking = config
        context.config_repository.save_app_config(context.config)
        context.video.reconfigure_tracking(config)
        context.ptz_automation.reconfigure(config)
        if mode == "off":
            context.ptz_automation.disarm("Framing mode was turned off through the API")
        return _automation_payload(context.ptz_automation.snapshot())

    @api.post("/automation/click-target")
    async def set_click_target(request: Request, payload: dict) -> dict:
        _require_privileged(request, context)
        try:
            x, y = float(payload["x"]), float(payload["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="x and y must be normalized Audience coordinates",
            ) from exc
        if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
            raise HTTPException(status_code=422, detail="x and y must be between 0 and 1")
        try:
            reference = context.ptz_automation.set_click_target_live(x, y)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "ok": True,
            "referenceTarget": {"x": reference[0], "y": reference[1]},
            "automation": _automation_payload(context.ptz_automation.snapshot()),
        }

    @api.post("/automation/arm")
    async def arm_automation(request: Request) -> dict:
        _require_privileged(request, context)
        ok, message = context.ptz_automation.arm()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return _automation_payload(context.ptz_automation.snapshot())

    @api.post("/automation/disarm")
    async def disarm_automation(request: Request) -> dict:
        _require_privileged(request, context)
        context.ptz_automation.disarm("Operator disarmed PTZ automation through the API")
        return _automation_payload(context.ptz_automation.snapshot())

    @api.get("/tracking/{source_name}")
    async def source_tracking_state(source_name: str) -> dict:
        try:
            source = VideoSourceKey(source_name.casefold())
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail="Tracking source must be 'audience' or 'ptz'.",
            ) from exc
        if source not in {VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ}:
            raise HTTPException(
                status_code=404,
                detail="Tracking source must be 'audience' or 'ptz'.",
            )
        return _snapshot_payload(context.video.tracking.snapshot(source))

    return api


def _require_privileged(request: Request, context) -> None:
    headers = {key.casefold(): value for key, value in request.headers.items()}
    if not is_authorized(headers, context.config.api, privileged=True):
        raise HTTPException(status_code=401, detail="A privileged Production Hub token is required")


def _automation_payload(snapshot) -> dict:
    decision = snapshot.decision
    return {
        "state": snapshot.state.value,
        "armed": snapshot.armed,
        "motionAuthority": snapshot.motion_authority,
        "mode": snapshot.mode,
        "message": snapshot.message,
        "geometryPath": snapshot.geometry_path,
        "commandsSent": snapshot.commands_sent,
        "lastDecisionMonotonic": snapshot.last_decision_monotonic,
        "lastCommandMonotonic": snapshot.last_command_monotonic,
        "lastError": snapshot.last_error,
        "actualPose": _pose_payload(snapshot.actual_pose),
        "commandedPose": _pose_payload(snapshot.commanded_pose),
        "decision": None
        if decision is None
        else {
            "state": decision.state.value,
            "mode": decision.mode,
            "reason": decision.reason,
            "targetIds": list(decision.target_ids),
            "targetConfidence": decision.target_confidence,
            "podiumFraming": decision.podium_framing,
            "targetBounds": None
            if decision.target_bounds is None
            else {
                "x": decision.target_bounds.x,
                "y": decision.target_bounds.y,
                "width": decision.target_bounds.width,
                "height": decision.target_bounds.height,
            },
            "desiredPose": _pose_payload(decision.desired_pose),
        },
    }


def _pose_payload(pose) -> dict | None:
    if pose is None:
        return None
    return {"pan": pose.pan, "tilt": pose.tilt, "zoom": pose.zoom}


def _relocalization_payload(snapshot, *, include_markers: bool = True) -> dict:
    payload = {
        "state": snapshot.state.value,
        "message": snapshot.message,
        "motionSafe": snapshot.motion_safe,
        "motionAuthority": False,
        "calibrationPath": snapshot.calibration_path,
        "approvalStatus": snapshot.approval_status,
        "analyzedSequence": snapshot.analyzed_sequence,
        "analyzedFrames": snapshot.analyzed_frames,
        "analysisFps": snapshot.analysis_fps,
        "inferenceMs": snapshot.inference_ms,
        "candidateMatches": snapshot.candidate_matches,
        "inliers": snapshot.inliers,
        "inlierRatio": snapshot.inlier_ratio,
        "medianErrorPixels": snapshot.median_error_pixels,
        "referenceCoverage": snapshot.reference_coverage,
        "referenceSize": list(snapshot.reference_size),
        "liveSize": list(snapshot.live_size),
        "referenceToLive": [list(row) for row in snapshot.reference_to_live],
        "lastError": snapshot.last_error,
    }
    if include_markers:
        payload["markers"] = [
            {"id": item.marker_id, "x": item.x, "y": item.y}
            for item in snapshot.marker_positions
        ]
    return payload
