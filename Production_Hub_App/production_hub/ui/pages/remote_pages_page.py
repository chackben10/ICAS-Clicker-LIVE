from __future__ import annotations

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from production_hub.core.config.remote_pages import discover_remote_pages
from production_hub.ui.pages.common import configure_table, scroll_page, title


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Remote Pages", "All HTML control and display pages discovered in this repository."))
    pages = discover_remote_pages(context.workspace_root, context.config.remote_pages)
    table = QTableWidget(len(pages), 8)
    table.setHorizontalHeaderLabels(["Page", "Kind", "Path", "Local URL", "LAN URL", "Status", "Required Integrations", "Source"])
    for row, page in enumerate(pages):
        path = str(page["path"])
        table.setItem(row, 0, QTableWidgetItem(str(page["name"])))
        table.setItem(row, 1, QTableWidgetItem(str(page["kind"])))
        table.setItem(row, 2, QTableWidgetItem(path))
        table.setItem(row, 3, QTableWidgetItem(f"{context.config.api.base_url}/remote/{path}"))
        lan = "" if not context.config.api.lan_access_enabled else f"http://<lan-ip>:{context.config.api.port}/remote/{path}"
        table.setItem(row, 4, QTableWidgetItem(lan))
        table.setItem(row, 5, QTableWidgetItem("Enabled" if page["enabled"] else "Disabled"))
        table.setItem(row, 6, QTableWidgetItem(", ".join(page["required_integrations"])))
        table.setItem(row, 7, QTableWidgetItem(str(page["source"])))
    configure_table(table)
    layout.addWidget(table)
    return scroll
