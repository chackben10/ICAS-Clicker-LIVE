"""Press-to-cue measurement, the number every other phase is judged against."""

from __future__ import annotations

import time

import pytest

from production_hub.core.metrics.press_to_cue import PressToCueRecorder
from production_hub.integrations.propresenter.service import ProPresenterService
from production_hub.core.config.models import ProPresenterConfig


def test_latency_spans_the_press_not_the_http_call() -> None:
    recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
    recorder.record_press("next", pressed_at=100.0)
    sample = recorder.record_cue("slide:abc:3", observed_at=100.4)

    assert sample is not None
    # 400 ms: dispatch plus HTTP plus ProPresenter's own processing. A client
    # that answered in 11 ms would still report 400 ms here, which is the point.
    assert sample.latency_ms == pytest.approx(400.0)
    assert sample.direction == "next"


def test_presses_match_cues_in_order() -> None:
    recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
    recorder.record_press("next", pressed_at=10.0)
    recorder.record_press("next", pressed_at=11.0)

    first = recorder.record_cue("slide:a:1", observed_at=10.5)
    second = recorder.record_cue("slide:a:2", observed_at=11.25)

    assert first is not None and second is not None
    assert first.latency_ms == pytest.approx(500.0)
    assert second.latency_ms == pytest.approx(250.0)


def test_one_cue_arriving_by_both_routes_consumes_one_press() -> None:
    """slide_index and status/slide describe the same change, milliseconds apart."""

    recorder = PressToCueRecorder()
    recorder.record_press("next", pressed_at=1.0)
    recorder.record_press("next", pressed_at=5.0)

    matched = recorder.record_cue("slide:a:1", observed_at=1.2)
    duplicate = recorder.record_cue("cue:same-slide", observed_at=1.23)

    assert matched is not None
    assert duplicate is None
    assert recorder.summary()["pending"] == 1


def test_a_cue_nobody_pressed_for_is_not_attributed_to_a_press() -> None:
    recorder = PressToCueRecorder()
    assert recorder.record_cue("slide:a:1", observed_at=3.0) is None
    assert recorder.summary()["unsolicited_cues"] == 1
    assert recorder.summary()["samples"] == 0


def test_a_press_that_produced_no_cue_is_recorded_as_unmatched() -> None:
    recorder = PressToCueRecorder(match_timeout_seconds=2.0, min_cue_interval_seconds=0.0)
    recorder.record_press("previous", pressed_at=0.0)

    # Far past the timeout: this press did not cause the cue that just arrived.
    assert recorder.record_cue("slide:a:9", observed_at=30.0) is None
    summary = recorder.summary()
    assert summary["unmatched"] == 1
    assert summary["samples"] == 0


def test_summary_reports_the_distribution_not_an_average() -> None:
    recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
    for index, latency in enumerate([0.05, 0.06, 0.07, 0.08, 3.0]):
        recorder.record_press("next", pressed_at=float(index))
        recorder.record_cue(f"slide:a:{index}", observed_at=float(index) + latency)

    summary = recorder.summary()
    assert summary["samples"] == 5
    # The single 3 s stall is exactly what an average would hide.
    assert summary["max_ms"] == pytest.approx(3000.0)
    assert summary["p95_ms"] == pytest.approx(3000.0)
    assert summary["p50_ms"] == pytest.approx(70.0)


class TestCueSignature:
    """The stream must identify normal slides and dynamic Bible verses alike."""

    def signature(self, path: str, data: dict) -> str:
        return ProPresenterService._cue_signature(path, data)

    def test_slide_index_push_identifies_the_slide(self) -> None:
        assert self.signature(
            "presentation/slide_index",
            {"presentation_index": {"presentation_id": {"uuid": "abc"}, "index": 4}},
        ) == "slide:abc:4"

    def test_bible_verse_is_identified_by_cue_uuid(self) -> None:
        # Dynamic Bible presentations never report a presentation index, so the
        # cue UUID from status/slide is the only signal a verse changed.
        assert self.signature("status/slide", {"current": {"uuid": "verse-2"}}) == "cue:verse-2"

    def test_a_push_without_a_slide_is_not_a_cue(self) -> None:
        assert self.signature("presentation/slide_index", {"presentation_index": {}}) == ""
        assert self.signature("look/current", {"id": {"name": "Bible"}}) == ""


@pytest.mark.asyncio
async def test_status_stream_push_stops_the_clock() -> None:
    """End to end: a press, then a stream push, produces one sample."""

    service = ProPresenterService(ProPresenterConfig(host="127.0.0.1", port=1))
    recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
    service.press_to_cue = recorder

    captured: list[tuple[str, object]] = []
    service._status_stream = None
    stream = service.start_status_stream(lambda path, data: captured.append((path, data)))
    try:
        # The real recorder clock, since the stream calls record_cue itself.
        recorder.record_press("next", pressed_at=time.monotonic())
        # Drive the stream's own ingest callback, as a real push would.
        stream.callback(
            "presentation/slide_index",
            {"presentation_index": {"presentation_id": {"uuid": "abc"}, "index": 1}},
        )
        assert recorder.summary()["samples"] == 1
        assert captured  # the caller's callback still runs
    finally:
        service.stop_status_stream()


class TestRerenderIndexRecovery:
    """A dynamic Bible presentation reports no slide index, so the index of the
    live verse is recovered by matching its text against the passage. A wrong
    match would jump the reading, so an ambiguous one does nothing."""

    def service(self):
        from production_hub.core.config.models import ProPresenterConfig
        from production_hub.integrations.propresenter.service import ProPresenterService
        return ProPresenterService(ProPresenterConfig(host="127.0.0.1", port=1))

    def passage(self, *texts):
        return {"presentation": {"groups": [{"slides": [{"text": t} for t in texts]}]}}

    @pytest.mark.asyncio
    async def test_it_retriggers_the_verse_that_is_live(self) -> None:
        from unittest.mock import AsyncMock
        service = self.service()
        service.client.get_json = AsyncMock(side_effect=[
            {"current": {"text": "Mark 3:2\rSome of them were looking"}},
            self.passage("Mark 3:1\rAnother time", "Mark 3:2\rSome of them were looking", "Mark 3:3\rJesus said"),
        ])
        service.client.trigger = AsyncMock(return_value=True)

        assert await service.rerender_active_slide() == ""
        service.client.trigger.assert_awaited_once_with("/presentation/active/1/trigger")

    @pytest.mark.asyncio
    async def test_an_ambiguous_match_refuses_rather_than_guessing(self) -> None:
        from unittest.mock import AsyncMock
        service = self.service()
        service.client.get_json = AsyncMock(side_effect=[
            {"current": {"text": "repeated"}},
            self.passage("repeated", "other", "repeated"),
        ])
        service.client.trigger = AsyncMock()

        reason = await service.rerender_active_slide()
        assert "refusing to guess" in reason
        service.client.trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_match_does_nothing(self) -> None:
        from unittest.mock import AsyncMock
        service = self.service()
        service.client.get_json = AsyncMock(side_effect=[
            {"current": {"text": "a verse"}},
            self.passage("something else"),
        ])
        service.client.trigger = AsyncMock()

        assert "not found" in await service.rerender_active_slide()
        service.client.trigger.assert_not_awaited()
