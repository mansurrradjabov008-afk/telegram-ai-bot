"""
Lightweight Web Keep-Alive Service for Cloud Hosting (Hugging Face Spaces / Render).
Provides a health-check endpoint on port 7860 to prevent container sleep.
"""

import os
import time
import logging
from aiohttp import web

logger = logging.getLogger("AntigravityButler.KeepAlive")

START_TIME = time.time()


async def handle_root(request: web.Request) -> web.Response:
    """Root endpoint for status check and keep-alive ping."""
    uptime = int(time.time() - START_TIME)
    data = {
        "status": "online",
        "service": "Antigravity Telegram AI Butler",
        "uptime_seconds": uptime,
        "message": "Butler core is actively monitoring Telegram."
    }
    return web.json_response(data)


async def handle_health(request: web.Request) -> web.Response:
    """Lightweight health probe endpoint."""
    return web.Response(text="OK", status=200)


def create_keepalive_app() -> web.Application:
    """Create configured aiohttp web application."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    return app


async def start_keepalive_server(host: str = "0.0.0.0", default_port: int = 7860) -> web.AppRunner:
    """
    Start the keep-alive web server.
    Port is read from environment variable PORT (standard in Hugging Face / Render).
    """
    port = int(os.getenv("PORT", str(default_port)))
    app = create_keepalive_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"🌐 Keep-Alive Web Server started on http://{host}:{port} (Zero-Sleep enabled)")
    return runner
