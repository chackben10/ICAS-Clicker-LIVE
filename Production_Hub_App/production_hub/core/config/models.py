from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, ClassVar, get_args, get_origin, get_type_hints


class ValidationError(ValueError):
    """Raised when configuration data is structurally invalid."""


def _coerce(value: Any, target: Any) -> Any:
    origin = get_origin(target)
    args = get_args(target)

    if origin in (list, tuple):
        item_type = args[0] if args else Any
        if not isinstance(value, list):
            raise ValidationError(f"Expected list, got {type(value).__name__}")
        return [_coerce(item, item_type) for item in value]

    if origin is dict:
        if not isinstance(value, dict):
            raise ValidationError(f"Expected object, got {type(value).__name__}")
        return dict(value)

    if origin in (UnionType, getattr(__import__("typing"), "Union")):
        non_none = [arg for arg in args if arg is not type(None)]
        if value is None:
            return None
        if non_none:
            return _coerce(value, non_none[0])

    if isinstance(target, type) and is_dataclass(target):
        if isinstance(value, target):
            return value
        if not isinstance(value, dict):
            raise ValidationError(f"Expected object for {target.__name__}")
        return target.from_dict(value)

    return value


class JsonModel:
    schema_version: ClassVar[int] = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any:
        if not isinstance(data, dict):
            raise ValidationError(f"Expected object for {cls.__name__}")
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in data:
                continue
            if item.name not in hints:
                kwargs[item.name] = data[item.name]
                continue
            kwargs[item.name] = _coerce(data[item.name], hints[item.name])
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _non_empty(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValidationError(f"{field_name} cannot be empty")
    return value


def _port(value: int, field_name: str = "port") -> int:
    value = int(value)
    if value < 1 or value > 65535:
        raise ValidationError(f"{field_name} must be between 1 and 65535")
    return value


@dataclass
class ApiServerConfig(JsonModel):
    bind_host: str = "127.0.0.1"
    port: int = 1337
    lan_access_enabled: bool = False
    cors_allow_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost",
            "http://127.0.0.1",
            "https://icas-clicker.work",
            "https://www.icas-clicker.work",
            "https://control.icas-clicker.work",
            "https://slides.icas-clicker.work",
        ]
    )
    require_token_for_privileged: bool = True
    read_only_public: bool = True
    access_token: str = ""

    def __post_init__(self) -> None:
        self.bind_host = _non_empty(self.bind_host, "api.bind_host")
        self.port = _port(self.port, "api.port")

    @property
    def base_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"


@dataclass
class PresentationMapping(JsonModel):
    label: str
    uuid: str
    description: str = ""

    def __post_init__(self) -> None:
        self.label = _non_empty(self.label, "presentation.label")
        self.uuid = _non_empty(self.uuid, "presentation.uuid")


@dataclass
class MacroMapping(JsonModel):
    display_name: str
    macro_name: str
    description: str = ""

    def __post_init__(self) -> None:
        self.display_name = _non_empty(self.display_name, "macro.display_name")
        self.macro_name = _non_empty(self.macro_name, "macro.macro_name")


@dataclass
class ServiceLogoMapping(JsonModel):
    name: str
    uuid: str

    def __post_init__(self) -> None:
        self.name = _non_empty(self.name, "service_logo.name")
        self.uuid = _non_empty(self.uuid, "service_logo.uuid")


@dataclass
class ThumbnailConfig(JsonModel):
    low_quality: int = 220
    high_quality: int = 800
    image_format: str = "png"
    low_cache_ttl_seconds: float = 20
    high_cache_ttl_seconds: float = 300
    max_cache_items: int = 500
    prefetch_max_slides: int = 250
    queue_delay_seconds: float = 0.02


@dataclass
class AudioConfig(JsonModel):
    playlists: list[str] = field(default_factory=lambda: ["Major Pads", "Minor Pads", "Neutral Pads"])
    cache_ttl_seconds: float = 300
    slide_label_sync_enabled: bool = True
    trigger_delay_seconds: float = 0.5
    prevent_duplicate_triggers: bool = True
    history_max: int = 500


@dataclass
class PresentationBehaviorConfig(JsonModel):
    prefer_active_when_valid: bool = True
    fall_back_to_focused: bool = True
    avoid_blank_preview_uuid: str = "7475C13E-FE99-4AF1-8760-526A845A1860"
    refocus_delay_seconds: float = 0.20
    ignore_announcements_focused: bool = True


@dataclass
class TimerConfig(JsonModel):
    timer_name: str = "Service Countdown"
    stop_reset_delay_seconds: float = 0.5


@dataclass
class ProPresenterConfig(JsonModel):
    enabled: bool = True
    host: str = "localhost"
    port: int = 49232
    auto_connect: bool = True
    automatic_reconnect: bool = True
    request_timeout_seconds: float = 2.5
    polling_interval_seconds: float = 0.75
    presentations: list[PresentationMapping] = field(default_factory=list)
    service_logos: list[ServiceLogoMapping] = field(default_factory=list)
    macros: list[MacroMapping] = field(default_factory=list)
    timer: TimerConfig = field(default_factory=TimerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    presentation_behavior: PresentationBehaviorConfig = field(default_factory=PresentationBehaviorConfig)
    thumbnails: ThumbnailConfig = field(default_factory=ThumbnailConfig)
    bible_macro_trigger_uuid: str = "69293C79-69BB-4061-86E1-76F627CB3085"
    bible_look_name: str = "Bible"
    clear_slide_delay_seconds: float = 0.5
    next_slide_key_code: int = 69
    previous_slide_key_code: int = 78

    def __post_init__(self) -> None:
        self.host = _non_empty(self.host, "propresenter.host")
        self.port = _port(self.port, "propresenter.port")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"


@dataclass
class ObsSourceMapping(JsonModel):
    scene_item_id: int
    source_name: str
    description: str = ""


@dataclass
class ObsLookRuleConfig(JsonModel):
    look_name: str
    target_scene: str
    show_ids: list[int]
    hide_ids: list[int]
    debounce_seconds: float = 0.20
    enabled: bool = True


@dataclass
class ObsConfig(JsonModel):
    enabled: bool = True
    host: str = "192.168.1.156"
    port: int = 4455
    password: str = ""
    auto_connect: bool = True
    automatic_reconnect: bool = True
    retry_delay_seconds: float = 0.75
    connection_timeout_seconds: float = 3.0
    main_layout_scene: str = "ProPresenter Input"
    default_transition: str = "Fade"
    special_transition: str = "Old Film Logo"
    fallback_transition: str = "Fade"
    fallback_duration_ms: int = 500
    special_transition_scenes: list[str] = field(
        default_factory=lambda: ["Stream Start", "Testimonies", "Stream Pause", "Thanks Screen"]
    )
    known_scenes: list[str] = field(default_factory=list)
    source_mappings: list[ObsSourceMapping] = field(default_factory=list)
    look_rules: list[ObsLookRuleConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.host = _non_empty(self.host, "obs.host")
        self.port = _port(self.port, "obs.port")


@dataclass
class PanasonicConfig(JsonModel):
    enabled: bool = True
    camera_ip: str = "192.168.50.80"
    username: str = "admin"
    password: str = "12345"
    request_timeout_seconds: float = 1.0
    aw_ptz_path: str = "/cgi-bin/aw_ptz"
    aw_cam_path: str = "/cgi-bin/aw_cam"
    default_pan_tilt_speed: int = 25
    default_zoom_speed: int = 20
    default_focus_speed: int = 20
    preset_names: dict[str, str] = field(default_factory=lambda: {"0": "Home"})

    def __post_init__(self) -> None:
        self.camera_ip = _non_empty(self.camera_ip, "panasonic.camera_ip")
        self.username = _non_empty(self.username, "panasonic.username")


@dataclass
class ViscaConfig(JsonModel):
    enabled: bool = True
    listen_ip: str = "0.0.0.0"
    udp_port: int = 52383
    reuse_address: bool = True
    reuse_port: bool = False
    ack_response_enabled: bool = True
    completion_response_enabled: bool = True
    tenveo_compatibility_enabled: bool = True
    autofocus_toggles_subject_tracking: bool = True
    tracking_toggle_debounce_seconds: float = 0.75
    port_conflict_behavior: str = "cancel"
    safe_mode_for_port_conflicts: bool = True

    def __post_init__(self) -> None:
        self.listen_ip = _non_empty(self.listen_ip, "visca.listen_ip")
        self.udp_port = _port(self.udp_port, "visca.udp_port")
        self.autofocus_toggles_subject_tracking = bool(
            self.autofocus_toggles_subject_tracking
        )
        self.tracking_toggle_debounce_seconds = max(
            0.25,
            min(3.0, float(self.tracking_toggle_debounce_seconds)),
        )


@dataclass
class MidiConfig(JsonModel):
    enabled: bool = True
    status_label: str = "Not Configured"
    input_name: str = ""
    auto_open_first_input: bool = True
    input_devices: list[str] = field(default_factory=list)
    output_devices: list[str] = field(default_factory=list)
    mappings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScoreboardConfig(JsonModel):
    enabled: bool = True


@dataclass
class VideoConfig(JsonModel):
    enabled: bool = True
    audience_enabled: bool = True
    audience_source_type: str = "ndi"
    audience_ndi_source_name: str = "Production Hub - Audience Cam"
    audience_device_id: str = ""
    audience_highest_bandwidth: bool = True
    audience_auto_connect: bool = True
    ptz_enabled: bool = True
    ptz_source_type: str = "local"
    ptz_ndi_source_name: str = "Production Hub - PTZ Cam"
    ptz_device_id: str = ""
    ptz_highest_bandwidth: bool = True
    ptz_auto_connect: bool = True
    preferred_width: int = 1920
    preferred_height: int = 1080
    preferred_fps: float = 30.0
    preview_fps: float = 12.0
    stale_after_seconds: float = 1.5
    recording_fps: float = 10.0
    recording_max_width: int = 1280

    def __post_init__(self) -> None:
        self.audience_source_type = self._source_type(
            self.audience_source_type,
            "video.audience_source_type",
        )
        self.audience_ndi_source_name = _non_empty(
            self.audience_ndi_source_name,
            "video.audience_ndi_source_name",
        )
        self.audience_device_id = str(self.audience_device_id or "").strip()
        self.ptz_source_type = self._source_type(
            self.ptz_source_type,
            "video.ptz_source_type",
        )
        self.ptz_ndi_source_name = _non_empty(
            self.ptz_ndi_source_name,
            "video.ptz_ndi_source_name",
        )
        self.ptz_device_id = str(self.ptz_device_id or "").strip()
        self.preferred_width = max(320, min(7680, int(self.preferred_width)))
        self.preferred_height = max(240, min(4320, int(self.preferred_height)))
        self.preferred_fps = max(1.0, min(120.0, float(self.preferred_fps)))
        self.preview_fps = max(1.0, min(30.0, float(self.preview_fps)))
        self.stale_after_seconds = max(0.5, min(30.0, float(self.stale_after_seconds)))
        self.recording_fps = max(1.0, min(30.0, float(self.recording_fps)))
        self.recording_max_width = max(320, min(3840, int(self.recording_max_width)))

    @staticmethod
    def _source_type(value: str, field_name: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized not in {"ndi", "local"}:
            raise ValidationError(f"{field_name} must be 'ndi' or 'local'")
        return normalized


@dataclass
class SceneRegionPoint(JsonModel):
    x: float
    y: float

    def __post_init__(self) -> None:
        self.x = max(0.0, min(1.0, float(self.x)))
        self.y = max(0.0, min(1.0, float(self.y)))


@dataclass
class CameraSceneRegion(JsonModel):
    id: str
    name: str
    points: list[SceneRegionPoint]
    kind: str = "custom"
    source: str = "audience"
    color: str = "#7c5cff"
    enabled: bool = True
    suggested: bool = False
    coordinate_space: str = "calibration_reference"
    calibration_reference: str = ""
    generation_method: str = ""
    generated_at: str = ""
    supporting_poses: list[str] = field(default_factory=list)
    support_points: int = 0
    confidence: float = 0.0

    def __post_init__(self) -> None:
        self.id = _non_empty(self.id, "camera_scene_region.id")
        self.name = _non_empty(self.name, "camera_scene_region.name")
        self.kind = str(self.kind or "custom").strip().casefold().replace(" ", "_")
        if self.kind not in {
            "stage",
            "front_stage",
            "altar",
            "podium",
            "audience",
            "custom",
        }:
            raise ValidationError(
                "camera_scene_region.kind must be stage, front_stage, altar, podium, audience, or custom"
            )
        self.source = str(self.source or "audience").strip().casefold()
        if self.source not in {"audience", "ptz"}:
            raise ValidationError("camera_scene_region.source must be audience or ptz")
        self.color = str(self.color or "#7c5cff").strip()
        self.suggested = bool(self.suggested)
        self.coordinate_space = str(
            self.coordinate_space or "calibration_reference"
        ).strip().casefold()
        if self.coordinate_space not in {"calibration_reference", "live_audience"}:
            raise ValidationError(
                "camera_scene_region.coordinate_space must be calibration_reference or live_audience"
            )
        self.calibration_reference = str(self.calibration_reference or "").strip()
        self.generation_method = str(self.generation_method or "").strip()
        self.generated_at = str(self.generated_at or "").strip()
        self.supporting_poses = [
            str(value).strip()
            for value in self.supporting_poses
            if str(value).strip()
        ]
        self.support_points = max(0, int(self.support_points))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if len(self.points) < 3:
            raise ValidationError("camera_scene_region.points must contain at least three points")


@dataclass
class PtzAutomationConfig(JsonModel):
    """Framing policy and bounded motion limits; arming is runtime-only."""

    mode: str = "off"
    podium_zoom_enabled: bool = True
    decision_fps: float = 4.0
    minimum_subject_age_frames: int = 2
    target_padding_x: float = 0.10
    target_padding_y: float = 0.12
    single_subject_frame_height: float = 0.72
    podium_subject_frame_height: float = 0.84
    group_frame_height: float = 0.76
    adaptive_subject_framing_enabled: bool = True
    subject_safety_padding_x: float = 0.05
    subject_safety_padding_y: float = 0.05
    subject_center_deadband_x: float = 0.06
    subject_center_deadband_y: float = 0.09
    subject_minimum_occupancy: float = 0.74
    subject_maximum_occupancy: float = 0.88
    group_minimum_occupancy: float = 0.70
    group_maximum_occupancy: float = 0.86
    raised_gesture_maximum_occupancy: float = 0.84
    click_target_width: float = 0.12
    click_target_height: float = 0.34
    target_dwell_seconds: float = 0.35
    target_loss_hold_seconds: float = 2.5
    target_loss_disarm_seconds: float = 12.0
    minimum_command_interval_seconds: float = 0.65
    pan_deadband_units: int = 80
    tilt_deadband_units: int = 60
    zoom_deadband_units: int = 35
    maximum_pan_step_units: int = 320
    maximum_tilt_step_units: int = 220
    maximum_zoom_step_units: int = 100
    manual_override_pan_units: int = 420
    manual_override_tilt_units: int = 320
    manual_override_zoom_units: int = 140
    manual_override_grace_seconds: float = 2.5

    def __post_init__(self) -> None:
        self.mode = str(self.mode or "off").strip().casefold().replace("+", "_")
        if self.mode not in {"off", "subject", "stage", "stage_altar", "click"}:
            raise ValidationError(
                "ptz_automation.mode must be off, subject, stage, stage_altar, or click"
            )
        self.decision_fps = max(1.0, min(8.0, float(self.decision_fps)))
        self.minimum_subject_age_frames = max(1, min(30, int(self.minimum_subject_age_frames)))
        self.target_padding_x = max(0.0, min(0.40, float(self.target_padding_x)))
        self.target_padding_y = max(0.0, min(0.40, float(self.target_padding_y)))
        self.single_subject_frame_height = max(
            0.30, min(0.95, float(self.single_subject_frame_height))
        )
        self.podium_subject_frame_height = max(
            0.40, min(0.98, float(self.podium_subject_frame_height))
        )
        self.group_frame_height = max(0.30, min(0.95, float(self.group_frame_height)))
        self.adaptive_subject_framing_enabled = bool(
            self.adaptive_subject_framing_enabled
        )
        for field_name in (
            "subject_safety_padding_x",
            "subject_safety_padding_y",
        ):
            setattr(
                self,
                field_name,
                max(0.0, min(0.20, float(getattr(self, field_name)))),
            )
        self.subject_center_deadband_x = max(
            0.0, min(0.25, float(self.subject_center_deadband_x))
        )
        self.subject_center_deadband_y = max(
            0.0, min(0.25, float(self.subject_center_deadband_y))
        )
        self.subject_minimum_occupancy = max(
            0.30, min(0.95, float(self.subject_minimum_occupancy))
        )
        self.subject_maximum_occupancy = max(
            self.subject_minimum_occupancy,
            min(0.98, float(self.subject_maximum_occupancy)),
        )
        self.group_minimum_occupancy = max(
            0.30, min(0.95, float(self.group_minimum_occupancy))
        )
        self.group_maximum_occupancy = max(
            self.group_minimum_occupancy,
            min(0.98, float(self.group_maximum_occupancy)),
        )
        self.raised_gesture_maximum_occupancy = max(
            0.50, min(0.95, float(self.raised_gesture_maximum_occupancy))
        )
        self.click_target_width = max(0.02, min(0.60, float(self.click_target_width)))
        self.click_target_height = max(0.05, min(0.80, float(self.click_target_height)))
        self.target_dwell_seconds = max(0.0, min(5.0, float(self.target_dwell_seconds)))
        self.target_loss_hold_seconds = max(
            0.5, min(30.0, float(self.target_loss_hold_seconds))
        )
        self.target_loss_disarm_seconds = max(
            self.target_loss_hold_seconds,
            min(120.0, float(self.target_loss_disarm_seconds)),
        )
        self.minimum_command_interval_seconds = max(
            0.25, min(5.0, float(self.minimum_command_interval_seconds))
        )
        for field_name, low, high in (
            ("pan_deadband_units", 10, 1000),
            ("tilt_deadband_units", 10, 1000),
            ("zoom_deadband_units", 5, 500),
            ("maximum_pan_step_units", 40, 2000),
            ("maximum_tilt_step_units", 40, 2000),
            ("maximum_zoom_step_units", 20, 500),
            ("manual_override_pan_units", 100, 4000),
            ("manual_override_tilt_units", 100, 4000),
            ("manual_override_zoom_units", 40, 1000),
        ):
            setattr(self, field_name, max(low, min(high, int(getattr(self, field_name)))))
        self.manual_override_grace_seconds = max(
            1.0, min(10.0, float(self.manual_override_grace_seconds))
        )


@dataclass
class CameraTrackingConfig(JsonModel):
    """Bounded observation and framing policy; runtime arming grants motion authority."""

    enabled: bool = False
    analyze_audience: bool = True
    analyze_ptz: bool = True
    analysis_fps: float = 4.0
    maximum_analysis_width: int = 960
    minimum_confidence: float = 0.25
    upper_body_only: bool = True
    body_pose_envelope_enabled: bool = True
    minimum_pose_joint_confidence: float = 0.20
    minimum_match_iou: float = 0.12
    maximum_center_distance: float = 0.18
    maximum_missed_frames: int = 4
    audience_region_filter_enabled: bool = True
    audience_region_kinds: list[str] = field(
        default_factory=lambda: ["stage", "front_stage", "altar", "podium"]
    )
    relocalization_enabled: bool = True
    relocalization_fps: float = 1.0
    relocalization_maximum_width: int = 1280
    relocalization_stale_seconds: float = 4.0
    scene_regions: list[CameraSceneRegion] = field(default_factory=list)
    automation: PtzAutomationConfig = field(default_factory=PtzAutomationConfig)

    def __post_init__(self) -> None:
        self.analysis_fps = max(0.5, min(12.0, float(self.analysis_fps)))
        self.maximum_analysis_width = max(320, min(1920, int(self.maximum_analysis_width)))
        self.minimum_confidence = max(0.0, min(1.0, float(self.minimum_confidence)))
        self.minimum_pose_joint_confidence = max(
            0.05,
            min(0.90, float(self.minimum_pose_joint_confidence)),
        )
        self.minimum_match_iou = max(0.0, min(1.0, float(self.minimum_match_iou)))
        self.maximum_center_distance = max(
            0.01,
            min(1.0, float(self.maximum_center_distance)),
        )
        self.maximum_missed_frames = max(0, min(30, int(self.maximum_missed_frames)))
        allowed_kinds = {"stage", "front_stage", "altar", "podium", "custom"}
        self.audience_region_kinds = [
            kind
            for value in self.audience_region_kinds
            if (kind := str(value).strip().casefold().replace(" ", "_")) in allowed_kinds
        ]
        if not self.audience_region_kinds:
            self.audience_region_kinds = ["stage", "front_stage", "altar", "podium"]
        self.relocalization_fps = max(0.25, min(4.0, float(self.relocalization_fps)))
        self.relocalization_maximum_width = max(
            640,
            min(1920, int(self.relocalization_maximum_width)),
        )
        self.relocalization_stale_seconds = max(
            2.0,
            min(30.0, float(self.relocalization_stale_seconds)),
        )


@dataclass
class IntegrationConfig(JsonModel):
    propresenter: ProPresenterConfig = field(default_factory=ProPresenterConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    panasonic: PanasonicConfig = field(default_factory=PanasonicConfig)
    visca: ViscaConfig = field(default_factory=ViscaConfig)
    scoreboard: ScoreboardConfig = field(default_factory=ScoreboardConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    camera_tracking: CameraTrackingConfig = field(default_factory=CameraTrackingConfig)


@dataclass
class RemotePageConfig(JsonModel):
    name: str
    path: str
    enabled: bool = True
    required_integrations: list[str] = field(default_factory=list)
    access_protected: bool = False

    def __post_init__(self) -> None:
        self.name = _non_empty(self.name, "remote_page.name")
        self.path = _non_empty(self.path, "remote_page.path")


@dataclass
class InputListItem(JsonModel):
    label: str
    value: str = ""
    description: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        self.label = _non_empty(self.label, "input_list_item.label")
        self.value = str(self.value if self.value not in {None, ""} else self.label).strip()
        self.description = str(self.description or "").strip()


@dataclass
class InputListColumn(JsonModel):
    key: str
    title: str
    data_type: str = "string"
    role: str = ""

    def __post_init__(self) -> None:
        self.key = _non_empty(self.key, "input_list_column.key")
        self.title = _non_empty(self.title, "input_list_column.title")
        self.data_type = str(self.data_type or "string").strip().lower()
        self.role = str(self.role or "").strip().lower()
        if self.data_type not in {
            "string",
            "int",
            "float",
            "bool",
            "array_string",
            "array_int",
            "array_object",
            "dictionary",
            "json",
        }:
            raise ValidationError(f"Unsupported input list column type: {self.data_type}")


@dataclass
class InputListObjectField(JsonModel):
    key: str
    source: str = "base"
    json_path: str = ""
    url_template: str = ""
    data_type: str = "string"
    result_mode: str = "first"
    separator: str = " "
    normalize_whitespace: bool = False
    refresh_seconds: float = 0

    def __post_init__(self) -> None:
        self.key = _non_empty(self.key, "input_list_object_field.key")
        self.source = str(self.source or "base").strip().lower()
        self.json_path = str(self.json_path or "").strip()
        self.url_template = str(self.url_template or "").strip()
        self.data_type = str(self.data_type or "string").strip().lower()
        self.result_mode = str(self.result_mode or "first").strip().lower()
        self.separator = str(self.separator if self.separator is not None else " ")
        self.refresh_seconds = max(0, float(self.refresh_seconds or 0))
        if self.source not in {"base", "request"}:
            raise ValidationError(f"Unsupported object field source: {self.source}")
        if self.data_type not in {"string", "int", "float", "bool", "json", "array_string", "array_int"}:
            raise ValidationError(f"Unsupported object field type: {self.data_type}")
        if self.result_mode not in {"first", "join", "all"}:
            raise ValidationError(f"Unsupported object field result mode: {self.result_mode}")


@dataclass
class InputListCell(JsonModel):
    mode: str = "static"
    value: Any = ""
    url: str = ""
    json_path: str = ""
    preview: str = ""
    json_key_path: str = ""
    json_value_path: str = ""
    object_fields: list[InputListObjectField] = field(default_factory=list)
    object_identity_field: str = "uuid"
    object_concurrency: int = 4
    object_enrichment_last_polled: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.mode = str(self.mode or "static").strip().lower()
        self.url = str(self.url or "").strip()
        self.json_path = str(self.json_path or "").strip()
        self.preview = str(self.preview or "").strip()
        self.json_key_path = str(self.json_key_path or "").strip()
        self.json_value_path = str(self.json_value_path or "").strip()
        self.object_fields = [
            item if isinstance(item, InputListObjectField) else InputListObjectField.from_dict(item)
            for item in self.object_fields
        ]
        self.object_identity_field = str(self.object_identity_field or "uuid").strip()
        self.object_concurrency = max(1, min(16, int(self.object_concurrency or 4)))
        self.object_enrichment_last_polled = {
            str(key): max(0, float(value or 0))
            for key, value in self.object_enrichment_last_polled.items()
            if str(key).strip()
        }
        if self.mode not in {"static", "polled"}:
            raise ValidationError(f"Unsupported input list cell mode: {self.mode}")


@dataclass
class InputListRow(JsonModel):
    enabled: bool = True
    cells: dict[str, InputListCell] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cells = {
            str(key): value if isinstance(value, InputListCell) else InputListCell.from_dict(value)
            for key, value in self.cells.items()
        }


@dataclass
class InputListDefinition(JsonModel):
    key: str
    name: str
    items: list[InputListItem] = field(default_factory=list)
    description: str = ""
    builtin: bool = False
    columns: list[InputListColumn] = field(default_factory=list)
    rows: list[InputListRow] = field(default_factory=list)
    polling_rate_seconds: float = 0

    def __post_init__(self) -> None:
        self.key = _non_empty(self.key, "input_list.key")
        self.name = _non_empty(self.name, "input_list.name")
        self.description = str(self.description or "").strip()
        self.polling_rate_seconds = max(0, float(self.polling_rate_seconds))
        if not self.columns and self.items:
            self.columns = [
                InputListColumn("label", "Label", "string", "label"),
                InputListColumn("value", "Value", "string", "value"),
                InputListColumn("description", "Description", "string"),
            ]
            self.rows = [
                InputListRow(
                    item.enabled,
                    {
                        "label": InputListCell("static", item.label),
                        "value": InputListCell("static", item.value),
                        "description": InputListCell("static", item.description),
                    },
                )
                for item in self.items
            ]


@dataclass
class UiPreferences(JsonModel):
    start_page: str = "Overview"
    sidebar_collapsed: bool = False
    theme: str = "system"
    endpoint_option_lists: dict[str, list[str]] = field(default_factory=dict)
    input_lists: list[InputListDefinition] = field(default_factory=list)
    input_lists_initialized: bool = False
    keep_running_after_window_close: bool = True
    show_menu_bar_icon: bool = True
    launch_at_login: bool = False


@dataclass
class AppConfig(JsonModel):
    schema_version: int = 1
    app_name: str = "Production Hub"
    subtitle: str = "Production automation, integrations, and diagnostics."
    active_profile: str = "Default Profile"
    api: ApiServerConfig = field(default_factory=ApiServerConfig)
    integrations: IntegrationConfig = field(default_factory=IntegrationConfig)
    remote_pages: list[RemotePageConfig] = field(default_factory=list)
    ui: UiPreferences = field(default_factory=UiPreferences)
    last_saved_at: str = ""

    def __post_init__(self) -> None:
        self.app_name = _non_empty(self.app_name, "app_name")
        self.active_profile = _non_empty(self.active_profile, "active_profile")


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def automatic_backups_dir(self) -> Path:
        return self.root / "backups" / "automatic"

    @property
    def manual_backups_dir(self) -> Path:
        return self.root / "backups" / "manual"

    def ensure(self) -> None:
        for path in (
            self.config_dir,
            self.state_dir,
            self.logs_dir,
            self.automatic_backups_dir,
            self.manual_backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
