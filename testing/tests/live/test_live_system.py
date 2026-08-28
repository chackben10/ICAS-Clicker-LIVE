from __future__ import annotations

import asyncio
import json
import re
import socket
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from production_hub.core.config.models import AppPaths
from production_hub.core.config.repository import ConfigRepository
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService
from production_hub.integrations.propresenter.audio_service import normalize_pad_key
from production_hub.tracking.service import PersonTrackingService
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey, VideoSourceState
from production_hub.video.ndi_native import NativeNDI
from production_hub.video.ndi_source import NDISourceSettings, NDIVideoSource
from testing.harness.benchmarks import percentile
from testing.harness.ndi_fixture_sender import (
    NDISyntheticSender,
    SINGLE_PERSON,
    SOURCE_NAME,
    fixture_frame,
    stream_scenario,
)
from testing.harness.macos_accessibility import enabled_button_names, navigate_production_hub_pages
from testing.harness.paths import production_data_root


pytestmark = [pytest.mark.live, pytest.mark.performance]
PROHIBITED_LIVE_ACTIONS = {"panasonic.save_preset"}
DESKTOP_PAGES = [
    "Overview",
    "Endpoints",
    "Input Lists",
    "Automations",
    "Integrations",
    "MIDI",
    "Camera Control",
    "Scoreboard",
    "Remote Pages",
    "Settings",
]


def request(config, path: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 15) -> tuple[int, object, float]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if config.api.access_token:
        headers["X-Production-Hub-Token"] = config.api.access_token
    target = f"{config.api.base_url.rstrip('/')}{path}"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(target, data=data, method=method, headers=headers), timeout=timeout) as response:
            body = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            try:
                return response.status, json.loads(body), elapsed
            except Exception:
                return response.status, body.decode(errors="replace"), elapsed
    except urllib.error.HTTPError as exc:
        body = exc.read()
        elapsed = (time.perf_counter() - started) * 1000
        try:
            detail = json.loads(body)
        except Exception:
            detail = body.decode(errors="replace")
        return exc.code, detail, elapsed


def propresenter_get(config, path: str, timeout: float = 10) -> object:
    target = f"{config.integrations.propresenter.base_url.rstrip('/')}{path}"
    with urllib.request.urlopen(target, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body else None


def focus_nonempty_propresenter_playlist(config) -> str:
    catalog = propresenter_get(config, "/playlists")
    candidates = catalog if isinstance(catalog, list) else catalog.get("items", [])
    for candidate in candidates:
        identifier = candidate.get("id") if isinstance(candidate, dict) else None
        playlist_uuid = str(identifier.get("uuid") if isinstance(identifier, dict) else "")
        if not playlist_uuid:
            continue
        playlist = propresenter_get(config, f"/playlist/{urllib.parse.quote(playlist_uuid, safe='')}")
        if not isinstance(playlist, dict) or not any(
            isinstance(item, dict) and item.get("type") == "presentation"
            for item in playlist.get("items", [])
        ):
            continue
        propresenter_get(config, f"/playlist/{urllib.parse.quote(playlist_uuid, safe='')}/focus")
        focused = _wait_until(
            lambda: propresenter_get(config, "/playlist/focused"),
            5,
            interval=0.1,
        )
        focused_id = focused.get("id") if isinstance(focused, dict) else None
        observed_uuid = str(focused_id.get("uuid") if isinstance(focused_id, dict) else "")
        if observed_uuid == playlist_uuid:
            return playlist_uuid
    raise AssertionError("No non-empty ProPresenter playlist can be focused for endpoint testing")


def add_metric(
    metrics: list[dict],
    name: str,
    samples: list[float],
    unit: str = "ms",
    *,
    direction: str = "lower_is_better",
) -> None:
    metrics.append(
        {
            "name": name,
            "unit": unit,
            "iterations": len(samples),
            "median": round(statistics.median(samples), 4),
            "p95": round(percentile(samples, 0.95), 4),
            "p99": round(percentile(samples, 0.99), 4),
            "minimum": round(min(samples), 4),
            "maximum": round(max(samples), 4),
            "direction": direction,
        }
    )


def test_enabled_live_integrations(live_config, live_metrics) -> None:
    statuses: list[float] = []
    status, payload, latency = request(live_config, "/admin/health")
    statuses.append(latency)
    assert status == 200
    assert isinstance(payload, dict)
    health = {str(item["name"]).casefold(): item for item in payload["integrations"]}
    expected = {
        "propresenter": live_config.integrations.propresenter.enabled,
        "obs": live_config.integrations.obs.enabled,
        "panasonic awp": live_config.integrations.panasonic.enabled,
        "midi": live_config.integrations.midi.enabled,
    }
    for name, enabled in expected.items():
        if enabled:
            assert name in health, f"Missing live health entry for {name}"
            assert health[name]["status"] == "Connected", health[name]

    for path in ("/health", "/active-presentation", "/slide-index", "/scene/current", "/score", "/api/camera/tracking"):
        for _ in range(5):
            status, _payload, latency = request(live_config, path)
            assert status == 200, (path, status, _payload)
            statuses.append(latency)
    add_metric(live_metrics, "enabled_integration_round_trip", statuses)


def test_built_app_macos_accessibility_navigation_and_inventory() -> None:
    visited = navigate_production_hub_pages(DESKTOP_PAGES)
    assert visited == DESKTOP_PAGES
    navigate_production_hub_pages(["Overview"])
    buttons = enabled_button_names()
    assert buttons
    assert "<missing>" not in buttons


def test_configured_propresenter_obs_and_input_assets(live_config) -> None:
    status, logos, _ = request(live_config, "/service_logos")
    assert status == 200
    configured_logos = {item.uuid for item in live_config.integrations.propresenter.service_logos}
    returned_logos = {item.get("uuid") for item in logos.get("items", logos if isinstance(logos, list) else [])}
    assert configured_logos <= returned_logos

    status, macros, _ = request(live_config, "/macros")
    assert status == 200
    configured_macros = {item.macro_name for item in live_config.integrations.propresenter.macros}
    returned_macros = {
        item.get("name") if isinstance(item, dict) else str(item)
        for item in macros.get("items", macros if isinstance(macros, list) else [])
    }
    assert configured_macros <= returned_macros

    status, items, _ = request(
        live_config,
        "/scene/items?scene=" + urllib.parse.quote(live_config.integrations.obs.main_layout_scene),
    )
    assert status == 200
    observed_ids = {
        int(item.get("scene_item_id") or item.get("sceneItemId"))
        for item in items.get("items", [])
    }
    configured_ids = {item.scene_item_id for item in live_config.integrations.obs.source_mappings}
    assert configured_ids <= observed_ids

    repository = ConfigRepository(AppPaths(production_data_root()))
    config = repository.load_app_config()
    list_keys = {item.key for item in config.ui.input_lists}
    referenced_sources = {
        input_def.option_source
        for endpoint in repository.load_endpoints()
        for input_def in endpoint.inputs
        if input_def.option_source
    }
    assert referenced_sources <= list_keys


def _endpoint_seed(
    endpoint,
    config,
    current_score: dict,
    *,
    audio_track: str,
    playlist_uuid: str,
) -> dict[str, object]:
    seed: dict[str, object] = {}
    option_values = {
        "service_logos": config.integrations.propresenter.service_logos[0].uuid,
        "macros": config.integrations.propresenter.macros[0].macro_name,
        "audio_playlists": config.integrations.propresenter.audio.playlists[0],
        "obs_scenes": config.integrations.obs.known_scenes[0],
    }
    for rule in endpoint.match_rules:
        if rule.operator in {"equals", "contains"}:
            seed[rule.input_name] = rule.value
        elif rule.operator == "exists":
            seed[rule.input_name] = "value"
    for item in endpoint.inputs:
        if item.name in seed:
            continue
        if item.option_source in option_values:
            seed[item.name] = option_values[item.option_source]
        elif item.default != "":
            seed[item.name] = item.default
        elif item.name in {"uuid", "presentation_uuid"}:
            seed[item.name] = config.integrations.propresenter.presentations[0].uuid
        elif item.name == "playlist_uuid":
            seed[item.name] = playlist_uuid
        elif item.name == "track":
            seed[item.name] = audio_track
        elif item.name in {"index", "item_index", "slide_index"}:
            seed[item.name] = 0
        elif item.name == "preset":
            seed[item.name] = "0"
        elif item.name == "query":
            seed[item.name] = "test"
        elif item.required:
            seed[item.name] = 1 if item.kind in {"integer", "float"} else True if item.kind == "bool" else "test"
    if endpoint.route == "/score":
        seed.update(current_score)
    return seed


def _endpoint_path(route: str, seed: dict[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        return urllib.parse.quote(str(seed[match.group(1)]), safe="")

    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", replace, route)


def test_every_configured_endpoint_executes_with_valid_production_inputs(live_config, live_metrics) -> None:
    repository = ConfigRepository(AppPaths(production_data_root()))
    endpoints = [item for item in repository.load_endpoints() if item.enabled]
    status, score, _ = request(live_config, "/score")
    assert status == 200 and isinstance(score, dict)
    audio_playlist = live_config.integrations.propresenter.audio.playlists[0]
    status, tracks, _ = request(live_config, "/audio/tracks?playlist=" + urllib.parse.quote(audio_playlist))
    assert status == 200 and tracks.get("items"), "The configured audio playlist has no testable tracks"
    first_track = tracks["items"][0]
    audio_track = str(first_track.get("name") if isinstance(first_track, dict) else first_track)
    status, focused_playlist, _ = request(live_config, "/playlist/focused")
    assert status == 200
    playlist_uuid = str((focused_playlist.get("id") or {}).get("uuid") or "")
    if not playlist_uuid:
        playlist_uuid = focus_nonempty_propresenter_playlist(live_config)
    latencies: list[float] = []
    exercised: set[str] = set()
    simulated_for_safety: set[str] = set()
    for endpoint in endpoints:
        action_types = {action.action_type for action in endpoint.actions}
        if action_types & PROHIBITED_LIVE_ACTIONS:
            simulated_for_safety.add(endpoint.key)
            continue
        seed = _endpoint_seed(
            endpoint,
            live_config,
            score,
            audio_track=audio_track,
            playlist_uuid=playlist_uuid,
        )
        path = _endpoint_path(endpoint.route, seed)
        method = endpoint.allowed_methods[0]
        query = urllib.parse.urlencode(seed, doseq=True)
        if method == "GET" and query:
            path = f"{path}?{query}"
        status, payload, latency = request(
            live_config,
            path,
            method=method,
            payload=seed if method in {"POST", "PUT", "PATCH"} else None,
        )
        assert 200 <= status < 300, f"{endpoint.key} {method} {path}: {status} {payload}"
        exercised.add(endpoint.key)
        latencies.append(latency)
    assert exercised | simulated_for_safety == {item.key for item in endpoints}
    add_metric(live_metrics, "configured_endpoint_latency", latencies)


@pytest.mark.asyncio
async def test_panasonic_and_visca_move_acknowledge_and_restore(live_config, live_metrics) -> None:
    camera = PanasonicAwpService(live_config.integrations.panasonic)
    original_pan, original_tilt = await camera.query_pan_tilt_position()
    original_zoom = await camera.query_zoom_position()
    samples: list[float] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    target = ("127.0.0.1", live_config.integrations.visca.udp_port)
    try:
        started = time.perf_counter()
        sock.sendto(bytes.fromhex("8101060105050203FF"), target)
        responses = {sock.recvfrom(64)[0], sock.recvfrom(64)[0]}
        samples.append((time.perf_counter() - started) * 1000)
        assert bytes.fromhex("9041FF") in responses
        assert bytes.fromhex("9051FF") in responses
        await asyncio.sleep(0.35)
        sock.sendto(bytes.fromhex("8101060105050303FF"), target)
        sock.recvfrom(64)
        sock.recvfrom(64)
        moved_pan, moved_tilt = await camera.query_pan_tilt_position()
        assert abs(moved_pan - original_pan) + abs(moved_tilt - original_tilt) > 4

        sequence = bytes.fromhex("0000002A")
        packet = bytes.fromhex("01000006") + sequence + bytes.fromhex("8101043802FF")
        started = time.perf_counter()
        sock.sendto(packet, target)
        over_ip = {sock.recvfrom(64)[0], sock.recvfrom(64)[0]}
        samples.append((time.perf_counter() - started) * 1000)
        assert bytes.fromhex("01110003") + sequence + bytes.fromhex("9041FF") in over_ip
        assert bytes.fromhex("01110003") + sequence + bytes.fromhex("9051FF") in over_ip

        started = time.perf_counter()
        await camera.absolute_zoom(min(0xFFF, original_zoom + 24))
        await camera.focus_manual()
        await camera.focus(55)
        await asyncio.sleep(0.15)
        await camera.stop_focus()
        samples.append((time.perf_counter() - started) * 1000)
    finally:
        sock.close()
        await camera.stop_pan_tilt()
        await camera.stop_zoom()
        await camera.absolute_pan_tilt(original_pan, original_tilt)
        await camera.absolute_zoom(original_zoom)
        await camera.focus_auto()
    add_metric(live_metrics, "panasonic_visca_round_trip", samples)


def test_iac_midi_mapping_reaches_its_final_effect(live_config, live_metrics) -> None:
    import mido

    mapping = next(
        (
            item
            for item in live_config.integrations.midi.mappings
            if item.get("event_type") == "note_on" and item.get("actions")
        ),
        None,
    )
    assert mapping, "No configured note_on MIDI mapping is available"
    action = mapping["actions"][0]
    assert action["action_type"] == "propresenter.audio_trigger", "The live MIDI verifier currently requires an observable audio mapping"
    output_names = list(mido.get_output_names())
    wanted = live_config.integrations.midi.input_name.casefold()
    output_name = next((name for name in output_names if wanted in name.casefold() or name.casefold() in wanted), "")
    assert output_name, f"Configured IAC bus is not available as an output: {output_names}"
    message = mido.Message(
        "note_on",
        # Production Hub uses -1 as the configured wildcard channel. MIDI
        # messages themselves must still carry a concrete 0..15 channel.
        channel=max(0, int(mapping["channel"])),
        note=int(mapping["number"]),
        velocity=100,
    )
    started = time.perf_counter()
    with mido.open_output(output_name) as port:
        port.send(message)
    expected_track = str(action.get("params", {}).get("track", ""))
    deadline = time.monotonic() + 8
    observed = ""
    while time.monotonic() < deadline:
        status, payload, _ = request(live_config, "/audio/active")
        assert status == 200
        observed = str(payload)
        if expected_track.casefold() in observed.casefold():
            break
        time.sleep(0.1)
    observed_track = observed.removeprefix("item:").strip()
    assert expected_track and normalize_pad_key(expected_track) == normalize_pad_key(observed_track), (
        expected_track,
        observed,
    )
    add_metric(live_metrics, "midi_end_to_effect", [(time.perf_counter() - started) * 1000])
    request(live_config, "/audio/clear", method="POST", payload={})


def _wait_until(predicate, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def test_synthetic_ndi_vision_tracking_and_dropout_recovery(live_config, live_metrics, tmp_path: Path) -> None:
    broker = LatestFrameBroker()
    tracking_config = type(live_config.integrations.camera_tracking).from_dict(
        live_config.integrations.camera_tracking.to_dict()
    )
    tracking_config.enabled = True
    tracking_config.analyze_audience = True
    tracking_config.audience_region_filter_enabled = False
    tracking = PersonTrackingService(broker, tracking_config, initially_active=True)
    scenario_metrics: list[float] = []
    with NDISyntheticSender(output_dir=tmp_path) as sender:
        sender.send(fixture_frame(SINGLE_PERSON, width=640, height=360), fps=15)
        discovery: tuple[str, list[str]] | None = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and discovery is None:
            discovered = NativeNDI.shared().discover_sources(1200)
            matched = next(
                (item for item in discovered if item == SOURCE_NAME or item.endswith(f"({SOURCE_NAME})")),
                "",
            )
            if matched:
                discovery = matched, discovered
        assert discovery is not None, discovered
        matched_source, _ = discovery
        source = NDIVideoSource(
            broker,
            NDISourceSettings(
                matched_source,
                publish_fps=15,
                stale_after_seconds=live_config.integrations.video.stale_after_seconds,
            ),
        )
        source.start()
        source.set_output_active(True)
        tracking.start()
        try:
            for scenario, minimum_subjects in (("single-person", 1), ("multi-person", 2), ("crossing", 1), ("rapid-motion", 1)):
                analyzed_before = tracking.snapshot(VideoSourceKey.AUDIENCE).analyzed_frames
                worker = threading.Thread(target=stream_scenario, args=(sender, scenario, 3.0, 20), daemon=True)
                started = time.perf_counter()
                worker.start()
                snapshot = _wait_until(
                    lambda: (
                        current
                        if (current := tracking.snapshot(VideoSourceKey.AUDIENCE)).analyzed_frames > analyzed_before
                        and current.raw_candidates >= minimum_subjects
                        else None
                    ),
                    8,
                )
                analysis_age = snapshot.analysis_age_seconds if snapshot is not None else None
                worker.join(timeout=5)
                assert snapshot is not None, (scenario, tracking.snapshot(VideoSourceKey.AUDIENCE))
                assert analysis_age is None or analysis_age < 2.0
                scenario_metrics.append(snapshot.inference_ms)

            before = broker.snapshot(VideoSourceKey.AUDIENCE).received_frames
            worker = threading.Thread(target=stream_scenario, args=(sender, "dropout", 6.0, 20), daemon=True)
            worker.start()
            stale = _wait_until(
                lambda: broker.snapshot(VideoSourceKey.AUDIENCE)
                if broker.snapshot(VideoSourceKey.AUDIENCE).state == VideoSourceState.RECONNECTING
                else None,
                6,
            )
            worker.join(timeout=8)
            assert stale is not None
            recovery_worker = threading.Thread(target=stream_scenario, args=(sender, "single-person", 3.0, 20), daemon=True)
            recovery_started = time.perf_counter()
            recovery_worker.start()
            recovered = _wait_until(
                lambda: broker.snapshot(VideoSourceKey.AUDIENCE)
                if broker.snapshot(VideoSourceKey.AUDIENCE).received_frames > before
                and broker.snapshot(VideoSourceKey.AUDIENCE).state == VideoSourceState.RUNNING
                else None,
                8,
            )
            recovery_worker.join(timeout=5)
            assert recovered is not None
            assert recovered.dropped_frames == 0
            add_metric(
                live_metrics,
                "ndi_reconnect_duration",
                [(time.perf_counter() - recovery_started) * 1000],
            )
            add_metric(
                live_metrics,
                "ndi_effective_fps",
                [recovered.effective_fps],
                unit="fps",
                direction="higher_is_better",
            )
        finally:
            tracking.stop()
            source.stop()
    add_metric(live_metrics, "tracking_inference", scenario_metrics)


@pytest.mark.asyncio
async def test_calibrated_click_trajectory_moves_then_disarms_and_restores(live_config, live_metrics) -> None:
    camera = PanasonicAwpService(live_config.integrations.panasonic)
    original_pan, original_tilt = await camera.query_pan_tilt_position()
    original_zoom = await camera.query_zoom_position()
    positions: list[tuple[int, int]] = []
    settle_samples: list[float] = []
    try:
        status, payload, _ = request(live_config, "/api/camera/automation/configure", method="POST", payload={"mode": "click"})
        assert status == 200, payload
        ready = _wait_until(
            lambda: calibration
            if (
                (status_payload := request(live_config, "/api/camera/calibration"))[0] == 200
                and isinstance((calibration := status_payload[1]), dict)
                and calibration.get("motionSafe") is True
            )
            else None,
            45,
            interval=0.25,
        )
        assert ready is not None, request(live_config, "/api/camera/calibration")[1]
        for x in (0.30, 0.70):
            status, payload, _ = request(
                live_config,
                "/api/camera/automation/click-target",
                method="POST",
                payload={"x": x, "y": 0.55},
            )
            assert status == 200, payload
            status, payload, _ = request(live_config, "/api/camera/automation/arm", method="POST", payload={})
            assert status == 200, payload
            started = time.perf_counter()
            prior = positions[-1] if positions else (original_pan, original_tilt)
            position = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                candidate = await camera.query_pan_tilt_position()
                if abs(candidate[0] - prior[0]) + abs(candidate[1] - prior[1]) > 20:
                    position = candidate
                    break
                await asyncio.sleep(0.2)
            assert position is not None, (x, prior)
            positions.append(position)
            settle_samples.append((time.perf_counter() - started) * 1000)
            request(live_config, "/api/camera/automation/disarm", method="POST", payload={})
        assert abs(positions[0][0] - positions[1][0]) + abs(positions[0][1] - positions[1][1]) > 20
    finally:
        request(live_config, "/api/camera/automation/disarm", method="POST", payload={})
        await camera.absolute_pan_tilt(original_pan, original_tilt)
        await camera.absolute_zoom(original_zoom)
        await camera.focus_auto()
    add_metric(live_metrics, "ptz_calibrated_settle", settle_samples)
