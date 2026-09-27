"""Stage scenarios for one, two, and three people, each tied to real footage.

Distances are metres from the camera-left edge of the stage deck (8.1 m wide,
lectern at ~3.2 m, side exits just past each end). Speeds come from
`fixtures/icas_footage_measurements.json`. A scenario's `source` names the
recording and time it reproduces, or says `stress` when it deliberately pushes
past what the recordings show.

Rules the scoring enforces (confirmed with the operator):
* the locked person is followed the whole time, including walking off stage;
  the camera never switches to someone else on its own;
* no part of a tracked head may leave the PTZ frame;
* a moving subject may not sit in the outer 20% of the frame for over 1 s;
* the camera may not hunt or overshoot after the subject stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .world import (
    LAYOUT,
    LECTERN_M,
    LEFT_EXIT_M,
    MEASURED,
    RIGHT_EXIT_M,
    STAGE_WIDTH_M,
    Person,
)

SPEED = MEASURED["walking_speeds_m_per_s"]
WALK_OFF = SPEED["walk_off_measured"]
NORMAL = SPEED["normal"]
BRISK = SPEED["brisk"]
HURRY = SPEED["hurry"]
SERMON_ZOOM = MEASURED["sermon_zoom"]["typical"]
FLOOR = LAYOUT["floor_feet_y"]
DECK = LAYOUT["deck_feet_y"]

YOUTH_0920 = "https://www.youtube.com/watch?v=22i-7y8CYc8"
SERMON_0906 = "https://www.youtube.com/watch?v=-Dj897BRXSk"
SERMON_0913 = "https://www.youtube.com/watch?v=bvuQFg7ESPM"


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    group: str  # "single" | "double" | "triple"
    title: str
    source: str
    build: Callable[[], list[Person]]
    targets: tuple[str, ...]
    duration: float
    zoom: int | None = SERMON_ZOOM  # None: operator frames the whole group
    stationary_windows: tuple[tuple[float, float], ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)
    # Rendered: real stage imagery and real people cut from our streams,
    # judged by the production detector (see render.py).
    rendered: bool = False


# --- single subject ---------------------------------------------------------------


def _lectern_sway() -> list[Person]:
    p = Person("preacher", x=LECTERN_M, posture="at_lectern", detector_profile="sermon0906")
    for offset in (0.25, -0.2, 0.3, -0.25, 0.0):
        p.hold(4.0, "at_lectern").walk_to(LECTERN_M + offset, 0.4, posture="at_lectern", ramp=0.3)
    p.hold(3.0, "hand_mic_gesture").hold(6.0, "at_lectern")
    return [p]


def _walk_off(speed: float, direction: str, *, newcomer: bool) -> Callable[[], list[Person]]:
    def build() -> list[Person]:
        start = LECTERN_M - 0.9 if direction == "left" else LECTERN_M + 0.9
        exit_x = LEFT_EXIT_M if direction == "left" else RIGHT_EXIT_M
        reader = Person("reader", x=start, detector_profile="youth0920_a")
        reader.hold(8.0, "standing").walk_to(exit_x, speed, posture="standing").hold(5.0, "turned_away")
        people = [reader]
        if newcomer:
            # The next speaker enters from the same side as the reader leaves.
            nxt = Person("next_speaker", x=exit_x, detector_profile="youth0920_b").appear_at(8.0)
            nxt.hold(8.0).walk_to(start, speed * 0.9).hold(8.0)
            people.append(nxt)
        return people

    return build


def _pacing(speed: float, legs: tuple[float, ...], pause: float = 3.5) -> Callable[[], list[Person]]:
    def build() -> list[Person]:
        p = Person("preacher", x=LECTERN_M, detector_profile="sermon0906").hold(3.0)
        for target in legs:
            p.walk_to(target, speed, posture="hand_mic_gesture").hold(pause, "hand_mic_gesture")
        return [p]

    return build


def _reversal() -> list[Person]:
    p = Person("preacher", x=LECTERN_M, detector_profile="sermon0906").hold(3.0)
    p.walk_to(LECTERN_M + 1.5, NORMAL, posture="hand_mic_gesture", ramp=0.4)
    p.walk_to(LECTERN_M - 1.5, NORMAL, posture="hand_mic_gesture", ramp=0.4)
    p.walk_to(LECTERN_M + 0.5, NORMAL, posture="hand_mic_gesture", ramp=0.4).hold(5.0)
    return [p]


def _step_down() -> list[Person]:
    p = Person("preacher", x=LECTERN_M, detector_profile="sermon0906").hold(3.0)
    p.walk_to(LECTERN_M + 0.6, WALK_OFF, feet_y=FLOOR, posture="standing").hold(6.0, "hand_mic_gesture")
    p.walk_to(LECTERN_M, WALK_OFF, feet_y=DECK, posture="standing").hold(5.0)
    return [p]


def _bend() -> list[Person]:
    p = Person("preacher", x=LECTERN_M, detector_profile="sermon0906").hold(4.0, "at_lectern")
    p.hold(1.8, "bending").hold(5.0, "at_lectern").hold(1.5, "bending").hold(6.0, "standing")
    return [p]


def _worship_leader() -> list[Person]:
    p = Person("leader", x=LECTERN_M + 1.2, detector_profile="youth0920_a").hold(4.0, "hand_mic_gesture")
    p.hold(4.0, "arms_raised").hold(3.0, "hand_mic_gesture").walk_to(LECTERN_M + 2.2, WALK_OFF, posture="hand_mic_gesture")
    p.hold(3.0, "arms_raised").hold(4.0, "standing")
    return [p]


def _turned_away() -> list[Person]:
    p = Person("speaker", x=LECTERN_M, detector_profile="youth0920_a").hold(4.0)
    p.walk_to(LECTERN_M + 2.0, 0.8, posture="turned_away").hold(3.0, "turned_away").hold(4.0, "standing")
    return [p]


def _crossing_behind() -> list[Person]:
    speaker = Person("speaker", x=LECTERN_M, detector_profile="youth0920_a").hold(20.0, "hand_mic_gesture")
    usher = Person("walker", x=LECTERN_M - 3.0, detector_profile="youth0920_b").appear_at(3.0)
    usher.hold(3.0).walk_to(LECTERN_M + 3.0, NORMAL).hold(4.0).walk_to(LECTERN_M - 3.0, BRISK).hold(4.0)
    return [speaker, usher]


SINGLE: list[Scenario] = [
    Scenario(
        "single_lectern_sermon_sway",
        "single",
        "Preacher at the lectern: small sways and a hand-mic gesture for 40 s",
        f"{SERMON_0913}&t=5732s (Sep 13 sermon, tracked 95:32-120:19)",
        _lectern_sway,
        ("preacher",),
        40.0,
        stationary_windows=((0.0, 40.0),),
        tags=("stationary",),
    ),
    Scenario(
        "single_reader_walks_off_left_measured_speed",
        "single",
        f"Reader finishes and walks off camera-left at the measured {WALK_OFF} m/s; next speaker enters",
        f"{YOUTH_0920}&t=7278s (Sep 20, operator took over at 121:18)",
        _walk_off(WALK_OFF, "left", newcomer=True),
        ("reader",),
        24.0,
        tags=("walk_off", "real_failure"),
    ),
    Scenario(
        "single_speaker_walks_off_left_normal",
        "single",
        f"Speaker walks off camera-left at a normal {NORMAL} m/s while the next speaker enters",
        f"{YOUTH_0920}&t=7960s (Sep 20, operator took over at 132:40)",
        _walk_off(NORMAL, "left", newcomer=True),
        ("reader",),
        22.0,
        tags=("walk_off", "real_failure"),
    ),
    Scenario(
        "single_speaker_walks_off_right_brisk",
        "single",
        f"Speaker walks briskly ({BRISK} m/s) off camera-right; nobody else on stage",
        "stress: same pattern as the Sep 20 walk-offs, mirrored and faster",
        _walk_off(BRISK, "right", newcomer=False),
        ("reader",),
        20.0,
        tags=("walk_off", "fast"),
    ),
    Scenario(
        "single_preacher_paces_normal",
        "single",
        f"Preacher paces lectern -> right 2.5 m -> left 4 m -> lectern at {NORMAL} m/s, pausing 3.5 s",
        f"{SERMON_0906}&t=300s (preaching style with hand mic)",
        _pacing(NORMAL, (LECTERN_M + 2.5, LECTERN_M - 1.5, LECTERN_M)),
        ("preacher",),
        22.0,
        tags=("pacing",),
    ),
    Scenario(
        "single_preacher_paces_brisk",
        "single",
        f"Same pacing at a brisk {BRISK} m/s",
        "stress: brisk preacher",
        _pacing(BRISK, (LECTERN_M + 2.5, LECTERN_M - 1.5, LECTERN_M)),
        ("preacher",),
        20.0,
        tags=("pacing", "fast"),
    ),
    Scenario(
        "single_preacher_hurries_short_bursts",
        "single",
        f"Short 1.5 m bursts at {HURRY} m/s with 3 s pauses",
        "stress: energetic preacher",
        _pacing(HURRY, (LECTERN_M + 1.5, LECTERN_M, LECTERN_M - 1.5, LECTERN_M), pause=3.0),
        ("preacher",),
        20.0,
        tags=("pacing", "fast"),
    ),
    Scenario(
        "single_direction_reversal",
        "single",
        "Walks right 1.5 m, immediately turns back 3 m left, then returns",
        "stress: sudden reversal while the camera is still accelerating",
        _reversal,
        ("preacher",),
        16.0,
        tags=("fast",),
    ),
    Scenario(
        "single_steps_down_to_floor",
        "single",
        "Steps down off the deck to the congregation floor, speaks, and returns",
        "stress: common during altar calls",
        _step_down,
        ("preacher",),
        20.0,
        tags=("tilt",),
    ),
    Scenario(
        "single_bends_at_lectern",
        "single",
        "Bends down at the lectern twice (detector loses the person about half the time)",
        "stress: picking up a Bible or notes",
        _bend,
        ("preacher",),
        19.0,
        stationary_windows=((0.0, 19.0),),
        tags=("stationary", "dropout"),
    ),
    Scenario(
        "single_worship_leader_arms_raised",
        "single",
        "Worship leader raises both arms, moves 1 m, raises arms again",
        f"{YOUTH_0920} (worship segments)",
        _worship_leader,
        ("leader",),
        20.0,
        tags=("gesture",),
    ),
    Scenario(
        "single_walks_turned_away",
        "single",
        "Speaker turns their back and walks 2 m toward the band (weaker detections)",
        "stress: turning to the choir/band",
        _turned_away,
        ("speaker",),
        14.0,
        tags=("dropout",),
    ),
    Scenario(
        "single_someone_crosses_behind",
        "single",
        "An usher walks across behind the speaker and back, briskly the second time",
        "stress: people crossing the stage during a reading",
        _crossing_behind,
        ("speaker",),
        20.0,
        stationary_windows=((0.0, 20.0),),
        tags=("identity",),
    ),
]


# --- two people --------------------------------------------------------------------


def _preacher_translator() -> list[Person]:
    preacher = Person("preacher", x=LECTERN_M, posture="at_lectern", detector_profile="sermon0906")
    translator = Person("translator", x=LECTERN_M + 2.4, detector_profile="sermon0906")
    preacher.hold(8.0, "at_lectern").walk_to(LECTERN_M + 1.2, WALK_OFF, posture="hand_mic_gesture")
    preacher.hold(5.0, "hand_mic_gesture").walk_to(LECTERN_M, WALK_OFF, posture="at_lectern").hold(6.0, "at_lectern")
    translator.hold(30.0, "hand_mic_gesture")
    return [preacher, translator]


def _duet() -> list[Person]:
    a = Person("singer_a", x=LECTERN_M + 0.6, detector_profile="youth0920_b")
    b = Person("singer_b", x=LECTERN_M + 1.2, detector_profile="youth0920_b")
    a.hold(6.0, "hand_mic_gesture").walk_to(LECTERN_M - 0.6, WALK_OFF, posture="hand_mic_gesture").hold(5.0)
    a.walk_to(LECTERN_M + 0.6, WALK_OFF, posture="hand_mic_gesture").hold(5.0)
    b.hold(25.0, "hand_mic_gesture")
    return [a, b]


def _two_walk_apart() -> list[Person]:
    a = Person("speaker_a", x=LECTERN_M, detector_profile="youth0920_a")
    b = Person("speaker_b", x=LECTERN_M + 0.8, detector_profile="youth0920_b")
    a.hold(4.0).walk_to(LECTERN_M - 1.8, NORMAL).hold(4.0).walk_to(LECTERN_M, NORMAL).hold(4.0)
    b.hold(4.0).walk_to(LECTERN_M + 2.6, NORMAL).hold(4.0).walk_to(LECTERN_M + 0.8, NORMAL).hold(4.0)
    return [a, b]


def _two_cross() -> list[Person]:
    a = Person("speaker_a", x=LECTERN_M - 0.8, detector_profile="youth0920_a")
    b = Person("speaker_b", x=LECTERN_M + 0.8, detector_profile="youth0920_b")
    a.hold(4.0).walk_to(LECTERN_M + 0.8, WALK_OFF).hold(6.0)
    b.hold(4.0).walk_to(LECTERN_M - 0.8, WALK_OFF).hold(6.0)
    return [a, b]


DOUBLE: list[Scenario] = [
    Scenario(
        "double_preacher_and_translator",
        "double",
        "Preacher at lectern + translator 2.4 m away; preacher walks toward the translator and back",
        f"{SERMON_0906}&t=300s (Sep 6 bilingual sermon)",
        _preacher_translator,
        ("preacher", "translator"),
        28.0,
        zoom=None,
    ),
    Scenario(
        "double_duet_one_steps_away",
        "double",
        "Two singers 0.6 m apart; one steps 1.2 m away and comes back",
        f"{YOUTH_0920}&t=5900s (Sep 20 youth duets)",
        _duet,
        ("singer_a", "singer_b"),
        24.0,
        zoom=None,
    ),
    Scenario(
        "double_walk_apart_and_regroup",
        "double",
        f"Two speakers walk apart to 4.4 m at {NORMAL} m/s and regroup",
        "stress",
        _two_walk_apart,
        ("speaker_a", "speaker_b"),
        20.0,
        zoom=None,
        tags=("fast",),
    ),
    Scenario(
        "double_swap_places",
        "double",
        "Two speakers cross and swap places",
        f"{YOUTH_0920}&t=6150s (speaker swaps)",
        _two_cross,
        ("speaker_a", "speaker_b"),
        14.0,
        zoom=None,
        tags=("identity",),
    ),
]


# --- three people --------------------------------------------------------------------


def _trio(spread: float, speed: float) -> Callable[[], list[Person]]:
    def build() -> list[Person]:
        centre = LECTERN_M + 1.0
        people = []
        for index, offset in enumerate((-0.6, 0.0, 0.6)):
            p = Person(f"singer_{index + 1}", x=centre + offset, detector_profile="youth0920_b")
            p.hold(5.0, "hand_mic_gesture")
            if spread:
                p.walk_to(centre + offset * spread, speed, posture="hand_mic_gesture").hold(4.0)
                p.walk_to(centre + offset, speed, posture="hand_mic_gesture")
            p.hold(6.0, "hand_mic_gesture")
            people.append(p)
        return people

    return build


def _trio_one_leaves() -> list[Person]:
    centre = LECTERN_M + 1.0
    people = [
        Person(f"singer_{index + 1}", x=centre + offset, detector_profile="youth0920_b")
        for index, offset in enumerate((-0.6, 0.0, 0.6))
    ]
    people[0].hold(16.0, "hand_mic_gesture")
    people[1].hold(16.0, "hand_mic_gesture")
    people[2].hold(5.0, "hand_mic_gesture").walk_to(RIGHT_EXIT_M, NORMAL).hold(6.0, "turned_away")
    return people


TRIPLE: list[Scenario] = [
    Scenario(
        "triple_trio_singing",
        "triple",
        "Three singers shoulder to shoulder (0.6 m spacing) for 11 s",
        f"{YOUTH_0920} (group songs)",
        _trio(0.0, 0.0),
        ("singer_1", "singer_2", "singer_3"),
        12.0,
        zoom=None,
        stationary_windows=((0.0, 12.0),),
    ),
    Scenario(
        "triple_spread_and_regroup",
        "triple",
        f"Trio spreads to 4 m wide at {NORMAL} m/s and regroups",
        "stress",
        _trio(3.4, NORMAL),
        ("singer_1", "singer_2", "singer_3"),
        22.0,
        zoom=None,
        tags=("fast",),
    ),
    Scenario(
        "triple_one_walks_off",
        "triple",
        "One of three singers walks off camera-right mid-song",
        "stress: follow-the-locked-people rule with a group",
        _trio_one_leaves,
        ("singer_1", "singer_2", "singer_3"),
        16.0,
        zoom=None,
        tags=("walk_off",),
    ),
]

# --- rendered: real stage, real people, real detector -------------------------------
#
# Cast: people cut out of the 2026-09-20 youth meeting (assets/sprites). Two
# pairs are deliberate look-alikes, as happens when readers swap: two girls in
# black jackets over white dresses, and two boys in blue hoodies.
ADULT = ("y0920-7960-p1", 1.58)  # speaker in a pink sari with a hand mic
READER = ("y0920-7960-p13", 1.55)  # black jacket, white dress
READER_TWIN = ("y0920-6330-p1", 1.55)  # black jacket, white dress
WHITE = ("y0920-5965-p4", 1.55)  # white cardigan, reading
BOY = ("y0920-6330-p2", 1.45)  # blue hoodie
BOY_TWIN = ("y0920-6330-p6", 1.45)  # blue hoodie
DUET_A = ("y0920-5965-p19", 1.55)  # black dress
DUET_B = ("y0920-5965-p20", 1.55)  # navy dress
PREACHER = ("s0913-5735-p1", 1.70)  # Sep 13 preacher: light-blue shirt, hand mic
DASH = 2.5
SPRINT = 3.5


def cast(name: str, role: tuple[str, float], x: float, **options) -> Person:
    return Person(name, x=x, actor=role[0], height_m=role[1], **options)


def _dash_and_stop() -> list[Person]:
    p = cast("speaker", ADULT, LECTERN_M - 0.8).hold(4.0)
    p.walk_to(LECTERN_M + 2.2, DASH, ramp=0.3).hold(5.0)
    p.walk_to(LECTERN_M - 1.8, 3.0, ramp=0.3).hold(5.0)
    return [p]


def _sprint_across() -> list[Person]:
    p = cast("speaker", BOY, 0.4).hold(4.0)
    p.walk_to(STAGE_WIDTH_M - 0.4, SPRINT, ramp=0.4).hold(6.0)
    return [p]


def _bursts() -> list[Person]:
    p = cast("speaker", WHITE, LECTERN_M - 1.0).hold(3.0)
    for target in (LECTERN_M, LECTERN_M + 1.0, LECTERN_M, LECTERN_M - 1.0):
        p.walk_to(target, 2.0, ramp=0.25).hold(1.5)
    p.hold(3.0)
    return [p]


def _rendered_walk_off(speed: float) -> Callable[[], list[Person]]:
    def build() -> list[Person]:
        start = LECTERN_M - 0.9
        reader = cast("reader", READER, start).hold(6.0).walk_to(LEFT_EXIT_M, speed).hold(4.0)
        twin = cast("next_reader", READER_TWIN, LEFT_EXIT_M).appear_at(6.0)
        twin.hold(6.0).walk_to(start, speed * 0.9).hold(6.0)
        return [reader, twin]

    return build


def _adult_walk_off_right() -> list[Person]:
    return [cast("speaker", ADULT, LECTERN_M + 0.8).hold(5.0).walk_to(RIGHT_EXIT_M, BRISK).hold(4.0)]


def _beside_lectern_sway() -> list[Person]:
    p = cast("speaker", ADULT, LECTERN_M - 0.75, feet_y=DECK - 0.01).hold(4.0)
    for offset in (-0.3, 0.1, -0.2, 0.0):
        p.walk_to(LECTERN_M - 0.75 + offset, 0.4, ramp=0.3).hold(4.0)
    return [p]


def _behind_lectern_then_out() -> list[Person]:
    p = cast("preacher", ADULT, LECTERN_M, feet_y=DECK - 0.01).hold(5.0)
    p.walk_to(LECTERN_M + 2.0, DASH, ramp=0.3).hold(5.0)
    return [p]


def _rendered_step_down() -> list[Person]:
    p = cast("speaker", BOY, LECTERN_M + 1.2).hold(3.0)
    p.walk_to(LECTERN_M + 0.9, NORMAL, feet_y=FLOOR).hold(4.0).walk_to(LECTERN_M + 1.2, NORMAL, feet_y=DECK).hold(4.0)
    return [p]


def _rendered_crossing_behind() -> list[Person]:
    speaker = cast("speaker", READER, LECTERN_M - 0.8).hold(16.0)
    walker = cast("walker", READER_TWIN, LECTERN_M - 3.5, feet_y=DECK - 0.012).appear_at(3.0)
    walker.hold(3.0).walk_to(LECTERN_M + 2.0, NORMAL).hold(2.0).walk_to(LECTERN_M - 3.5, BRISK).hold(3.0)
    return [speaker, walker]


def _duet_dash() -> list[Person]:
    a = cast("singer_a", DUET_A, LECTERN_M + 0.4).hold(4.0)
    b = cast("singer_b", DUET_B, LECTERN_M + 0.95).hold(20.0)
    a.walk_to(LECTERN_M - 2.4, DASH, ramp=0.3).hold(4.0).walk_to(LECTERN_M + 0.4, DASH, ramp=0.3).hold(5.0)
    return [a, b]


def _twins_swap() -> list[Person]:
    a = cast("reader_a", READER, LECTERN_M - 0.8).hold(4.0).walk_to(LECTERN_M + 0.9, 1.5).hold(5.0)
    b = cast("reader_b", READER_TWIN, LECTERN_M + 0.9).hold(4.0).walk_to(LECTERN_M - 0.8, 1.5).hold(5.0)
    return [a, b]


def _scatter_pair() -> list[Person]:
    a = cast("boy_a", BOY, LECTERN_M + 0.6).hold(4.0).walk_to(LECTERN_M - 2.0, 2.0, ramp=0.3).hold(4.0)
    b = cast("boy_b", BOY_TWIN, LECTERN_M + 1.1).hold(4.0).walk_to(LECTERN_M + 3.6, 2.0, ramp=0.3).hold(4.0)
    return [a, b]


def _trio_tight() -> list[Person]:
    centre = LECTERN_M + 1.2
    roles = (DUET_A, READER, DUET_B)
    return [cast(f"singer_{i + 1}", role, centre + (i - 1) * 0.42).hold(12.0) for i, role in enumerate(roles)]


def _trio_middle_leaves() -> list[Person]:
    centre = LECTERN_M + 1.2
    left = cast("singer_1", DUET_A, centre - 0.5).hold(18.0)
    right = cast("singer_3", DUET_B, centre + 0.5).hold(18.0)
    middle = cast("singer_2", READER, centre, feet_y=DECK - 0.006).hold(4.0)
    middle.walk_to(centre - 3.0, DASH, ramp=0.3).hold(4.0).walk_to(centre, DASH, ramp=0.3).hold(5.0)
    return [left, middle, right]


def _trio_scatter() -> list[Person]:
    centre = LECTERN_M + 1.0
    people = []
    for i, (role, target) in enumerate(((BOY, -2.4), (READER, 0.0), (BOY_TWIN, 2.4))):
        p = cast(f"member_{i + 1}", role, centre + (i - 1) * 0.55).hold(4.0)
        if target:
            p.walk_to(centre + target, 2.0, ramp=0.3).hold(3.0).walk_to(centre + (i - 1) * 0.55, 2.0, ramp=0.3)
        p.hold(4.0)
        people.append(p)
    return people


def _trio_lookalikes_cross() -> list[Person]:
    a = cast("reader_a", READER, LECTERN_M - 0.2).hold(4.0).walk_to(LECTERN_M + 1.8, 1.4).hold(5.0)
    b = cast("reader_b", READER_TWIN, LECTERN_M + 1.8).hold(4.0).walk_to(LECTERN_M - 0.2, 1.4).hold(5.0)
    c = cast("singer", DUET_B, LECTERN_M + 0.8, feet_y=DECK - 0.012).hold(12.0)
    return [a, b, c]


def _preacher_paces(speed: float) -> Callable[[], list[Person]]:
    def build() -> list[Person]:
        p = cast("preacher", PREACHER, LECTERN_M + 0.9).hold(3.0)
        for target in (LECTERN_M + 3.2, LECTERN_M - 0.9, LECTERN_M + 1.5):
            p.walk_to(target, speed).hold(3.5)
        return [p]

    return build


def _preacher_darts() -> list[Person]:
    p = cast("preacher", PREACHER, LECTERN_M + 1.0).hold(3.0)
    for target in (LECTERN_M + 2.4, LECTERN_M + 0.4, LECTERN_M + 2.0):
        p.walk_to(target, DASH, ramp=0.25).hold(2.5)
    return [p]


def _preacher_leaves_next_arrives() -> list[Person]:
    preacher = cast("preacher", PREACHER, LECTERN_M + 1.2).hold(5.0).walk_to(RIGHT_EXIT_M, NORMAL).hold(4.0)
    elder = cast("next_speaker", ADULT, LEFT_EXIT_M, feet_y=DECK - 0.01).appear_at(4.0)
    elder.hold(4.0).walk_to(LECTERN_M - 0.7, 1.0).hold(6.0)
    return [preacher, elder]


def _preacher_translator_rendered() -> list[Person]:
    preacher = cast("preacher", PREACHER, LECTERN_M + 0.2).hold(4.0)
    preacher.walk_to(LECTERN_M + 1.6, NORMAL).hold(3.0).walk_to(LECTERN_M - 0.6, BRISK).hold(4.0)
    translator = cast("translator", ADULT, LECTERN_M + 2.6).hold(20.0)
    return [preacher, translator]


RENDERED: list[Scenario] = [
    Scenario("rendered_single_preacher_paces_normal", "single",
             f"Sep 13 preacher paces 2.3 m / 4.1 m / 2.4 m legs at {NORMAL} m/s with 3.5 s pauses",
             f"{SERMON_0913}&t=5735s (preacher cut from this sermon)",
             _preacher_paces(NORMAL), ("preacher",), 18.0, rendered=True, tags=("pacing",)),
    Scenario("rendered_single_preacher_paces_brisk", "single",
             f"Same pacing at a brisk {BRISK} m/s",
             f"{SERMON_0913}&t=5735s",
             _preacher_paces(BRISK), ("preacher",), 16.0, rendered=True, tags=("pacing", "fast")),
    Scenario("rendered_single_preacher_darts", "single",
             f"Preacher darts 1.4-2 m at {DASH} m/s three times, stopping dead each time",
             "stress: emphatic preaching",
             _preacher_darts, ("preacher",), 14.0, rendered=True, tags=("fast",)),
    Scenario("rendered_single_preacher_leaves_next_speaker_arrives", "single",
             "Preacher walks off camera-right while the next speaker walks to the lectern",
             f"{SERMON_0913}&t=7190s (Sep 13 end of sermon; operator took over at 120:19)",
             _preacher_leaves_next_arrives, ("preacher",), 16.0, rendered=True,
             tags=("walk_off", "identity", "real_failure")),
    Scenario("rendered_double_preacher_and_translator", "double",
             "Preacher paces between the lectern and a translator 2.4 m away",
             f"{SERMON_0906}&t=300s (bilingual sermon)",
             _preacher_translator_rendered, ("preacher", "translator"), 18.0, zoom=None, rendered=True),
    Scenario("rendered_single_dash_and_stop", "single",
             f"Speaker dashes 3 m at {DASH} m/s, stops dead, then 4 m back at 3.0 m/s",
             "stress on the real stage; speaker cut from 2026-09-20 (132:40 walk-off)",
             _dash_and_stop, ("speaker",), 22.0, rendered=True, tags=("fast",)),
    Scenario("rendered_single_sprint_across_stage", "single",
             f"Boy sprints the full 7 m stage at {SPRINT} m/s and stops",
             "stress on the real stage; boy cut from 2026-09-20",
             _sprint_across, ("speaker",), 12.0, rendered=True, tags=("fast",)),
    Scenario("rendered_single_stop_start_bursts", "single",
             "Four 1 m bursts at 2.0 m/s with 1.5 s stops",
             "stress on the real stage",
             _bursts, ("speaker",), 20.0, rendered=True, tags=("fast",)),
    Scenario("rendered_single_reader_walks_off_twin_enters", "single",
             f"Reader walks off camera-left at {WALK_OFF} m/s while a look-alike reader walks in",
             f"{YOUTH_0920}&t=7278s (Sep 20 manual takeovers at 121:18 and 132:40)",
             _rendered_walk_off(WALK_OFF), ("reader",), 18.0, rendered=True, tags=("walk_off", "identity", "real_failure")),
    Scenario("rendered_single_reader_walks_off_twin_enters_normal", "single",
             f"Same walk-off at {NORMAL} m/s",
             f"{YOUTH_0920}&t=7960s",
             _rendered_walk_off(NORMAL), ("reader",), 16.0, rendered=True, tags=("walk_off", "identity", "real_failure")),
    Scenario("rendered_single_adult_walks_off_right_brisk", "single",
             f"Adult speaker walks briskly ({BRISK} m/s) off camera-right",
             "stress on the real stage",
             _adult_walk_off_right, ("speaker",), 12.0, rendered=True, tags=("walk_off", "fast")),
    Scenario("rendered_single_beside_lectern_sway", "single",
             "Speaker beside the lectern, small sways for 20 s",
             f"{SERMON_0913}&t=5732s (sermon framing)",
             _beside_lectern_sway, ("speaker",), 20.0, rendered=True,
             stationary_windows=((0.0, 20.0),), tags=("stationary",)),
    Scenario("rendered_single_behind_lectern_then_dash", "single",
             f"Preacher mostly hidden behind the lectern, then dashes 2 m out at {DASH} m/s",
             "stress: the lectern hides everything below the shoulders",
             _behind_lectern_then_out, ("preacher",), 13.0, rendered=True, tags=("occlusion", "fast")),
    Scenario("rendered_single_steps_down_to_floor", "single",
             "Steps down off the deck to the floor and back up",
             "stress on the real stage (altar calls)",
             _rendered_step_down, ("speaker",), 15.0, rendered=True, tags=("tilt",)),
    Scenario("rendered_single_lookalike_crosses_behind", "single",
             "A look-alike reader walks behind the speaker and back, briskly the second time",
             "stress: readers swapping behind the speaker",
             _rendered_crossing_behind, ("speaker",), 16.0, rendered=True,
             stationary_windows=((0.0, 16.0),), tags=("identity",)),
    Scenario("rendered_double_duet_one_dashes_off", "double",
             f"Duet 0.55 m apart; one singer dashes 2.8 m away at {DASH} m/s and back",
             f"{YOUTH_0920}&t=5990s (Sep 20 duet)",
             _duet_dash, ("singer_a", "singer_b"), 18.0, zoom=None, rendered=True, tags=("fast",)),
    Scenario("rendered_double_lookalikes_swap_places", "double",
             "Two look-alike readers swap places at 1.5 m/s",
             f"{YOUTH_0920} (speaker swaps)",
             _twins_swap, ("reader_a", "reader_b"), 12.0, zoom=None, rendered=True, tags=("identity",)),
    Scenario("rendered_double_scatter_opposite_ways", "double",
             "Two look-alike boys run apart at 2.0 m/s to 5.6 m",
             "stress on the real stage",
             _scatter_pair, ("boy_a", "boy_b"), 10.0, zoom=None, rendered=True, tags=("fast",)),
    Scenario("rendered_triple_shoulder_to_shoulder", "triple",
             "Three singers 0.42 m apart, overlapping in the frame",
             f"{YOUTH_0920}&t=5490s (Sep 20 group, 91:30)",
             _trio_tight, ("singer_1", "singer_2", "singer_3"), 12.0, zoom=None, rendered=True,
             stationary_windows=((0.0, 12.0),)),
    Scenario("rendered_triple_middle_dashes_out", "triple",
             f"The middle singer dashes 3 m out at {DASH} m/s and back",
             "stress on the real stage",
             _trio_middle_leaves, ("singer_1", "singer_2", "singer_3"), 18.0, zoom=None, rendered=True, tags=("fast",)),
    Scenario("rendered_triple_scatter_and_regroup", "triple",
             "Two members run 2.4 m out to each side at 2.0 m/s and regroup",
             "stress on the real stage",
             _trio_scatter, ("member_1", "member_2", "member_3"), 14.0, zoom=None, rendered=True, tags=("fast",)),
    Scenario("rendered_triple_lookalikes_cross", "triple",
             "Two look-alike readers cross in front of a third singer",
             "stress: identity through overlap",
             _trio_lookalikes_cross, ("reader_a", "reader_b", "singer"), 12.0, zoom=None, rendered=True, tags=("identity",)),
]

ALL: list[Scenario] = SINGLE + DOUBLE + TRIPLE + RENDERED
BY_NAME = {item.name: item for item in ALL}

assert STAGE_WIDTH_M > 7.0, "stage fixture looks wrong"
