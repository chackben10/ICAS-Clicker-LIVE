"""The operator's switch between the improved and the earlier subject tracking."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from production_hub.app.bootstrap import build_context


def test_unchecking_improved_tracking_saves_and_falls_back_at_once(qtbot) -> None:
    from production_hub.ui.pages.camera_control_page import CameraControlPage

    with TemporaryDirectory() as directory:
        context = build_context(Path(directory))
        automation = context.ptz_automation
        automation.follow_tracking_setting = True
        automation.apply_tracking_setting()
        page = CameraControlPage(context)
        qtbot.addWidget(page)
        box = page.improved_tracking
        assert box.objectName() == "improved_subject_tracking"
        assert box.isChecked() and automation.tracking_refinements_enabled

        box.setChecked(False)

        assert not automation.tracking_refinements_enabled
        assert not automation.safe_zone_tracking_enabled
        assert not context.video.tracking.validated_boxes_enabled
        saved = context.config_repository.load_app_config()
        assert saved.integrations.camera_tracking.automation.improved_tracking_enabled is False

        box.setChecked(True)
        assert automation.tracking_refinements_enabled
        context.video.shutdown()
