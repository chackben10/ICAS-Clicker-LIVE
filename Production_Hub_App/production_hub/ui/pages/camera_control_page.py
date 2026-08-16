from __future__ import annotations

import asyncio
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QProcess, QRectF, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QTextCursor,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.config.models import CameraSceneRegion, SceneRegionPoint
from production_hub.calibration.light_level import assess_camera_light
from production_hub.calibration.mosaic import (
    CalibrationMosaic,
    ensure_calibration_mosaic,
    load_calibration_mosaic,
)
from production_hub.calibration.review import (
    CalibrationReviewData,
    CalibrationReviewMarker,
    CalibrationReviewPose,
    load_active_calibration_review,
    load_latest_calibration_review,
)
from production_hub.calibration.relocalization import RelocalizationState
from production_hub.calibration.relocalization import qimage_to_bgr
from production_hub.calibration.store import CalibrationRegistry
from production_hub.ui.pages.common import responsive_grid, run_background, scroll_page, title
from production_hub.tracking.models import (
    NormalizedRect,
    TrackedSubject,
    TrackingSnapshot,
    TrackingState,
)
from production_hub.tracking.framing import FramingState
from production_hub.tracking.ptz_geometry import PtzGeometryModel, PtzMotorPose
from production_hub.tracking.scene_regions import suggested_church_scene_regions
from production_hub.video.models import VideoSourceKey, VideoSourceSnapshot


class VideoPreview(QFrame):
    subject_clicked = Signal(int)
    region_point_clicked = Signal(float, float)
    region_clicked = Signal(str)
    calibration_marker_clicked = Signal(int)
    frame_target_clicked = Signal(float, float)
    frame_target_box_drawn = Signal(float, float, float, float)

    def __init__(
        self,
        empty_text: str,
        *,
        aspect_ratio: float | None = 16.0 / 9.0,
    ) -> None:
        super().__init__()
        self._aspect_ratio = (
            float(aspect_ratio)
            if aspect_ratio is not None and float(aspect_ratio) > 0.0
            else None
        )
        self.setObjectName("VideoPreview")
        self.setMinimumSize(320, 180)
        self._aspect_minimum_height = 180
        if self._aspect_ratio is not None:
            policy = QSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
        self.setStyleSheet("QFrame#VideoPreview { background: #090b0f; border: 1px solid #343a46; border-radius: 8px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.image_label = QLabel(empty_text)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("color: #9298a5; background: transparent;")
        self.image_label.setMinimumSize(300, 168)
        layout.addWidget(self.image_label)
        self._image: QImage | None = None
        self._subjects: tuple[TrackedSubject, ...] = ()
        self._regions: tuple[CameraSceneRegion, ...] = ()
        self._selected_region_id = ""
        self._calibration_markers: tuple[tuple[int, float, float], ...] = ()
        self._selected_calibration_marker_id = 0
        self._draft_region: tuple[tuple[float, float], ...] = ()
        self._region_drawing = False
        self._frame_target_mode = False
        self._frame_target_point: tuple[float, float] | None = None
        self._frame_target_bounds = None
        self._frame_drag_start: tuple[float, float] | None = None
        self._frame_drag_current: tuple[float, float] | None = None
        self._frame_target_opacity = 1.0
        self._frame_target_fade_step = 0.05
        self._frame_target_fade_timer = QTimer(self)
        self._frame_target_fade_timer.setInterval(50)
        self._frame_target_fade_timer.timeout.connect(self._fade_frame_target_step)
        self.sequence = 0

    @property
    def has_image(self) -> bool:
        return self._image is not None and not self._image.isNull()

    @property
    def image(self) -> QImage | None:
        return QImage(self._image) if self.has_image else None

    def hasHeightForWidth(self) -> bool:
        return self._aspect_ratio is not None

    def heightForWidth(self, width: int) -> int:
        if self._aspect_ratio is None:
            return super().heightForWidth(width)
        return max(
            self._aspect_minimum_height,
            round(max(1, width) / self._aspect_ratio),
        )

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self._aspect_ratio is None:
            return hint
        width = max(self.minimumWidth(), hint.width())
        return QSize(width, self.heightForWidth(width))

    def set_aspect_ratio(self, aspect_ratio: float | None) -> None:
        selected = (
            float(aspect_ratio)
            if aspect_ratio is not None and float(aspect_ratio) > 0.0
            else None
        )
        if selected == self._aspect_ratio:
            return
        self._aspect_ratio = selected
        if selected is None:
            self.setMinimumHeight(self._aspect_minimum_height)
            self.setMaximumHeight(16777215)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Expanding,
            )
        else:
            policy = QSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            desired_height = self.heightForWidth(max(1, self.width()))
            self.setMinimumHeight(desired_height)
            self.setMaximumHeight(desired_height)
        self.updateGeometry()
        self._render()

    def set_frame(self, image: QImage, sequence: int) -> None:
        if sequence == self.sequence:
            return
        self.sequence = sequence
        self._image = QImage(image)
        self._render()

    def clear(self, message: str) -> None:
        self.sequence = 0
        self._image = None
        self._subjects = ()
        self._calibration_markers = ()
        self._selected_calibration_marker_id = 0
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(message)

    def set_subjects(self, subjects: tuple[TrackedSubject, ...]) -> None:
        if subjects == self._subjects:
            return
        self._subjects = tuple(subjects)
        self._render()

    def set_regions(
        self,
        regions: tuple[CameraSceneRegion, ...],
        selected_region_id: str = "",
    ) -> None:
        self._regions = tuple(regions)
        self._selected_region_id = str(selected_region_id or "")
        self._render()

    def set_calibration_markers(
        self,
        markers: tuple[tuple[int, float, float], ...],
        selected_marker_id: int = 0,
    ) -> None:
        self._calibration_markers = tuple(markers)
        self._selected_calibration_marker_id = int(selected_marker_id)
        self._render()

    def set_region_draft(
        self,
        points: tuple[tuple[float, float], ...],
        *,
        drawing: bool,
    ) -> None:
        self._draft_region = tuple(points)
        self._region_drawing = bool(drawing)
        self.setCursor(
            Qt.CursorShape.CrossCursor if drawing else Qt.CursorShape.ArrowCursor
        )
        self._render()

    def set_frame_target_mode(self, enabled: bool) -> None:
        self._frame_target_mode = bool(enabled)
        self.setCursor(
            Qt.CursorShape.CrossCursor
            if self._frame_target_mode or self._region_drawing
            else Qt.CursorShape.ArrowCursor
        )

    def set_frame_target(
        self,
        point: tuple[float, float] | None = None,
        bounds=None,
    ) -> None:
        self._frame_target_fade_timer.stop()
        self._frame_target_opacity = 1.0
        self._frame_target_point = point
        self._frame_target_bounds = bounds
        self._render()

    def fade_frame_target(self, duration_ms: int = 1000) -> None:
        if self._frame_target_point is None and self._frame_target_bounds is None:
            return
        steps = max(1, round(max(100, int(duration_ms)) / 50))
        self._frame_target_fade_step = 1.0 / steps
        self._frame_target_opacity = 1.0
        self._frame_target_fade_timer.start()

    def _fade_frame_target_step(self) -> None:
        self._frame_target_opacity = max(
            0.0,
            self._frame_target_opacity - self._frame_target_fade_step,
        )
        if self._frame_target_opacity <= 0.0:
            self._frame_target_fade_timer.stop()
            self._frame_target_point = None
            self._frame_target_bounds = None
            self._frame_target_opacity = 1.0
        self._render()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            normalized = self._normalized_position(event.position().toPoint())
            if normalized is not None:
                x, y = normalized
                if self._frame_target_mode:
                    self._frame_target_fade_timer.stop()
                    self._frame_target_opacity = 1.0
                    self._frame_drag_start = (x, y)
                    self._frame_drag_current = (x, y)
                    self._frame_target_point = None
                    self._frame_target_bounds = None
                    self._render()
                    event.accept()
                    return
                if self._region_drawing:
                    self.region_point_clicked.emit(x, y)
                    event.accept()
                    return
                if self._calibration_markers:
                    marker_id, distance = min(
                        (
                            marker_id,
                            (marker_x - x) ** 2 + (marker_y - y) ** 2,
                        )
                        for marker_id, marker_x, marker_y in self._calibration_markers
                    )
                    if distance <= 0.000625:
                        self.calibration_marker_clicked.emit(marker_id)
                        event.accept()
                        return
                region_matches = [
                    region
                    for region in self._regions
                    if region.enabled and self._region_contains(region, x, y)
                ]
                if region_matches:
                    chosen_region = min(region_matches, key=self._region_area)
                    self.region_clicked.emit(chosen_region.id)
                    event.accept()
                    return
                matches = [item for item in self._subjects if item.bounds.contains(x, y)]
                if matches:
                    chosen = min(matches, key=lambda item: (item.bounds.area, -item.confidence))
                    self.subject_clicked.emit(chosen.track_id)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._frame_target_mode and self._frame_drag_start is not None:
            normalized = self._normalized_position(event.position().toPoint())
            if normalized is not None:
                self._frame_drag_current = normalized
                self._frame_target_bounds = self._drag_bounds(
                    self._frame_drag_start,
                    normalized,
                )
                self._render()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._frame_target_mode
            and self._frame_drag_start is not None
        ):
            start = self._frame_drag_start
            end = self._normalized_position(event.position().toPoint()) or self._frame_drag_current or start
            bounds = self._drag_bounds(start, end)
            self._frame_drag_start = None
            self._frame_drag_current = None
            if bounds.width >= 0.015 and bounds.height >= 0.015:
                self._frame_target_point = None
                self._frame_target_bounds = bounds
                self.frame_target_box_drawn.emit(
                    bounds.x,
                    bounds.y,
                    bounds.width,
                    bounds.height,
                )
            else:
                self._frame_target_bounds = None
                self._frame_target_point = end
                self.frame_target_clicked.emit(*end)
            self._render()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _drag_bounds(
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> NormalizedRect:
        left, right = sorted((start[0], end[0]))
        top, bottom = sorted((start[1], end[1]))
        return NormalizedRect(left, top, right - left, bottom - top).clamped()

    def _normalized_position(self, point) -> tuple[float, float] | None:
        pixmap = self.image_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        label_point = self.image_label.mapFrom(self, point)
        offset_x = (self.image_label.width() - pixmap.width()) / 2.0
        offset_y = (self.image_label.height() - pixmap.height()) / 2.0
        x = (label_point.x() - offset_x) / max(1, pixmap.width())
        y = (label_point.y() - offset_y) / max(1, pixmap.height())
        if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
            return None
        return x, y

    @staticmethod
    def _region_contains(region: CameraSceneRegion, x: float, y: float) -> bool:
        polygon = QPolygonF([QPointF(point.x, point.y) for point in region.points])
        return polygon.containsPoint(QPointF(x, y), Qt.FillRule.OddEvenFill)

    @staticmethod
    def _region_area(region: CameraSceneRegion) -> float:
        points = region.points
        return abs(
            sum(
                point.x * points[(index + 1) % len(points)].y
                - points[(index + 1) % len(points)].x * point.y
                for index, point in enumerate(points)
            )
        ) / 2.0

    def resizeEvent(self, event) -> None:
        if self._aspect_ratio is not None:
            desired_height = self.heightForWidth(event.size().width())
            if (
                self.minimumHeight() != desired_height
                or self.maximumHeight() != desired_height
            ):
                self.setMinimumHeight(desired_height)
                self.setMaximumHeight(desired_height)
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        if self._image is None or self._image.isNull():
            return
        size = self.image_label.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        pixmap = QPixmap.fromImage(self._image).scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if self._subjects:
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_regions(painter, pixmap)
            self._paint_calibration_markers(painter, pixmap)
            self._paint_frame_target(painter, pixmap)
            for subject in self._subjects:
                color = QColor("#ffb020" if subject.selected else "#25d0c8")
                fill = QColor(color)
                fill.setAlpha(35 if subject.selected else 18)
                rect = QRectF(
                    subject.bounds.x * pixmap.width(),
                    subject.bounds.y * pixmap.height(),
                    subject.bounds.width * pixmap.width(),
                    subject.bounds.height * pixmap.height(),
                )
                painter.fillRect(rect, fill)
                painter.setPen(QPen(color, 3 if subject.selected else 2))
                painter.drawRoundedRect(rect, 4, 4)
                label = f"S{subject.track_id}  {subject.confidence:.0%}"
                label_rect = QRectF(rect.x(), max(0.0, rect.y() - 22), 100, 22)
                painter.fillRect(label_rect, QColor(9, 11, 15, 210))
                painter.setPen(color)
                painter.drawText(
                    label_rect.adjusted(5, 0, -3, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    label,
                )
            painter.end()
        elif (
            self._regions
            or self._draft_region
            or self._calibration_markers
            or self._frame_target_point
            or self._frame_target_bounds is not None
        ):
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_regions(painter, pixmap)
            self._paint_calibration_markers(painter, pixmap)
            self._paint_frame_target(painter, pixmap)
            painter.end()
        self.image_label.setText("")
        self.image_label.setPixmap(pixmap)

    def _paint_regions(self, painter: QPainter, pixmap: QPixmap) -> None:
        for region in self._regions:
            if not region.enabled:
                continue
            polygon = QPolygonF(
                [
                    QPointF(point.x * pixmap.width(), point.y * pixmap.height())
                    for point in region.points
                ]
            )
            color = QColor(region.color)
            if not color.isValid():
                color = QColor("#7c5cff")
            fill = QColor(color)
            selected = region.id == self._selected_region_id
            fill.setAlpha(76 if selected else 30)
            painter.setPen(QPen(color, 4 if selected else 2))
            painter.setBrush(fill)
            painter.drawPolygon(polygon)
            label_point = polygon.boundingRect().topLeft() + QPointF(5, 5)
            label_width = max(150, min(280, 14 + len(region.name) * 8))
            label_rect = QRectF(label_point.x(), label_point.y(), label_width, 24)
            painter.fillRect(label_rect, QColor(9, 11, 15, 205))
            painter.setPen(color)
            painter.drawText(
                label_rect.adjusted(5, 0, -3, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                region.name,
            )
        if self._draft_region:
            points = [
                QPointF(x * pixmap.width(), y * pixmap.height())
                for x, y in self._draft_region
            ]
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ffffff"), 2, Qt.PenStyle.DashLine))
            if len(points) >= 2:
                painter.drawPolyline(QPolygonF(points))
            for point in points:
                painter.drawEllipse(point, 4, 4)

    def _paint_calibration_markers(self, painter: QPainter, pixmap: QPixmap) -> None:
        markers = sorted(
            self._calibration_markers,
            key=lambda item: item[0] == self._selected_calibration_marker_id,
        )
        show_all_labels = len(markers) <= 30
        for marker_id, normalized_x, normalized_y in markers:
            point = QPointF(
                normalized_x * pixmap.width(),
                normalized_y * pixmap.height(),
            )
            selected = marker_id == self._selected_calibration_marker_id
            color = QColor("#ffb020" if selected else "#25d0c8")
            painter.setPen(QPen(QColor(5, 8, 12, 230), 6 if selected else 4))
            painter.setBrush(QColor(5, 8, 12, 210))
            painter.drawEllipse(point, 8 if selected else 6, 8 if selected else 6)
            painter.setPen(QPen(color, 3 if selected else 2))
            painter.setBrush(color)
            painter.drawEllipse(point, 5 if selected else 3, 5 if selected else 3)
            if not selected and not show_all_labels:
                continue
            label = f"M{marker_id:03d}"
            label_rect = QRectF(point.x() + 7, point.y() - 11, 44, 20)
            painter.fillRect(label_rect, QColor(9, 11, 15, 210))
            painter.setPen(color)
            painter.drawText(
                label_rect.adjusted(4, 0, -2, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )

    def _paint_frame_target(self, painter: QPainter, pixmap: QPixmap) -> None:
        color = QColor("#ff496c")
        color.setAlpha(round(255 * self._frame_target_opacity))
        if self._frame_target_bounds is not None:
            bounds = self._frame_target_bounds
            rect = QRectF(
                bounds.x * pixmap.width(),
                bounds.y * pixmap.height(),
                bounds.width * pixmap.width(),
                bounds.height * pixmap.height(),
            )
            fill = QColor(color)
            fill.setAlpha(round(28 * self._frame_target_opacity))
            painter.fillRect(rect, fill)
            painter.setPen(QPen(color, 3, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(rect, 5, 5)
        if self._frame_target_point is not None:
            x, y = self._frame_target_point
            point = QPointF(x * pixmap.width(), y * pixmap.height())
            outline = QColor(5, 8, 12)
            outline.setAlpha(round(255 * self._frame_target_opacity))
            painter.setPen(QPen(outline, 6))
            painter.drawEllipse(point, 9, 9)
            painter.setPen(QPen(color, 3))
            painter.drawLine(point + QPointF(-12, 0), point + QPointF(12, 0))
            painter.drawLine(point + QPointF(0, -12), point + QPointF(0, 12))


class ClickToFrameWindow(QWidget):
    """Small independent Audience view that never raises the main window."""

    visibility_changed = Signal(bool)
    tracking_toggle_requested = Signal()

    def __init__(self, context) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.context = context
        self.frame_provider = None
        self.tracking_state_provider = None
        self.setWindowTitle("Production Hub · Click to Frame")
        self.setMinimumSize(360, 240)
        self.resize(520, 340)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if hasattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow"):
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.preview = VideoPreview("Waiting for Audience Cam", aspect_ratio=None)
        self.preview.set_frame_target_mode(True)
        layout.addWidget(self.preview, 1)
        self.tracking_button = QPushButton("Enable Subject Tracking")
        self.tracking_button.clicked.connect(self.tracking_toggle_requested.emit)
        layout.addWidget(self.tracking_button)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh_frame)

    def refresh_frame(self) -> None:
        if callable(self.frame_provider):
            provided = self.frame_provider()
            if provided is not None:
                image, sequence = provided
                self.preview.set_frame(image, sequence)
            self.refresh_tracking_button()
            return
        packet = self.context.video.frame(VideoSourceKey.AUDIENCE)
        snapshot = self.context.video.snapshot(VideoSourceKey.AUDIENCE)
        if packet is not None:
            self.preview.set_frame(packet.image, packet.sequence)
        elif self.preview.sequence:
            self.preview.clear(snapshot.message or "No Audience video frame")
        self.refresh_tracking_button()

    def refresh_tracking_button(self) -> None:
        if not callable(self.tracking_state_provider):
            return
        enabled, requested = self.tracking_state_provider()
        if enabled:
            self.tracking_button.setText("Disable Subject Tracking")
            self.tracking_button.setStyleSheet(
                "QPushButton { background: #126442; color: white; font-weight: 700; }"
            )
        elif requested:
            self.tracking_button.setText("Cancel Starting Subject Tracking")
            self.tracking_button.setStyleSheet("")
        else:
            self.tracking_button.setText("Enable Subject Tracking")
            self.tracking_button.setStyleSheet("")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_frame()
        self.refresh_timer.start()
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        self.refresh_timer.stop()
        self.visibility_changed.emit(False)
        super().hideEvent(event)


class CameraControlPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.click_to_frame_window = ClickToFrameWindow(context)
        self.click_to_frame_window.visibility_changed.connect(
            lambda _visible: self._update_preview_activity()
        )
        self.click_to_frame_window.visibility_changed.connect(
            lambda visible: self._update_calibration_activity(
                click_visible=visible
            )
        )
        self._pending_subject_arm_until = 0.0
        self._syncing_automation_control = False
        self._calibration_output_buffer = ""
        self._calibration_spinner_index = 0
        self._post_calibration_lock_deadline = 0.0
        self._click_mosaic: CalibrationMosaic | None = None
        self._click_mosaic_image: QImage | None = None
        self._click_mosaic_bgr: np.ndarray | None = None
        self._click_reference_map_x: np.ndarray | None = None
        self._click_reference_map_y: np.ndarray | None = None
        self._click_reference_alpha: np.ndarray | None = None
        self._click_live_map_x: np.ndarray | None = None
        self._click_live_map_y: np.ndarray | None = None
        self._click_live_alpha: np.ndarray | None = None
        self._click_live_map_key: tuple[int, int, int] = (-1, 0, 0)
        self._click_visual_reference_to_live: tuple[
            tuple[float, float, float], ...
        ] | None = None
        self._click_visual_reference_size: tuple[int, int] = (0, 0)
        self._click_visual_live_size: tuple[int, int] = (0, 0)
        self._click_visual_alignment_sequence = -1
        self._click_geometry: PtzGeometryModel | None = None
        self._click_mosaic_building = False
        self._click_mosaic_version = 0
        self._click_composite_packet_sequence = -1
        self._click_composite_version = -1
        self._click_composite_image: QImage | None = None
        self._click_composite_last_render_monotonic = 0.0
        self._click_composite_pose_sequence = -1
        self._click_composite_relocalization_sequence = -1
        self._click_render_sequence = 0
        self._click_composite_reference_aligned = False
        self._click_current_pose: PtzMotorPose | None = None
        self._click_pose_sequence = 0
        self._click_pose_polling = False
        self._click_last_pose_poll_monotonic = 0.0
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.context.video.initialize_qt()
        self._last_recording_state = False
        self._local_devices = []
        self.build()
        self.refresh_source_options(discover_ndi=False)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh_video_status)
        self.calibration_spinner_timer = QTimer(self)
        self.calibration_spinner_timer.setInterval(140)
        self.calibration_spinner_timer.timeout.connect(self._advance_calibration_spinner)
        self.post_calibration_lock_timer = QTimer(self)
        self.post_calibration_lock_timer.setInterval(250)
        self.post_calibration_lock_timer.timeout.connect(
            self._monitor_post_calibration_lock
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "refresh_timer"):
            self._update_preview_activity()
            tracking_requested = (
                self.context.config.integrations.camera_tracking.automation.mode
                == "subject"
            )
            self.context.video.set_tracking_activity(
                tracking_requested,
                owner="camera_page",
            )
            self.context.ptz_automation.set_shadow_active(
                False,
                owner="camera_page",
            )
            self._update_calibration_activity(page_visible=True)
            self.refresh_timer.start()
            self.refresh_video_status()

    def hideEvent(self, event) -> None:
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.stop()
            self.context.video.set_tracking_activity(False, owner="camera_page")
            self.context.ptz_automation.set_shadow_active(
                False,
                owner="camera_page",
            )
            self._update_preview_activity()
            self._update_calibration_activity(page_visible=False)
        super().hideEvent(event)

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(
            title(
                "Camera Control",
                "Choose the two video inputs, calibrate once, then move the camera and turn Subject Tracking on.",
            )
        )

        layout.addWidget(self.video_group())
        # Advanced calibration, tracking, drawing, and diagnostic widgets stay
        # alive for the underlying services and review dialogs, but no longer
        # crowd the normal volunteer workflow.
        self._advanced_calibration_group = self.camera_calibration_group()
        self._advanced_tracking_group = self.tracking_group()
        self._advanced_regions_group = self.scene_regions_group()
        self._advanced_diagnostics_group = self.diagnostics_group()
        layout.addWidget(self.simple_camera_calibration_group())
        layout.addWidget(self.ptz_automation_group())
        layout.addWidget(self.click_to_frame_group())

        layout.addWidget(
            responsive_grid(
                [self.system_group(), self.ptz_group(), self.preset_group()],
                min_column_width=300,
                max_columns=3,
            )
        )
        layout.addWidget(self.status)
        layout.addStretch()

    def simple_camera_calibration_group(self) -> QGroupBox:
        group = QGroupBox("Camera Sync")
        layout = QVBoxLayout(group)
        top = QHBoxLayout()
        explanation = QLabel(
            "Run this before service if either camera has shifted. Production Hub finds and saves the structural alignment automatically."
        )
        explanation.setWordWrap(True)
        top.addWidget(explanation, 1)
        button = QPushButton("Calibrate Camera Sync…")
        button.clicked.connect(self.request_camera_calibration)
        self.simple_calibration_button = button
        top.addWidget(button)
        layout.addLayout(top)
        state_row = QHBoxLayout()
        self.simple_calibration_indicator = QLabel("✓")
        self.simple_calibration_indicator.setFixedWidth(22)
        self.simple_calibration_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.simple_calibration_status = QLabel()
        self.simple_calibration_status.setWordWrap(True)
        state_row.addWidget(self.simple_calibration_indicator)
        state_row.addWidget(self.simple_calibration_status, 1)
        layout.addLayout(state_row)
        self.simple_calibration_progress = QProgressBar()
        self.simple_calibration_progress.setRange(0, 100)
        self.simple_calibration_progress.setValue(0)
        self.simple_calibration_progress.setTextVisible(True)
        self.simple_calibration_progress.hide()
        layout.addWidget(self.simple_calibration_progress)
        self._refresh_simple_calibration_status()
        return group

    def _refresh_simple_calibration_status(self) -> None:
        if not hasattr(self, "simple_calibration_status"):
            return
        active = load_active_calibration_review(self.context.paths.root)
        if active is None:
            self.simple_calibration_indicator.setText("○")
            self.simple_calibration_status.setText(
                "No active Camera Sync is saved. Run calibration before using automated framing."
            )
            return
        self.simple_calibration_indicator.setText("✓")
        self.simple_calibration_status.setText(
            f"Active Camera Sync remembered · {active.map_path.parent.name} · "
            f"{active.marker_count} structural markers"
        )

    def video_group(self) -> QGroupBox:
        group = QGroupBox("Video Inputs")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(12)
        self.source_type_boxes: dict[VideoSourceKey, QComboBox] = {}
        self.source_selectors: dict[VideoSourceKey, QComboBox] = {}
        self.source_quality_checks: dict[VideoSourceKey, QCheckBox] = {}
        self.source_quality_labels: dict[VideoSourceKey, QWidget] = {}
        self.source_headings: dict[VideoSourceKey, QLabel] = {}
        self.source_refresh_buttons: dict[VideoSourceKey, QPushButton] = {}
        self.source_connect_buttons: dict[VideoSourceKey, QPushButton] = {}
        self.source_privacy_buttons: dict[VideoSourceKey, QPushButton] = {}

        audience = self._video_input_panel(VideoSourceKey.AUDIENCE, "Audience Cam")
        ptz = self._video_input_panel(VideoSourceKey.PTZ, "PTZ Cam")
        layout.addWidget(audience, 0, 0)
        layout.addWidget(ptz, 0, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return group

    def camera_calibration_group(self) -> QGroupBox:
        group = QGroupBox("Camera Calibration")
        layout = QVBoxLayout(group)
        explanation = QLabel(
            "Build the Audience-to-PTZ camera map, then draw operational Stage, Altar, and "
            "Podium boundaries against its reference image. Individual feature points remain "
            "available only as advanced calibration diagnostics."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self._calibration_review_data: CalibrationReviewData | None = None
        self._calibration_process: QProcess | None = None
        self._calibration_frame_sequence = 1000000
        self.calibration_summary = QLabel("Loading the latest saved camera synchronization…")
        self.calibration_summary.setWordWrap(True)
        layout.addWidget(self.calibration_summary)
        self.calibration_lock_summary = QLabel(
            "Live Audience lock is initializing. Automated camera motion remains blocked."
        )
        self.calibration_lock_summary.setWordWrap(True)
        layout.addWidget(self.calibration_lock_summary)
        self.relocalization_enabled = QCheckBox("Enable live Audience calibration lock")
        self.relocalization_enabled.setChecked(
            self.context.config.integrations.camera_tracking.relocalization_enabled
        )
        self.relocalization_enabled.setToolTip(
            "Runs curated landmark matching at a low rate on the latest Audience frame. "
            "It never sends PTZ commands."
        )
        self.relocalization_enabled.toggled.connect(
            self.update_relocalization_config
        )
        layout.addWidget(self.relocalization_enabled)
        controls = QHBoxLayout()
        review = QPushButton("Advanced Marker Diagnostics…")
        review.clicked.connect(self.open_camera_calibration_dialog)
        calibrate = QPushButton("Calibrate PTZ Camera to Audience Camera…")
        calibrate.clicked.connect(self.request_camera_calibration)
        self.calibration_group_button = calibrate
        controls.addWidget(review)
        controls.addWidget(calibrate)
        controls.addStretch()
        layout.addLayout(controls)
        self._build_camera_calibration_dialog()
        self.reload_camera_calibration_review()
        return group

    def _build_camera_calibration_dialog(self) -> None:
        self.camera_calibration_dialog = QDialog(self)
        self.camera_calibration_dialog.setWindowTitle(
            "Production Hub · Camera Tracking Marker Review"
        )
        self.camera_calibration_dialog.setModal(False)
        self.camera_calibration_dialog.resize(1500, 900)
        self.camera_calibration_dialog.setMinimumSize(1040, 680)
        root = QVBoxLayout(self.camera_calibration_dialog)
        heading = QLabel("Camera Tracking Markers · Audience ↔ PTZ")
        heading.setObjectName("PageTitle")
        root.addWidget(heading)
        explanation = QLabel(
            "Each marker is detected automatically in the Audience reference image and "
            "mapped to the selected PTZ motor pose. Select a point in either image or the "
            "list to highlight the same physical feature in both views."
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("PTZ pose"))
        self.calibration_pose_selector = QComboBox()
        self.calibration_pose_selector.currentIndexChanged.connect(
            self._calibration_pose_changed
        )
        toolbar.addWidget(self.calibration_pose_selector, 1)
        self.calibration_show_all_markers = QCheckBox("Show all markers")
        self.calibration_show_all_markers.setChecked(True)
        self.calibration_show_all_markers.toggled.connect(
            lambda _checked: self._refresh_calibration_marker_overlays()
        )
        toolbar.addWidget(self.calibration_show_all_markers)
        self.calibration_use_live_images = QCheckBox("Use live camera images")
        self.calibration_use_live_images.setToolTip(
            "Only use live images while the PTZ is at the selected calibrated motor pose."
        )
        self.calibration_use_live_images.toggled.connect(
            lambda _checked: (
                self._update_calibration_review_images(),
                self._refresh_calibration_marker_overlays(),
            )
        )
        toolbar.addWidget(self.calibration_use_live_images)
        root.addLayout(toolbar)

        self.calibration_review_summary = QLabel("No calibration loaded")
        self.calibration_review_summary.setWordWrap(True)
        root.addWidget(self.calibration_review_summary)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        views = QSplitter(Qt.Orientation.Horizontal)
        audience_panel = QWidget()
        audience_layout = QVBoxLayout(audience_panel)
        audience_layout.setContentsMargins(0, 0, 4, 0)
        audience_heading = QLabel("Audience Cam · reference points")
        audience_heading.setObjectName("CardTitle")
        audience_layout.addWidget(audience_heading)
        self.calibration_audience_preview = VideoPreview(
            "No saved Audience calibration image"
        )
        self.calibration_audience_preview.setMinimumSize(440, 248)
        audience_layout.addWidget(self.calibration_audience_preview, 1)
        views.addWidget(audience_panel)

        ptz_panel = QWidget()
        ptz_layout = QVBoxLayout(ptz_panel)
        ptz_layout.setContentsMargins(4, 0, 0, 0)
        self.calibration_ptz_heading = QLabel("PTZ Cam · mapped points")
        self.calibration_ptz_heading.setObjectName("CardTitle")
        ptz_layout.addWidget(self.calibration_ptz_heading)
        self.calibration_ptz_preview = VideoPreview("No saved PTZ calibration image")
        self.calibration_ptz_preview.setMinimumSize(440, 248)
        ptz_layout.addWidget(self.calibration_ptz_preview, 1)
        views.addWidget(ptz_panel)
        views.setSizes([570, 570])
        splitter.addWidget(views)

        marker_panel = QWidget()
        marker_layout = QVBoxLayout(marker_panel)
        marker_layout.setContentsMargins(8, 0, 0, 0)
        marker_heading = QLabel("Mapped Tracking Points")
        marker_heading.setObjectName("CardTitle")
        marker_layout.addWidget(marker_heading)
        self.calibration_marker_list = QListWidget()
        self.calibration_marker_list.currentItemChanged.connect(
            self._calibration_marker_selection_changed
        )
        marker_layout.addWidget(self.calibration_marker_list, 1)
        curation = QGridLayout()
        self.calibration_exclude_button = QPushButton("Exclude Selected")
        self.calibration_exclude_button.clicked.connect(
            self.exclude_selected_calibration_marker
        )
        self.calibration_restore_button = QPushButton("Restore Excluded…")
        self.calibration_restore_button.clicked.connect(
            self.restore_excluded_calibration_marker
        )
        self.calibration_approve_button = QPushButton("Approve and Activate")
        self.calibration_approve_button.clicked.connect(
            self.approve_and_activate_calibration
        )
        self.calibration_rollback_button = QPushButton("Roll Back Active")
        self.calibration_rollback_button.clicked.connect(
            self.rollback_active_calibration
        )
        curation.addWidget(self.calibration_exclude_button, 0, 0)
        curation.addWidget(self.calibration_restore_button, 0, 1)
        curation.addWidget(self.calibration_approve_button, 1, 0)
        curation.addWidget(self.calibration_rollback_button, 1, 1)
        marker_layout.addLayout(curation)
        reload_button = QPushButton("Reload Saved Calibration")
        reload_button.clicked.connect(self.reload_camera_calibration_review)
        reveal_button = QPushButton("Show Calibration Files")
        reveal_button.clicked.connect(self.reveal_camera_calibration)
        marker_layout.addWidget(reload_button)
        marker_layout.addWidget(reveal_button)
        splitter.addWidget(marker_panel)
        splitter.setSizes([1180, 280])
        splitter.setStretchFactor(0, 1)
        root.addWidget(splitter, 1)

        self.calibration_process_status = QLabel(
            "Calibration is idle. Saved images remain reviewable without live cameras."
        )
        self.calibration_process_status.setWordWrap(True)
        root.addWidget(self.calibration_process_status)
        self.calibration_process_log = QTextEdit()
        self.calibration_process_log.setReadOnly(True)
        self.calibration_process_log.setMaximumHeight(120)
        self.calibration_process_log.setPlaceholderText(
            "Calibration progress will appear here."
        )
        root.addWidget(self.calibration_process_log)

        actions = QHBoxLayout()
        self.calibration_dialog_run_button = QPushButton(
            "Calibrate PTZ Camera to Audience Camera…"
        )
        self.calibration_dialog_run_button.clicked.connect(
            self.request_camera_calibration
        )
        close = QPushButton("Close Review")
        close.clicked.connect(self.camera_calibration_dialog.close)
        actions.addWidget(self.calibration_dialog_run_button)
        actions.addStretch()
        actions.addWidget(close)
        root.addLayout(actions)

        self.calibration_audience_preview.calibration_marker_clicked.connect(
            self.select_calibration_marker
        )
        self.calibration_ptz_preview.calibration_marker_clicked.connect(
            self.select_calibration_marker
        )
        self.camera_calibration_dialog.finished.connect(
            lambda _result: self._update_calibration_activity()
        )

    def open_camera_calibration_dialog(self) -> None:
        self.reload_camera_calibration_review()
        self.camera_calibration_dialog.show()
        self.camera_calibration_dialog.raise_()
        self.camera_calibration_dialog.activateWindow()
        self._update_calibration_activity()

    def reload_camera_calibration_review(self) -> None:
        review = load_latest_calibration_review(self.context.paths.root)
        self._calibration_review_data = review
        self._refresh_simple_calibration_status()
        self.calibration_pose_selector.blockSignals(True)
        self.calibration_pose_selector.clear()
        if review is not None:
            for pose in review.poses:
                self.calibration_pose_selector.addItem(
                    f"{pose.index:02d} · {pose.name.replace('-', ' ').title()}",
                    pose.index,
                )
        self.calibration_pose_selector.blockSignals(False)
        if review is None:
            self.calibration_summary.setText(
                "No accepted camera synchronization is saved yet. Calibration will remain "
                "blocked until both live feeds have sufficient light and visible detail."
            )
            self.calibration_review_summary.setText("No accepted calibration is available.")
            self.calibration_marker_list.clear()
            self.calibration_audience_preview.clear("No saved Audience calibration image")
            self.calibration_ptz_preview.clear("No saved PTZ calibration image")
            self.calibration_exclude_button.setEnabled(False)
            self.calibration_restore_button.setEnabled(False)
            self.calibration_approve_button.setEnabled(False)
            self.calibration_rollback_button.setEnabled(False)
            return
        approval = review.approval_status.replace("_", " ").title()
        self.calibration_summary.setText(
            f"Accepted synchronization · {len(review.poses)} PTZ pose(s) · "
            f"{review.marker_count} enabled / {review.total_marker_count} total tracking point(s) · "
            f"{approval}."
        )
        curatable = review.map_path.name == "full_sync.json"
        self.calibration_exclude_button.setEnabled(
            curatable and review.marker_count > 24
        )
        self.calibration_restore_button.setEnabled(
            curatable and bool(review.excluded_markers)
        )
        self.calibration_approve_button.setEnabled(
            curatable and review.approval_status != "approved"
        )
        self.calibration_rollback_button.setEnabled(True)
        self._calibration_pose_changed()

    def _selected_calibration_pose(self) -> CalibrationReviewPose | None:
        review = self._calibration_review_data
        selected_index = self.calibration_pose_selector.currentIndex()
        if review is None or not 0 <= selected_index < len(review.poses):
            return None
        return review.poses[selected_index]

    def _calibration_pose_changed(self, _index: int = -1) -> None:
        pose = self._selected_calibration_pose()
        review = self._calibration_review_data
        if pose is None or review is None:
            return
        selected_marker = self.selected_calibration_marker_id()
        self.calibration_marker_list.blockSignals(True)
        self.calibration_marker_list.clear()
        for marker in pose.markers:
            measurement = (
                f"local correction {marker.reference_error_pixels:.2f}px"
                if marker.stability == "guided_structural_match"
                else f"error {marker.reference_error_pixels:.2f}px"
            )
            item = QListWidgetItem(
                f"M{marker.marker_id:03d} · observed ×{marker.repeatability} · "
                f"{measurement}"
            )
            item.setData(Qt.ItemDataRole.UserRole, marker.marker_id)
            item.setToolTip(
                f"Audience ({marker.audience_x:.4f}, {marker.audience_y:.4f})\n"
                f"PTZ ({marker.ptz_x:.4f}, {marker.ptz_y:.4f})\n"
                f"{marker.stability or 'calibrated'} · structure {marker.structure_score:.3f}"
            )
            self.calibration_marker_list.addItem(item)
            if marker.marker_id == selected_marker:
                self.calibration_marker_list.setCurrentItem(item)
        if self.calibration_marker_list.currentItem() is None and self.calibration_marker_list.count():
            self.calibration_marker_list.setCurrentRow(0)
        self.calibration_marker_list.blockSignals(False)
        motor = pose.motor_position
        motor_text = ""
        if all(key in motor for key in ("pan", "tilt", "zoom")):
            motor_text = (
                f" · motor {int(motor['pan']):04X} / {int(motor['tilt']):04X} / "
                f"{int(motor['zoom']):03X}"
            )
        self.calibration_review_summary.setText(
            f"{review.approval_status.replace('_', ' ').title()} · "
            f"{review.status.title()} map · {len(pose.markers)} mapped points · "
            f"PTZ link {pose.link_inliers} inliers at {pose.link_error_pixels:.2f}px"
            f"{motor_text} · {review.map_path}"
        )
        self.calibration_ptz_heading.setText(
            f"PTZ Cam · {pose.name.replace('-', ' ').title()} mapped points"
        )
        self._update_calibration_review_images()
        self._refresh_calibration_marker_overlays()

    def _update_calibration_review_images(self) -> None:
        pose = self._selected_calibration_pose()
        review = self._calibration_review_data
        if pose is None or review is None:
            return
        use_live = self.calibration_use_live_images.isChecked()
        audience_image = self.audience_preview.image if use_live else QImage(
            str(review.audience_image_path)
        )
        ptz_image = self.ptz_preview.image if use_live else QImage(str(pose.image_path))
        self._calibration_frame_sequence += 1
        if audience_image is not None and not audience_image.isNull():
            self.calibration_audience_preview.set_frame(
                audience_image,
                self._calibration_frame_sequence,
            )
        else:
            self.calibration_audience_preview.clear(
                "Live Audience frame unavailable" if use_live else "Saved Audience image missing"
            )
        self._calibration_frame_sequence += 1
        if ptz_image is not None and not ptz_image.isNull():
            self.calibration_ptz_preview.set_frame(
                ptz_image,
                self._calibration_frame_sequence,
            )
        else:
            self.calibration_ptz_preview.clear(
                "Live PTZ frame unavailable" if use_live else "Saved PTZ image missing"
            )

    def selected_calibration_marker_id(self) -> int:
        item = self.calibration_marker_list.currentItem()
        return int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else 0

    def _calibration_marker_selection_changed(self, _current=None, _previous=None) -> None:
        self._refresh_calibration_marker_overlays()

    def _refresh_calibration_marker_overlays(self) -> None:
        pose = self._selected_calibration_pose()
        review = self._calibration_review_data
        if pose is None or review is None:
            return
        selected = self.selected_calibration_marker_id()
        audience_markers = review.audience_markers
        pose_markers = pose.markers
        if not self.calibration_show_all_markers.isChecked() and selected:
            audience_markers = tuple(
                item for item in audience_markers if item.marker_id == selected
            )
            pose_markers = tuple(
                item for item in pose_markers if item.marker_id == selected
            )
        if self.calibration_use_live_images.isChecked():
            relocated = {
                item.marker_id: item
                for item in self.context.video.relocalization.snapshot().marker_positions
            }
            audience_overlay = tuple(
                (item.marker_id, relocated[item.marker_id].x, relocated[item.marker_id].y)
                for item in audience_markers
                if item.marker_id in relocated
            )
        else:
            audience_overlay = tuple(
                (item.marker_id, item.audience_x, item.audience_y)
                for item in audience_markers
            )
        self.calibration_audience_preview.set_calibration_markers(
            audience_overlay,
            selected_marker_id=selected,
        )
        self.calibration_ptz_preview.set_calibration_markers(
            tuple((item.marker_id, item.ptz_x, item.ptz_y) for item in pose_markers),
            selected_marker_id=selected,
        )

    def select_calibration_marker(self, marker_id: int) -> None:
        for index in range(self.calibration_marker_list.count()):
            item = self.calibration_marker_list.item(index)
            if int(item.data(Qt.ItemDataRole.UserRole)) == int(marker_id):
                self.calibration_marker_list.setCurrentItem(item)
                self.calibration_marker_list.scrollToItem(item)
                return
        review = self._calibration_review_data
        if review is None:
            return
        for pose_index, pose in enumerate(review.poses):
            if any(item.marker_id == int(marker_id) for item in pose.markers):
                self.calibration_pose_selector.setCurrentIndex(pose_index)
                for index in range(self.calibration_marker_list.count()):
                    item = self.calibration_marker_list.item(index)
                    if int(item.data(Qt.ItemDataRole.UserRole)) == int(marker_id):
                        self.calibration_marker_list.setCurrentItem(item)
                        self.calibration_marker_list.scrollToItem(item)
                        return

    def exclude_selected_calibration_marker(self) -> None:
        review = self._calibration_review_data
        marker_id = self.selected_calibration_marker_id()
        if review is None or marker_id <= 0:
            self.status.setText("Select a calibration marker to exclude first.")
            return
        try:
            CalibrationRegistry(self.context.paths.root).exclude_marker(
                review.map_path,
                marker_id,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Marker Could Not Be Excluded", str(exc))
            return
        self.context.video.relocalization.reload_calibration()
        self.reload_camera_calibration_review()
        self.status.setText(
            f"Excluded marker M{marker_id:03d}. Re-approve the calibration before live use."
        )

    def restore_excluded_calibration_marker(self) -> None:
        review = self._calibration_review_data
        if review is None or not review.excluded_markers:
            self.status.setText("This calibration has no excluded markers.")
            return
        labels = [f"M{item.marker_id:03d}" for item in review.excluded_markers]
        selected, ok = QInputDialog.getItem(
            self,
            "Restore Calibration Marker",
            "Excluded marker:",
            labels,
            0,
            False,
        )
        if not ok:
            return
        marker_id = int(str(selected).removeprefix("M"))
        try:
            CalibrationRegistry(self.context.paths.root).restore_marker(
                review.map_path,
                marker_id,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Marker Could Not Be Restored", str(exc))
            return
        self.context.video.relocalization.reload_calibration()
        self.reload_camera_calibration_review()
        self.status.setText(
            f"Restored marker M{marker_id:03d}. Re-approve the calibration before live use."
        )

    def approve_and_activate_calibration(self) -> None:
        review = self._calibration_review_data
        if review is None:
            return
        confirmation = QMessageBox.question(
            self,
            "Approve Camera Calibration?",
            "Approve the enabled markers and make this the active runtime calibration?\n\n"
            "Automated PTZ motion will remain disabled.",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        try:
            CalibrationRegistry(self.context.paths.root).approve_and_activate(
                review.map_path
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Calibration Could Not Be Approved", str(exc))
            return
        self.context.video.relocalization.reload_calibration()
        self.reload_camera_calibration_review()
        self.reload_click_to_frame_mosaic()
        self.status.setText("Calibration approved and activated. PTZ motion remains disabled.")

    def rollback_active_calibration(self) -> None:
        confirmation = QMessageBox.question(
            self,
            "Roll Back Active Calibration?",
            "Restore the previously approved calibration map?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        try:
            path = CalibrationRegistry(self.context.paths.root).rollback()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Calibration Could Not Be Restored", str(exc))
            return
        self.context.video.relocalization.reload_calibration()
        self.reload_camera_calibration_review()
        self.reload_click_to_frame_mosaic()
        self.status.setText(f"Restored active calibration: {path.parent.name}")

    def reveal_camera_calibration(self) -> None:
        review = self._calibration_review_data
        directory = review.map_path.parent if review is not None else self.context.paths.root
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def request_camera_calibration(self) -> None:
        self.context.ptz_automation.disarm("Camera calibration was requested")
        if self._calibration_process is not None and self._calibration_process.state() != QProcess.ProcessState.NotRunning:
            self.calibration_process_status.setText("Camera calibration is already running.")
            self.status.setText("Camera Sync calibration is already running.")
            return
        audience_light = assess_camera_light(self.audience_preview.image)
        ptz_light = assess_camera_light(self.ptz_preview.image)
        light_message = (
            f"Audience: {audience_light.message}\nPTZ: {ptz_light.message}"
        )
        self.calibration_process_status.setText(light_message.replace("\n", " · "))
        if not audience_light.acceptable or not ptz_light.acceptable:
            QMessageBox.warning(
                self,
                "Calibration Blocked by Camera Light Check",
                "Production Hub will not move the PTZ because one or both live feeds do "
                f"not contain enough illuminated detail.\n\n{light_message}",
            )
            self.status.setText(light_message.replace("\n", " · "))
            return
        answer = QMessageBox.question(
            self,
            "Calibrate PTZ Camera to Audience Camera",
            "This routine will physically move the PTZ through eleven overlapping "
            "structural-coverage poses, including the stage, walls, aisle, and fixed pews. "
            "It will calculate the camera alignment and return to its starting position.\n\n"
            "Confirm the room and camera movement area are clear. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_camera_calibration_process()

    def _start_camera_calibration_process(self) -> None:
        app_root = Path(__file__).resolve().parents[3]
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if getattr(sys, "frozen", False):
            program = sys.executable
            arguments = ["--calibrate-ptz-to-audience"]
        else:
            program = sys.executable
            arguments = [str(app_root / "main.py"), "--calibrate-ptz-to-audience"]
        arguments.extend(
            [
                "--data-dir",
                str(self.context.paths.root),
                "--confirm-movement",
            ]
        )
        process.setProgram(program)
        process.setArguments(arguments)
        process.setWorkingDirectory(str(app_root))
        process.readyReadStandardOutput.connect(self._read_camera_calibration_output)
        process.finished.connect(self._camera_calibration_finished)
        process.errorOccurred.connect(self._camera_calibration_process_error)
        self._calibration_process = process
        self._calibration_output_buffer = ""
        self.calibration_process_log.clear()
        self.calibration_process_status.setText(
            "Calibration running · do not close Production Hub or operate the PTZ controller."
        )
        self.calibration_group_button.setEnabled(False)
        self.calibration_dialog_run_button.setEnabled(False)
        self.simple_calibration_button.setEnabled(False)
        self.simple_calibration_button.setText("Calibrating…")
        self.simple_calibration_progress.setValue(1)
        self.simple_calibration_progress.setFormat("Starting Camera Sync…")
        self.simple_calibration_progress.show()
        self.simple_calibration_indicator.setText("◐")
        self.simple_calibration_status.setText(
            "Starting calibration · preparing the two live camera feeds"
        )
        self.calibration_spinner_timer.start()
        self.status.setText(
            "Camera Sync calibration is running. Do not operate the PTZ controller."
        )
        process.start()
        self._update_calibration_activity()

    def _read_camera_calibration_output(self) -> None:
        process = self._calibration_process
        if process is None:
            return
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if output:
            self.calibration_process_log.moveCursor(QTextCursor.MoveOperation.End)
            self.calibration_process_log.insertPlainText(output)
            self.calibration_process_log.ensureCursorVisible()
            self._update_simple_calibration_progress(output)

    def _update_simple_calibration_progress(self, output: str) -> None:
        combined = self._calibration_output_buffer + output
        lines = combined.splitlines(keepends=True)
        self._calibration_output_buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._calibration_output_buffer = lines.pop()
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("Step 1/4"):
                self._set_simple_calibration_progress(
                    5,
                    "Capturing the Audience and PTZ reference views…",
                )
            elif line.startswith("Step 2/4"):
                self._set_simple_calibration_progress(
                    18,
                    "Mapping the room with the guarded PTZ sweep…",
                )
            elif match := re.match(r"\[(\d+)/(\d+)\]\s+Moving to\s+([^:]+)", line):
                current = int(match.group(1))
                total = max(1, int(match.group(2)))
                progress = 18 + round(57 * current / total)
                pose = match.group(3).replace("-", " ").title()
                self._set_simple_calibration_progress(
                    progress,
                    f"Mapping position {current} of {total} · {pose}",
                )
            elif line.startswith("Returning PTZ"):
                self._set_simple_calibration_progress(
                    78,
                    "Returning the PTZ to its original position…",
                )
            elif line.startswith("Step 3/4"):
                self._set_simple_calibration_progress(
                    84,
                    "Building the full camera synchronization map…",
                )
            elif line.startswith("Step 4/4"):
                self._set_simple_calibration_progress(
                    94,
                    "Saving and activating Camera Sync…",
                )
            elif line.startswith("Calibration complete and active"):
                self._set_simple_calibration_progress(
                    97,
                    "Calibration saved · establishing the live Audience lock…",
                )

    def _set_simple_calibration_progress(self, value: int, message: str) -> None:
        progress = max(0, min(100, int(value)))
        self.simple_calibration_progress.setValue(progress)
        self.simple_calibration_progress.setFormat(f"{progress}% · {message}")
        self.simple_calibration_status.setText(message)

    def _advance_calibration_spinner(self) -> None:
        frames = ("◐", "◓", "◑", "◒")
        self.simple_calibration_indicator.setText(
            frames[self._calibration_spinner_index % len(frames)]
        )
        self._calibration_spinner_index += 1

    def _camera_calibration_process_error(self, error) -> None:
        self.calibration_group_button.setEnabled(True)
        self.calibration_dialog_run_button.setEnabled(True)
        self.simple_calibration_button.setEnabled(True)
        self.simple_calibration_button.setText("Calibrate Camera Sync…")
        self.calibration_spinner_timer.stop()
        self.simple_calibration_indicator.setText("⚠")
        self.simple_calibration_progress.setFormat("Calibration could not continue")
        self.simple_calibration_status.setText(
            f"Camera Sync could not continue: {error}"
        )
        self.calibration_process_status.setText(
            f"Could not start or continue calibration: {error}. No new PTZ movement "
            "will be requested by this process."
        )
        self._update_calibration_activity()

    def _camera_calibration_finished(self, exit_code: int, _exit_status) -> None:
        self._read_camera_calibration_output()
        self.calibration_group_button.setEnabled(True)
        self.calibration_dialog_run_button.setEnabled(True)
        self.simple_calibration_button.setEnabled(True)
        self.simple_calibration_button.setText("Calibrate Camera Sync…")
        if int(exit_code) == 0:
            self.calibration_process_status.setText(
                "Calibration complete and PTZ return verified. Loading the new marker map…"
            )
            latest = load_latest_calibration_review(self.context.paths.root)
            if latest is not None:
                try:
                    CalibrationRegistry(
                        self.context.paths.root
                    ).approve_and_activate(latest.map_path)
                except (OSError, TypeError, ValueError) as exc:
                    self._finish_calibration_with_error(
                        f"Calibration was created but could not be activated: {exc}"
                    )
                    self._update_calibration_activity()
                    return
            self.context.video.relocalization.reload_calibration()
            self.reload_click_to_frame_mosaic()
            self.context.video.set_calibration_activity(
                True,
                owner="post_calibration_lock",
            )
            self._post_calibration_lock_deadline = time.monotonic() + 20.0
            self.post_calibration_lock_timer.start()
            self.reload_camera_calibration_review()
            self._set_simple_calibration_progress(
                97,
                "Calibration saved · establishing the live Audience lock…",
            )
            self.status.setText(
                "Camera Sync calibration completed. Establishing live lock…"
            )
        else:
            self.calibration_process_status.setText(
                f"Calibration stopped with exit code {exit_code}. Review the log; the sweep "
                "attempts camera restoration in every failure path."
            )
            self.status.setText(
                f"Camera Sync calibration stopped with exit code {exit_code}."
            )
            self._finish_calibration_with_error(
                f"Calibration stopped with exit code {exit_code}. The previous Camera Sync remains active."
            )
        self._update_calibration_activity()

    def _finish_calibration_with_error(self, message: str) -> None:
        self.calibration_spinner_timer.stop()
        self.post_calibration_lock_timer.stop()
        self.simple_calibration_indicator.setText("⚠")
        self.simple_calibration_progress.setFormat("Calibration incomplete")
        self.simple_calibration_status.setText(message)

    def _monitor_post_calibration_lock(self) -> None:
        snapshot = self.context.video.relocalization.snapshot()
        if snapshot.motion_safe:
            self.post_calibration_lock_timer.stop()
            self.calibration_spinner_timer.stop()
            self.simple_calibration_indicator.setText("✓")
            self._set_simple_calibration_progress(
                100,
                "Camera Sync locked and ready for Subject Tracking and Click to Frame",
            )
            self.status.setText("Camera Sync is locked and ready.")
            QTimer.singleShot(5000, self._release_post_calibration_lock)
            return
        if time.monotonic() >= self._post_calibration_lock_deadline:
            self.post_calibration_lock_timer.stop()
            self.calibration_spinner_timer.stop()
            self.simple_calibration_indicator.setText("✓")
            self.simple_calibration_progress.setValue(100)
            self.simple_calibration_progress.setFormat("Calibration saved")
            self.simple_calibration_status.setText(
                "Camera Sync is saved. Live lock will retry automatically when framing is used."
            )
            self._release_post_calibration_lock()

    def _release_post_calibration_lock(self) -> None:
        self.context.video.set_calibration_activity(
            False,
            owner="post_calibration_lock",
        )

    def _video_input_panel(self, source: VideoSourceKey, display_name: str) -> QWidget:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(display_name)
        heading.setObjectName("CardTitle")
        panel_layout.addWidget(heading)
        self.source_headings[source] = heading

        preview = VideoPreview(f"Select a source for {display_name}")
        detail = QLabel("Not connected")
        detail.setWordWrap(True)
        panel_layout.addWidget(preview)
        panel_layout.addWidget(detail)
        if source == VideoSourceKey.AUDIENCE:
            self.audience_preview = preview
            self.audience_detail = detail
        else:
            self.ptz_preview = preview
            self.ptz_detail = detail
        preview.subject_clicked.connect(
            lambda track_id, key=source: self.toggle_tracked_subject(key, track_id)
        )

        config = self.context.config.integrations.video
        selected_type = self.context.video.source_type(source)
        source_type = QComboBox()
        source_type.addItem("NDI", "ndi")
        source_type.addItem("Local camera", "local")
        source_type.setCurrentIndex(max(0, source_type.findData(selected_type)))
        self.source_type_boxes[source] = source_type

        selector = QComboBox()
        self.source_selectors[source] = selector
        quality = QCheckBox("Full-bandwidth video")
        quality.setChecked(
            config.audience_highest_bandwidth
            if source == VideoSourceKey.AUDIENCE
            else config.ptz_highest_bandwidth
        )
        self.source_quality_checks[source] = quality

        form = QFormLayout()
        form.addRow("Input type", source_type)
        form.addRow("Source", selector)
        form.addRow("Quality", quality)
        quality_label = form.labelForField(quality)
        assert quality_label is not None
        self.source_quality_labels[source] = quality_label
        panel_layout.addLayout(form)

        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh Sources")
        privacy = QPushButton("Camera Privacy…")
        disconnect = QPushButton("Disconnect")
        connect = QPushButton("Connect")
        refresh.clicked.connect(self.refresh_source_options)
        privacy.clicked.connect(self.open_camera_privacy_settings)
        disconnect.clicked.connect(lambda _checked=False, key=source: self.context.video.stop_source(key))
        connect.clicked.connect(lambda _checked=False, key=source: self.connect_video_source(key))
        self.source_refresh_buttons[source] = refresh
        self.source_connect_buttons[source] = connect
        self.source_privacy_buttons[source] = privacy
        buttons.addWidget(refresh)
        buttons.addWidget(privacy)
        buttons.addStretch()
        buttons.addWidget(disconnect)
        buttons.addWidget(connect)
        panel_layout.addLayout(buttons)
        source_type.currentIndexChanged.connect(
            lambda _index, key=source: self.source_type_changed(key)
        )
        self.source_type_changed(source)
        return panel

    def tracking_group(self) -> QGroupBox:
        group = QGroupBox("Stage-Aware Person Detection")
        layout = QVBoxLayout(group)
        explanation = QLabel(
            "Apple Vision detects and associates people. Audience detections are admitted only "
            "inside enabled Stage, Front Stage, Altar, or Podium drawings. Click a subject box "
            "to select or deselect it; orange boxes are selected."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        config = self.context.config.integrations.camera_tracking
        controls = QHBoxLayout()
        self.tracking_enabled = QCheckBox("Enable person detection")
        self.tracking_enabled.setChecked(config.enabled)
        self.tracking_audience = QCheckBox("Analyze Audience")
        self.tracking_audience.setChecked(config.analyze_audience)
        self.tracking_ptz = QCheckBox("Analyze PTZ")
        self.tracking_ptz.setChecked(config.analyze_ptz)
        self.tracking_rate = QComboBox()
        for rate in (2.0, 4.0, 6.0, 8.0):
            self.tracking_rate.addItem(f"{rate:g} fps/source", rate)
        rate_index = self.tracking_rate.findData(float(config.analysis_fps))
        if rate_index < 0:
            self.tracking_rate.addItem(
                f"{config.analysis_fps:g} fps/source",
                float(config.analysis_fps),
            )
            rate_index = self.tracking_rate.count() - 1
        self.tracking_rate.setCurrentIndex(rate_index)
        controls.addWidget(self.tracking_enabled)
        controls.addWidget(self.tracking_audience)
        controls.addWidget(self.tracking_ptz)
        controls.addWidget(QLabel("Analysis rate"))
        controls.addWidget(self.tracking_rate)
        controls.addStretch()
        layout.addLayout(controls)

        selection = QHBoxLayout()
        self.select_all_subject_buttons: dict[VideoSourceKey, QPushButton] = {}
        for label, source in (
            ("Select All Detected · Audience", VideoSourceKey.AUDIENCE),
            ("Select All Detected · PTZ", VideoSourceKey.PTZ),
        ):
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, key=source: self.select_all_tracked_subjects(key)
            )
            self.select_all_subject_buttons[source] = button
            selection.addWidget(button)
        clear = QPushButton("Clear Subject Selection")
        clear.clicked.connect(self.clear_tracked_subjects)
        selection.addWidget(clear)
        selection.addStretch()
        layout.addLayout(selection)

        self.tracking_summary = QLabel("Person detection is disabled")
        self.tracking_summary.setWordWrap(True)
        layout.addWidget(self.tracking_summary)
        self.tracking_enabled.toggled.connect(self.update_tracking_config)
        self.tracking_audience.toggled.connect(self.update_tracking_config)
        self.tracking_ptz.toggled.connect(self.update_tracking_config)
        self.tracking_rate.currentIndexChanged.connect(self.update_tracking_config)
        return group

    def ptz_automation_group(self) -> QGroupBox:
        group = QGroupBox("Automation")
        layout = QVBoxLayout(group)
        explanation = QLabel(
            "Move and zoom the PTZ until the intended speaker or group is in frame, then turn Subject Tracking on. The Tenveo Auto Focus button toggles tracking; other manual PTZ, lens, preset, VISCA, or click-to-frame commands turn it off."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Mode"))
        self.automation_mode = QComboBox()
        self.automation_mode.addItem("Off", "off")
        self.automation_mode.addItem("Subject Tracking", "subject")
        controls.addWidget(self.automation_mode)
        controls.addStretch()
        layout.addLayout(controls)
        self.automation_summary = QLabel("Off · manual camera control")
        self.automation_summary.setWordWrap(True)
        layout.addWidget(self.automation_summary)
        self.automation_mode.currentIndexChanged.connect(self._automation_mode_changed)
        self._sync_automation_control()
        return group

    def _automation_mode_changed(self, _value=None) -> None:
        if self._syncing_automation_control:
            return
        self.set_subject_tracking_enabled(
            str(self.automation_mode.currentData() or "off") == "subject"
        )

    def subject_tracking_enabled(self) -> bool:
        return bool(
            self.context.ptz_automation.armed
            and self.context.config.integrations.camera_tracking.automation.mode == "subject"
        )

    def subject_tracking_requested(self) -> bool:
        return self.subject_tracking_enabled() or self._pending_subject_arm_until > 0.0

    def set_subject_tracking_enabled(
        self,
        enabled: bool,
        *,
        show_errors: bool = True,
    ) -> tuple[bool, str]:
        enabled = bool(enabled)
        if not enabled:
            self._pending_subject_arm_until = 0.0
            self.context.ptz_automation.disarm("Subject Tracking was turned off")
            self.context.video.set_tracking_activity(False, owner="subject_toggle")
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        config.enabled = True
        config.analyze_ptz = True
        config.analyze_audience = True
        config.automation.mode = "subject" if enabled else "off"
        config.automation.podium_zoom_enabled = True
        config.__post_init__()
        self._save_tracking_config(config)
        self.context.ptz_automation.set_shadow_active(False, owner="camera_page")
        if not enabled:
            self.status.setText("Subject Tracking is off. Manual camera control is available.")
            self._sync_automation_control()
            return True, "Subject Tracking is off"

        # Start analysis before attempting to arm. If frames are not analyzed
        # yet, this temporary owner remains active during the bounded retry.
        self.context.video.set_tracking_activity(True, owner="subject_toggle")
        ok, message = self.context.ptz_automation.arm()
        if ok:
            self._pending_subject_arm_until = 0.0
            self.context.video.set_tracking_activity(False, owner="subject_toggle")
            self.status.setText("Subject Tracking is on.")
            self._sync_automation_control()
            return True, message
        if self._subject_start_retryable(message):
            self._pending_subject_arm_until = time.monotonic() + 6.0
            self.status.setText(
                "Starting person detection… Subject Tracking will turn on automatically."
            )
            self._sync_automation_control(requested=True)
            return True, "Subject Tracking is starting"

        self._pending_subject_arm_until = 0.0
        self.context.video.set_tracking_activity(False, owner="subject_toggle")
        config.automation.mode = "off"
        self._save_tracking_config(config)
        self.status.setText(message)
        self._sync_automation_control()
        if show_errors:
            QMessageBox.warning(self, "Subject Tracking Could Not Start", message)
        return False, message

    def continue_pending_subject_arm(self) -> None:
        if self._pending_subject_arm_until <= 0.0:
            return
        if time.monotonic() > self._pending_subject_arm_until:
            self._pending_subject_arm_until = 0.0
            self.set_subject_tracking_enabled(False, show_errors=False)
            self.status.setText(
                "Subject Tracking could not start because camera analysis was not ready."
            )
            return
        ok, message = self.context.ptz_automation.arm()
        if ok:
            self._pending_subject_arm_until = 0.0
            self.context.video.set_tracking_activity(False, owner="subject_toggle")
            self.status.setText("Subject Tracking is on.")
        elif not self._subject_start_retryable(message):
            self._pending_subject_arm_until = 0.0
            self.context.video.set_tracking_activity(False, owner="subject_toggle")
            self.set_subject_tracking_enabled(False, show_errors=False)
            self.status.setText(message)

    @staticmethod
    def _subject_start_retryable(message: str) -> bool:
        return any(
            text in message
            for text in (
                "fresh analysis",
                "calibration is not locked",
                "does not have a fresh frame",
            )
        )

    def _sync_automation_control(self, *, requested: bool = False) -> None:
        if not hasattr(self, "automation_mode"):
            return
        active = self.subject_tracking_enabled()
        wants_tracking = requested or self._pending_subject_arm_until > 0.0 or active
        self._syncing_automation_control = True
        self.automation_mode.setCurrentIndex(1 if wants_tracking else 0)
        self._syncing_automation_control = False

    def update_ptz_automation_config(self, _value=None) -> None:
        self._automation_mode_changed(_value)

    def arm_ptz_automation(self) -> None:
        self.set_subject_tracking_enabled(True)

    def disarm_ptz_automation(self) -> None:
        self.set_subject_tracking_enabled(False)

    def click_to_frame_group(self) -> QGroupBox:
        group = QGroupBox("Click to Frame")
        layout = QVBoxLayout(group)
        explanation = QLabel(
            "The live Audience image is locally aligned to a clean PTZ panorama using Camera Sync "
            "structure. Click anywhere to move the PTZ while preserving zoom, "
            "or drag a box to move and zoom so it fills the shot. The light area is the current PTZ view."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.click_to_frame_preview = VideoPreview(
            "Connect Audience Cam to use click-to-frame",
            aspect_ratio=None,
        )
        self.click_to_frame_preview.set_frame_target_mode(True)
        self.click_to_frame_preview.setMinimumSize(480, 270)
        self.click_to_frame_preview.setMaximumHeight(430)
        layout.addWidget(self.click_to_frame_preview)
        controls = QHBoxLayout()
        self.floating_click_button = QPushButton(
            "Show Floating Click-to-Frame Window"
        )
        self.floating_click_button.clicked.connect(
            lambda: self.toggle_click_to_frame_window()
        )
        reset = QPushButton("Reset Floating Window")
        reset.clicked.connect(self.reset_click_to_frame_window)
        controls.addWidget(self.floating_click_button)
        controls.addWidget(reset)
        controls.addStretch()
        layout.addLayout(controls)
        self.click_to_frame_status = QLabel(
            "Ready · Camera Sync will lock automatically when you click or drag"
        )
        self.click_to_frame_status.setWordWrap(True)
        layout.addWidget(self.click_to_frame_status)
        for preview in (
            self.click_to_frame_preview,
            self.click_to_frame_window.preview,
        ):
            preview.frame_target_clicked.connect(self.set_ptz_click_target)
            preview.frame_target_box_drawn.connect(self.set_ptz_frame_box)
        self.click_to_frame_window.frame_provider = (
            self._click_to_frame_composite_frame
        )
        self.click_to_frame_window.tracking_state_provider = lambda: (
            self.subject_tracking_enabled(),
            self.subject_tracking_requested(),
        )
        self.click_to_frame_window.tracking_toggle_requested.connect(
            self.toggle_subject_tracking_from_click_window
        )
        self.reload_click_to_frame_mosaic()
        return group

    def toggle_subject_tracking_from_click_window(self) -> None:
        self.set_subject_tracking_enabled(
            not self.subject_tracking_requested(),
            show_errors=False,
        )
        self.click_to_frame_window.refresh_tracking_button()

    def reload_click_to_frame_mosaic(self) -> None:
        """Load or lazily build the extended click-to-frame calibration view."""

        review = load_active_calibration_review(self.context.paths.root)
        if review is None:
            self._apply_click_to_frame_mosaic(None)
            return
        cached = load_calibration_mosaic(review)
        if cached is not None:
            self._apply_click_to_frame_mosaic(cached)
            return
        if self._click_mosaic_building:
            return
        self._click_mosaic_building = True
        if hasattr(self, "click_to_frame_status"):
            self._set_click_to_frame_status(
                "Preparing the PTZ panorama and local Audience alignment…"
            )

        async def work() -> str:
            await asyncio.to_thread(ensure_calibration_mosaic, review)
            return "PTZ panorama ready"

        def done(ok: bool, message: str) -> None:
            self._click_mosaic_building = False
            active = load_active_calibration_review(self.context.paths.root)
            if active is not None and active.map_path.resolve() != review.map_path.resolve():
                self._apply_click_to_frame_mosaic(None)
                self.reload_click_to_frame_mosaic()
                return
            mosaic = load_calibration_mosaic(active) if active is not None else None
            self._apply_click_to_frame_mosaic(mosaic)
            if ok and mosaic is not None:
                self._set_click_to_frame_status(
                    "Ready · live Audience view locally aligned to the PTZ panorama"
                )
            elif not ok:
                self._set_click_to_frame_status(
                    f"Live Audience view ready · PTZ panorama unavailable: {message}"
                )

        run_background(work, done)

    def _apply_click_to_frame_mosaic(
        self,
        mosaic: CalibrationMosaic | None,
    ) -> None:
        image = QImage(str(mosaic.image_path)) if mosaic is not None else QImage()
        if mosaic is None or image.isNull():
            self._click_mosaic = None
            self._click_mosaic_image = None
            self._click_mosaic_bgr = None
            self._click_reference_map_x = None
            self._click_reference_map_y = None
            self._click_reference_alpha = None
            self._click_live_map_x = None
            self._click_live_map_y = None
            self._click_live_alpha = None
            self._click_live_map_key = (-1, 0, 0)
            self._click_visual_reference_to_live = None
            self._click_visual_reference_size = (0, 0)
            self._click_visual_live_size = (0, 0)
            self._click_visual_alignment_sequence = -1
            self._click_geometry = None
        else:
            try:
                map_x, map_y, valid = mosaic.warp_mesh.reference_maps(mosaic)
                # Triangle rasterization can leave sub-pixel cracks. Close
                # those before edge feathering so the live image reads as one
                # clean surface rather than a collection of mesh panels.
                closed = cv2.morphologyEx(
                    valid,
                    cv2.MORPH_CLOSE,
                    np.ones((7, 7), dtype=np.uint8),
                )
                distance = cv2.distanceTransform(closed, cv2.DIST_L2, 3)
                alpha = np.clip(distance / 18.0, 0.0, 1.0).astype(np.float32)
                self._click_mosaic = mosaic
                self._click_mosaic_image = image
                self._click_mosaic_bgr = qimage_to_bgr(image)
                self._click_reference_map_x = map_x
                self._click_reference_map_y = map_y
                self._click_reference_alpha = alpha
                self._click_live_map_x = None
                self._click_live_map_y = None
                self._click_live_alpha = None
                self._click_live_map_key = (-1, 0, 0)
                self._click_visual_reference_to_live = None
                self._click_visual_reference_size = (0, 0)
                self._click_visual_live_size = (0, 0)
                self._click_visual_alignment_sequence = -1
                self._click_geometry = PtzGeometryModel.load_active_panorama(
                    self.context.paths.root
                )
            except (OSError, TypeError, ValueError):
                self._click_mosaic = None
                self._click_mosaic_image = None
                self._click_mosaic_bgr = None
                self._click_reference_map_x = None
                self._click_reference_map_y = None
                self._click_reference_alpha = None
                self._click_live_map_x = None
                self._click_live_map_y = None
                self._click_live_alpha = None
                self._click_live_map_key = (-1, 0, 0)
                self._click_visual_reference_to_live = None
                self._click_visual_reference_size = (0, 0)
                self._click_visual_live_size = (0, 0)
                self._click_visual_alignment_sequence = -1
                self._click_geometry = None
        ratio = (
            self._click_mosaic.width / self._click_mosaic.height
            if self._click_mosaic is not None
            else 16.0 / 9.0
        )
        for preview in (
            self.click_to_frame_preview,
            self.click_to_frame_window.preview,
        ):
            preview.set_aspect_ratio(ratio)
        floating_width = max(420, self.click_to_frame_window.width())
        floating_height = round(floating_width / ratio) + 58
        self.click_to_frame_window.setMinimumSize(360, round(360 / ratio) + 58)
        if not self.click_to_frame_window.isVisible():
            self.click_to_frame_window.resize(floating_width, floating_height)
        self._click_mosaic_version += 1
        self._click_composite_packet_sequence = -1
        self._click_composite_version = -1
        self._click_composite_pose_sequence = -1
        self._click_composite_relocalization_sequence = -1
        self._click_composite_last_render_monotonic = 0.0
        self._click_composite_reference_aligned = False
        self._click_composite_image = None

    def _click_to_frame_composite_frame(self) -> tuple[QImage, int] | None:
        packet = self.context.video.frame(VideoSourceKey.AUDIENCE)
        if packet is None:
            return None
        mosaic = self._click_mosaic
        base = self._click_mosaic_image
        if mosaic is None or base is None or base.isNull():
            return QImage(packet.image), packet.sequence

        self._schedule_click_pose_refresh()
        relocalization = self.context.video.relocalization.snapshot()
        audience_state = self.context.video.snapshot(VideoSourceKey.AUDIENCE)
        # The strict relocalization lock gates PTZ movement, not operator
        # visibility. Click-to-Frame must continue showing the latest Audience
        # pixels when the room is dark, landmarks are temporarily obscured, or
        # relocalization is still reacquiring. In those cases the visual uses
        # the last usable alignment, then the saved calibration pose as a
        # deterministic fallback. Motion remains fail-closed elsewhere.
        live_ready = bool(
            audience_state.frame_age_seconds is not None
            and audience_state.frame_age_seconds <= 1.5
        )
        now = time.monotonic()
        source_changed = packet.sequence != self._click_composite_packet_sequence
        pose_changed = self._click_pose_sequence != self._click_composite_pose_sequence
        lock_changed = (
            relocalization.analyzed_sequence
            != self._click_composite_relocalization_sequence
        )
        version_changed = self._click_composite_version != self._click_mosaic_version
        alignment_changed = live_ready != self._click_composite_reference_aligned
        if self._click_composite_image is not None and not (
            pose_changed or lock_changed or version_changed or alignment_changed
        ):
            if not source_changed or now - self._click_composite_last_render_monotonic < 0.20:
                return QImage(self._click_composite_image), self._click_render_sequence

        composite = QImage(base)
        reference_aligned = False
        if (
            live_ready
            and self._click_mosaic_bgr is not None
            and self._click_reference_map_x is not None
            and self._click_reference_map_y is not None
            and self._click_reference_alpha is not None
        ):
            try:
                live_bgr = qimage_to_bgr(packet.image)
                (
                    reference_to_live_value,
                    reference_size,
                    live_size,
                    visual_alignment_sequence,
                ) = self._click_visual_alignment(
                    relocalization,
                    mosaic,
                    packet.image.size(),
                )
                live_map_key = (
                    visual_alignment_sequence,
                    packet.image.width(),
                    packet.image.height(),
                )
                if (
                    self._click_live_map_key != live_map_key
                    or self._click_live_map_x is None
                    or self._click_live_map_y is None
                    or self._click_live_alpha is None
                ):
                    reference_to_live = np.asarray(
                        reference_to_live_value,
                        dtype=np.float64,
                    )
                    audience_to_reference = np.asarray(
                        (
                            (
                                reference_size[0]
                                / mosaic.audience_size[0],
                                0.0,
                                0.0,
                            ),
                            (
                                0.0,
                                reference_size[1]
                                / mosaic.audience_size[1],
                                0.0,
                            ),
                            (0.0, 0.0, 1.0),
                        ),
                        dtype=np.float64,
                    )
                    live_to_packet = np.asarray(
                        (
                            (
                                packet.image.width() / live_size[0],
                                0.0,
                                0.0,
                            ),
                            (
                                0.0,
                                packet.image.height() / live_size[1],
                                0.0,
                            ),
                            (0.0, 0.0, 1.0),
                        ),
                        dtype=np.float64,
                    )
                    transform = (
                        live_to_packet @ reference_to_live @ audience_to_reference
                    )
                    reference_x = self._click_reference_map_x
                    reference_y = self._click_reference_map_y
                    denominator = (
                        transform[2, 0] * reference_x
                        + transform[2, 1] * reference_y
                        + transform[2, 2]
                    )
                    safe = np.abs(denominator) > 1e-7
                    inverse = np.divide(
                        1.0,
                        denominator,
                        out=np.zeros_like(denominator),
                        where=safe,
                    )
                    live_x = (
                        (
                            transform[0, 0] * reference_x
                            + transform[0, 1] * reference_y
                            + transform[0, 2]
                        )
                        * inverse
                    ).astype(np.float32)
                    live_y = (
                        (
                            transform[1, 0] * reference_x
                            + transform[1, 1] * reference_y
                            + transform[1, 2]
                        )
                        * inverse
                    ).astype(np.float32)
                    live_x[~safe] = -1.0
                    live_y[~safe] = -1.0
                    inside = (
                        safe
                        & (live_x >= 0.0)
                        & (live_y >= 0.0)
                        & (live_x < packet.image.width() - 1)
                        & (live_y < packet.image.height() - 1)
                    )
                    self._click_live_map_x = live_x
                    self._click_live_map_y = live_y
                    self._click_live_alpha = (
                        self._click_reference_alpha * inside.astype(np.float32)
                    )
                    self._click_live_map_key = live_map_key
                live_x = self._click_live_map_x
                live_y = self._click_live_map_y
                alpha = self._click_live_alpha
                warped = cv2.remap(
                    live_bgr,
                    live_x,
                    live_y,
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )
                blended = cv2.blendLinear(
                    warped,
                    self._click_mosaic_bgr,
                    alpha,
                    1.0 - alpha,
                )
                composite = self._qimage_from_bgr(blended)
                reference_aligned = True
            except (cv2.error, FloatingPointError, TypeError, ValueError):
                reference_aligned = False
        painter = QPainter(composite)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_current_ptz_footprint(painter, mosaic, composite.size())
        painter.end()
        self._click_composite_packet_sequence = packet.sequence
        self._click_composite_version = self._click_mosaic_version
        self._click_composite_pose_sequence = self._click_pose_sequence
        self._click_composite_relocalization_sequence = (
            relocalization.analyzed_sequence
        )
        self._click_composite_last_render_monotonic = now
        self._click_composite_reference_aligned = reference_aligned
        self._click_composite_image = composite
        self._click_render_sequence += 1
        return QImage(composite), self._click_render_sequence

    def _click_visual_alignment(
        self,
        relocalization,
        mosaic: CalibrationMosaic,
        live_size: QSize,
    ) -> tuple[
        tuple[tuple[float, float, float], ...],
        tuple[int, int],
        tuple[int, int],
        int,
    ]:
        """Choose alignment for display without weakening PTZ motion safety."""

        reference_size = tuple(int(value) for value in relocalization.reference_size)
        analyzed_live_size = tuple(int(value) for value in relocalization.live_size)
        matrix = np.asarray(relocalization.reference_to_live, dtype=np.float64)
        current_alignment_usable = bool(
            relocalization.state
            in {RelocalizationState.LOCKED, RelocalizationState.DEGRADED}
            and len(reference_size) == 2
            and len(analyzed_live_size) == 2
            and min(*reference_size, *analyzed_live_size) > 0
            and matrix.shape == (3, 3)
            and np.isfinite(matrix).all()
        )
        if current_alignment_usable:
            selected = tuple(tuple(float(value) for value in row) for row in matrix)
            self._click_visual_reference_to_live = selected
            self._click_visual_reference_size = reference_size
            self._click_visual_live_size = analyzed_live_size
            self._click_visual_alignment_sequence = int(
                relocalization.analyzed_sequence
            )
            return (
                selected,
                reference_size,
                analyzed_live_size,
                self._click_visual_alignment_sequence,
            )

        if (
            self._click_visual_reference_to_live is not None
            and self._click_visual_reference_size == mosaic.audience_size
            and min(*self._click_visual_live_size) > 0
        ):
            return (
                self._click_visual_reference_to_live,
                self._click_visual_reference_size,
                self._click_visual_live_size,
                self._click_visual_alignment_sequence,
            )

        # A completed Camera Sync stores the Audience reference at the camera's
        # calibrated pose. Until live relocalization has a usable result, scale
        # that reference directly to the current packet. This keeps the view
        # live and spatially coherent instead of revealing the frozen scan.
        packet_size = (max(1, live_size.width()), max(1, live_size.height()))
        fallback_reference_size = mosaic.audience_size
        scale_x = packet_size[0] / max(1, fallback_reference_size[0])
        scale_y = packet_size[1] / max(1, fallback_reference_size[1])
        fallback = (
            (scale_x, 0.0, 0.0),
            (0.0, scale_y, 0.0),
            (0.0, 0.0, 1.0),
        )
        return fallback, fallback_reference_size, packet_size, -2

    @staticmethod
    def _qimage_from_bgr(image: np.ndarray) -> QImage:
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("Click-to-Frame composite must be a BGR image.")
        contiguous = np.ascontiguousarray(image)
        height, width = contiguous.shape[:2]
        return QImage(
            contiguous.data,
            width,
            height,
            contiguous.strides[0],
            QImage.Format.Format_BGR888,
        ).copy()

    @staticmethod
    def _qtransform_from_homography(
        matrix: tuple[tuple[float, float, float], ...],
    ) -> QTransform:
        return QTransform(
            matrix[0][0],
            matrix[1][0],
            matrix[2][0],
            matrix[0][1],
            matrix[1][1],
            matrix[2][1],
            matrix[0][2],
            matrix[1][2],
            matrix[2][2],
        )

    def _paint_current_ptz_footprint(
        self,
        painter: QPainter,
        mosaic: CalibrationMosaic,
        canvas_size: QSize,
    ) -> None:
        geometry = self._click_geometry
        pose = self._click_current_pose
        if geometry is None or pose is None:
            return
        try:
            reference_polygon = geometry.reference_polygon_for_pose(pose)
        except (TypeError, ValueError):
            return
        polygon = QPolygonF(
            [
                QPointF(
                    mosaic.reference_point_to_canvas(x, y)[0]
                    * canvas_size.width(),
                    mosaic.reference_point_to_canvas(x, y)[1]
                    * canvas_size.height(),
                )
                for x, y in reference_polygon
            ]
        )
        fill = QColor(255, 244, 180, 42)
        outline = QColor(255, 248, 204, 235)
        painter.setBrush(fill)
        painter.setPen(QPen(outline, 3))
        painter.drawPolygon(polygon)
        label_rect = polygon.boundingRect()
        if label_rect.width() >= 90 and label_rect.height() >= 32:
            label = QRectF(
                label_rect.left() + 8,
                label_rect.top() + 8,
                104,
                24,
            )
            painter.fillRect(label, QColor(9, 11, 15, 190))
            painter.setPen(outline)
            painter.drawText(
                label.adjusted(6, 0, -4, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "CURRENT PTZ",
            )

    def _schedule_click_pose_refresh(self) -> None:
        if not self.context.config.integrations.panasonic.enabled:
            return
        calibration_running = (
            self._calibration_process is not None
            and self._calibration_process.state() != QProcess.ProcessState.NotRunning
        )
        if calibration_running:
            return
        automation = self.context.ptz_automation.snapshot()
        if automation.armed and automation.actual_pose is not None:
            self._set_click_current_pose(automation.actual_pose)
            return
        now = time.monotonic()
        if (
            self._click_pose_polling
            or now - self._click_last_pose_poll_monotonic < 0.50
        ):
            return
        self._click_pose_polling = True
        self._click_last_pose_poll_monotonic = now
        selected: PtzMotorPose | None = None

        async def work() -> str:
            nonlocal selected
            pan, tilt = await self.context.panasonic.query_pan_tilt_position()
            zoom = await self.context.panasonic.query_zoom_position()
            selected = PtzMotorPose(
                pan,
                tilt,
                zoom,
            )
            return "PTZ pose refreshed"

        def done(ok: bool, _message: str) -> None:
            self._click_pose_polling = False
            if ok and selected is not None:
                self._set_click_current_pose(selected)

        run_background(work, done)

    def _set_click_current_pose(self, pose: PtzMotorPose) -> None:
        if pose == self._click_current_pose:
            return
        self._click_current_pose = pose
        self._click_pose_sequence += 1

    def set_ptz_click_target(self, x: float, y: float) -> None:
        self._run_click_to_frame(x, y, 0.0, 0.0)

    def set_ptz_frame_box(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        self._run_click_to_frame(x, y, width, height)

    def _run_click_to_frame(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        self.set_subject_tracking_enabled(False, show_errors=False)
        target_bounds = (
            NormalizedRect(x, y, width, height)
            if width >= 0.015 and height >= 0.015
            else None
        )
        target_point = None if target_bounds is not None else (x, y)
        for preview in (
            self.click_to_frame_preview,
            self.click_to_frame_window.preview,
        ):
            preview.set_frame_target(target_point, target_bounds)

        mosaic = self._click_mosaic
        if mosaic is None:
            reference_x, reference_y, reference_width, reference_height = (
                x,
                y,
                width,
                height,
            )
            uses_live_audience = True
        else:
            reference_x, reference_y, reference_width, reference_height = (
                mosaic.canvas_rect_to_reference(x, y, width, height)
            )
            uses_live_audience = False

        if uses_live_audience:
            self.context.video.relocalization.reload_calibration()
            self.context.video.set_calibration_activity(
                True,
                owner="click_to_frame",
            )
        self.status.setText("Framing the PTZ from the Audience view…")
        self._set_click_to_frame_status(
            "Refreshing the live Camera Sync lock…"
            if uses_live_audience
            else "Framing from the aligned Camera Sync view…"
        )

        framed_pose: PtzMotorPose | None = None

        async def work() -> str:
            nonlocal framed_pose
            if uses_live_audience:
                deadline = time.monotonic() + 8.0
                while (
                    not self.context.video.relocalization.snapshot().motion_safe
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.1)
                if not self.context.video.relocalization.snapshot().motion_safe:
                    raise ValueError(
                        "Camera Sync could not match the current Audience frame. "
                        "Keep the Audience view clear and run Calibrate Camera Sync again if either camera moved."
                    )
                command = self.context.ptz_automation.frame_live_target
            else:
                command = self.context.ptz_automation.frame_panorama_target
            framed_pose = await asyncio.to_thread(
                command,
                reference_x,
                reference_y,
                reference_width,
                reference_height,
            )
            return (
                f"PTZ framed at {framed_pose.pan:04X}/{framed_pose.tilt:04X}/"
                f"{framed_pose.zoom:03X}. "
                "Subject Tracking is off."
            )

        def done(ok: bool, message: str) -> None:
            if uses_live_audience:
                self.context.video.set_calibration_activity(
                    False,
                    owner="click_to_frame",
                )
            self.status.setText(message if ok else f"Click-to-frame blocked: {message}")
            self._set_click_to_frame_status(
                message if ok else f"Could not frame the PTZ · {message}"
            )
            if ok:
                if framed_pose is not None:
                    self._set_click_current_pose(framed_pose)
                for preview in (
                    self.click_to_frame_preview,
                    self.click_to_frame_window.preview,
                ):
                    preview.fade_frame_target(1000)

        run_background(work, done)

    def _set_click_to_frame_status(self, message: str) -> None:
        self.click_to_frame_status.setText(message)
        self.click_to_frame_window.tracking_button.setToolTip(message)

    def show_click_to_frame_window(self) -> None:
        self.click_to_frame_window.show()
        self.floating_click_button.setText("Hide Floating Click-to-Frame Window")
        self._update_preview_activity()

    def hide_click_to_frame_window(self) -> None:
        self.click_to_frame_window.hide()
        self.floating_click_button.setText("Show Floating Click-to-Frame Window")
        self._update_preview_activity()

    def toggle_click_to_frame_window(self, visible: bool | None = None) -> None:
        show = (
            not self.click_to_frame_window.isVisible()
            if visible is None
            else bool(visible)
        )
        if show:
            self.show_click_to_frame_window()
        else:
            self.hide_click_to_frame_window()

    def reset_click_to_frame_window(self) -> None:
        self.click_to_frame_window.resize(520, 340)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.click_to_frame_window.move(
                available.right() - self.click_to_frame_window.width() - 24,
                available.top() + 54,
            )
        self.show_click_to_frame_window()

    def _update_preview_activity(self) -> None:
        self.context.video.set_preview_active(
            self.isVisible() or self.click_to_frame_window.isVisible()
        )

    def clear_ptz_click_target(self) -> None:
        self.click_to_frame_preview.set_frame_target(None, None)
        self.click_to_frame_window.preview.set_frame_target(None, None)

    def scene_regions_group(self) -> QGroupBox:
        group = QGroupBox("Phase 3C · Calibrated Scene Planes")
        layout = QVBoxLayout(group)
        explanation = QLabel(
            "Define Stage, Front of Stage, Altar, podium, and audience planes against the "
            "approved calibration reference. When Audience Cam drifts, locked planes move "
            "with the live image automatically."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self._region_draft_points: list[tuple[float, float]] = []
        self._region_draft_name = ""
        self._region_draft_kind = "custom"
        self._region_draft_color = "#7c5cff"
        self._region_replacing_id = ""
        self._region_editor_coordinate_space = "reference"
        self._updating_region_list = False
        self.region_summary = QLabel("No scene drawings saved")
        self.region_summary.setWordWrap(True)
        layout.addWidget(self.region_summary)
        open_editor = QPushButton("Open Scene Drawing Review…")
        open_editor.clicked.connect(self.open_scene_region_editor)
        layout.addWidget(open_editor)
        self._build_scene_region_dialog()
        self.reload_scene_regions()
        self._update_region_drawing_controls()
        return group

    def _build_scene_region_dialog(self) -> None:
        self.scene_region_dialog = QDialog(self)
        self.scene_region_dialog.setWindowTitle("Production Hub · Calibrated Scene Plane Review")
        self.scene_region_dialog.setModal(False)
        self.scene_region_dialog.resize(1420, 860)
        self.scene_region_dialog.setMinimumSize(960, 640)
        root = QVBoxLayout(self.scene_region_dialog)
        heading = QLabel("Calibrated Scene Planes · Audience Camera")
        heading.setObjectName("PageTitle")
        root.addWidget(heading)
        explanation = QLabel(
            "Planes are stored against the approved Audience reference—not the camera's current "
            "pixel position. A locked live view is automatically converted back to that reference."
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)
        self.region_coordinate_summary = QLabel("Loading calibration coordinate space…")
        self.region_coordinate_summary.setWordWrap(True)
        root.addWidget(self.region_coordinate_summary)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.region_editor_preview = VideoPreview("Connect Audience Cam to review scene drawings")
        self.region_editor_preview.setMinimumSize(700, 394)
        left_layout.addWidget(self.region_editor_preview, 1)
        self.region_instructions = QLabel(
            "Select a drawing to review it, or choose Draw New Region and click at least three boundary points."
        )
        self.region_instructions.setWordWrap(True)
        left_layout.addWidget(self.region_instructions)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        list_heading = QLabel("Saved Drawings")
        list_heading.setObjectName("CardTitle")
        right_layout.addWidget(list_heading)
        self.region_show_selected_only = QCheckBox("Show only selected drawing")
        self.region_show_selected_only.setChecked(True)
        self.region_show_selected_only.setToolTip(
            "Turn this off to compare every visible drawing at once."
        )
        self.region_show_selected_only.toggled.connect(
            lambda _checked: self.scene_region_selection_changed(
                self.region_list.currentItem()
            )
        )
        right_layout.addWidget(self.region_show_selected_only)
        self.region_list = QListWidget()
        self.region_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.region_list.itemChanged.connect(self.scene_region_enabled_changed)
        self.region_list.currentItemChanged.connect(self.scene_region_selection_changed)
        right_layout.addWidget(self.region_list, 1)

        suggestions = QPushButton("Add / Restore Stage Starting Drawings")
        suggestions.clicked.connect(self.add_suggested_scene_regions)
        right_layout.addWidget(suggestions)
        manage = QGridLayout()
        new_region = QPushButton("Draw New Region…")
        new_region.clicked.connect(self.begin_scene_region)
        redraw = QPushButton("Redraw Selected")
        redraw.clicked.connect(self.redraw_scene_region)
        rename = QPushButton("Rename Selected")
        rename.clicked.connect(self.rename_scene_region)
        delete = QPushButton("Delete Selected")
        delete.clicked.connect(self.delete_scene_region)
        manage.addWidget(new_region, 0, 0)
        manage.addWidget(redraw, 0, 1)
        manage.addWidget(rename, 1, 0)
        manage.addWidget(delete, 1, 1)
        right_layout.addLayout(manage)

        drawing = QGridLayout()
        self.region_undo_button = QPushButton("Undo Point")
        self.region_undo_button.clicked.connect(self.undo_scene_region_point)
        self.region_finish_button = QPushButton("Finish Drawing")
        self.region_finish_button.clicked.connect(self.finish_scene_region)
        self.region_cancel_button = QPushButton("Cancel Drawing")
        self.region_cancel_button.clicked.connect(self.cancel_scene_region)
        drawing.addWidget(self.region_undo_button, 0, 0)
        drawing.addWidget(self.region_finish_button, 0, 1)
        drawing.addWidget(self.region_cancel_button, 1, 0, 1, 2)
        right_layout.addLayout(drawing)
        right_layout.addStretch()
        close = QPushButton("Close Review")
        close.clicked.connect(self.scene_region_dialog.close)
        right_layout.addWidget(close)
        splitter.addWidget(right)
        splitter.setSizes([1040, 340])
        splitter.setStretchFactor(0, 1)
        root.addWidget(splitter, 1)

        self.region_editor_preview.region_point_clicked.connect(self.add_scene_region_point)
        self.region_editor_preview.region_clicked.connect(self.select_scene_region_from_preview)
        self.scene_region_dialog.finished.connect(self._scene_region_dialog_finished)

    def open_scene_region_editor(self) -> None:
        self._sync_region_editor_frame()
        self.reload_scene_regions()
        self.scene_region_dialog.show()
        self.scene_region_dialog.raise_()
        self.scene_region_dialog.activateWindow()
        self._update_calibration_activity()

    def _scene_region_dialog_finished(self, _result: int) -> None:
        self.cancel_scene_region(update_status=False)
        self._update_calibration_activity()

    def _update_calibration_activity(
        self,
        *,
        page_visible: bool | None = None,
        click_visible: bool | None = None,
    ) -> None:
        if not hasattr(self, "scene_region_dialog"):
            return
        calibration_process_running = (
            self._calibration_process is not None
            and self._calibration_process.state() != QProcess.ProcessState.NotRunning
        )
        active = (
            self.scene_region_dialog.isVisible()
            or self.camera_calibration_dialog.isVisible()
            or calibration_process_running
            or (
                self.isVisible() if page_visible is None else bool(page_visible)
            )
            or (
                self.click_to_frame_window.isVisible()
                if click_visible is None
                else bool(click_visible)
            )
        )
        self.context.video.set_calibration_activity(active)

    def _sync_region_editor_frame(self) -> None:
        image = self.audience_preview.image
        lock = self.context.video.relocalization.snapshot()
        review = load_active_calibration_review(self.context.paths.root)
        if image is not None and lock.state == RelocalizationState.LOCKED:
            self._region_editor_coordinate_space = "live"
            self.region_editor_preview.set_frame(image, self.audience_preview.sequence)
            self.region_coordinate_summary.setText(
                f"Live stabilized coordinates · {lock.inliers} landmark inliers · "
                f"{lock.median_error_pixels:.2f}px. Drawings will be converted to the approved reference."
            )
        elif review is not None:
            reference = QImage(str(review.audience_image_path))
            if not reference.isNull():
                self._region_editor_coordinate_space = "reference"
                self._calibration_frame_sequence += 1
                self.region_editor_preview.set_frame(
                    reference,
                    self._calibration_frame_sequence,
                )
                self.region_coordinate_summary.setText(
                    "Saved calibration reference coordinates · live lock is unavailable or degraded."
                )
        elif image is not None:
            self._region_editor_coordinate_space = "reference"
            self.region_editor_preview.set_frame(image, self.audience_preview.sequence)
            self.region_coordinate_summary.setText(
                "Uncalibrated fallback coordinates · approve a calibration before automation."
            )
        selected_id = ""
        if self.region_list.currentItem() is not None:
            selected_id = str(
                self.region_list.currentItem().data(Qt.ItemDataRole.UserRole) or ""
            )
        self.region_editor_preview.set_regions(
            self._editor_regions_for_selection(selected_id),
            selected_region_id=selected_id,
        )

    def select_scene_region_from_preview(self, region_id: str) -> None:
        for index in range(self.region_list.count()):
            item = self.region_list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == region_id:
                self.region_list.setCurrentItem(item)
                return

    def diagnostics_group(self) -> QGroupBox:
        group = QGroupBox("Video Diagnostics and Replay")
        layout = QVBoxLayout(group)
        self.video_summary = QLabel("Initializing video services…")
        self.video_summary.setWordWrap(True)
        layout.addWidget(self.video_summary)

        controls = QHBoxLayout()
        self.record_button = QPushButton("Start Diagnostic Recording")
        self.record_button.setEnabled(self.context.video.recording_available)
        self.record_button.clicked.connect(self.toggle_recording)
        replay_button = QPushButton("Replay Recording…")
        replay_button.setEnabled(self.context.video.recording_available)
        replay_button.clicked.connect(self.choose_replay)
        stop_replay_button = QPushButton("Stop Replay")
        stop_replay_button.clicked.connect(self.context.video.stop_replay)
        reveal_button = QPushButton("Show Recordings")
        reveal_button.clicked.connect(self.reveal_recordings)
        controls.addWidget(self.record_button)
        controls.addWidget(replay_button)
        controls.addWidget(stop_replay_button)
        controls.addStretch()
        controls.addWidget(reveal_button)
        layout.addLayout(controls)

        if not self.context.video.recording_available:
            missing = QLabel("Recording/replay is unavailable because Qt Multimedia could not initialize.")
            missing.setWordWrap(True)
            layout.addWidget(missing)

        self.replay_preview = VideoPreview("No diagnostic replay loaded")
        layout.addWidget(self.replay_preview)
        self.replay_detail = QLabel("Replay stopped")
        layout.addWidget(self.replay_detail)
        return group

    def refresh_video_status(self) -> None:
        for source, preview, detail in (
            (VideoSourceKey.AUDIENCE, self.audience_preview, self.audience_detail),
            (VideoSourceKey.PTZ, self.ptz_preview, self.ptz_detail),
            (VideoSourceKey.REPLAY, self.replay_preview, self.replay_detail),
        ):
            packet = self.context.video.frame(source)
            snapshot = self.context.video.snapshot(source)
            if packet is not None:
                preview.set_frame(packet.image, packet.sequence)
            elif preview.sequence:
                preview.clear(snapshot.message or "No video frame")
            detail.setText(self.format_video_status(snapshot))

        click_frame = self._click_to_frame_composite_frame()
        if click_frame is not None:
            click_image, click_sequence = click_frame
            self.click_to_frame_preview.set_frame(click_image, click_sequence)
        self.click_to_frame_window.refresh_tracking_button()

        audience_tracking = self.context.video.tracking.snapshot(VideoSourceKey.AUDIENCE)
        ptz_tracking = self.context.video.tracking.snapshot(VideoSourceKey.PTZ)
        # Keep the normal operator page visually quiet; analysis continues
        # without subject boxes or calibration markers over the live feeds.
        self.audience_preview.set_subjects(())
        self.ptz_preview.set_subjects(())
        self.audience_preview.set_regions(())
        if self.scene_region_dialog.isVisible():
            self._sync_region_editor_frame()
        if (
            self.camera_calibration_dialog.isVisible()
            and self.calibration_use_live_images.isChecked()
        ):
            self._update_calibration_review_images()
        for source, snapshot, label in (
            (VideoSourceKey.AUDIENCE, audience_tracking, "Audience"),
            (VideoSourceKey.PTZ, ptz_tracking, "PTZ"),
        ):
            button = self.select_all_subject_buttons[source]
            count = len(snapshot.subjects) if snapshot.state == TrackingState.RUNNING else 0
            button.setText(f"Select All Detected · {label} ({count})")
            button.setEnabled(count > 0)
        self.tracking_summary.setText(
            "Audience: "
            f"{self.format_tracking_status(audience_tracking)} · PTZ: "
            f"{self.format_tracking_status(ptz_tracking)}"
        )
        relocalization = self.context.video.relocalization.snapshot()
        self.calibration_lock_summary.setText(
            self.format_relocalization_status(relocalization)
        )
        self.continue_pending_subject_arm()
        automation = self.context.ptz_automation.snapshot()
        if (
            not automation.armed
            and self._pending_subject_arm_until <= 0.0
            and self.context.config.integrations.camera_tracking.automation.mode == "subject"
        ):
            # External VISCA, manual controls, target loss, and safety blocks
            # all disarm in the service. Persist that as the one visible Off
            # state so every control surface agrees.
            self.set_subject_tracking_enabled(False, show_errors=False)
            automation = self.context.ptz_automation.snapshot()
        decision = automation.decision
        pose_text = ""
        if decision is not None and decision.desired_pose is not None:
            desired = decision.desired_pose
            pose_text = (
                f" · recommendation {desired.pan:04X}/{desired.tilt:04X}/{desired.zoom:03X}"
            )
        authority = "Tracking ON" if automation.motion_authority else "Off"
        decision_text = decision.reason if decision is not None else automation.message
        self.automation_summary.setText(
            f"{authority} · {decision_text}{pose_text}"
        )
        self._sync_automation_control()
        self.floating_click_button.setText(
            "Hide Floating Click-to-Frame Window"
            if self.click_to_frame_window.isVisible()
            else "Show Floating Click-to-Frame Window"
        )
        if (
            self.camera_calibration_dialog.isVisible()
            and self.calibration_use_live_images.isChecked()
        ):
            self._refresh_calibration_marker_overlays()

        for source in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ):
            if self.source_type_boxes[source].currentData() == "ndi":
                self._add_discovered_ndi_sources(self.source_selectors[source])

        audience = self.context.video.snapshot(VideoSourceKey.AUDIENCE)
        ptz = self.context.video.snapshot(VideoSourceKey.PTZ)
        ndi_version = self.context.video.ndi_runtime_version
        recording = self.context.video.recorder.recording
        recording_text = "recording" if recording else "not recording"
        self.video_summary.setText(
            f"NDI {ndi_version} · Audience {audience.state.value} · PTZ {ptz.state.value} · {recording_text}. "
            "Pixel conversion and encoding work is bounded and isolated from the UI thread."
        )
        if recording != self._last_recording_state:
            self._last_recording_state = recording
            self.record_button.setText("Stop Diagnostic Recording" if recording else "Start Diagnostic Recording")

    @staticmethod
    def format_video_status(snapshot: VideoSourceSnapshot) -> str:
        parts = [snapshot.state.value.replace("_", " ").title()]
        if snapshot.negotiated_format:
            parts.append(snapshot.negotiated_format)
        if snapshot.effective_fps > 0:
            parts.append(f"preview {snapshot.effective_fps:.1f} fps")
        age = snapshot.frame_age_seconds
        if age is not None:
            parts.append(f"age {age * 1000:.0f} ms")
        if snapshot.dropped_frames:
            parts.append(f"NDI drops {snapshot.dropped_frames}")
        if snapshot.message and snapshot.message != "Receiving video":
            parts.append(snapshot.message)
        return " · ".join(parts)

    @staticmethod
    def format_tracking_status(snapshot: TrackingSnapshot) -> str:
        if snapshot.state != TrackingState.RUNNING:
            detail = snapshot.message
            if snapshot.last_error and snapshot.last_error not in detail:
                detail = f"{detail} ({snapshot.last_error})"
            return f"{snapshot.state.value.replace('_', ' ').title()} — {detail}"
        selected = snapshot.selected_count
        return (
            f"{len(snapshot.subjects)} stage subject(s), {selected} selected, "
            f"{snapshot.suppressed_candidates} outside-stage suppressed, "
            f"{snapshot.inference_ms:.0f} ms inference, {snapshot.analysis_fps:.1f} fps"
        )

    @staticmethod
    def format_relocalization_status(snapshot) -> str:
        state = snapshot.state.value.replace("_", " ").title()
        safety = "mapping safe" if snapshot.motion_safe else "motion blocked"
        metrics = ""
        if snapshot.inliers:
            metrics = (
                f" · {snapshot.inliers} inliers · "
                f"{snapshot.median_error_pixels:.2f}px · {snapshot.inference_ms:.0f}ms"
            )
        detail = snapshot.message
        if snapshot.last_error and snapshot.last_error not in detail:
            detail = f"{detail} ({snapshot.last_error})"
        return f"Live Audience lock: {state} · {safety}{metrics} — {detail}"

    def update_relocalization_config(self, _checked: bool = False) -> None:
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        config.relocalization_enabled = self.relocalization_enabled.isChecked()
        config.__post_init__()
        self._save_tracking_config(config)
        self._update_calibration_activity()
        self.status.setText(
            "Live Audience calibration lock enabled. PTZ motion remains disabled."
            if config.relocalization_enabled
            else "Live Audience calibration lock disabled; automated motion is blocked."
        )

    def update_tracking_config(self, _value=None) -> None:
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        config.enabled = self.tracking_enabled.isChecked()
        config.analyze_audience = self.tracking_audience.isChecked()
        config.analyze_ptz = self.tracking_ptz.isChecked()
        config.analysis_fps = float(self.tracking_rate.currentData())
        config.__post_init__()
        self._save_tracking_config(config)
        self.context.video.set_tracking_activity(config.enabled and self.isVisible())
        self.status.setText(
            "Person detection enabled in shadow mode. No camera commands will be sent."
            if config.enabled
            else "Person detection disabled."
        )

    def toggle_tracked_subject(self, source: VideoSourceKey, track_id: int) -> None:
        selected = self.context.video.tracking.toggle_subject(source, track_id)
        self.status.setText(
            f"Subject S{track_id} {'selected' if selected else 'deselected'} on {source.value.title()}."
        )
        self.refresh_video_status()

    def select_all_tracked_subjects(self, source: VideoSourceKey) -> None:
        self.context.video.tracking.select_all_visible(source)
        self.status.setText(f"Selected all visible {source.value.title()} subjects.")
        self.refresh_video_status()

    def clear_tracked_subjects(self) -> None:
        self.context.video.tracking.clear_selection()
        self.status.setText("Cleared all subject selections.")
        self.refresh_video_status()

    def begin_scene_region(self) -> None:
        if self._region_draft_name:
            self.status.setText("Finish or cancel the current region before starting another.")
            return
        self._sync_region_editor_frame()
        if not self.region_editor_preview.has_image:
            self.status.setText("Connect the Audience feed before drawing a scene region.")
            return
        options = ["Stage", "Front of Stage", "Altar", "Podium", "Audience", "Custom"]
        selected, ok = QInputDialog.getItem(
            self,
            "New Scene Region",
            "Region type:",
            options,
            0,
            False,
        )
        if not ok:
            return
        default_name = str(selected)
        name, ok = QInputDialog.getText(
            self,
            "Name Scene Region",
            "Region name:",
            text=default_name,
        )
        name = name.strip()
        if not ok or not name:
            return
        kind_map = {
            "Stage": "stage",
            "Front of Stage": "front_stage",
            "Altar": "altar",
            "Podium": "podium",
            "Audience": "audience",
            "Custom": "custom",
        }
        color_map = {
            "stage": "#25d0c8",
            "front_stage": "#ffb020",
            "altar": "#d467ff",
            "podium": "#ef5da8",
            "audience": "#5b8def",
            "custom": "#7c5cff",
        }
        self._region_draft_name = name
        self._region_draft_kind = kind_map[str(selected)]
        self._region_draft_color = color_map[self._region_draft_kind]
        self._region_draft_points = []
        self._region_replacing_id = ""
        self.region_editor_preview.set_region_draft((), drawing=True)
        self.region_instructions.setText(
            f"Drawing {name}: click boundary points clockwise or counter-clockwise on Audience Cam, "
            "then choose Finish Region."
        )
        self.status.setText(f"Drawing scene region: {name}")
        self._update_region_drawing_controls()

    def add_scene_region_point(self, x: float, y: float) -> None:
        if not self._region_draft_name:
            return
        self._region_draft_points.append((float(x), float(y)))
        self.region_editor_preview.set_region_draft(
            tuple(self._region_draft_points),
            drawing=True,
        )
        self.region_instructions.setText(
            f"{self._region_draft_name}: {len(self._region_draft_points)} point(s). "
            "At least three are required."
        )
        self._update_region_drawing_controls()

    def undo_scene_region_point(self) -> None:
        if self._region_draft_points:
            self._region_draft_points.pop()
            self.region_editor_preview.set_region_draft(
                tuple(self._region_draft_points),
                drawing=True,
            )
        self._update_region_drawing_controls()

    def finish_scene_region(self) -> None:
        if not self._region_draft_name or len(self._region_draft_points) < 3:
            self.status.setText("A scene region requires at least three boundary points.")
            return
        reference_points = list(self._region_draft_points)
        if self._region_editor_coordinate_space == "live":
            converted = [
                self.context.video.relocalization.live_point_to_reference(x, y)
                for x, y in reference_points
            ]
            if any(item is None for item in converted):
                self.status.setText(
                    "Live calibration lock was lost. The scene plane was not saved."
                )
                return
            reference_points = [item for item in converted if item is not None]
        current = self.context.config.integrations.camera_tracking
        replaced = next(
            (item for item in current.scene_regions if item.id == self._region_replacing_id),
            None,
        )
        region_id = self._region_replacing_id or uuid4().hex
        region = CameraSceneRegion(
            id=region_id,
            name=self._region_draft_name,
            kind=self._region_draft_kind,
            source="audience",
            color=self._region_draft_color,
            suggested=False,
            coordinate_space="calibration_reference",
            calibration_reference=(
                active.created_at
                if (active := load_active_calibration_review(self.context.paths.root)) is not None
                else ""
            ),
            points=[SceneRegionPoint(x, y) for x, y in reference_points],
        )
        config = type(current).from_dict(current.to_dict())
        if self._region_replacing_id:
            config.scene_regions = [
                region if item.id == self._region_replacing_id else item
                for item in config.scene_regions
            ]
        else:
            config.scene_regions.append(region)
        self._save_tracking_config(config)
        saved_id = region.id
        self.cancel_scene_region(update_status=False)
        self.reload_scene_regions(selected_id=saved_id)
        self.status.setText(f"Saved Audience scene region: {region.name}")

    def cancel_scene_region(self, _checked: bool = False, *, update_status: bool = True) -> None:
        was_drawing = bool(self._region_draft_name)
        self._region_draft_points = []
        self._region_draft_name = ""
        self._region_draft_kind = "custom"
        self._region_draft_color = "#7c5cff"
        self._region_replacing_id = ""
        self.region_editor_preview.set_region_draft((), drawing=False)
        self.region_instructions.setText(
            "Connect the Audience feed, then choose Draw New Region and click at least three boundary points."
        )
        self._update_region_drawing_controls()
        if update_status and was_drawing:
            self.status.setText("Cancelled scene-region drawing.")

    def reload_scene_regions(self, selected_id: str = "") -> None:
        if not hasattr(self, "region_list"):
            return
        if not selected_id and self.region_list.currentItem() is not None:
            selected_id = str(self.region_list.currentItem().data(Qt.ItemDataRole.UserRole) or "")
        labels = {
            "stage": "Stage",
            "front_stage": "Front of Stage",
            "altar": "Altar",
            "podium": "Podium",
            "audience": "Audience",
            "custom": "Custom",
        }
        self._updating_region_list = True
        self.region_list.clear()
        regions = [
            region
            for region in self.context.config.integrations.camera_tracking.scene_regions
            if region.source == "audience"
        ]
        if regions and not selected_id:
            selected_id = regions[0].id
        active = load_active_calibration_review(self.context.paths.root)
        current_reference = active.created_at if active is not None else ""
        for region in regions:
            origin = " · Suggested" if region.suggested else ""
            compatible = (
                not region.calibration_reference
                or not current_reference
                or region.calibration_reference == current_reference
            )
            compatibility = "" if compatible else " · Needs Redraw"
            row = QListWidgetItem(
                f"{region.name} · {labels.get(region.kind, 'Custom')}"
                f"{origin}{compatibility}"
            )
            origin_detail = (
                "Suggested drawing"
                if region.suggested
                else "Manual drawing"
            )
            row.setToolTip(
                f"{region.name}\n"
                f"Type: {labels.get(region.kind, 'Custom')}\n"
                f"Origin: {origin_detail}\n"
                f"Coordinates: {region.coordinate_space.replace('_', ' ')}\n"
                f"Calibration: {region.calibration_reference or 'legacy current reference'}"
            )
            row.setData(Qt.ItemDataRole.UserRole, region.id)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(
                Qt.CheckState.Checked if region.enabled else Qt.CheckState.Unchecked
            )
            self.region_list.addItem(row)
            if region.id == selected_id:
                self.region_list.setCurrentItem(row)
        self._updating_region_list = False
        self.region_editor_preview.set_regions(
            self._editor_regions_for_selection(selected_id),
            selected_region_id=selected_id,
        )
        suggested_count = sum(1 for region in regions if region.suggested)
        enabled_count = sum(1 for region in regions if region.enabled)
        self.region_summary.setText(
            f"{len(regions)} drawing(s) saved · {enabled_count} visible · "
            f"{suggested_count} suggested for review"
            if regions
            else "No scene drawings saved. Open the review window to add suggestions."
        )

    def scene_region_selection_changed(self, current, _previous=None) -> None:
        region_id = (
            str(current.data(Qt.ItemDataRole.UserRole) or "")
            if current is not None
            else ""
        )
        self.region_editor_preview.set_regions(
            self._editor_regions_for_selection(region_id),
            selected_region_id=region_id,
        )

    def _editor_regions_for_selection(
        self,
        selected_region_id: str,
    ) -> tuple[CameraSceneRegion, ...]:
        regions = tuple(
            region
            for region in self.context.config.integrations.camera_tracking.scene_regions
            if region.source == "audience"
        )
        if self.region_show_selected_only.isChecked() and selected_region_id:
            regions = tuple(region for region in regions if region.id == selected_region_id)
        if self._region_editor_coordinate_space == "live":
            return self.context.video.relocalization.stabilized_regions(regions)
        return regions

    def scene_region_enabled_changed(self, item: QListWidgetItem) -> None:
        if self._updating_region_list:
            return
        region_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        for region in config.scene_regions:
            if region.id == region_id:
                region.enabled = item.checkState() == Qt.CheckState.Checked
                break
        self._save_tracking_config(config)
        self.reload_scene_regions(selected_id=region_id)

    def rename_scene_region(self) -> None:
        region_id = self.selected_scene_region_id()
        if not region_id:
            return
        current = self.context.config.integrations.camera_tracking
        existing = next((region for region in current.scene_regions if region.id == region_id), None)
        if existing is None:
            return
        name, ok = QInputDialog.getText(
            self,
            "Rename Scene Region",
            "Region name:",
            text=existing.name,
        )
        name = name.strip()
        if not ok or not name:
            return
        config = type(current).from_dict(current.to_dict())
        for region in config.scene_regions:
            if region.id == region_id:
                region.name = name
                break
        self._save_tracking_config(config)
        self.reload_scene_regions(selected_id=region_id)
        self.status.setText(f"Renamed scene region to {name}.")

    def redraw_scene_region(self) -> None:
        if self._region_draft_name:
            self.status.setText("Finish or cancel the current region before redrawing another.")
            return
        self._sync_region_editor_frame()
        if not self.region_editor_preview.has_image:
            self.status.setText("Connect the Audience feed before redrawing a scene region.")
            return
        region_id = self.selected_scene_region_id()
        if not region_id:
            return
        current = self.context.config.integrations.camera_tracking
        region = next((item for item in current.scene_regions if item.id == region_id), None)
        if region is None:
            return
        self._region_draft_name = region.name
        self._region_draft_kind = region.kind
        self._region_draft_color = region.color
        self._region_replacing_id = region.id
        self._region_draft_points = []
        self.region_editor_preview.set_region_draft((), drawing=True)
        self.region_instructions.setText(
            f"Redrawing {region.name}: click a new boundary with at least three points, then Finish Region."
        )
        self.status.setText(f"Redrawing scene region: {region.name}")
        self._update_region_drawing_controls()

    def delete_scene_region(self) -> None:
        region_ids = {
            str(item.data(Qt.ItemDataRole.UserRole) or "")
            for item in self.region_list.selectedItems()
        }
        if not region_ids:
            region_id = self.selected_scene_region_id()
            region_ids = {region_id} if region_id else set()
        if not region_ids:
            return
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        removed = [region for region in config.scene_regions if region.id in region_ids]
        config.scene_regions = [
            region for region in config.scene_regions if region.id not in region_ids
        ]
        self._save_tracking_config(config)
        self.reload_scene_regions()
        if removed:
            self.status.setText(
                f"Deleted {len(removed)} scene drawing(s). A configuration backup was created."
            )

    def add_suggested_scene_regions(self) -> None:
        current = self.context.config.integrations.camera_tracking
        config = type(current).from_dict(current.to_dict())
        existing = {region.id for region in config.scene_regions}
        additions = [
            region
            for region in suggested_church_scene_regions()
            if region.id not in existing
        ]
        if not additions:
            self.status.setText("All suggested scene drawings are already present.")
            return
        calibration_reference = (
            active.created_at
            if (active := load_active_calibration_review(self.context.paths.root)) is not None
            else ""
        )
        for region in additions:
            region.coordinate_space = "calibration_reference"
            region.calibration_reference = calibration_reference
        config.scene_regions.extend(additions)
        self._save_tracking_config(config)
        self.reload_scene_regions(selected_id=additions[0].id)
        self.status.setText(
            f"Added {len(additions)} suggested scene drawing(s) for review."
        )

    def selected_scene_region_id(self) -> str:
        item = self.region_list.currentItem()
        if item is None:
            self.status.setText("Select a scene region first.")
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _save_tracking_config(self, config) -> None:
        self.context.config.integrations.camera_tracking = config
        self.context.config_repository.save_app_config(self.context.config)
        self.context.video.reconfigure_tracking(config)
        self.context.ptz_automation.reconfigure(config)

    def _update_region_drawing_controls(self) -> None:
        drawing = bool(self._region_draft_name)
        self.region_undo_button.setEnabled(drawing and bool(self._region_draft_points))
        self.region_finish_button.setEnabled(drawing and len(self._region_draft_points) >= 3)
        self.region_cancel_button.setEnabled(drawing)

    def source_type_changed(self, source: VideoSourceKey) -> None:
        source_type = str(self.source_type_boxes[source].currentData())
        display_name = "Audience Cam" if source == VideoSourceKey.AUDIENCE else "PTZ Cam"
        mode_name = "NDI" if source_type == "ndi" else "Local Camera"
        self.source_headings[source].setText(f"{display_name} · {mode_name}")
        quality_visible = source_type == "ndi"
        self.source_quality_checks[source].setVisible(quality_visible)
        self.source_quality_labels[source].setVisible(quality_visible)
        self.source_privacy_buttons[source].setVisible(not quality_visible)
        selector = self.source_selectors[source]
        selector.setEditable(quality_visible)
        self._populate_source_selector(source)

    def refresh_source_options(self, _checked: bool = False, *, discover_ndi: bool = True) -> None:
        self._local_devices = self.context.video.local_devices()
        for source in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ):
            if self.source_type_boxes[source].currentData() == "local":
                self._populate_source_selector(source, preserve_current=True)
        if not discover_ndi:
            return
        for button in self.source_refresh_buttons.values():
            button.setEnabled(False)
        self.status.setText("Refreshing local cameras and discovering NDI sources…")

        async def work() -> str:
            sources = await asyncio.to_thread(self.context.video.discover_ndi_sources, 800)
            return f"Found {len(self._local_devices)} local camera(s) and {len(sources)} NDI source(s)."

        def done(ok: bool, message: str) -> None:
            for button in self.source_refresh_buttons.values():
                button.setEnabled(True)
            if ok:
                for source in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ):
                    if self.source_type_boxes[source].currentData() == "ndi":
                        self._add_discovered_ndi_sources(self.source_selectors[source])
            self.status.setText(message if ok else f"Source discovery failed: {message}")

        run_background(work, done)

    def _populate_source_selector(
        self,
        source: VideoSourceKey,
        *,
        preserve_current: bool = False,
    ) -> None:
        selector = self.source_selectors[source]
        source_type = str(self.source_type_boxes[source].currentData())
        previous_text = selector.currentText() if preserve_current else ""
        previous_data = selector.currentData() if preserve_current else None
        config = self.context.config.integrations.video
        selector.blockSignals(True)
        selector.clear()
        if source_type == "ndi":
            configured = (
                config.audience_ndi_source_name
                if source == VideoSourceKey.AUDIENCE
                else config.ptz_ndi_source_name
            )
            selector.addItem(previous_text or configured)
            self._add_discovered_ndi_sources(selector)
            selector.setCurrentText(previous_text or configured)
            selector.setEnabled(True)
            self.source_connect_buttons[source].setEnabled(True)
        else:
            configured = (
                config.audience_device_id
                if source == VideoSourceKey.AUDIENCE
                else config.ptz_device_id
            )
            for device in self._local_devices:
                suffix = " (Default)" if device.is_default else ""
                selector.addItem(f"{device.name}{suffix}", device.id)
            wanted = previous_data or configured
            if wanted:
                index = selector.findData(wanted)
                if index >= 0:
                    selector.setCurrentIndex(index)
            selector.setEnabled(bool(self._local_devices))
            self.source_connect_buttons[source].setEnabled(bool(self._local_devices))
            if not self._local_devices:
                detail = self.audience_detail if source == VideoSourceKey.AUDIENCE else self.ptz_detail
                detail.setText("No local camera sources were found.")
        selector.blockSignals(False)

    def _add_discovered_ndi_sources(self, selector: QComboBox) -> None:
        current = selector.currentText()
        existing = {selector.itemText(index) for index in range(selector.count())}
        for source_name in self.context.video.discovered_ndi_sources:
            if source_name not in existing:
                selector.addItem(source_name)
                existing.add(source_name)
        selector.setCurrentText(current)

    def connect_video_source(self, source: VideoSourceKey) -> None:
        source_type = str(self.source_type_boxes[source].currentData())
        selector = self.source_selectors[source]
        identifier = selector.currentText().strip() if source_type == "ndi" else selector.currentData()
        if not identifier:
            self.status.setText(
                f"Choose an {'NDI' if source_type == 'ndi' else 'local camera'} source first."
            )
            return
        current = self.context.config.integrations.video
        config = type(current).from_dict(current.to_dict())
        if source == VideoSourceKey.AUDIENCE:
            config.audience_source_type = source_type
            config.audience_enabled = True
            config.audience_highest_bandwidth = self.source_quality_checks[source].isChecked()
            if source_type == "ndi":
                config.audience_ndi_source_name = str(identifier)
            else:
                config.audience_device_id = str(identifier)
        else:
            config.ptz_source_type = source_type
            config.ptz_enabled = True
            config.ptz_highest_bandwidth = self.source_quality_checks[source].isChecked()
            if source_type == "ndi":
                config.ptz_ndi_source_name = str(identifier)
            else:
                config.ptz_device_id = str(identifier)
        self.context.config.integrations.video = config
        self.context.config_repository.save_app_config(self.context.config)
        self.context.video.reconfigure(config)
        self.context.video.start_source(source)
        self.status.setText(f"Connecting {source.value.title()} to {selector.currentText()}…")

    def refresh_ndi_sources(self) -> None:
        self.refresh_source_options()

    def refresh_local_devices(self) -> None:
        self.refresh_source_options(discover_ndi=False)

    def connect_audience(self) -> None:
        self.connect_video_source(VideoSourceKey.AUDIENCE)

    def connect_ptz(self) -> None:
        self.connect_video_source(VideoSourceKey.PTZ)

    @staticmethod
    def open_camera_privacy_settings() -> None:
        QDesktopServices.openUrl(
            QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")
        )

    def toggle_recording(self) -> None:
        try:
            if self.context.video.recorder.recording:
                directory = self.context.video.stop_recording()
                self.status.setText(f"Diagnostic recording saved to {directory}" if directory else "Recording stopped.")
            else:
                directory = self.context.video.start_recording()
                self.status.setText(f"Recording Audience and PTZ diagnostics to {directory}")
        except Exception as exc:
            self.status.setText(f"Recording failed: {exc}")

    def choose_replay(self) -> None:
        start = self.context.video.recorder.last_directory or self.context.video.recordings_root
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Replay Diagnostic Video",
            str(start),
            "Video files (*.mp4 *.mov *.mkv);;All files (*)",
        )
        if not path:
            return
        try:
            self.context.video.start_replay(Path(path))
            self.status.setText(f"Replaying {Path(path).name}")
        except Exception as exc:
            self.status.setText(f"Replay failed: {exc}")

    def reveal_recordings(self) -> None:
        root = self.context.video.recordings_root
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def system_group(self) -> QGroupBox:
        group = QGroupBox("System")
        layout = QGridLayout(group)
        buttons = [
            ("Menu ON", "DUS:1", "aw_cam"),
            ("Menu OFF", "DUS:0", "aw_cam"),
            ("Camera Feed", "DCB:0", "aw_cam"),
            ("Color Bars", "DCB:1", "aw_cam"),
            ("Power ON", "#On", "aw_ptz"),
            ("Standby", "#Of", "aw_ptz"),
            ("AWB", "#AWA", "aw_ptz"),
            ("Menu Up", "DUP:1", "aw_cam"),
            ("Menu Left", "DLT:1", "aw_cam"),
            ("Menu OK", "DIT:1", "aw_cam"),
            ("Menu Right", "DRT:1", "aw_cam"),
            ("Menu Down", "DDW:1", "aw_cam"),
        ]
        for idx, (label, command, endpoint) in enumerate(buttons):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, c=command, e=endpoint: self.send_command(c, e))
            layout.addWidget(button, idx // 2, idx % 2)
        return group

    def ptz_group(self) -> QGroupBox:
        group = QGroupBox("PTZ and Lens")
        layout = QVBoxLayout(group)

        self.pan_speed = self.slider("Pan/Tilt Speed", 1, 49, self.context.config.integrations.panasonic.default_pan_tilt_speed, layout)
        arrows = QWidget()
        arrows_layout = QGridLayout(arrows)
        for label, row, col, command_factory in [
            ("Up", 0, 1, lambda: f"#PTS50{50 + self.pan_speed.value():02d}"),
            ("Left", 1, 0, lambda: f"#PTS{50 - self.pan_speed.value():02d}50"),
            ("Stop", 1, 1, lambda: "#PTS5050"),
            ("Right", 1, 2, lambda: f"#PTS{50 + self.pan_speed.value():02d}50"),
            ("Down", 2, 1, lambda: f"#PTS50{50 - self.pan_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            arrows_layout.addWidget(button, row, col)
        layout.addWidget(arrows)

        self.zoom_speed = self.slider("Zoom Speed", 1, 49, self.context.config.integrations.panasonic.default_zoom_speed, layout)
        zoom_row = QHBoxLayout()
        for label, command_factory in [
            ("Zoom Out", lambda: f"#Z{50 - self.zoom_speed.value():02d}"),
            ("Zoom Stop", lambda: "#Z50"),
            ("Zoom In", lambda: f"#Z{50 + self.zoom_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            zoom_row.addWidget(button)
        layout.addLayout(zoom_row)

        self.focus_speed = self.slider("Focus Speed", 1, 49, self.context.config.integrations.panasonic.default_focus_speed, layout)
        focus_row = QHBoxLayout()
        for label, command_factory in [
            ("Auto", lambda: "#D11"),
            ("Manual", lambda: "#D10"),
            ("Near", lambda: f"#F{50 - self.focus_speed.value():02d}"),
            ("Stop", lambda: "#F50"),
            ("Far", lambda: f"#F{50 + self.focus_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            focus_row.addWidget(button)
        layout.addLayout(focus_row)
        return group

    def slider(self, label: str, low: int, high: int, value: int, parent_layout: QVBoxLayout) -> QSlider:
        row = QHBoxLayout()
        text = QLabel(label)
        value_label = QLabel(str(value))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        slider.valueChanged.connect(lambda val: value_label.setText(str(val)))
        row.addWidget(text)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        parent_layout.addLayout(row)
        return slider

    def preset_group(self) -> QGroupBox:
        group = QGroupBox("Position Presets")
        layout = QVBoxLayout(group)
        self.preset_list = QListWidget()
        self.reload_presets()
        self.preset_list.itemDoubleClicked.connect(lambda _item: self.recall_selected_preset())
        layout.addWidget(self.preset_list)
        row = QHBoxLayout()
        recall = QPushButton("Recall")
        save = QPushButton("Save")
        rename = QPushButton("Rename")
        recall.clicked.connect(self.recall_selected_preset)
        save.clicked.connect(self.save_selected_preset)
        rename.clicked.connect(self.rename_selected_preset)
        row.addWidget(recall)
        row.addWidget(save)
        row.addWidget(rename)
        layout.addLayout(row)
        return group

    def reload_presets(self) -> None:
        if not hasattr(self, "preset_list"):
            self.preset_list = QListWidget()
        selected = self.preset_list.currentItem()
        selected_number = int(selected.data(Qt.ItemDataRole.UserRole)) if selected is not None else 0
        self.preset_list.clear()
        for item in self.context.panasonic_presets.list_presets():
            text = f"Preset {int(item['number']):02d}"
            if item["name"]:
                text += f" - {item['name']}"
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole, int(item["number"]))
            self.preset_list.addItem(row)
            if int(item["number"]) == selected_number:
                self.preset_list.setCurrentItem(row)

    def selected_preset(self) -> int | None:
        item = self.preset_list.currentItem()
        if item is None:
            self.status.setText("Select a preset first.")
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def send_command(self, command: str, endpoint: str = "aw_ptz") -> None:
        self.context.ptz_automation.manual_override(f"Panasonic command {command}")
        self._run_panasonic_action(
            lambda: self.context.panasonic.send_command(command, endpoint),
            f"Sending {command}…",
            "Command sent.",
        )

    def _run_panasonic_action(self, action, pending: str, success: str) -> None:
        self.status.setText(pending)

        def done(ok: bool, message: str) -> None:
            succeeded = ok and message.casefold() not in {"false", "none"}
            self.status.setText(success if succeeded else f"Camera command failed: {message}")

        run_background(
            action,
            done,
        )

    def recall_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is not None:
            self.context.ptz_automation.manual_override(
                f"Preset {number:02d} recall"
            )
            self._run_panasonic_action(
                lambda: self.context.panasonic.recall_preset(number),
                f"Recalling Preset {number:02d}…",
                f"Preset {number:02d} recalled.",
            )

    def save_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is None:
            return
        if number == 0:
            self.status.setText("Preset 00 is Home and cannot be overwritten.")
            return
        self.context.ptz_automation.manual_override(
            f"Preset {number:02d} save"
        )
        self._run_panasonic_action(
            lambda: self.context.panasonic.save_preset(number),
            f"Saving Preset {number:02d}…",
            f"Preset {number:02d} saved.",
        )

    def rename_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is None or number == 0:
            self.status.setText("Preset 00 is Home and cannot be renamed.")
            return
        current = self.context.config.integrations.panasonic.preset_names.get(str(number), "")
        name, ok = QInputDialog.getText(self, "Rename Preset", f"Name for Preset {number:02d}:", text=current)
        if ok:
            self.context.panasonic_presets.rename(number, name)
            self.context.config_repository.save_app_config(self.context.config)
            self.reload_presets()
            self.status.setText(f"Preset {number:02d} renamed.")


def build_page(context) -> QWidget:
    return CameraControlPage(context)
