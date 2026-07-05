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
    port_conflict_behavior: str = "cancel"
    safe_mode_for_port_conflicts: bool = True

    def __post_init__(self) -> None:
        self.listen_ip = _non_empty(self.listen_ip, "visca.listen_ip")
        self.udp_port = _port(self.udp_port, "visca.udp_port")


@dataclass
class MidiConfig(JsonModel):
    enabled: bool = False
    status_label: str = "Not Configured"
    input_devices: list[str] = field(default_factory=list)
    output_devices: list[str] = field(default_factory=list)
    mappings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IntegrationConfig(JsonModel):
    propresenter: ProPresenterConfig = field(default_factory=ProPresenterConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    panasonic: PanasonicConfig = field(default_factory=PanasonicConfig)
    visca: ViscaConfig = field(default_factory=ViscaConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)


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
class UiPreferences(JsonModel):
    start_page: str = "Overview"
    sidebar_collapsed: bool = False
    theme: str = "system"
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
