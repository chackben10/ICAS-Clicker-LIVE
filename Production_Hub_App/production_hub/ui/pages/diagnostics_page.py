from __future__ import annotations

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from production_hub.ui.pages.common import configure_table, scroll_page, title


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Diagnostics", "Health, structured logs, request history, and automation inspector."))
    integrations = context.health_monitor.integration_list()
    table = QTableWidget(len(integrations), 5)
    table.setHorizontalHeaderLabels(["Integration", "Status", "Target", "Last success", "Last error"])
    for row, item in enumerate(integrations):
        table.setItem(row, 0, QTableWidgetItem(item.name))
        table.setItem(row, 1, QTableWidgetItem(item.status))
        table.setItem(row, 2, QTableWidgetItem(item.target))
        table.setItem(row, 3, QTableWidgetItem(item.last_success_at))
        table.setItem(row, 4, QTableWidgetItem(item.last_error))
    configure_table(table)
    layout.addWidget(table)

    state = context.runtime_state_repo.load()
    requests = state.endpoint_request_history[-100:]
    req_table = QTableWidget(len(requests), 6)
    req_table.setHorizontalHeaderLabels(["Timestamp", "Method", "Route", "Status", "Caller", "Duration ms"])
    for row, item in enumerate(requests):
        req_table.setItem(row, 0, QTableWidgetItem(item.timestamp))
        req_table.setItem(row, 1, QTableWidgetItem(item.method))
        req_table.setItem(row, 2, QTableWidgetItem(item.route))
        req_table.setItem(row, 3, QTableWidgetItem(str(item.status_code)))
        req_table.setItem(row, 4, QTableWidgetItem(item.caller_ip))
        req_table.setItem(row, 5, QTableWidgetItem(str(item.duration_ms)))
    configure_table(req_table)
    layout.addWidget(req_table)
    return scroll
