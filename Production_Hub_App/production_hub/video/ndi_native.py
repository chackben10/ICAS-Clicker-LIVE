from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path


class NDIUnavailableError(RuntimeError):
    pass


class NativeVideoFrame(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("fourcc", ctypes.c_int),
        ("frame_rate_numerator", ctypes.c_int),
        ("frame_rate_denominator", ctypes.c_int),
        ("picture_aspect_ratio", ctypes.c_float),
        ("frame_format_type", ctypes.c_int),
        ("timecode", ctypes.c_int64),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("line_stride_bytes", ctypes.c_int),
        ("timestamp", ctypes.c_int64),
        ("private_frame", ctypes.c_void_p),
    ]


class NativePerformance(ctypes.Structure):
    _fields_ = [
        ("total_video_frames", ctypes.c_int64),
        ("dropped_video_frames", ctypes.c_int64),
    ]


def _bridge_candidates() -> list[Path]:
    configured = os.environ.get("PRODUCTION_HUB_NDI_BRIDGE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    executable = Path(sys.executable).resolve()
    candidates.extend(
        [
            Path(getattr(sys, "_MEIPASS", "")) / "ProductionHubNative" / "libProductionHubNDI.dylib",
            executable.parents[1] / "Resources" / "ProductionHubNative" / "libProductionHubNDI.dylib",
            Path(__file__).resolve().parents[2] / ".build" / "native" / "libProductionHubNDI.dylib",
        ]
    )
    return [candidate for candidate in candidates if str(candidate) and candidate.exists()]


class NativeNDI:
    """Typed ctypes wrapper around Production Hub's narrow NDI C bridge."""

    _instance: NativeNDI | None = None
    _instance_lock = threading.Lock()

    @classmethod
    def shared(cls) -> NativeNDI:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self, bridge_path: Path | None = None) -> None:
        candidates = [bridge_path] if bridge_path else _bridge_candidates()
        if not candidates:
            raise NDIUnavailableError(
                "Production Hub's NDI receiver bridge is missing. Run scripts/build_native_video.py."
            )
        self.library = ctypes.CDLL(str(candidates[0]))
        self._declare_api()
        if not self.library.ph_ndi_initialize(None):
            raise NDIUnavailableError(self.last_error)

    def _declare_api(self) -> None:
        lib = self.library
        lib.ph_ndi_initialize.argtypes = [ctypes.c_char_p]
        lib.ph_ndi_initialize.restype = ctypes.c_bool
        lib.ph_ndi_is_loaded.restype = ctypes.c_bool
        lib.ph_ndi_version.restype = ctypes.c_char_p
        lib.ph_ndi_last_error.restype = ctypes.c_char_p
        lib.ph_ndi_finder_create.restype = ctypes.c_void_p
        lib.ph_ndi_finder_destroy.argtypes = [ctypes.c_void_p]
        lib.ph_ndi_finder_wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.ph_ndi_finder_wait.restype = ctypes.c_bool
        lib.ph_ndi_finder_source_count.argtypes = [ctypes.c_void_p]
        lib.ph_ndi_finder_source_count.restype = ctypes.c_uint32
        lib.ph_ndi_finder_source_name.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        lib.ph_ndi_finder_source_name.restype = ctypes.c_bool
        lib.ph_ndi_receiver_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_bool]
        lib.ph_ndi_receiver_create.restype = ctypes.c_void_p
        lib.ph_ndi_receiver_destroy.argtypes = [ctypes.c_void_p]
        lib.ph_ndi_receiver_capture_video.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(NativeVideoFrame),
        ]
        lib.ph_ndi_receiver_capture_video.restype = ctypes.c_int
        lib.ph_ndi_receiver_release_video.argtypes = [ctypes.c_void_p, ctypes.POINTER(NativeVideoFrame)]
        lib.ph_ndi_receiver_performance.argtypes = [ctypes.c_void_p, ctypes.POINTER(NativePerformance)]
        lib.ph_ndi_receiver_performance.restype = ctypes.c_bool

    @property
    def version(self) -> str:
        return self._decode(self.library.ph_ndi_version())

    @property
    def last_error(self) -> str:
        return self._decode(self.library.ph_ndi_last_error())

    def discover_sources(self, wait_ms: int = 500) -> list[str]:
        finder = self.library.ph_ndi_finder_create()
        if not finder:
            raise NDIUnavailableError(self.last_error)
        try:
            self.library.ph_ndi_finder_wait(finder, max(0, int(wait_ms)))
            count = int(self.library.ph_ndi_finder_source_count(finder))
            sources: list[str] = []
            for index in range(count):
                buffer = ctypes.create_string_buffer(2048)
                if self.library.ph_ndi_finder_source_name(finder, index, buffer, len(buffer)):
                    sources.append(buffer.value.decode("utf-8", errors="replace"))
            return sources
        finally:
            self.library.ph_ndi_finder_destroy(finder)

    def create_receiver(
        self,
        source_name: str,
        *,
        highest_bandwidth: bool,
        receiver_name: str = "Production Hub - Video Receiver",
    ) -> ctypes.c_void_p:
        receiver = self.library.ph_ndi_receiver_create(
            source_name.encode("utf-8"),
            receiver_name.encode("utf-8"),
            bool(highest_bandwidth),
        )
        if not receiver:
            raise NDIUnavailableError(self.last_error)
        return receiver

    @staticmethod
    def _decode(value: bytes | None) -> str:
        return value.decode("utf-8", errors="replace") if value else ""
