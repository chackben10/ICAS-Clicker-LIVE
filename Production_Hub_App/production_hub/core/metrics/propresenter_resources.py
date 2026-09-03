"""Sample ProPresenter's memory across a real service.

The lag correlates with ProPresenter's resident size, not with anything
Production Hub does: 7.58 GB resident with 4.5 GB swapped out on a 16 GB
machine is a paging render pipeline, and a click then waits on disk.  A
one-hour synthetic soak never reached that state, so it could not have
validated any fix.  This samples the real thing, for the length of a real
service, so the growth curve can be read against the press-to-cue distribution.

Sampling is intentionally cheap -- one `ps` and one `sysctl` per interval.
`vmmap` and `footprint` walk the whole address space of a multi-gigabyte
process and take seconds; putting either on a timer would add exactly the kind
of load this file exists to measure.  Use `detailed_snapshot()` by hand, once,
when a curve needs explaining.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any


DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_PROCESS_NAME = "ProPresenter"
_SAMPLE_TIMEOUT_SECONDS = 5.0
_DETAILED_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ResourceSample:
    recorded_at: float
    pid: int
    rss_bytes: int
    swap_used_bytes: int
    cpu_percent: float


@dataclass
class ResourceHistory:
    samples: list[ResourceSample] = field(default_factory=list)

    def add(self, sample: ResourceSample) -> None:
        self.samples.append(sample)

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"samples": 0}
        first = self.samples[0]
        last = self.samples[-1]
        elapsed_hours = max((last.recorded_at - first.recorded_at) / 3600.0, 1e-9)
        growth_bytes = last.rss_bytes - first.rss_bytes
        return {
            "samples": len(self.samples),
            "first_rss_bytes": first.rss_bytes,
            "last_rss_bytes": last.rss_bytes,
            "peak_rss_bytes": max(sample.rss_bytes for sample in self.samples),
            "peak_swap_used_bytes": max(sample.swap_used_bytes for sample in self.samples),
            "rss_growth_bytes_per_hour": int(growth_bytes / elapsed_hours),
            "elapsed_seconds": round(last.recorded_at - first.recorded_at, 1),
        }


def _run(command: list[str], timeout: float) -> str:
    try:
        process = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return process.stdout if process.returncode == 0 else ""


def find_process_id(process_name: str = DEFAULT_PROCESS_NAME) -> int:
    """Return ProPresenter's pid, or 0 when it is not running."""

    output = _run(["/usr/bin/pgrep", "-x", process_name], _SAMPLE_TIMEOUT_SECONDS).strip()
    if not output:
        return 0
    for line in output.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return 0


def swap_used_bytes() -> int:
    """System-wide swap in use. Once this is non-zero, cues wait on disk."""

    output = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], _SAMPLE_TIMEOUT_SECONDS)
    match = re.search(r"used\s*=\s*([0-9.]+)([KMG])", output)
    if not match:
        return 0
    value = float(match.group(1))
    scale = {"K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2)]
    return int(value * scale)


def sample_process(pid: int) -> tuple[int, float]:
    """Return (rss_bytes, cpu_percent) for one pid."""

    if pid <= 0:
        return 0, 0.0
    output = _run(["/bin/ps", "-o", "rss=,%cpu=", "-p", str(pid)], _SAMPLE_TIMEOUT_SECONDS).strip()
    if not output:
        return 0, 0.0
    fields = output.split()
    if len(fields) < 2:
        return 0, 0.0
    try:
        # ps reports RSS in kilobytes.
        return int(fields[0]) * 1024, float(fields[1])
    except ValueError:
        return 0, 0.0


def detailed_snapshot(pid: int) -> dict[str, Any]:
    """One expensive `vmmap` read. Call by hand, never on a timer."""

    if pid <= 0:
        return {"pid": 0, "available": False}
    output = _run(["/usr/bin/vmmap", "--summary", str(pid)], _DETAILED_TIMEOUT_SECONDS)
    if not output:
        return {"pid": pid, "available": False}
    regions: dict[str, str] = {}
    for label in ("IOAccelerator", "IOSurface", "MALLOC_LARGE", "Dirty Size"):
        match = re.search(rf"^{re.escape(label)}.*?([0-9.]+[KMG])", output, re.MULTILINE)
        if match:
            regions[label] = match.group(1)
    return {"pid": pid, "available": True, "regions": regions}


class ProPresenterResourceSampler:
    """Background sampler; start it with the app and leave it running."""

    def __init__(
        self,
        logger: Any | None = None,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        process_name: str = DEFAULT_PROCESS_NAME,
    ) -> None:
        self.logger = logger
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.process_name = str(process_name)
        self.history = ResourceHistory()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-propresenter-resources",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def sample_once(self) -> ResourceSample | None:
        pid = find_process_id(self.process_name)
        if pid <= 0:
            return None
        rss, cpu = sample_process(pid)
        sample = ResourceSample(
            recorded_at=time.monotonic(),
            pid=pid,
            rss_bytes=rss,
            swap_used_bytes=swap_used_bytes(),
            cpu_percent=cpu,
        )
        with self._lock:
            self.history.add(sample)
        if self.logger is not None:
            self.logger.info(
                "propresenter_resources",
                "ProPresenter resource sample",
                pid=sample.pid,
                rss_mb=round(sample.rss_bytes / 1024**2, 1),
                swap_used_mb=round(sample.swap_used_bytes / 1024**2, 1),
                cpu_percent=sample.cpu_percent,
            )
        return sample

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return self.history.summary()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sample_once()
            except Exception as exc:  # a sampler must never take the app down
                if self.logger is not None:
                    self.logger.warning(
                        "propresenter_resources_failed",
                        "ProPresenter resource sample failed",
                        error=str(exc),
                    )
            self._stop_event.wait(self.interval_seconds)
