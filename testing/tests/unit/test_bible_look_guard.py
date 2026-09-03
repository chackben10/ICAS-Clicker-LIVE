"""The Bible Look correction.

This is the mechanism, not a fallback. The operator does not press anything
first, and the Song Looks must keep their alternate theme because that is what
formats the lower thirds -- so the theme override cannot be removed and a verse
will briefly render with the song's theme. The requirement is that it becomes
correct within a second or two, every time.

"Every time" rests on two properties, and these tests pin both: two independent
detectors, so a silent signal cannot disable the correction; and confirmation
that the macro actually took effect, so a failed trigger is retried rather than
assumed.
"""

from __future__ import annotations

from production_hub.app.bible_look_guard import (
    BibleLookGuard,
    looks_like_bible_presentation,
    looks_like_scripture_cue,
)
from production_hub.integrations.propresenter.status_stream import ProPresenterStatusStream


BIBLE = {"presentation": {"groups": [{"name": "John 3:16"}]}}
SONG = {"presentation": {"groups": [{"name": "Verse 1"}, {"name": "Chorus"}]}}
VERSE_CUE = {"current": {"uuid": "cue-1", "text": "John 3:16\rFor God so loved the world"}}
LYRIC_CUE = {"current": {"uuid": "cue-2", "text": "Amazing grace how sweet the sound\rThat saved a wretch"}}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event, _message, **metadata):
        self.events.append((event, metadata))

    def warning(self, event, _message, **metadata):
        self.events.append((event, metadata))

    def error(self, event, _message, **metadata):
        self.events.append((event, metadata))

    def find(self, event: str) -> list[dict]:
        return [metadata for name, metadata in self.events if name == event]


class Harness:
    """A guard plus the scheduler the application wires to its event loop."""

    def __init__(self, **kwargs) -> None:
        self.fired = 0
        self.clock = Clock()
        self.pending: list[tuple[float, object]] = []
        self.logger = RecordingLogger()
        self.guard = BibleLookGuard(
            "Bible",
            self._fire,
            logger=self.logger,
            clock=self.clock,
            schedule=self._schedule,
            **kwargs,
        )

    def _fire(self) -> None:
        self.fired += 1

    def _schedule(self, delay: float, callback) -> None:
        self.pending.append((self.clock.now + delay, callback))

    def run_timers(self, until: float) -> None:
        """Advance the clock, running confirmations as they come due."""

        while True:
            due = sorted(item for item in self.pending if item[0] <= until)
            if not due:
                break
            when, callback = due[0]
            self.pending.remove((when, callback))
            self.clock.now = max(self.clock.now, when)
            callback()
        self.clock.now = max(self.clock.now, until)


class TestDetection:
    def test_a_single_colon_titled_group_is_a_bible_presentation(self) -> None:
        assert looks_like_bible_presentation(BIBLE) is True

    def test_a_song_is_not(self) -> None:
        assert looks_like_bible_presentation(SONG) is False

    def test_a_single_group_without_a_reference_is_not(self) -> None:
        assert looks_like_bible_presentation({"presentation": {"groups": [{"name": "Notices"}]}}) is False

    def test_malformed_payloads_are_not(self) -> None:
        for payload in (None, {}, {"presentation": None}, {"presentation": {"groups": None}}):
            assert looks_like_bible_presentation(payload) is False

    def test_a_verse_cue_is_recognised_by_its_reference_line(self) -> None:
        assert looks_like_scripture_cue(VERSE_CUE) is True

    def test_references_with_a_numbered_book_or_range_are_recognised(self) -> None:
        for reference in ("1 John 3:16", "John 3:16-18", "Song of Songs 2:1", "Genesis 1:1"):
            assert looks_like_scripture_cue({"current": {"text": f"{reference}\rbody"}}) is True, reference

    def test_a_malayalam_reference_is_recognised(self) -> None:
        assert looks_like_scripture_cue({"current": {"text": "യോഹന്നാൻ 3:16\rവാക്യം"}}) is True

    def test_a_song_lyric_is_not_a_scripture_cue(self) -> None:
        assert looks_like_scripture_cue(LYRIC_CUE) is False

    def test_a_lyric_that_merely_contains_a_colon_is_not(self) -> None:
        # Matching the whole first line, rather than searching for a colon, is
        # what keeps a lyric like this from switching the look mid-song.
        assert looks_like_scripture_cue({"current": {"text": "meet me at 3:16 tonight\rmore"}}) is False


class TestCorrection:
    """The correction applies immediately, then confirms it took effect.

    Immediacy is the requirement, not a detail. A verse's text styling is baked
    when it renders, so a Look applied afterwards fixes the background and
    leaves the text wrong -- which is the partial result seen in production.
    The only winning outcome is the Look landing before the render, so any
    delay before applying works against it.
    """

    def settle(self, harness: Harness) -> None:
        harness.run_timers(until=harness.clock.now + harness.guard.settle_seconds)

    def test_a_verse_under_a_song_look_is_corrected_immediately(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        assert harness.fired == 1, "the look must be applied without delay"

    def test_no_delay_is_configured_by_default(self) -> None:
        """A settle window would make stale text more likely, not less."""

        harness = Harness()
        assert harness.guard.settle_seconds == 0.0
        assert harness.guard.reassert_count == 0

    def test_the_cue_alone_is_enough(self) -> None:
        """If presentation/current never arrives, the cue text still fires it."""

        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("status/slide", VERSE_CUE)
        assert harness.fired == 1

    def test_the_presentation_alone_is_enough(self) -> None:
        """And if the cue text is unrecognisable, the presentation still fires."""

        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("status/slide", LYRIC_CUE)
        harness.guard.observe("presentation/current", BIBLE)
        assert harness.fired == 1

    def test_the_correct_look_is_left_alone(self) -> None:
        """Re-applying a Look that is already set repairs nothing.

        Stale text styling is baked into the rendered slide; the Look is
        already correct, so triggering the macro again just costs a request.
        """

        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Bible"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=10.0)
        assert harness.fired == 0

    def test_re_asserting_is_available_but_opt_in(self) -> None:
        harness = Harness(reassert_count=1)
        harness.guard.observe("look/current", {"id": {"name": "Bible"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=10.0)
        assert harness.fired == 1

    def test_a_song_is_never_corrected(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", SONG)
        harness.guard.observe("status/slide", LYRIC_CUE)
        harness.run_timers(until=10.0)
        assert harness.fired == 0

    def test_an_unobserved_look_is_never_guessed_at(self) -> None:
        harness = Harness()
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=5.0)
        assert harness.fired == 0
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.run_timers(until=harness.clock.now + 5.0)
        assert harness.fired >= 1

    def test_a_macro_that_did_not_take_is_retried_without_further_pushes(self) -> None:
        """The operator may leave one verse on screen; no more pushes arrive."""

        harness = Harness(retry_delay_seconds=1.5, max_attempts=4)
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        assert harness.fired == 1

        # ProPresenter never reports the Bible look, so confirmation refires.
        harness.run_timers(until=6.0)
        assert harness.fired > 1

    def test_applying_stops_once_the_look_is_right(self) -> None:
        harness = Harness(retry_delay_seconds=1.5)
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        assert harness.fired == 1

        harness.guard.observe("look/current", {"id": {"name": "Bible"}})
        harness.run_timers(until=30.0)
        assert harness.fired == 1

    def test_retrying_is_bounded(self) -> None:
        harness = Harness(retry_delay_seconds=1.0, max_attempts=3)
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=60.0)
        assert harness.fired == 3

    def test_repeated_pushes_do_not_fire_a_burst(self) -> None:
        harness = Harness(retry_delay_seconds=1.5)
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        for _ in range(20):
            harness.clock.now += 0.05
            harness.guard.observe("presentation/current", BIBLE)
        assert harness.fired <= 1

    def test_a_later_reading_is_corrected_again_immediately(self) -> None:
        """A budget spent on one reading must not disarm the next."""

        harness = Harness(retry_delay_seconds=1.0, max_attempts=2)
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=30.0)
        assert harness.fired == 2  # budget spent, gave up

        # The reading ends and a later one starts under the wrong look again.
        harness.guard.observe("presentation/current", SONG)
        harness.guard.observe("status/slide", LYRIC_CUE)
        harness.guard.observe("presentation/current", BIBLE)
        harness.run_timers(until=harness.clock.now + 5.0)
        assert harness.fired > 2

    def test_unrelated_pushes_are_ignored(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        assert harness.guard.observe("presentation/slide_index", {"presentation_index": {"index": 2}}) is False


def test_presentation_current_is_subscribed_but_never_cached_as_active() -> None:
    """The guard needs these pushes; the active_presentation cache must not.

    Caching presentation/current as active_presentation once let a library
    selection satisfy the propresenter.active_presentation condition and misfire
    Auto Show mid-service. It answers a different question.
    """

    assert "presentation/current" in ProPresenterStatusStream.PATHS
    assert "status/slide" in ProPresenterStatusStream.PATHS

    from production_hub.core.config.models import ProPresenterConfig
    from production_hub.integrations.propresenter.service import ProPresenterService

    service = ProPresenterService(ProPresenterConfig(host="127.0.0.1", port=1))
    stream = service.start_status_stream()
    try:
        stream.callback("presentation/current", BIBLE)
        assert service._cached_monitor_value("active_presentation") is None
    finally:
        service.stop_status_stream()


def test_bible_endpoint_triggers_the_configured_macro() -> None:
    """The manual override remains available, but nothing depends on it."""

    import os
    from tempfile import TemporaryDirectory
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from production_hub.api.server import create_app
    from production_hub.app.bootstrap import build_context

    with TemporaryDirectory() as directory:
        os.environ["PRODUCTION_HUB_DATA_ROOT"] = directory
        context = build_context()
        context.propresenter.client.trigger_macro = AsyncMock(return_value=True)
        client = TestClient(create_app(context))

        response = client.post("/bible")

        assert response.status_code == 200, response.text
        context.propresenter.client.trigger_macro.assert_awaited_once_with(
            context.config.integrations.propresenter.bible_macro_trigger_uuid
        )


def test_the_bible_pad_does_not_collide_with_an_audio_pad() -> None:
    from production_hub.core.config.defaults import (
        BIBLE_MODE_PAD_NOTE,
        build_default_midi_mappings,
    )

    mappings = build_default_midi_mappings()
    bible = [item for item in mappings if item.number == BIBLE_MODE_PAD_NOTE]
    assert len(bible) == 1
    assert bible[0].action.action_type == "propresenter.trigger_macro"
    assert len({item.number for item in mappings}) == len(mappings)


class TestDetectionLead:
    """Whether the Look can be set before the verse renders.

    Text styling is baked at render time, so this gap is the whole question. If
    ProPresenter names the Bible presentation before the first verse cue, there
    is a window to set the Look in. If not, no post-hoc correction can un-bake
    the text, and the answer has to come from ProPresenter's configuration.
    """

    def test_a_presentation_seen_before_the_first_cue_is_preemptable(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.clock.now += 0.4
        harness.guard.observe("status/slide", VERSE_CUE)

        lead = harness.logger.find("bible_detection_lead")
        assert len(lead) == 1
        assert lead[0]["lead_ms"] == 400.0
        assert lead[0]["preemptable"] is True

    def test_a_cue_arriving_first_is_not_preemptable(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("status/slide", VERSE_CUE)
        harness.clock.now += 0.2
        harness.guard.observe("presentation/current", BIBLE)

        lead = harness.logger.find("bible_detection_lead")
        assert len(lead) == 1
        assert lead[0]["lead_ms"] == -200.0
        assert lead[0]["preemptable"] is False

    def test_the_lead_is_reported_once_per_reading(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.guard.observe("status/slide", VERSE_CUE)
        for _ in range(5):
            harness.clock.now += 0.5
            harness.guard.observe("status/slide", VERSE_CUE)
        assert len(harness.logger.find("bible_detection_lead")) == 1

    def test_a_later_reading_is_measured_again(self) -> None:
        harness = Harness()
        harness.guard.observe("look/current", {"id": {"name": "Song"}})
        harness.guard.observe("presentation/current", BIBLE)
        harness.guard.observe("status/slide", VERSE_CUE)

        # The reading ends, which resets the measurement.
        harness.guard.observe("presentation/current", SONG)
        harness.guard.observe("status/slide", LYRIC_CUE)

        harness.clock.now += 30.0
        harness.guard.observe("presentation/current", BIBLE)
        harness.clock.now += 0.25
        harness.guard.observe("status/slide", VERSE_CUE)
        assert len(harness.logger.find("bible_detection_lead")) == 2
