"""Measure Apple Vision person-detection cost per PTZ analysis (no camera, no app).

Usage: vision_cost.py PTZ_VIDEO [FRAMES]
Reports wall and process-CPU milliseconds per analysis and what 4/s and 12/s
cost as a share of one CPU core, so a rate change can be weighed against
ProPresenter, which shares the Mac.
"""
import sys, time, resource
sys.path[:0] = ["Production_Hub_App"]
import cv2
from PySide6.QtGui import QImage
from production_hub.core.config.models import CameraTrackingConfig
from production_hub.tracking.service import _default_detector_factory

path = sys.argv[1]; count = int(sys.argv[2]) if len(sys.argv) > 2 else 120
detector = _default_detector_factory(CameraTrackingConfig(enabled=True))
cap = cv2.VideoCapture(path); frames = []
while len(frames) < count:
    ok, frame = cap.read()
    if not ok: break
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frames.append(QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy())
detector.detect(frames[0])  # warm up
cpu0 = resource.getrusage(resource.RUSAGE_SELF); t0 = time.perf_counter()
for image in frames: detector.detect(image)
wall = (time.perf_counter() - t0) / len(frames)
cpu1 = resource.getrusage(resource.RUSAGE_SELF)
cpu = ((cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)) / len(frames)
print(f"{len(frames)} frames: wall {wall*1000:.1f} ms, CPU {cpu*1000:.1f} ms per analysis")
for rate in (4, 8, 12):
    print(f"  {rate:2}/s -> {cpu*rate*100:.0f}% of one core")
