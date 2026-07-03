from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import dataclass

from production_hub.api.server import create_app
from production_hub.app.bootstrap import ApplicationContext
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_OFFLINE, STATUS_RECONNECTING
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.visca.udp_listener import ViscaUdpListener


@dataclass
class ApiServerHandle:
    thread: threading.Thread
    server: object

    def stop(self) -> None:
        setattr(self.server, "should_exit", True)
        self.thread.join(timeout=5)


def start_api_server(context: ApplicationContext) -> ApiServerHandle:
    try:
        import uvicorn
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Uvicorn is required to run the embedded API server. Install requirements.txt.") from exc

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((context.config.api.bind_host, context.config.api.port))
    except OSError as exc:
        raise RuntimeError(f"API server cannot bind {context.config.api.base_url}: {exc}") from exc
    finally:
        probe.close()

    app = create_app(context)
    config = uvicorn.Config(
        app,
        host=context.config.api.bind_host,
        port=context.config.api.port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, name="production-hub-api", daemon=True)
    thread.start()
    context.health_monitor.update(IntegrationHealth("Remote API Server", STATUS_CONNECTED, context.config.api.base_url))
    return ApiServerHandle(thread=thread, server=server)


async def startup_checks(context: ApplicationContext) -> None:
    if context.config.integrations.obs.enabled:
        context.health_monitor.update(IntegrationHealth("OBS", STATUS_RECONNECTING, context.obs.client.target))
        connected = await context.obs.connect()
        context.health_monitor.update(context.obs.client.health())
        if not connected:
            context.logger.warning("obs_unavailable", "OBS is not currently available", error=context.obs.client.last_error)

    if context.config.integrations.propresenter.enabled:
        try:
            await context.propresenter.health_check()
        except Exception as exc:
            context.propresenter.client.mark_error(str(exc))
            context.logger.warning("propresenter_unavailable", "ProPresenter is not currently available", error=str(exc))
        context.health_monitor.update(context.propresenter.client.health())

    if context.config.integrations.panasonic.enabled:
        try:
            await context.panasonic.test_connection()
        except Exception as exc:
            context.panasonic.client.mark_error(str(exc))
            context.logger.warning("panasonic_unavailable", "Panasonic AWP is not currently available", error=str(exc))
        context.health_monitor.update(context.panasonic.client.health())


async def start_visca_listener(context: ApplicationContext) -> ViscaUdpListener | None:
    if not context.config.integrations.visca.enabled:
        return None

    async def handle(command: PanasonicCommand) -> None:
        await context.panasonic.send_command(command.command, command.endpoint)

    listener = ViscaUdpListener(context.config.integrations.visca, handle)
    try:
        await listener.start()
        target = f"{context.config.integrations.visca.listen_ip}:{context.config.integrations.visca.udp_port}"
        context.health_monitor.update(IntegrationHealth("VISCA Bridge", STATUS_CONNECTED, target))
        return listener
    except Exception as exc:
        target = f"{context.config.integrations.visca.listen_ip}:{context.config.integrations.visca.udp_port}"
        context.health_monitor.update(IntegrationHealth("VISCA Bridge", STATUS_OFFLINE, target, last_error=str(exc)))
        context.logger.warning("visca_unavailable", "VISCA bridge did not start", error=str(exc))
        return None
