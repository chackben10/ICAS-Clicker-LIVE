from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from typing import Any


class ProPresenterBibleModeObserver:
    """Observe only ProPresenter's Bible-mode control through macOS AX.

    ProPresenter 21 does not publish an API event when the operator enters the
    Bible tab. Its API events arrive after the cue has rendered, and switching
    Looks at that point clears a one-cue dynamic Bible presentation. A native
    AX value-change notification lets Production Hub apply the Look before the
    operator chooses a verse without installing a global mouse listener.
    """

    RETRY_SECONDS = 5.0
    SEMANTIC_POLL_SECONDS = 0.5
    UNAVAILABLE_LOG_SECONDS = 60.0

    def __init__(self, callback: Callable[[bool], None], logger: Any) -> None:
        self.callback = callback
        self.logger = logger
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_selected = False
        self._last_error = ""
        self._last_unavailable_log = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-bible-mode",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        self._thread = None

    @staticmethod
    def _attribute_result(api: Any, element: Any, name: str) -> tuple[int, Any]:
        error, value = api.AXUIElementCopyAttributeValue(element, name, None)
        return int(error), value

    @classmethod
    def _attribute(cls, api: Any, element: Any, name: str) -> Any:
        error, value = cls._attribute_result(api, element, name)
        return value if error == 0 else None

    @classmethod
    def _find_propresenter_pid(cls, quartz: Any) -> int:
        options = (
            quartz.kCGWindowListOptionOnScreenOnly
            | quartz.kCGWindowListExcludeDesktopElements
        )
        windows = quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID)
        return next(
            (
                int(window.get(quartz.kCGWindowOwnerPID, 0))
                for window in windows
                if str(window.get(quartz.kCGWindowOwnerName, "")) == "ProPresenter"
                and int(window.get(quartz.kCGWindowLayer, 0)) == 0
            ),
            0,
        )

    @classmethod
    def _find_bible_radio(cls, api: Any, application: Any) -> Any | None:
        windows = cls._attribute(api, application, api.kAXWindowsAttribute) or []
        main_window = None
        for window in windows:
            identifier = str(
                cls._attribute(api, window, api.kAXIdentifierAttribute) or ""
            )
            title = str(cls._attribute(api, window, api.kAXTitleAttribute) or "")
            if identifier == "ProPresenterMainWindow.Ready" or title == "ProPresenter":
                main_window = window
                break
        if main_window is None:
            return None

        # The toolbar is a direct child of the main window. Restricting the
        # search to that subtree avoids enumerating large song libraries.
        window_children = cls._attribute(api, main_window, api.kAXChildrenAttribute) or []
        toolbar = next(
            (
                child
                for child in window_children
                if cls._attribute(api, child, api.kAXRoleAttribute) == api.kAXToolbarRole
            ),
            None,
        )
        if toolbar is None:
            return None

        pending = [toolbar]
        inspected = 0
        while pending and inspected < 80:
            element = pending.pop()
            inspected += 1
            role = cls._attribute(api, element, api.kAXRoleAttribute)
            if role == api.kAXRadioButtonRole:
                description = str(
                    cls._attribute(api, element, api.kAXDescriptionAttribute) or ""
                )
                help_text = str(
                    cls._attribute(api, element, api.kAXHelpAttribute) or ""
                )
                if description == "Bible" or "from the Bible" in help_text:
                    return element
            children = cls._attribute(api, element, api.kAXChildrenAttribute) or []
            pending.extend(children)
        return None

    def _publish_value(self, api: Any, element: Any) -> bool:
        error, value = self._attribute_result(
            api,
            element,
            api.kAXValueAttribute,
        )
        if error != 0:
            return False
        selected = bool(value)
        if selected == self._last_selected:
            return True
        self._last_selected = selected
        self.callback(selected)
        return True

    def _refresh_and_publish(self, api: Any, application: Any) -> Any | None:
        """Resolve ProPresenter's current toolbar radio before reading it."""

        bible_radio = self._find_bible_radio(api, application)
        if bible_radio is None or not self._publish_value(api, bible_radio):
            return None
        return bible_radio

    @staticmethod
    def _native_callback(objc: Any, api: Any, callback: Callable[..., None]) -> Any:
        """Give PyObjC the retained C closure required by AXObserverCreate."""

        return objc.callbackFor(api.AXObserverCreate)(callback)

    @staticmethod
    def _notification_is_unsupported(api: Any, error: Any) -> bool:
        return int(error) == int(
            getattr(api, "kAXErrorNotificationUnsupported", -25207)
        )

    def _warn_unavailable(self, message: str, *, error: str = "") -> None:
        now = time.monotonic()
        signature = f"{message}\0{error}"
        if (
            signature == self._last_error
            and now - self._last_unavailable_log < self.UNAVAILABLE_LOG_SECONDS
        ):
            return
        self._last_error = signature
        self._last_unavailable_log = now
        self.logger.warning(
            "bible_mode_observer_unavailable",
            message,
            error=error,
        )

    def _run(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            import ApplicationServices as api
            import Quartz
            import objc
        except Exception as exc:
            self._warn_unavailable(
                "The macOS Bible-mode observer is unavailable",
                error=str(exc),
            )
            return

        while not self._stop_event.is_set():
            if not bool(api.AXIsProcessTrusted()):
                self._warn_unavailable(
                    "Grant Production Hub Accessibility access so the Bible Look can be applied before the first verse"
                )
                self._stop_event.wait(self.RETRY_SECONDS)
                continue

            pid = self._find_propresenter_pid(Quartz)
            if pid <= 0:
                self._warn_unavailable("ProPresenter is not available for Bible-mode observation")
                self._stop_event.wait(self.RETRY_SECONDS)
                continue

            try:
                application = api.AXUIElementCreateApplication(pid)
                bible_radio = self._find_bible_radio(api, application)
                if bible_radio is None:
                    self._warn_unavailable(
                        "ProPresenter's Bible-mode control is not currently available"
                    )
                    self._stop_event.wait(self.RETRY_SECONDS)
                    continue

                def observer_callback(_observer, element, _notification, _context):
                    self._publish_value(api, element)

                native_callback = self._native_callback(objc, api, observer_callback)
                error, observer = api.AXObserverCreate(pid, native_callback, None)
                if int(error) != 0 or observer is None:
                    raise RuntimeError(f"AXObserverCreate returned {error}")
                error = api.AXObserverAddNotification(
                    observer,
                    bible_radio,
                    api.kAXValueChangedNotification,
                    None,
                )
                use_semantic_poll = self._notification_is_unsupported(api, error)
                if int(error) != 0 and not use_semantic_poll:
                    raise RuntimeError(f"AXObserverAddNotification returned {error}")

                source = None
                run_loop = None
                if not use_semantic_poll:
                    source = api.AXObserverGetRunLoopSource(observer)
                    run_loop = api.CFRunLoopGetCurrent()
                    api.CFRunLoopAddSource(run_loop, source, api.kCFRunLoopCommonModes)
                self._last_error = ""
                self.logger.info(
                    "bible_mode_observer_started",
                    "Watching ProPresenter's Bible mode without a global mouse listener",
                    pid=pid,
                    mode=("semantic_value_poll" if use_semantic_poll else "native_notification"),
                    interval_seconds=(
                        self.SEMANTIC_POLL_SECONDS if use_semantic_poll else None
                    ),
                )
                # Publish the current value so a restored Bible tab is repaired
                # immediately after either app restarts.
                self._last_selected = False
                self._publish_value(api, bible_radio)

                next_pid_check = time.monotonic() + self.RETRY_SECONDS
                while not self._stop_event.is_set():
                    if use_semantic_poll:
                        # ProPresenter 21 exposes the Bible radio's AXValue but
                        # rejects kAXValueChangedNotification and silently
                        # replaces that custom control on mode changes. Resolve
                        # it again from the small toolbar subtree before each
                        # read; no presentation or media library is traversed.
                        if self._stop_event.wait(self.SEMANTIC_POLL_SECONDS):
                            break
                        replacement = self._refresh_and_publish(api, application)
                        if replacement is None:
                            raise RuntimeError(
                                "ProPresenter replaced the Bible-mode control"
                            )
                        bible_radio = replacement
                    else:
                        api.CFRunLoopRunInMode(
                            api.kCFRunLoopDefaultMode,
                            1.0,
                            False,
                        )
                    now = time.monotonic()
                    if now < next_pid_check:
                        continue
                    next_pid_check = now + self.RETRY_SECONDS
                    if self._find_propresenter_pid(Quartz) != pid:
                        break

                if not use_semantic_poll:
                    api.AXObserverRemoveNotification(
                        observer,
                        bible_radio,
                        api.kAXValueChangedNotification,
                    )
                    api.CFRunLoopRemoveSource(
                        run_loop,
                        source,
                        api.kCFRunLoopCommonModes,
                    )
            except Exception as exc:
                self._warn_unavailable(
                    "The ProPresenter Bible-mode observer disconnected; retrying",
                    error=str(exc),
                )
                self._stop_event.wait(self.RETRY_SECONDS)


class BibleCueCorrector:
    """Apply the Bible macro on Bible-mode entry, before a verse can render."""

    def __init__(
        self,
        propresenter: Any,
        logger: Any,
        *,
        observer_factory: Callable[[Callable[[bool], None], Any], Any]
        | None = None,
    ) -> None:
        self.propresenter = propresenter
        self.logger = logger
        self._current_look = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: asyncio.Task[None] | None = None
        factory = observer_factory or ProPresenterBibleModeObserver
        self._observer = factory(self._mode_value_changed, logger)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._observer.start()

    def observe(self, path: str, data: Any) -> None:
        if path != "look/current":
            return
        look_id = data.get("id") if isinstance(data, dict) else None
        self._current_look = str(
            look_id.get("name") if isinstance(look_id, dict) else ""
        ).strip()

    def _mode_value_changed(self, selected: bool) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._schedule_mode_change, bool(selected))

    def _schedule_mode_change(self, selected: bool) -> None:
        if not selected:
            return
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._apply_bible_macro())

    async def _apply_bible_macro(self) -> None:
        try:
            desired_look = str(
                getattr(self.propresenter.config, "bible_look_name", "") or ""
            ).strip()
            # ProPresenter 21 can leave the official aggregate Look stream one
            # transition behind. Verify once through the authoritative endpoint
            # on each Bible-mode entry instead of trusting that cache.
            self._current_look = await self.propresenter.current_look_name()
            if desired_look and self._current_look.casefold() == desired_look.casefold():
                return
            triggered = await self.propresenter.trigger_macro(
                self.propresenter.config.bible_macro_trigger_uuid
            )
            if not triggered:
                raise RuntimeError("ProPresenter rejected the Bible macro")
            self.logger.info(
                "bible_look_prepared",
                "Applied the Bible Look when ProPresenter entered Bible mode",
                look=desired_look,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "bible_look_prepare_failed",
                "The Bible Look could not be prepared before verse selection",
                error=str(exc),
            )

    def stop(self) -> None:
        self._observer.stop()
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
