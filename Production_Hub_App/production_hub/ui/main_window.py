from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QHBoxLayout,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from production_hub.ui.pages import (
    automations_page,
    camera_control_page,
    endpoints_page,
    input_lists_page,
    integrations_page,
    midi_page,
    overview_page,
    remote_pages_page,
    scoreboard_page,
    settings_page,
)


NAV_EXPANDED_WIDTH = 220
NAV_COLLAPSED_WIDTH = 64
MIN_CONTENT_WIDTH = 720
MIN_WINDOW_HEIGHT = 620


class MainWindow(QMainWindow):
    def __init__(self, context, api_handle=None) -> None:
        super().__init__()
        self.context = context
        self.api_handle = api_handle
        self._quitting = False
        self.tray_icon = None
        self.file_menu = None
        self.edit_menu = None
        self.view_menu = None
        self.navigate_menu = None
        self.tools_menu = None
        self.help_menu = None
        self.full_sidebar_action = None
        self.nav_collapsed = False
        self.page_names: list[str] = []
        self.setWindowTitle("Production Hub")
        self.resize(1180, 780)
        self.setMinimumHeight(MIN_WINDOW_HEIGHT)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav_panel = QWidget()
        self.nav_panel.setObjectName("SidebarPanel")
        self.nav_panel.setFixedWidth(NAV_EXPANDED_WIDTH)
        nav_layout = QVBoxLayout(self.nav_panel)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Sidebar")
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.nav.setIconSize(QSize(20, 20))
        nav_layout.addWidget(self.nav, 1)

        self.nav_toggle = QPushButton()
        self.nav_toggle.setObjectName("SidebarToggle")
        self.nav_toggle.setIconSize(QSize(24, 24))
        self.nav_toggle.setToolTip("Collapse navigation")
        self.nav_toggle.clicked.connect(lambda: self.set_nav_collapsed(not self.nav_collapsed))
        nav_layout.addWidget(self.nav_toggle)

        self.stack = QStackedWidget()

        pages = [
            ("Overview", overview_page.build_page(context)),
            ("Endpoints", endpoints_page.build_page(context)),
            ("Input Lists", input_lists_page.build_page(context)),
            ("Automations", automations_page.build_page(context)),
            ("Integrations", integrations_page.build_page(context)),
            ("MIDI", midi_page.build_page(context)),
            ("Camera Control", camera_control_page.build_page(context)),
            ("Scoreboard", scoreboard_page.build_page(context)),
            ("Remote Pages", remote_pages_page.build_page(context)),
            ("Settings", settings_page.build_page(context)),
        ]
        for name, widget in pages:
            item = QListWidgetItem(nav_icon(name), name)
            item.setToolTip(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setSizeHint(QSize(NAV_EXPANDED_WIDTH - 16, 38))
            self.nav.addItem(item)
            self.stack.addWidget(widget)
            self.page_names.append(name)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        root.addWidget(self.nav_panel)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.apply_theme()
        self.setup_menu_bar()
        self.remove_spinbox_arrows()
        self.set_nav_collapsed(False)
        self.setup_tray_icon()

    def set_nav_collapsed(self, collapsed: bool) -> None:
        self.nav_collapsed = collapsed
        nav_width = NAV_COLLAPSED_WIDTH if collapsed else NAV_EXPANDED_WIDTH
        self.nav_panel.setFixedWidth(nav_width)
        self.nav.setObjectName("SidebarCollapsed" if collapsed else "Sidebar")
        self.nav.setIconSize(QSize(24, 24) if collapsed else QSize(20, 20))
        self.nav_toggle.setIcon(sidebar_toggle_icon(collapsed))
        self.nav_toggle.setToolTip("Expand navigation" if collapsed else "Collapse navigation")
        alignment = Qt.AlignmentFlag.AlignCenter if collapsed else Qt.AlignmentFlag.AlignLeft
        if collapsed:
            self.nav.setViewMode(QListView.ViewMode.IconMode)
            self.nav.setFlow(QListView.Flow.TopToBottom)
            self.nav.setWrapping(False)
            self.nav.setMovement(QListView.Movement.Static)
            self.nav.setResizeMode(QListView.ResizeMode.Fixed)
            self.nav.setGridSize(QSize(nav_width - 16, 46))
            self.nav.setSpacing(4)
        else:
            self.nav.setViewMode(QListView.ViewMode.ListMode)
            self.nav.setGridSize(QSize())
            self.nav.setSpacing(0)
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            item.setText("" if collapsed else self.page_names[row])
            item.setTextAlignment(alignment | Qt.AlignmentFlag.AlignVCenter)
            item.setSizeHint(QSize(nav_width - 16, 44 if collapsed else 38))
        if self.full_sidebar_action is not None:
            self.full_sidebar_action.blockSignals(True)
            self.full_sidebar_action.setChecked(not collapsed)
            self.full_sidebar_action.blockSignals(False)
        self.nav.style().unpolish(self.nav)
        self.nav.style().polish(self.nav)
        self.setMinimumWidth(self.minimum_window_width())
        if self.width() < self.minimumWidth():
            self.resize(self.minimumWidth(), self.height())

    def minimum_window_width(self) -> int:
        nav_width = NAV_COLLAPSED_WIDTH if self.nav_collapsed else NAV_EXPANDED_WIDTH
        return nav_width + MIN_CONTENT_WIDTH

    def remove_spinbox_arrows(self) -> None:
        for spinbox in self.findChildren(QAbstractSpinBox):
            spinbox.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def setup_tray_icon(self) -> None:
        if not self.context.config.ui.show_menu_bar_icon or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = app_icon(self.style())
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("Production Hub")
        menu = QMenu()
        show_action = QAction("Show Production Hub", self)
        show_action.triggered.connect(self.show_from_tray)
        quit_action = QAction("Quit Production Hub", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        self._quitting = True
        self.close()

    def setup_menu_bar(self) -> None:
        self.setup_file_menu()
        self.setup_edit_menu()
        self.setup_view_menu()
        self.setup_navigate_menu()
        self.setup_tools_menu()
        self.setup_help_menu()

    def setup_file_menu(self) -> None:
        self.file_menu = self.menuBar().addMenu("File")
        self.add_menu_action(self.file_menu, "Reload Current Tab", "Ctrl+R", self.reload_current_page)
        self.file_menu.addSeparator()
        self.add_menu_action(self.file_menu, "Quit Production Hub", QKeySequence.StandardKey.Quit, self.quit_from_menu)

    def setup_edit_menu(self) -> None:
        self.edit_menu = self.menuBar().addMenu("Edit")
        self.add_menu_action(self.edit_menu, "Undo", QKeySequence.StandardKey.Undo, self.undo_change)
        self.add_menu_action(self.edit_menu, "Redo", QKeySequence.StandardKey.Redo, self.redo_change)
        self.edit_menu.addSeparator()
        self.add_menu_action(self.edit_menu, "Cut", QKeySequence.StandardKey.Cut, lambda: self.invoke_focused("cut"))
        self.add_menu_action(self.edit_menu, "Copy", QKeySequence.StandardKey.Copy, lambda: self.invoke_focused("copy"))
        self.add_menu_action(self.edit_menu, "Paste", QKeySequence.StandardKey.Paste, lambda: self.invoke_focused("paste"))

    def setup_view_menu(self) -> None:
        self.view_menu = self.menuBar().addMenu("View")
        self.full_sidebar_action = QAction("Full Sidebar", self)
        self.full_sidebar_action.setCheckable(True)
        self.full_sidebar_action.setChecked(True)
        self.full_sidebar_action.setShortcut(QKeySequence("Ctrl+B"))
        self.full_sidebar_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.full_sidebar_action.triggered.connect(lambda checked: self.set_nav_collapsed(not checked))
        self.view_menu.addAction(self.full_sidebar_action)
        self.addAction(self.full_sidebar_action)
        self.view_menu.addSeparator()
        self.add_menu_action(self.view_menu, "Reset Window Size", "Ctrl+Shift+0", self.reset_window_size)

    def setup_navigate_menu(self) -> None:
        self.navigate_menu = self.menuBar().addMenu("Navigate")
        for index, name in enumerate(self.page_names):
            shortcut_number = "0" if index == 9 else str(index + 1)
            shortcut = f"Ctrl+{shortcut_number}" if index < 10 else ""
            self.add_menu_action(
                self.navigate_menu,
                name,
                shortcut,
                lambda _checked=False, page_index=index: self.show_page(page_index),
            )

    def setup_tools_menu(self) -> None:
        self.tools_menu = self.menuBar().addMenu("Tools")
        self.add_menu_action(self.tools_menu, "Pause All Automations", "Ctrl+Shift+P", self.pause_all_automations)
        self.add_menu_action(self.tools_menu, "Resume Automations", "Ctrl+Shift+G", self.resume_automations)
        self.tools_menu.addSeparator()
        self.add_menu_action(self.tools_menu, "Open Endpoint Builder", "", lambda: self.show_page_by_name("Endpoints"))
        self.add_menu_action(self.tools_menu, "Open Input Lists", "", lambda: self.show_page_by_name("Input Lists"))
        self.add_menu_action(self.tools_menu, "Open Automation Builder", "", lambda: self.show_page_by_name("Automations"))
        self.add_menu_action(self.tools_menu, "Open Remote Pages", "", lambda: self.show_page_by_name("Remote Pages"))
        self.add_menu_action(self.tools_menu, "Open Integration Diagnostics", "", lambda: self.show_page_by_name("Integrations"))
        self.add_menu_action(self.tools_menu, "Open Settings", "", lambda: self.show_page_by_name("Settings"))

    def setup_help_menu(self) -> None:
        self.help_menu = self.menuBar().addMenu("Help")
        self.add_menu_action(self.help_menu, "About Production Hub", "", self.show_about_dialog)

    def add_menu_action(self, menu: QMenu, label: str, sequence, handler) -> QAction:
        action = QAction(label, self)
        if isinstance(sequence, QKeySequence):
            action.setShortcut(sequence)
        elif isinstance(sequence, str):
            if sequence:
                action.setShortcut(QKeySequence(sequence))
        else:
            action.setShortcuts(sequence)
        action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        action.triggered.connect(handler)
        menu.addAction(action)
        self.addAction(action)
        return action

    def invoke_focused(self, method_name: str) -> None:
        widget = QApplication.focusWidget()
        method = getattr(widget, method_name, None) if widget is not None else None
        if callable(method):
            method()

    def undo_change(self) -> None:
        self.show_history_message(self.context.undo_manager.undo())

    def redo_change(self) -> None:
        self.show_history_message(self.context.undo_manager.redo())

    def show_history_message(self, message: str) -> None:
        self.show_status_message(message)

    def show_status_message(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)
        current = self.stack.currentWidget()
        status = getattr(current, "status", None)
        if status is not None:
            status.setText(message)

    def show_page(self, index: int) -> None:
        if 0 <= index < self.stack.count():
            self.nav.setCurrentRow(index)

    def show_page_by_name(self, name: str) -> None:
        if name in self.page_names:
            self.show_page(self.page_names.index(name))

    def reload_current_page(self) -> None:
        current = self.stack.currentWidget()
        reload_method = getattr(current, "reload", None)
        if callable(reload_method):
            reload_method()
            self.show_status_message("Current tab reloaded.")
            return
        self.show_status_message("This tab does not have a reload action.")

    def reset_window_size(self) -> None:
        self.resize(1180, 780)
        self.set_nav_collapsed(False)

    def pause_all_automations(self) -> None:
        self.context.automation_engine.pause_all()
        self.show_status_message("All automations paused.")

    def resume_automations(self) -> None:
        self.context.automation_engine.resume_all()
        self.show_status_message("Automations resumed.")

    def quit_from_menu(self) -> None:
        self._quitting = True
        self.close()

    def show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            "About Production Hub",
            "Production Hub\n\nLocal control surface for endpoints, automations, remote pages, camera control, and scoreboard tools.",
        )

    def closeEvent(self, event) -> None:
        if self.context.config.ui.keep_running_after_window_close and not self._quitting:
            self.hide()
            event.ignore()
            return
        if self.tray_icon:
            self.tray_icon.hide()
        if self.api_handle:
            self.api_handle.stop()
        event.accept()

    def apply_theme(self) -> None:
        theme = normalized_theme(self.context.config.ui.theme)
        apply_application_theme(QApplication.instance(), theme)
        effective = effective_theme(QApplication.instance(), theme)
        self.setStyleSheet(STYLE + (DARK_STYLE_OVERRIDES if effective == "dark" else LIGHT_STYLE_OVERRIDES))


def nav_icon(name: str) -> QIcon:
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#e8edf3"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "Overview":
        for x, y in [(6, 6), (16, 6), (6, 16), (16, 16)]:
            painter.drawRoundedRect(QRectF(x, y, 6, 6), 1.5, 1.5)
    elif name == "Endpoints":
        painter.drawLine(10, 14, 18, 8)
        painter.drawLine(10, 14, 18, 20)
        for x, y in [(6, 11), (17, 5), (17, 17)]:
            painter.drawEllipse(QRectF(x, y, 6, 6))
    elif name == "Input Lists":
        for y in [7, 13, 19]:
            painter.drawLine(9, y, 23, y)
            painter.drawEllipse(QRectF(5, y - 2, 4, 4))
    elif name == "Automations":
        painter.drawEllipse(QRectF(5, 5, 18, 18))
        painter.setBrush(QColor("#e8edf3"))
        painter.drawPolygon(QPolygonF([QPointF(12, 9), QPointF(12, 19), QPointF(19, 14)]))
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "Integrations":
        painter.drawRoundedRect(QRectF(6, 10, 10, 8), 2, 2)
        painter.drawLine(16, 14, 22, 14)
        painter.drawLine(8, 8, 8, 11)
        painter.drawLine(14, 8, 14, 11)
        painter.drawLine(21, 10, 21, 18)
    elif name == "MIDI":
        painter.drawLine(6, 20, 6, 8)
        painter.drawLine(10, 20, 10, 11)
        painter.drawLine(14, 20, 14, 7)
        painter.drawLine(18, 20, 18, 12)
        painter.drawLine(22, 20, 22, 9)
        for x, y in [(4, 6), (8, 9), (12, 5), (16, 10), (20, 7)]:
            painter.drawEllipse(QRectF(x, y, 4, 4))
    elif name == "Camera Control":
        painter.drawRoundedRect(QRectF(5, 9, 18, 12), 2, 2)
        painter.drawRoundedRect(QRectF(8, 6, 6, 4), 1, 1)
        painter.drawEllipse(QRectF(12, 12, 5, 5))
    elif name == "Scoreboard":
        painter.drawRoundedRect(QRectF(5, 7, 18, 14), 2, 2)
        painter.drawLine(10, 11, 18, 11)
        painter.drawLine(10, 16, 18, 16)
        painter.drawLine(14, 9, 14, 19)
    elif name == "Remote Pages":
        painter.drawRoundedRect(QRectF(5, 7, 18, 13), 2, 2)
        painter.drawLine(11, 23, 17, 23)
        painter.drawLine(14, 20, 14, 23)
    elif name == "Data & Storage":
        painter.drawEllipse(QRectF(6, 6, 16, 6))
        painter.drawLine(6, 9, 6, 19)
        painter.drawLine(22, 9, 22, 19)
        painter.drawEllipse(QRectF(6, 16, 16, 6))
    elif name == "Diagnostics":
        painter.drawLine(5, 15, 9, 15)
        painter.drawLine(9, 15, 12, 9)
        painter.drawLine(12, 9, 16, 20)
        painter.drawLine(16, 20, 19, 13)
        painter.drawLine(19, 13, 23, 13)
    elif name == "Settings":
        painter.drawEllipse(QRectF(10, 10, 8, 8))
        for x1, y1, x2, y2 in [(14, 4, 14, 8), (14, 20, 14, 24), (4, 14, 8, 14), (20, 14, 24, 14), (7, 7, 10, 10), (18, 18, 21, 21), (21, 7, 18, 10), (10, 18, 7, 21)]:
            painter.drawLine(x1, y1, x2, y2)
    else:
        painter.drawRoundedRect(QRectF(6, 6, 16, 16), 3, 3)

    painter.end()
    return QIcon(pixmap)


def sidebar_toggle_icon(collapsed: bool) -> QIcon:
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#e8edf3"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(5, 6, 18, 16), 3, 3)
    painter.drawLine(11, 7, 11, 21)
    if collapsed:
        painter.drawLine(14, 11, 18, 14)
        painter.drawLine(18, 14, 14, 17)
    else:
        painter.drawLine(18, 11, 14, 14)
        painter.drawLine(14, 14, 18, 17)
    painter.end()
    return QIcon(pixmap)


STYLE = """
QMainWindow {
  background: #f6f7f8;
  color: #1c2430;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI";
  font-size: 13px;
}
QWidget {
  color: #1c2430;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI";
  font-size: 13px;
}
QMenuBar {
  background: #f6f7f8;
  color: #1c2430;
  border-bottom: 1px solid #dce1e7;
  padding: 2px 8px;
}
QMenuBar::item {
  background: transparent;
  padding: 5px 10px;
  border-radius: 5px;
}
QMenuBar::item:selected {
  background: #e8edf3;
}
QStatusBar {
  background: #f6f7f8;
  color: #46515f;
  border-top: 1px solid #dce1e7;
}
QScrollArea, #PageBody {
  background: #f6f7f8;
}
QLabel {
  background: transparent;
}
#Sidebar, #SidebarCollapsed {
  background: #202733;
  color: #e8edf3;
  border: 0;
}
#Sidebar {
  padding: 6px 8px 12px 8px;
}
#SidebarCollapsed {
  padding: 6px 8px 10px 8px;
}
#SidebarPanel {
  background: #202733;
  border: 0;
}
#SidebarToggle {
  background: #202733;
  color: #e8edf3;
  border: 0;
  border-radius: 0;
  min-height: 46px;
  padding: 6px 8px 10px 8px;
}
#SidebarToggle:hover {
  background: #2b3544;
}
#Sidebar::item {
  min-height: 34px;
  padding: 6px 10px;
  border-radius: 6px;
}
#Sidebar::item:selected {
  background: #3a4656;
}
#SidebarCollapsed::item {
  min-height: 42px;
  padding: 7px 0px;
  border-radius: 8px;
}
#SidebarCollapsed::item:selected {
  background: #3a4656;
}
#PageTitle {
  font-size: 26px;
  font-weight: 700;
}
#PageSubtitle {
  color: #5c6673;
}
#HelpText, #StatusText {
  color: #46515f;
}
#BuilderSidebarPanel {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
}
#BuilderEditorScroll {
  background: transparent;
  border: 0;
}
#BuilderEditor {
  background: #f6f7f8;
}
#BuilderPanelTitle {
  font-size: 15px;
  font-weight: 800;
  color: #1c2430;
}
#InlineSectionLabel {
  color: #46515f;
  font-weight: 700;
}
#BuilderSection {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
  margin-top: 18px;
  padding: 12px;
  font-weight: 700;
}
#BuilderSection::title {
  subcontrol-origin: margin;
  left: 12px;
  padding: 0 5px;
  background: #ffffff;
  color: #1c2430;
}
#BuilderList {
  border-radius: 6px;
}
#BuilderStepList {
  background: #ffffff;
  border: 1px solid #cfd6de;
  border-radius: 6px;
  padding: 4px;
}
#BuilderStepList::item {
  min-height: 28px;
  padding: 5px 7px;
  border-radius: 5px;
}
#BuilderStepList::item:selected {
  background: #dbeafe;
  color: #152033;
}
#SequenceListPanel {
  background: transparent;
}
#SequenceEditorPanel {
  background: #ffffff;
  border: 1px solid #e5e9ee;
  border-radius: 6px;
}
#Card {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
}
#IntegrationCard {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
}
#IntegrationCard[disabledIntegration="true"] {
  background: #eef1f4;
  border-color: #d3dae2;
}
#CardTitle {
  font-size: 15px;
  font-weight: 700;
}
#MetaLabel {
  color: #64707d;
  font-weight: 600;
}
QPushButton {
  background: #263548;
  color: #ffffff;
  border: 0;
  border-radius: 6px;
  padding: 7px 10px;
}
QPushButton:hover {
  background: #33455d;
}
QTableWidget {
  background: #ffffff;
  alternate-background-color: #f8fafc;
  border: 1px solid #dce1e7;
  border-radius: 8px;
  gridline-color: #e5e9ee;
  selection-background-color: #dbeafe;
  selection-color: #152033;
}
QHeaderView::section {
  background: #edf1f5;
  color: #2a3441;
  border: 0;
  min-height: 34px;
  padding: 7px 10px;
  font-weight: 700;
}
QGroupBox {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
  margin-top: 18px;
  padding: 12px;
  font-weight: 700;
}
QGroupBox::title {
  subcontrol-origin: margin;
  left: 12px;
  padding: 0 4px;
  background: #ffffff;
}
QLineEdit, QTextEdit, QListWidget, QSpinBox, QDoubleSpinBox {
  background: #ffffff;
  border: 1px solid #cfd6de;
  border-radius: 6px;
  padding: 6px;
}
QSpinBox, QDoubleSpinBox {
  min-height: 30px;
}
QSpinBox, QDoubleSpinBox {
  padding-right: 6px;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
  width: 0px;
  border: 0;
}
QSpinBox::up-arrow, QSpinBox::down-arrow,
QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {
  width: 0px;
  height: 0px;
}
#TargetList {
  min-height: 86px;
}
#CodeEditor {
  font-family: Menlo, Monaco, "Courier New";
  font-size: 12px;
}
#ScoreRowCard {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
}
#ScoreSummaryBar {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
}
#SummaryText {
  color: #46515f;
  font-weight: 700;
}
#ScoreValue {
  font-size: 24px;
  font-weight: 800;
  color: #1c2430;
  background: transparent;
}
#QueueTotal {
  font-size: 22px;
  font-weight: 800;
  color: #1c2430;
  background: transparent;
}
#TableScoreValue {
  font-size: 20px;
  font-weight: 800;
  color: #1c2430;
  background: transparent;
}
#InlineNumericEdit {
  max-width: 92px;
}
#DangerButton {
  background: #b42318;
}
#DangerButton:hover {
  background: #c83127;
}
"""

LIGHT_STYLE_OVERRIDES = """
QMenu {
  color: #1c2430;
}
"""

DARK_STYLE_OVERRIDES = """
QMainWindow {
  background: #1f2329;
  color: #f2f5f8;
}
QWidget {
  color: #f2f5f8;
}
QMenu {
  color: #f2f5f8;
}
QMenuBar {
  background: #1f2329;
  color: #f2f5f8;
  border-bottom: 1px solid #343b45;
}
QMenuBar::item:selected {
  background: #343b45;
}
QStatusBar, QScrollArea, #PageBody {
  background: #1f2329;
  color: #c9d1db;
}
#SidebarPanel, #Sidebar, #SidebarCollapsed, #SidebarToggle {
  background: #161a20;
}
#Sidebar::item, #SidebarCollapsed::item {
  color: #dce4ed;
}
#Sidebar::item:selected, #SidebarCollapsed::item:selected {
  background: #2f6fed;
  color: #ffffff;
}
#PageTitle {
  color: #f6f8fb;
}
#PageSubtitle, #HelpText, #MetaLabel, #SummaryText, #StatusText {
  color: #b8c2cf;
}
#SectionTitle, #InlineSectionLabel, #CardTitle {
  color: #f2f5f8;
}
#BuilderSidebarPanel, #BuilderSection, #Card, #IntegrationCard, QGroupBox, #SequenceEditorPanel, #ScoreRowCard, #ScoreSummaryBar {
  background: #282e36;
  border: 1px solid #3b444f;
}
#IntegrationCard[disabledIntegration="true"] {
  background: #222830;
  border-color: #343b45;
}
#BuilderEditor, #BuilderEditorScroll {
  background: #1f2329;
  border: 0;
}
#BuilderPanelTitle {
  color: #f2f5f8;
}
#BuilderSection::title {
  background: #282e36;
  color: #f2f5f8;
}
#SequenceListPanel {
  background: transparent;
}
QGroupBox::title {
  background: #282e36;
  color: #f2f5f8;
}
QLineEdit, QTextEdit, QListWidget, QSpinBox, QDoubleSpinBox, #BuilderStepList {
  background: #171b21;
  color: #f2f5f8;
  border: 1px solid #46515f;
  selection-background-color: #2f6fed;
  selection-color: #ffffff;
}
QTableWidget, #BuilderList {
  background: #171b21;
  alternate-background-color: #20262e;
  color: #f2f5f8;
  border: 1px solid #3b444f;
  gridline-color: #343b45;
  selection-background-color: #2f6fed;
  selection-color: #ffffff;
}
QHeaderView::section {
  background: #282e36;
  color: #f2f5f8;
}
#BuilderStepList::item:selected {
  background: #2f6fed;
  color: #ffffff;
}
#ScoreValue, #QueueTotal, #TableScoreValue {
  color: #f6f8fb;
}
"""


def normalized_theme(value: str) -> str:
    theme = str(value or "system").strip().lower()
    return theme if theme in {"system", "light", "dark"} else "system"


def effective_theme(app: QApplication | None, theme: str) -> str:
    if theme in {"light", "dark"}:
        return theme
    if app is not None:
        try:
            if app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
                return "dark"
        except Exception:
            pass
    return "light"


def apply_application_theme(app: QApplication | None, theme: str) -> None:
    if app is None:
        return
    try:
        color_scheme = {
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
            "system": Qt.ColorScheme.Unknown,
        }[theme]
        app.styleHints().setColorScheme(color_scheme)
    except Exception:
        pass


def run_desktop_app(context, api_handle=None) -> int:
    app = QApplication.instance() or QApplication([])
    apply_application_theme(app, normalized_theme(context.config.ui.theme))
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    app.setWindowIcon(app_icon(app.style()))
    window = MainWindow(context, api_handle)
    window.setWindowIcon(app_icon(window.style()))
    window.show()
    return app.exec()


def app_icon(style=None) -> QIcon:
    icon_candidates: list[Path] = []
    for root in app_resource_roots():
        icon_candidates.extend(
            [
                root / "assets" / "ProductionHub.icns",
                root / "assets" / "production_hub_icon.icns",
                root / "ProductionHub.icns",
            ]
        )
    for icon_path in icon_candidates:
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                return icon
    if style is not None:
        return style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    return QIcon()


def app_resource_roots() -> list[Path]:
    roots: list[Path] = []
    bundle_resource_root = Path(sys.executable).resolve().parents[1] / "Resources" if getattr(sys, "frozen", False) else None
    if bundle_resource_root is not None:
        roots.append(bundle_resource_root)
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        roots.append(Path(pyinstaller_root))
    roots.append(Path(__file__).resolve().parents[2])
    return roots
