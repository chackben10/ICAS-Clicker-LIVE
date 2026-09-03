from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from production_hub.app.dev_launcher import (
    DEV_APP_NAME,
    DEV_CHILD_ARGUMENT,
    prepare_development_app,
)
from production_hub.core.config.models import AppConfig
from production_hub.integrations.obs.service import ObsService
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService
from testing.harness.paths import APP_ROOT, REPOSITORY_ROOT, production_data_root
from testing.harness.reporting import redact


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _load_config(data_root: Path | None = None) -> AppConfig:
    root = data_root or production_data_root()
    payload = json.loads((root / "config" / "default_profile.json").read_text(encoding="utf-8"))
    return AppConfig.from_dict(payload)


def _capture_ndi_frame(source_name: str, timeout: float = 12.0):
    from production_hub.video.frame_broker import LatestFrameBroker
    from production_hub.video.models import VideoSourceKey
    from production_hub.video.ndi_source import NDISourceSettings, NDIVideoSource

    broker = LatestFrameBroker()
    source = NDIVideoSource(
        broker,
        NDISourceSettings(
            source_name,
            publish_fps=4,
            stale_after_seconds=2.0,
        ),
    )
    source.start()
    source.set_output_active(True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            packet = broker.frame(VideoSourceKey.AUDIENCE)
            if packet is not None:
                return packet.image
            time.sleep(0.1)
        raise RuntimeError(str(broker.snapshot(VideoSourceKey.AUDIENCE).message))
    finally:
        source.stop()


def live_preflight(data_root: Path | None = None) -> tuple[AppConfig, list[PreflightCheck]]:
    root = data_root or production_data_root()
    checks: list[PreflightCheck] = []

    config_path = root / "config" / "default_profile.json"
    checks.append(PreflightCheck("production_config", config_path.is_file(), str(config_path)))
    if not config_path.is_file():
        return AppConfig(), checks
    config = _load_config(root)

    applications = {
        "ProPresenter": Path("/Applications/ProPresenter.app"),
        "Production Hub": Path("/Applications/Production Hub.app"),
    }
    for name, path in applications.items():
        checks.append(PreflightCheck(f"application:{name}", path.is_dir(), str(path)))

    if config.integrations.propresenter.enabled:
        target = f"{config.integrations.propresenter.host}:{config.integrations.propresenter.port}"
        checks.append(
            PreflightCheck(
                "propresenter_api",
                _tcp_reachable(config.integrations.propresenter.host, config.integrations.propresenter.port),
                target,
            )
        )
        configured_assets = (
            bool(config.integrations.propresenter.presentations)
            and bool(config.integrations.propresenter.service_logos)
            and bool(config.integrations.propresenter.macros)
        )
        checks.append(PreflightCheck("propresenter_assets", configured_assets, "presentations, logos, and macros configured"))

    if config.integrations.obs.enabled:
        target = f"{config.integrations.obs.host}:{config.integrations.obs.port}"
        checks.append(
            PreflightCheck(
                "obs_websocket",
                _tcp_reachable(config.integrations.obs.host, config.integrations.obs.port),
                target,
            )
        )
        checks.append(
            PreflightCheck(
                "obs_contract",
                bool(config.integrations.obs.known_scenes and config.integrations.obs.look_rules),
                "known scenes and look rules configured",
            )
        )

    if config.integrations.panasonic.enabled:
        checks.append(
            PreflightCheck(
                "panasonic_http",
                _tcp_reachable(config.integrations.panasonic.camera_ip, 80),
                config.integrations.panasonic.camera_ip,
            )
        )

    ndi_runtime = Path("/usr/local/lib/libndi.dylib")
    checks.append(PreflightCheck("ndi_runtime", ndi_runtime.is_file(), str(ndi_runtime)))
    if config.integrations.video.enabled and config.integrations.video.audience_enabled:
        try:
            from production_hub.video.ndi_native import NativeNDI

            sources = NativeNDI.shared().discover_sources(1200)
            wanted = config.integrations.video.audience_ndi_source_name.casefold()
            found = any(wanted in source.casefold() for source in sources)
            checks.append(PreflightCheck("ndi_audience_source", found, config.integrations.video.audience_ndi_source_name))
        except Exception as exc:
            checks.append(PreflightCheck("ndi_audience_source", False, str(exc)))

    try:
        import mido

        inputs = list(mido.get_input_names())
        wanted_midi = config.integrations.midi.input_name
        checks.append(PreflightCheck("midi_input", wanted_midi in inputs, wanted_midi or "configured MIDI input"))
    except Exception as exc:
        checks.append(PreflightCheck("midi_input", False, str(exc)))

    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtMultimedia import QMediaDevices
        from production_hub.video.local_camera_source import encode_device_id

        qt_application = QCoreApplication.instance()
        if qt_application is None:
            qt_application = QCoreApplication(["production-hub-live-preflight"])
        device_ids = {encode_device_id(bytes(device.id())) for device in QMediaDevices.videoInputs()}
        configured_id = config.integrations.video.ptz_device_id
        checks.append(PreflightCheck("ptz_capture", configured_id in device_ids, configured_id or "configured PTZ device"))
    except Exception as exc:
        checks.append(PreflightCheck("ptz_capture", False, str(exc)))

    active_manifest = root / "calibration" / "active-map.json"
    approved = False
    active_detail = str(active_manifest)
    try:
        manifest = json.loads(active_manifest.read_text(encoding="utf-8"))
        active_map = Path(manifest["active_map"])
        review = active_map.with_suffix(".review.json")
        review_payload = json.loads(review.read_text(encoding="utf-8"))
        approved = active_map.is_file() and review_payload.get("approval_status") == "approved"
        active_detail = str(active_map)
    except Exception:
        pass
    checks.append(PreflightCheck("approved_calibration", approved, active_detail))

    if approved and config.integrations.video.enabled and config.integrations.video.audience_enabled:
        try:
            from production_hub.calibration.light_level import assess_camera_light
            live_image = _capture_ndi_frame(config.integrations.video.audience_ndi_source_name)
            light = assess_camera_light(live_image)
            checks.append(PreflightCheck("audience_frame_quality", light.acceptable, light.message))
        except Exception as exc:
            checks.append(PreflightCheck("audience_frame_quality", False, str(exc)))
            checks.append(PreflightCheck("live_calibration_lock", False, str(exc)))
        else:
            try:
                import cv2

                from production_hub.calibration.relocalization import AudienceRelocalizer, qimage_to_bgr
                from production_hub.calibration.review import load_active_calibration_review

                review = load_active_calibration_review(root)
                if review is None:
                    raise RuntimeError("No approved calibration review can be loaded")
                reference = cv2.imread(str(review.audience_image_path))
                result = AudienceRelocalizer(
                    reference,
                    review,
                    maximum_width=config.integrations.camera_tracking.relocalization_maximum_width,
                ).estimate(qimage_to_bgr(live_image))
                checks.append(
                    PreflightCheck(
                        "live_calibration_lock",
                        result.state.value == "locked",
                        (
                            f"{result.state.value}: {result.candidate_matches} matches, "
                            f"{result.inliers} inliers, {result.median_error_pixels:.2f}px median error"
                        ),
                    )
                )
            except Exception as exc:
                checks.append(PreflightCheck("live_calibration_lock", False, str(exc)))

    try:
        import Quartz

        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        unlocked = not bool(session.get("CGSSessionScreenIsLocked", False))
        checks.append(PreflightCheck("gui_unlocked", unlocked, "active macOS GUI session"))
    except Exception as exc:
        checks.append(PreflightCheck("gui_unlocked", False, str(exc)))

    try:
        from testing.harness.macos_accessibility import run_applescript

        ui_enabled = run_applescript('tell application "System Events" to return UI elements enabled')
        window_count = run_applescript(
            'tell application "System Events" to tell process "Production Hub" to return count of windows'
        )
        trusted = ui_enabled.casefold() == "true" and int(window_count or "0") > 0
        checks.append(
            PreflightCheck(
                "accessibility_permission",
                trusted,
                "System Events can inspect the Production Hub UI",
            )
        )
    except Exception as exc:
        checks.append(PreflightCheck("accessibility_permission", False, str(exc)))

    return config, checks


class RecoverySnapshot:
    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = (data_root or production_data_root()).resolve()
        self.path = Path(tempfile.mkdtemp(prefix="production-hub-recovery-"))
        os.chmod(self.path, 0o700)
        self.captured = False
        self.restored = False

    def capture(self) -> None:
        if self.captured:
            raise RuntimeError("Recovery state was already captured")
        for name in ("config", "state"):
            source = self.data_root / name
            if source.exists():
                shutil.copytree(source, self.path / name, copy_function=shutil.copy2)
        manifest = {"data_root": str(self.data_root), "directories": [name for name in ("config", "state") if (self.path / name).exists()]}
        (self.path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path / "manifest.json", 0o600)
        self.captured = True

    def restore(self) -> None:
        if not self.captured:
            raise RuntimeError("Cannot restore before capturing recovery state")
        for name in ("config", "state"):
            source = self.path / name
            if not source.exists():
                continue
            destination = self.data_root / name
            displaced = self.path / f"{name}-after-tests"
            if destination.exists():
                os.replace(destination, displaced)
            try:
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            except Exception:
                if destination.exists():
                    shutil.rmtree(destination)
                if displaced.exists():
                    os.replace(displaced, destination)
                raise
            if displaced.exists():
                shutil.rmtree(displaced)
        self.restored = True

    def discard(self) -> None:
        if not self.restored:
            raise RuntimeError("Recovery data is retained until restoration succeeds")
        shutil.rmtree(self.path)


class ApplicationLifecycle:
    def __init__(self) -> None:
        self.initially_running = {
            "ProPresenter": self.is_running("ProPresenter"),
            "Production Hub": self.is_running("Production Hub"),
            DEV_APP_NAME: self.is_running(DEV_APP_NAME),
        }

    @staticmethod
    def _osascript(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["osascript", "-e", script], text=True, capture_output=True, check=check)

    @classmethod
    def is_running(cls, app_name: str) -> bool:
        result = cls._osascript(f'application "{app_name}" is running', check=False)
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    @staticmethod
    def _quit_script(app_name: str) -> str:
        if app_name in {"Production Hub", DEV_APP_NAME}:
            # The app intentionally stays resident when its window is closed.
            # Drive its own Quit action so MainWindow marks the close as a real
            # application shutdown before QApplication receives it.
            return f"""tell application "System Events"
if exists process "{app_name}" then
    tell process "{app_name}"
        set frontmost to true
        keystroke "q" using command down
    end tell
end if
end tell"""
        return f'tell application "{app_name}" to quit'

    @classmethod
    def quit_gracefully(cls, app_name: str, timeout: float, *, retry_interval: float = 1.0) -> None:
        if not cls.is_running(app_name):
            return
        deadline = time.monotonic() + timeout
        next_request = 0.0
        errors: list[str] = []
        while time.monotonic() < deadline:
            if not cls.is_running(app_name):
                return
            now = time.monotonic()
            if now >= next_request:
                result = cls._osascript(cls._quit_script(app_name), check=False)
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
                    errors.append(detail)
                next_request = now + retry_interval
            time.sleep(0.25)
        diagnostic = f" Last Apple-event error: {errors[-1]}" if errors else ""
        raise TimeoutError(
            f"{app_name} did not quit gracefully within {timeout:.0f} seconds; it was not force-quit.{diagnostic}"
        )

    @staticmethod
    def open_application(path_or_name: str, *, new_instance: bool = False) -> None:
        command = ["open"]
        if new_instance:
            command.append("-n")
        if path_or_name.endswith(".app") or "/" in path_or_name:
            command.append(path_or_name)
        else:
            command.extend(["-a", path_or_name])
        subprocess.run(command, check=True)

    @staticmethod
    def wait_http(url: str, timeout: float) -> float:
        started = time.perf_counter()
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if 200 <= response.status < 500:
                        return time.perf_counter() - started
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise TimeoutError(f"Timed out waiting for {url}: {last_error}")

    @staticmethod
    def build_production_hub(python: str) -> Path:
        subprocess.run(
            [python, str(APP_ROOT / "scripts" / "build_macos_app.py"), "--no-install"],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        bundle = APP_ROOT / "dist" / "Production Hub.app"
        if not bundle.is_dir():
            raise RuntimeError(f"Build did not produce {bundle}")
        return bundle

    @staticmethod
    def open_source_development_hub() -> Path:
        """Launch live source through a stable TCC/code-signing identity."""

        bundle = prepare_development_app(APP_ROOT)
        subprocess.run(
            [
                "open",
                "-n",
                str(bundle),
                "--args",
                str(APP_ROOT / "main.py"),
                DEV_CHILD_ARGUMENT,
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        return bundle


async def capture_external_state(config: AppConfig) -> dict[str, Any]:
    state: dict[str, Any] = {}
    hub_base = config.api.base_url.rstrip("/")
    paths = {
        "scoreboard": "/score",
        "auto_show": "/auto-show",
        "clicker_activation": "/clicker-presentation-activation",
        "obs_scene": "/scene/current",
        "obs_items": f"/scene/items?scene={config.integrations.obs.main_layout_scene.replace(' ', '%20')}",
        "presentation": "/active-presentation",
        "focused_playlist": "/playlist/focused",
        "slide_index": "/slide-index",
    }

    def fetch(path: str) -> Any:
        return fetch_absolute(f"{hub_base}{path}")

    def fetch_absolute(url: str) -> Any:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read()
            try:
                return json.loads(body.decode("utf-8"))
            except Exception:
                return body.decode("utf-8", errors="replace")

    for key, path in paths.items():
        try:
            state[key] = await asyncio.to_thread(fetch, path)
        except Exception as exc:
            state[f"{key}_capture_error"] = str(exc)

    try:
        state["propresenter_look"] = await asyncio.to_thread(
            lambda: fetch_absolute(f"{config.integrations.propresenter.base_url}/look/current")
        )
    except Exception as exc:
        state["propresenter_look_capture_error"] = str(exc)

    obs = ObsService(config.integrations.obs)
    try:
        transition = await obs.client.call("get_current_scene_transition")
        raw = transition if isinstance(transition, dict) else vars(transition)
        state["obs_transition"] = {
            "name": raw.get("transitionName") or raw.get("transition_name") or raw.get("currentSceneTransitionName") or raw.get("current_scene_transition_name") or "",
            "duration": raw.get("transitionDuration") or raw.get("transition_duration") or raw.get("currentSceneTransitionDuration") or raw.get("current_scene_transition_duration"),
        }
    except Exception as exc:
        state["obs_transition_capture_error"] = str(exc)

    camera = PanasonicAwpService(config.integrations.panasonic)
    try:
        state["camera_pan_tilt"] = list(await camera.query_pan_tilt_position())
        state["camera_zoom"] = await camera.query_zoom_position()
    except Exception as exc:
        state["camera_capture_error"] = str(exc)
    return state


async def restore_external_state(config: AppConfig, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    hub_base = config.api.base_url.rstrip("/")

    def request(path: str, payload: dict[str, Any] | None = None, method: str = "POST") -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json"}
        if config.api.access_token:
            headers["X-Production-Hub-Token"] = config.api.access_token
        req = urllib.request.Request(
            f"{hub_base}{path}",
            data=data if method != "GET" else None,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=8):
            pass

    restore_requests: list[tuple[str, dict[str, Any] | None, str]] = []
    if isinstance(state.get("scoreboard"), dict):
        restore_requests.append(("/score", state["scoreboard"], "POST"))
    if isinstance(state.get("auto_show"), dict):
        restore_requests.append(("/auto-show", {"enabled": state["auto_show"].get("enabled", False)}, "POST"))
    if isinstance(state.get("clicker_activation"), dict):
        restore_requests.append(
            ("/clicker-presentation-activation", {"enabled": state["clicker_activation"].get("enabled", True)}, "POST")
        )
    obs_scene = state.get("obs_scene")
    if isinstance(obs_scene, dict):
        scene_name = str(
            obs_scene.get("scene")
            or obs_scene.get("name")
            or obs_scene.get("currentScene")
            or obs_scene.get("currentProgramSceneName")
            or ""
        )
        if scene_name:
            scene_params: dict[str, str | int] = {"name": scene_name}
            transition = state.get("obs_transition")
            if isinstance(transition, dict) and transition.get("name"):
                scene_params["transition"] = str(transition["name"])
                if transition.get("duration") is not None:
                    scene_params["duration"] = int(transition["duration"])
            restore_requests.append((f"/scene/set?{urllib.parse.urlencode(scene_params)}", None, "GET"))
    for path, payload, method in restore_requests:
        try:
            await asyncio.to_thread(request, path, payload, method)
        except Exception as exc:
            errors.append(f"restore {path}: {exc}")

    obs_items = state.get("obs_items")
    if isinstance(obs_items, dict) and isinstance(obs_items.get("items"), list):
        try:
            scene_name = str(obs_items.get("sceneName") or config.integrations.obs.main_layout_scene)
            expected: dict[int, bool] = {}
            obs = ObsService(config.integrations.obs)
            for item in obs_items["items"]:
                scene_item_id = int(item.get("sceneItemId") or item.get("id") or item.get("scene_item_id"))
                enabled = bool(item.get("enabled", item.get("sceneItemEnabled")))
                expected[scene_item_id] = enabled
                await obs.set_scene_item_enabled(scene_name, scene_item_id, enabled)
            observed = {item.scene_item_id: item.enabled for item in await obs.get_scene_items(scene_name)}
            mismatches = {
                item_id: {"expected": enabled, "actual": observed.get(item_id)}
                for item_id, enabled in expected.items()
                if observed.get(item_id) != enabled
            }
            if mismatches:
                raise RuntimeError(f"OBS item visibility verification failed: {mismatches}")
        except Exception as exc:
            errors.append(f"restore OBS scene items: {exc}")

    presentation = state.get("presentation")
    slide = state.get("slide_index")
    if isinstance(presentation, dict) and isinstance(slide, dict):
        presentation_obj = presentation.get("presentation") or {}
        uuid = str(((presentation_obj.get("id") or {}).get("uuid")) or "")
        index = (slide.get("presentation_index") or {}).get("index")
        if uuid and index is not None:
            try:
                await asyncio.to_thread(request, f"/presentation/{uuid}/{int(index)}/trigger", None, "POST")
            except Exception as exc:
                errors.append(f"restore ProPresenter presentation: {exc}")

    focused_playlist = state.get("focused_playlist")
    if isinstance(focused_playlist, dict):
        focused_id = focused_playlist.get("playlist") or focused_playlist.get("id") or {}
        focused_uuid = str(focused_id.get("uuid") if isinstance(focused_id, dict) else "")
        focused_name = str(focused_id.get("name") if isinstance(focused_id, dict) else "")
        focused_index = focused_id.get("index") if isinstance(focused_id, dict) else None
        if focused_uuid:
            try:
                def focused_playlist_json() -> dict[str, Any]:
                    with urllib.request.urlopen(
                        f"{config.integrations.propresenter.base_url}/playlist/focused",
                        timeout=8,
                    ) as response:
                        body = response.read()
                        return json.loads(body) if body else {}

                current = await asyncio.to_thread(focused_playlist_json)
                current_id = current.get("playlist") or current.get("id") or {}
                current_uuid = str(current_id.get("uuid") if isinstance(current_id, dict) else "")
                if current_uuid != focused_uuid:
                    focus_errors: list[str] = []
                    identifiers = [focused_name, str(focused_index) if focused_index is not None else "", focused_uuid]
                    for identifier in dict.fromkeys(item for item in identifiers if item):
                        try:
                            focus_url = (
                                f"{config.integrations.propresenter.base_url}/playlist/"
                                f"{urllib.parse.quote(identifier, safe='')}/focus"
                            )
                            await asyncio.to_thread(lambda url=focus_url: urllib.request.urlopen(url, timeout=8).close())
                            break
                        except Exception as exc:
                            focus_errors.append(f"{identifier}: {exc}")
                    else:
                        raise RuntimeError("; ".join(focus_errors))
                    current = await asyncio.to_thread(focused_playlist_json)
                    current_id = current.get("playlist") or current.get("id") or {}
                    current_uuid = str(current_id.get("uuid") if isinstance(current_id, dict) else "")
                    if current_uuid != focused_uuid:
                        raise RuntimeError(f"expected playlist {focused_uuid}, observed {current_uuid or 'none'}")
            except Exception as exc:
                errors.append(f"restore ProPresenter playlist focus: {exc}")

    look = state.get("propresenter_look")
    if isinstance(look, dict):
        look_id = look.get("id") or {}
        look_uuid = str(look_id.get("uuid") if isinstance(look_id, dict) else "")
        look_name = str(look_id.get("name") if isinstance(look_id, dict) else "")
        look_index = look_id.get("index") if isinstance(look_id, dict) else None
        if look_uuid:
            try:
                def propresenter_json(path: str) -> Any:
                    with urllib.request.urlopen(f"{config.integrations.propresenter.base_url}{path}", timeout=8) as response:
                        body = response.read()
                        return json.loads(body) if body else None

                current = await asyncio.to_thread(propresenter_json, "/look/current")
                current_uuid = str((((current or {}).get("id") or {}).get("uuid")) or "")
                if current_uuid != look_uuid:
                    trigger_errors: list[str] = []
                    identifiers = [look_name, str(look_index) if look_index is not None else "", look_uuid]
                    for identifier in dict.fromkeys(item for item in identifiers if item):
                        try:
                            await asyncio.to_thread(
                                propresenter_json,
                                f"/look/{urllib.parse.quote(identifier, safe='')}/trigger",
                            )
                            break
                        except Exception as exc:
                            trigger_errors.append(f"{identifier}: {exc}")
                    else:
                        raise RuntimeError("; ".join(trigger_errors))
                    current = await asyncio.to_thread(propresenter_json, "/look/current")
                    current_uuid = str((((current or {}).get("id") or {}).get("uuid")) or "")
                    if current_uuid != look_uuid:
                        raise RuntimeError(f"expected look {look_uuid}, observed {current_uuid or 'none'}")
            except Exception as exc:
                errors.append(f"restore ProPresenter look: {exc}")

    for neutral_path in ("/audio/clear", "/timer/stop-reset", "/api/camera/automation/disarm"):
        try:
            await asyncio.to_thread(request, neutral_path, {}, "POST")
        except Exception as exc:
            errors.append(f"neutralize {neutral_path}: {exc}")

    camera = PanasonicAwpService(config.integrations.panasonic)
    try:
        if isinstance(state.get("camera_pan_tilt"), list) and len(state["camera_pan_tilt"]) == 2:
            await camera.absolute_pan_tilt(int(state["camera_pan_tilt"][0]), int(state["camera_pan_tilt"][1]))
        if state.get("camera_zoom") is not None:
            await camera.absolute_zoom(int(state["camera_zoom"]))
        await camera.focus_auto()
    except Exception as exc:
        errors.append(f"restore camera: {exc}")
    return errors


def sanitized_checks(checks: list[PreflightCheck]) -> list[dict[str, Any]]:
    return redact([asdict(check) for check in checks])
