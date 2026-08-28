from __future__ import annotations

import argparse
import ctypes
import subprocess
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from testing.harness.paths import OUTPUT_ROOT, TESTING_ROOT, VIDEO_TO_NDI_ROOT


SOURCE_NAME = "Production Hub Test - Synthetic Audience"
SINGLE_PERSON = TESTING_ROOT / "fixtures" / "tracking" / "single-person.png"
TWO_PERSON = TESTING_ROOT / "fixtures" / "tracking" / "two-person-crossing.png"


def build_bridge(output_dir: Path) -> Path:
    """Compile only the repository's NDI shim; the official runtime stays external."""

    output_dir.mkdir(parents=True, exist_ok=True)
    dylib = output_dir / "libproduction_hub_test_ndi.dylib"
    source = VIDEO_TO_NDI_ROOT / "Sources" / "CNDIShim" / "CNDIShim.c"
    include = VIDEO_TO_NDI_ROOT / "Sources" / "CNDIShim" / "include"
    if not dylib.exists() or dylib.stat().st_mtime < source.stat().st_mtime:
        subprocess.run(
            [
                "clang",
                "-dynamiclib",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(include),
                str(source),
                "-o",
                str(dylib),
            ],
            check=True,
        )
    return dylib


class NDISyntheticSender(AbstractContextManager["NDISyntheticSender"]):
    def __init__(self, source_name: str = SOURCE_NAME, output_dir: Path | None = None) -> None:
        self.source_name = source_name
        self.output_dir = output_dir or OUTPUT_ROOT / ".ndi-fixture"
        self._library: ctypes.CDLL | None = None
        self._sender: int | None = None

    def __enter__(self) -> "NDISyntheticSender":
        library = ctypes.CDLL(str(build_bridge(self.output_dir)))
        library.ndi_bridge_initialize.argtypes = [ctypes.c_char_p]
        library.ndi_bridge_initialize.restype = ctypes.c_bool
        library.ndi_bridge_last_error.restype = ctypes.c_char_p
        library.ndi_bridge_sender_create.argtypes = [ctypes.c_char_p]
        library.ndi_bridge_sender_create.restype = ctypes.c_void_p
        library.ndi_bridge_sender_destroy.argtypes = [ctypes.c_void_p]
        library.ndi_bridge_sender_connection_count.argtypes = [ctypes.c_void_p]
        library.ndi_bridge_sender_connection_count.restype = ctypes.c_int
        library.ndi_bridge_sender_send_bgra.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int64,
        ]
        library.ndi_bridge_sender_send_bgra.restype = ctypes.c_bool
        if not library.ndi_bridge_initialize(None):
            detail = (library.ndi_bridge_last_error() or b"unknown NDI error").decode(errors="replace")
            raise RuntimeError(detail)
        sender = library.ndi_bridge_sender_create(self.source_name.encode())
        if not sender:
            detail = (library.ndi_bridge_last_error() or b"sender creation failed").decode(errors="replace")
            library.ndi_bridge_shutdown()
            raise RuntimeError(detail)
        self._library = library
        self._sender = int(sender)
        return self

    @property
    def connections(self) -> int:
        if not self._library or not self._sender:
            return 0
        return int(self._library.ndi_bridge_sender_connection_count(self._sender))

    def send(self, frame: QImage, *, fps: int = 30, timecode: int = 0) -> None:
        if not self._library or not self._sender:
            raise RuntimeError("NDI synthetic sender is not running")
        image = frame.convertToFormat(QImage.Format.Format_ARGB32)
        byte_count = image.bytesPerLine() * image.height()
        pixels = bytes(image.constBits()[:byte_count])
        buffer = (ctypes.c_uint8 * byte_count).from_buffer_copy(pixels)
        ok = self._library.ndi_bridge_sender_send_bgra(
            self._sender,
            buffer,
            image.width(),
            image.height(),
            image.bytesPerLine(),
            fps,
            1,
            timecode,
        )
        if not ok:
            detail = (self._library.ndi_bridge_last_error() or b"frame send failed").decode(errors="replace")
            raise RuntimeError(detail)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._library and self._sender:
            self._library.ndi_bridge_sender_destroy(self._sender)
        if self._library:
            self._library.ndi_bridge_shutdown()
        self._sender = None
        self._library = None


def fixture_frame(path: Path, *, width: int = 1280, height: int = 720, offset_x: int = 0) -> QImage:
    source = QImage(str(path))
    if source.isNull():
        raise RuntimeError(f"Could not load tracking fixture {path}")
    source = source.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    frame = QImage(width, height, QImage.Format.Format_ARGB32)
    frame.fill(QColor("#111111"))
    painter = QPainter(frame)
    painter.drawImage((width - source.width()) // 2 + offset_x, (height - source.height()) // 2, source)
    painter.end()
    return frame


def scenario_frames(scenario: str, frame_count: int, *, width: int = 1280, height: int = 720) -> Iterator[QImage | None]:
    if scenario not in {"single-person", "multi-person", "crossing", "dropout", "rapid-motion"}:
        raise ValueError(f"Unknown NDI fixture scenario: {scenario}")
    for index in range(frame_count):
        if scenario == "dropout" and frame_count // 3 <= index < (frame_count * 2) // 3:
            yield None
            continue
        source = TWO_PERSON if scenario in {"multi-person", "crossing"} else SINGLE_PERSON
        if scenario == "crossing":
            offset = round(((index / max(1, frame_count - 1)) - 0.5) * width * 0.10)
        elif scenario == "rapid-motion":
            offset = round(((-1) ** index) * width * 0.09)
        else:
            offset = 0
        yield fixture_frame(source, width=width, height=height, offset_x=offset)


def stream_scenario(sender: NDISyntheticSender, scenario: str, duration: float, fps: int = 30) -> dict[str, float | int | str]:
    frame_count = max(1, round(duration * fps))
    started = time.perf_counter()
    sent = 0
    dropped = 0
    for index, frame in enumerate(scenario_frames(scenario, frame_count)):
        deadline = started + index / fps
        remaining = deadline - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
        if frame is None:
            dropped += 1
            continue
        sender.send(frame, fps=fps, timecode=index)
        sent += 1
    elapsed = time.perf_counter() - started
    return {
        "scenario": scenario,
        "frames_sent": sent,
        "intentional_dropouts": dropped,
        "duration_seconds": round(elapsed, 3),
        "effective_fps": round(sent / elapsed, 3) if elapsed else 0.0,
        "connections": sender.connections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream privacy-safe PTZ tracking regression fixtures over NDI")
    parser.add_argument("--source-name", default=SOURCE_NAME)
    parser.add_argument("--scenario", action="append", choices=["single-person", "multi-person", "crossing", "dropout", "rapid-motion"])
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    scenarios = args.scenario or ["single-person", "multi-person", "crossing", "dropout", "rapid-motion"]
    with NDISyntheticSender(args.source_name) as sender:
        for scenario in scenarios:
            print(stream_scenario(sender, scenario, args.seconds, args.fps), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
