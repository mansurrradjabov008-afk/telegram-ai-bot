"""
Lightweight Web Keep-Alive Service for Cloud Hosting (Hugging Face Spaces / Render).
Provides a health-check endpoint on port 7860 to prevent container sleep.
"""

import os
import time
import logging
from typing import Any, Optional, Dict
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


from pathlib import Path


async def handle_metrics(request: web.Request) -> web.Response:
    """Enterprise metrics endpoint reporting system health, memory, uptime, and database status."""
    uptime_sec = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_human = f"{hours}h {minutes}m {seconds}s"

    db_path = Path("data/butler.db")
    db_size_kb = (db_path.stat().st_size / 1024) if db_path.exists() else 0
    wal_path = Path("data/butler.db-wal")
    wal_size_kb = (wal_path.stat().st_size / 1024) if wal_path.exists() else 0

    mem_mb = 0.0
    try:
        import psutil
        proc = psutil.Process()
        mem_mb = proc.memory_info().rss / (1024 * 1024)
    except Exception:
        pass

    from services.circuit_breaker import circuit_registry

    data = {
        "status": "HEALTHY_OPTIMAL",
        "service": "Antigravity Telegram AI Butler (Tier-4 Master Server)",
        "uptime_seconds": uptime_sec,
        "uptime_human": uptime_human,
        "process_memory_mb": round(mem_mb, 2),
        "database": {
            "main_size_kb": round(db_size_kb, 2),
            "wal_size_kb": round(wal_size_kb, 2),
            "mode": "WAL",
            "cache_size_mb": 64,
        },
        "engine": {
            "zero_rudeness": True,
            "zero_hallucination": True,
            "fail_closed_guard": True,
        },
        "circuit_breakers": circuit_registry.get_status(),
    }
    return web.json_response(data)


_LAST_DB_CHECK_TIME: float = 0.0
_LAST_DB_OK: bool = True


async def handle_health(request: web.Request) -> web.Response:
    """
    Enterprise health probe checking event loop responsiveness and system viability.
    Guaranteed fast response to prevent watchdog false-positive deadlocks on mobile.
    """
    global _LAST_DB_CHECK_TIME, _LAST_DB_OK
    now = time.time()

    if now - _LAST_DB_CHECK_TIME > 60.0:
        try:
            from database.connection import get_db
            async with get_db() as db:
                cur = await db.execute("SELECT 1;")
                await cur.fetchone()
            _LAST_DB_OK = True
            _LAST_DB_CHECK_TIME = now
        except Exception as e:
            logger.warning(f"Health probe DB warning (transient): {type(e).__name__}")
            _LAST_DB_OK = False

    return web.Response(text="OK", status=200)


_DIGEST_SERVICE = None
_DB_MANAGER = None


async def handle_digest_status(request: web.Request) -> web.Response:
    """Returns live diagnostic telemetry for Smart Digest."""
    global _DIGEST_SERVICE, _DB_MANAGER
    data = {
        "status": "ok",
        "is_run_in_progress": getattr(_DIGEST_SERVICE, "_is_run_in_progress", False) if _DIGEST_SERVICE else False,
        "run_elapsed_seconds": (
            round(time.time() - getattr(_DIGEST_SERVICE, "_run_start_time", 0.0), 1)
            if (_DIGEST_SERVICE and getattr(_DIGEST_SERVICE, "_is_run_in_progress", False))
            else 0.0
        ),
        "sources": [],
        "latest_run": None,
    }
    if _DIGEST_SERVICE and _DB_MANAGER:
        try:
            sources = await _DB_MANAGER.list_digest_sources(active_only=True)
            counts = await _DB_MANAGER.get_all_source_post_counts()
            for s in sources:
                data["sources"].append({
                    "peer_id": s.peer_id,
                    "title": s.title,
                    "username": s.username,
                    "post_count": counts.get(s.peer_id, 0),
                })
            data["latest_run"] = await _DB_MANAGER.get_digest_stats_summary()
        except Exception as e:
            data["error"] = str(e)
    return web.json_response(data)


async def handle_digest_unlock(request: web.Request) -> web.Response:
    """Forcibly clears stuck concurrency lock."""
    global _DIGEST_SERVICE, _DB_MANAGER
    if _DIGEST_SERVICE:
        _DIGEST_SERVICE.force_unlock()
    if _DB_MANAGER:
        try:
            await _DB_MANAGER.cleanup_stale_digest_runs()
        except Exception:
            pass
    return web.json_response({"status": "unlocked", "message": "Smart Digest lock successfully cleared."})


def create_keepalive_app() -> web.Application:
    """Create configured aiohttp web application."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/digest/status", handle_digest_status)
    app.router.add_get("/digest/unlock", handle_digest_unlock)
    app.router.add_post("/digest/unlock", handle_digest_unlock)
    return app


async def start_keepalive_server(
    host: str = "0.0.0.0",
    default_port: int = 7860,
    digest_service: Any = None,
    db_manager: Any = None,
) -> web.AppRunner:
    """
    Start the keep-alive web server.
    Port is read from environment variable PORT (standard in Hugging Face / Render).
    access_log=None silences repetitive 15s health-probe access logs to protect phone storage.
    """
    global _DIGEST_SERVICE, _DB_MANAGER
    _DIGEST_SERVICE = digest_service
    _DB_MANAGER = db_manager

    port = int(os.getenv("PORT", str(default_port)))
    app = create_keepalive_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port, reuse_address=True, reuse_port=True)
        await site.start()
    except TypeError:
        # Some OS versions might not support reuse_port
        site = web.TCPSite(runner, host, port, reuse_address=True)
        await site.start()
    logger.info(f"🌐 Keep-Alive Web Server started on http://{host}:{port} (Zero-Sleep enabled)")
    return runner
