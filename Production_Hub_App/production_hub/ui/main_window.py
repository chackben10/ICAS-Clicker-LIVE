from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QWidget,
)

from production_hub.ui.pages import (
    automations_page,
    camera_control_page,
    data_storage_page,
    diagnostics_page,
    endpoints_page,
    extensions_page,
    integrations_page,
    overview_page,
    remote_pages_page,
    scoreboard_page,
    settings_page,
)


class MainWindow(QMainWindow):
    def __init__(self, context, api_handle=None) -> None:
        super().__init__()
        self.context = context
        self.api_handle = api_handle
        self._quitting = False
        self.tray_icon = None
        self.setWindowTitle("Production Hub")
        self.resize(1180, 780)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Sidebar")
        self.nav.setFixedWidth(220)
        self.stack = QStackedWidget()

        pages = [
            ("Overview", overview_page.build_page(context)),
            ("Endpoints", endpoints_page.build_page(context)),
            ("Automations", automations_page.build_page(context)),
            ("Integrations", integrations_page.build_page(context)),
            ("Camera Control", camera_control_page.build_page(context)),
            ("Scoreboard", scoreboard_page.build_page(context)),
            ("Remote Pages", remote_pages_page.build_page(context)),
            ("Data & Storage", data_storage_page.build_page(context)),
            ("Diagnostics", diagnostics_page.build_page(context)),
            ("Extensions", extensions_page.build_page(context)),
            ("Settings", settings_page.build_page(context)),
        ]
        for name, widget in pages:
            self.nav.addItem(QListWidgetItem(name))
            self.stack.addWidget(widget)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        root.addWidget(self.nav)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(STYLE)
        self.setup_tray_icon()

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
QScrollArea, #PageBody {
  background: #f6f7f8;
}
QLabel {
  background: transparent;
}
#Sidebar {
  background: #202733;
  color: #e8edf3;
  border: 0;
  padding: 12px 8px;
}
#Sidebar::item {
  min-height: 34px;
  padding: 6px 10px;
  border-radius: 6px;
}
#Sidebar::item:selected {
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
#Card {
  background: #ffffff;
  border: 1px solid #dce1e7;
  border-radius: 8px;
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
QLineEdit, QTextEdit, QListWidget {
  background: #ffffff;
  border: 1px solid #cfd6de;
  border-radius: 6px;
  padding: 6px;
}
QAbstractItemView {
  background: #ffffff;
  border: 1px solid #cfd6de;
  selection-background-color: #dbeafe;
  selection-color: #152033;
  padding: 4px;
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


def run_desktop_app(context, api_handle=None) -> int:
    app = QApplication.instance() or QApplication([])
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
