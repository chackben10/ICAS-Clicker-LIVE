"""Set the Bible Look as early as ProPresenter allows.

The operator will not press a button first -- that was tried and abandoned --
so this is the mechanism, not insurance behind a manual action.

Scope, established by measuring the live system rather than reasoning about it:
this guard fixes the *Look*, and that is all it can fix. The verse reference
going missing is a different fault with a different cause. The song themes'
Fullscreen slides have no `REFERENCE` element while the Bible theme's does, so a
verse generated under a Song Look has nowhere to render its reference. That is
ProPresenter configuration; adding the element to the song themes fixes it, and
no amount of Look correction will. See docs/PRESS_TO_CUE.md.

Nor can Production Hub force a re-render to repair one after the fact: a dynamic
Bible cue is not addressable through the API. `presentation/slide_index` reports
a null index, `presentation/active` carries no UUID, and there is no cue-trigger
route -- `/presentation/active/0/trigger` hits the underlying song instead.

What remains worth doing is getting the Bible Look set promptly and reliably:

* Two independent detectors. `presentation/current` names the dynamic Bible
  presentation; `status/slide` carries the cue text, whose first line is the
  scripture reference. Either alone fires it, so a signal ProPresenter does not
  publish cannot disable the correction.
* It confirms. A trigger is not assumed to have taken: the guard re-checks and
  fires again until `look/current` reports the Bible look, on a timer, so a
  verse left on screen with no further pushes is still handled.
* `bible_detection_lead` is logged once per reading, recording whether
  ProPresenter named the Bible presentation before the first verse cue arrived.

It stays push-driven and idle-free, and it is not an observer of ProPresenter's
UI. The deleted Accessibility poller re-walked the toolbar subtree twice a
second, and every one of those reads was a synchronous call served by the same
main thread that renders cues.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable


# Apply the Look with no delay.
#
# The observed partial failure is: background and layout correct, verse *text*
# still carrying the song's styling, and it stays that way. That means the Look
# did apply -- the background proves it -- and the text was already rendered
# with its styling baked in. Applying the Look again cannot re-flow text that
# has already been composited.
#
# So the only way to win is for the Look to be set BEFORE the verse renders.
# Every millisecond of delay here makes that less likely, which is why this is
# zero and not a settle window.
DEFAULT_SETTLE_SECONDS = 0.0
# How long to wait before confirming the look actually changed.
DEFAULT_RETRY_DELAY_SECONDS = 1.5
# Attempts per episode. A macro that has not taken after this many tries is not
# going to, and repeating forever would fight the operator.
DEFAULT_MAX_ATTEMPTS = 4
# Re-applications after look/current already reports the Bible look. Off by
# default: the evidence says a partial result is stale baked text, not a partly
# applied Look, so re-applying costs a trigger and repairs nothing. Kept
# configurable because it is the cheap thing to try if that ever changes.
DEFAULT_REASSERT_COUNT = 0

# A cue whose entire first line is a scripture reference: "John 3:16",
# "1 John 3:16-18", "യോഹന്നാൻ 3:16". Matching the whole line rather than
# searching for a colon keeps song lyrics out -- a lyric line is not, in its
# entirety, one-to-three words followed by chapter:verse.
_REFERENCE_LINE = re.compile(
    r"^\s*\S+(?:[  ]+\S+){0,2}[  ]+\d+\s*[:.]\s*\d+(?:\s*[-–]\s*\d+)?\s*$"
)


def looks_like_bible_presentation(payload: Any) -> bool:
    """A dynamic Bible presentation: one group, titled like a reference.

    Matches the `bible_look_enforcement` automation's condition, so both agree
    on what "Bible" means.
    """

    if not isinstance(payload, dict):
        return False
    presentation = payload.get("presentation")
    if not isinstance(presentation, dict):
        return False
    groups = presentation.get("groups")
    if not isinstance(groups, list) or len(groups) != 1:
        return False
    first = groups[0]
    name = str(first.get("name") or "") if isinstance(first, dict) else ""
    return ":" in name


def cue_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("current", "slide"):
        current = payload.get(key)
        if isinstance(current, dict) and current.get("text"):
            return str(current.get("text") or "")
    return str(payload.get("text") or "")


def looks_like_scripture_cue(payload: Any) -> bool:
    """True when the rendered cue's first line is a scripture reference."""

    text = cue_text(payload)
    if not text:
        return False
    # ProPresenter separates the reference from the verse body with \r.
    first_line = re.split(r"[\r\n]", text, maxsplit=1)[0]
    return bool(_REFERENCE_LINE.match(first_line))


class BibleLookGuard:
    def __init__(
        self,
        bible_look_name: str,
        fire: Callable[[], None],
        *,
        logger: Any | None = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        reassert_count: int = DEFAULT_REASSERT_COUNT,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bible_look_name = str(bible_look_name or "").strip()
        self.fire = fire
        self.logger = logger
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.reassert_count = max(0, int(reassert_count))
        # Called as schedule(delay_seconds, callback) so the guard can confirm
        # the correction took even when no further pushes arrive -- the operator
        # may leave one verse on screen.
        self.schedule = schedule
        self.clock = clock
        self._lock = threading.Lock()
        self._current_look = ""
        self._look_known = False
        self._bible_presentation = False
        self._scripture_cue = False
        self._attempts = 0
        self._reasserts = 0
        self._last_fired_at: float | None = None
        self._settling = False
        self._gave_up = False
        self._last_reference = ""
        # The cue currently on screen. A push repeating it is not a slide
        # change and must not prompt another correction.
        self._last_cue_signature = ""
        # Set once the Bible look has actually been observed during this
        # reading. After that the operator owns the look.
        self._look_was_correct = False
        # When each detector first saw this reading. The gap between them says
        # whether the Look can be set before the verse renders, which is the
        # only way stale baked text is avoidable. Logged once per reading.
        self._presentation_seen_at: float | None = None
        self._cue_seen_at: float | None = None
        self._lead_logged = False

    # -- observation ------------------------------------------------------

    def observe(self, path: str, data: Any) -> bool:
        """Handle one status push. Returns True when the macro was fired.

        Only a change of slide prompts a check. A look change records the new
        look and stops there: an operator switching looks while a verse is on
        screen is a deliberate act, and correcting it would be Production Hub
        overruling the person running the service.
        """

        content_changed = False
        with self._lock:
            if path == "look/current":
                if not isinstance(data, dict):
                    return False
                identifier = data.get("id")
                self._current_look = (
                    str(identifier.get("name") or "").strip() if isinstance(identifier, dict) else ""
                )
                # The first sighting of the look is initial state, not an
                # operator action, so it may prompt a check. Every later look
                # change is a deliberate choice and must not.
                content_changed = not self._look_known
                self._look_known = True
                if self._current_look == self.bible_look_name:
                    self._look_was_correct = True
            elif path == "presentation/current":
                was = self._bible_presentation
                self._bible_presentation = looks_like_bible_presentation(data)
                content_changed = self._bible_presentation and not was
                if self._bible_presentation and self._presentation_seen_at is None:
                    self._presentation_seen_at = self.clock()
            elif path == "status/slide":
                self._scripture_cue = looks_like_scripture_cue(data)
                current = data.get("current") if isinstance(data, dict) else None
                signature = str((current or {}).get("uuid") or "") if isinstance(current, dict) else ""
                if signature and signature != self._last_cue_signature:
                    self._last_cue_signature = signature
                    content_changed = True
                if self._scripture_cue:
                    self._last_reference = re.split(r"[\r\n]", cue_text(data), maxsplit=1)[0].strip()
                    if self._cue_seen_at is None:
                        self._cue_seen_at = self.clock()
            else:
                return False
        self._log_detection_lead()
        if not content_changed:
            return False
        return self._evaluate()

    def verify(self) -> bool:
        """Re-check after a correction, and fire again if it did not take."""

        return self._evaluate()

    @property
    def bible_active(self) -> bool:
        with self._lock:
            return self._bible_active_locked()

    def _bible_active_locked(self) -> bool:
        return self._bible_presentation or self._scripture_cue

    # -- correction -------------------------------------------------------

    def _log_detection_lead(self) -> None:
        """Record, once per reading, which detector saw the Bible first.

        This is the measurement that says whether the problem is solvable at
        all. A verse's text styling is baked when it renders, so the Look has
        to be set before that. If ProPresenter names the Bible presentation
        before the first verse cue arrives, there is a window to set it in. If
        the cue comes first, no post-hoc correction can un-bake the text and
        the fix has to come from ProPresenter's configuration instead.
        """

        with self._lock:
            if (
                self._lead_logged
                or self._presentation_seen_at is None
                or self._cue_seen_at is None
            ):
                return
            self._lead_logged = True
            lead_ms = round((self._cue_seen_at - self._presentation_seen_at) * 1000, 1)
        if self.logger is None:
            return
        self.logger.info(
            "bible_detection_lead",
            "Time between ProPresenter naming the Bible presentation and the first verse cue",
            lead_ms=lead_ms,
            preemptable=lead_ms > 0,
        )

    def _evaluate(self) -> bool:
        """Decide whether to apply the Bible look now, and do it.

        Applied immediately on detection: the sooner the Look is set, the more
        likely it lands before the verse renders, which is the only outcome
        that avoids stale text styling.
        """

        action = ""
        attempt = 0
        current_look = ""
        reference = ""
        with self._lock:
            if not self._bible_active_locked():
                # The reading ended; the next one starts fresh.
                self._look_was_correct = False
                self._last_cue_signature = ""
                self._reset_episode_locked()
                return False
            # Until a look/current push has arrived the current look is unknown
            # rather than wrong, and firing on that guess would be a misfire.
            if not self._look_known or not self.bible_look_name:
                return False
            if self._gave_up:
                return False

            now = self.clock()
            correct = self._current_look == self.bible_look_name
            if correct:
                self._look_was_correct = True
            if correct and self._reasserts >= self.reassert_count:
                return False
            if self._look_was_correct and not correct:
                # The macro already took for this reading and the operator has
                # since chosen a different look. That is their call to make.
                return False
            if self._last_fired_at is None:
                # settle_seconds is 0 by default and should stay there; it
                # exists only so the delay can be reintroduced from config if
                # the production machine ever wants it.
                if not self._settling and self.settle_seconds > 0:
                    self._settling = True
                    if self.schedule is not None:
                        self.schedule(self.settle_seconds, self.verify)
                        return False
                    # With no scheduler the settle cannot be awaited; apply now
                    # rather than never.
            elif now - self._last_fired_at < self.retry_delay_seconds:
                return False
            elif not correct and self._attempts >= self.max_attempts:
                self._gave_up = True
                if self.logger is not None:
                    self.logger.warning(
                        "bible_look_correction_exhausted",
                        "Bible macro did not take effect; giving up until the look or presentation changes",
                        attempts=self._attempts,
                        current_look=self._current_look,
                    )
                return False

            self._settling = False
            self._last_fired_at = now
            if correct:
                self._reasserts += 1
                action = "reassert"
            else:
                self._attempts += 1
                action = "apply"
            attempt = self._attempts
            current_look = self._current_look
            reference = self._last_reference

        if self.logger is not None:
            self.logger.info(
                "bible_look_corrected",
                (
                    "Re-applying the Bible look to settle the rendered verse"
                    if action == "reassert"
                    else "Bible presentation is live without the Bible look; triggering the Bible macro"
                ),
                action=action,
                current_look=current_look,
                attempt=attempt,
                reference=reference,
            )
        self.fire()
        # Confirm rather than assume, so a trigger that never took effect is
        # retried instead of silently trusted.
        if self.schedule is not None:
            self.schedule(self.retry_delay_seconds, self.verify)
        return True

    def _reset_episode_locked(self) -> None:
        self._attempts = 0
        self._reasserts = 0
        self._last_fired_at = None
        self._settling = False
        self._gave_up = False
        self._presentation_seen_at = None
        self._cue_seen_at = None
        self._lead_logged = False
