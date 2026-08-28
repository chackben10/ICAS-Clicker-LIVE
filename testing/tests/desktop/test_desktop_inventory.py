from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QAbstractButton

from production_hub.app.bootstrap import build_context
from production_hub.ui.main_window import MainWindow
from production_hub.ui.pages.common import assign_test_identifiers


@pytest.mark.desktop
def test_actionable_qt_controls_are_identifiable_and_connected(qtbot) -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        context.config.ui.show_menu_bar_icon = False
        window = MainWindow(context)
        qtbot.addWidget(window)
        try:
            assign_test_identifiers(window)
            buttons = [
                button
                for button in window.findChildren(QAbstractButton)
                if not button.objectName().startswith("qt_")
            ]
            assert buttons
            names = [button.objectName() for button in buttons]
            assert all(names)
            assert all(button.accessibleName() for button in buttons)
            for index in range(window.stack.count()):
                page_names = [
                    button.objectName()
                    for button in window.stack.widget(index).findChildren(QAbstractButton)
                    if not button.objectName().startswith("qt_")
                ]
                assert len(page_names) == len(set(page_names)), window.page_names[index]
            actions = [action for action in window.findChildren(QAction) if not action.isSeparator()]
            assert actions
            assert all(action.objectName() for action in actions)
            assert len({action.objectName() for action in actions}) == len(actions)
            navigate_actions = [action for action in window.navigate_menu.actions() if not action.isSeparator()]
            assert [action.text() for action in navigate_actions] == window.page_names
            assert all(action.menuRole() == QAction.MenuRole.NoRole for action in navigate_actions)
            assert window.nav_toggle.objectName() == "SidebarToggle"
            assert window.nav_toggle.receivers("2clicked(bool)") > 0
        finally:
            window.camera_state_timer.stop()
            window.close()
            context.video.shutdown()


@pytest.mark.desktop
def test_every_desktop_page_is_constructed_and_navigable(qtbot) -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        context.config.ui.show_menu_bar_icon = False
        window = MainWindow(context)
        qtbot.addWidget(window)
        try:
            expected = {
                "Overview",
                "Endpoints",
                "Input Lists",
                "Automations",
                "Integrations",
                "MIDI",
                "Camera Control",
                "Scoreboard",
                "Remote Pages",
                "Settings",
            }
            assert set(window.page_names) == expected
            for index, name in enumerate(window.page_names):
                window.nav.setCurrentRow(index)
                assert window.stack.currentIndex() == index, name
                assert window.stack.currentWidget() is not None
        finally:
            window.camera_state_timer.stop()
            window.close()
            context.video.shutdown()
