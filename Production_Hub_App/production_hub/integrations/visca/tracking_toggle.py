from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from production_hub.integrations.panasonic_awp.models import PanasonicCommand


TRACKING_ACTIVITY_OWNER = "visca_autofocus_toggle"
ARM_RETRY_SECONDS = 6.0
ARM_RETRY_INTERVAL_SECONDS = 0.25


class ViscaAutofocusTrackingToggle:
    """Consume a Tenveo autofocus command as a Subject Tracking toggle."""

    def __init__(
        self,
        context: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self._clock = clock
        self._last_toggle_at = float("-inf")
        self._lock = asyncio.Lock()
        self._arm_task: asyncio.Task[None] | None = None

    async def consume(self, command: PanasonicCommand) -> bool:
        """Return True when an autofocus command was handled and consumed."""

        visca = self.context.config.integrations.visca
        if (
            not visca.autofocus_toggles_subject_tracking
            or command.command != "#D11"
        ):
            return False

        async with self._lock:
            now = self._clock()
            if now - self._last_toggle_at < visca.tracking_toggle_debounce_seconds:
                self._log(
                    "info",
                    "visca_tracking_toggle_debounced",
                    "Ignored a repeated VISCA autofocus packet",
                )
                return True
            self._last_toggle_at = now
            requested = (
                self.context.config.integrations.camera_tracking.automation.mode
                == "subject"
            )
            if requested:
                self._cancel_pending_arm()
                self._set_requested(False, "Tenveo Auto Focus button turned tracking off")
                self._log(
                    "info",
                    "visca_tracking_toggled",
                    "Subject Tracking turned off from the Tenveo Auto Focus button",
                    enabled=False,
                )
            else:
                try:
                    self.context.video.set_tracking_activity(
                        True,
                        owner=TRACKING_ACTIVITY_OWNER,
                    )
                    self._persist_mode(True)
                except Exception as exc:
                    self.context.video.set_tracking_activity(
                        False,
                        owner=TRACKING_ACTIVITY_OWNER,
                    )
                    self._log(
                        "warning",
                        "visca_tracking_toggle_failed",
                        "Subject Tracking could not be requested from VISCA",
                        error=str(exc),
                    )
                    return True
                self._cancel_pending_arm()
                self._arm_task = asyncio.create_task(self._arm_when_ready())
                self._log(
                    "info",
                    "visca_tracking_toggled",
                    "Subject Tracking requested from the Tenveo Auto Focus button",
                    enabled=True,
                )
        return True

    def shutdown(self) -> None:
        self._cancel_pending_arm()
        self.context.video.set_tracking_activity(
            False,
            owner=TRACKING_ACTIVITY_OWNER,
        )

    async def _arm_when_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + ARM_RETRY_SECONDS
        try:
            while (
                self.context.config.integrations.camera_tracking.automation.mode
                == "subject"
            ):
                ok, message = self.context.ptz_automation.arm()
                if ok:
                    # The automation service now owns analysis activity.
                    self.context.video.set_tracking_activity(
                        False,
                        owner=TRACKING_ACTIVITY_OWNER,
                    )
                    self._log(
                        "info",
                        "visca_tracking_armed",
                        "Subject Tracking started from the Tenveo Auto Focus button",
                    )
                    return
                if not _start_retryable(message):
                    self._set_requested(
                        False,
                        f"Tenveo tracking toggle could not start: {message}",
                    )
                    self._log(
                        "warning",
                        "visca_tracking_arm_failed",
                        "Subject Tracking could not start from VISCA",
                        error=message,
                    )
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    self._set_requested(
                        False,
                        "Tenveo tracking toggle timed out waiting for camera analysis",
                    )
                    self._log(
                        "warning",
                        "visca_tracking_arm_timed_out",
                        "Subject Tracking timed out waiting for fresh analysis",
                    )
                    return
                await asyncio.sleep(ARM_RETRY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    def _set_requested(self, enabled: bool, reason: str) -> None:
        if not enabled:
            self.context.ptz_automation.disarm(reason)
            self.context.video.set_tracking_activity(
                False,
                owner=TRACKING_ACTIVITY_OWNER,
            )
        self._persist_mode(enabled)

    def _persist_mode(self, enabled: bool) -> None:
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        config.enabled = True
        config.analyze_ptz = True
        config.analyze_audience = True
        config.automation.mode = "subject" if enabled else "off"
        config.automation.podium_zoom_enabled = True
        config.__post_init__()
        self.context.config.integrations.camera_tracking = config
        save = getattr(
            self.context.config_repository,
            "save_runtime_app_config",
            self.context.config_repository.save_app_config,
        )
        # This button is expected to be used frequently during a service, so
        # persist the mode atomically without creating a backup for every press.
        save(self.context.config)
        self.context.video.reconfigure_tracking(config)
        self.context.ptz_automation.reconfigure(config)

    def _cancel_pending_arm(self) -> None:
        task = self._arm_task
        self._arm_task = None
        if task is not None and not task.done():
            task.cancel()

    def _log(self, level: str, event: str, message: str, **metadata: object) -> None:
        callback = getattr(self.context.logger, level, None)
        if callback is not None:
            callback(event, message, **metadata)


def _start_retryable(message: str) -> bool:
    return any(
        text in str(message)
        for text in (
            "fresh analysis",
            "fresh frame",
            "Waiting for",
        )
    )
