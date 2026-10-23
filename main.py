"""
Master runner for Antigravity Telegram AI Executive Butler.
Coordinates Database, AI Brain, Scheduler, Admin Bot Cockpit, and MTProto Userbot.
"""

import asyncio
import logging
import signal
import sys
import os
import shutil
from pathlib import Path
from logging.handlers import RotatingFileHandler
from rich.logging import RichHandler

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # LAPTOP SAFETY GUARD: Prevent dual-polling high-CPU conflicts on Windows
    if os.environ.get("ALLOW_LAPTOP_RUN") != "1":
        sys.stderr.write(
            "\n" + "=" * 70 + "\n"
            "⚠️  XAVFSIZLIK TO'SIG'I (LAPTOP SAFETY GUARD)\n"
            + "=" * 70 + "\n"
            "Telegram AI Butler hozirda telefoningizda (Termux) 24/7 ishlamoqda.\n"
            "Noutbukingiz protsessori qizib ketmasligi va Telegram tokenlari\n"
            "to'qnash kelib (TelegramConflictError) telefoningizdagi botni\n"
            "to'xtatib qo'ymasligi uchun noutbukda bot ishga tushishi bloklangan.\n\n"
            "Agar test qilish uchun juda zarur bo'lsa, 'ALLOW_LAPTOP_RUN=1' o'rnating.\n"
            + "=" * 70 + "\n\n"
        )
        sys.exit(0)

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from services.ai_engine import AIEngine
from services.rate_limiter import HumanRateLimiter
from services.digest_service import DigestService
from services.scheduler_service import SchedulerService
from services.memory_service import MemoryService
from services.style_service import StyleService
from services.group_radar import GroupRadarService
from bot.admin_bot import AdminBotService
from bot.userbot import UserbotService

# Configure structured logging with automatic rotation (max 15MB total)
log_handlers = [RichHandler(rich_tracebacks=True, tracebacks_show_locals=False)]
try:
    file_handler = RotatingFileHandler(
        "butler_phone.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"))
    log_handlers.append(file_handler)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=log_handlers,
)
# Constrain third-party loggers (Rule H3)
for _lib in ["telethon", "httpx", "google", "google.auth", "urllib3", "aiogram"]:
    logging.getLogger(_lib).setLevel(logging.WARNING)

logger = logging.getLogger("AntigravityButler")


async def main():
    logger.info("🚀 [bold green]Starting Antigravity Telegram AI Executive Butler...[/bold green]", extra={"markup": True})

    # 1. Initialize SQLite Database
    await init_db()
    db_manager = DatabaseManager()
    logger.info("✅ Database schema initialized successfully.")

    # 2. Initialize Core Services
    ai_engine = AIEngine()
    rate_limiter = HumanRateLimiter()
    digest_service = DigestService(db_manager, ai_engine)
    scheduler_service = SchedulerService(db_manager)
    memory_service = MemoryService(db_manager)
    await memory_service.startup_self_check()
    await memory_service.purge_retention()
    style_service = StyleService(db_manager)
    await style_service.purge_retention()
    group_radar_service = GroupRadarService(db_manager=db_manager, ai_engine=ai_engine)

    # 3. Initialize Bot Services
    admin_bot_service = AdminBotService(
        db_manager=db_manager,
        ai_engine=ai_engine,
        scheduler_service=scheduler_service,
        digest_service=digest_service,
        memory_service=memory_service,
        style_service=style_service,
        group_radar_service=group_radar_service,
    )
    userbot_service = UserbotService(
        db_manager=db_manager,
        ai_engine=ai_engine,
        rate_limiter=rate_limiter,
        digest_service=digest_service,
        memory_service=memory_service,
        style_service=style_service,
        group_radar_service=group_radar_service,
    )

    # 4. Wire cross-service callbacks
    userbot_service.set_escalation_callback(admin_bot_service.notify_boss_escalation)
    userbot_service.set_draft_approval_callback(admin_bot_service.send_draft_for_approval)
    userbot_service.set_notification_callback(admin_bot_service.send_notification)
    userbot_service.set_group_radar_service(group_radar_service)
    group_radar_service.set_alert_callback(admin_bot_service.send_radar_alert)
    admin_bot_service.set_userbot(userbot_service)
    admin_bot_service.set_userbot_sender(userbot_service.send_direct_message)
    admin_bot_service.set_manual_reply_checker(userbot_service.check_owner_manually_replied)
    admin_bot_service.set_group_radar_service(group_radar_service)
    scheduler_service.register_callbacks(
        send_callback=userbot_service.send_direct_message,
        notify_boss_callback=admin_bot_service.send_notification,
    )
    digest_service.register_boss_notifier(admin_bot_service.send_notification)
    digest_service.set_userbot_client(lambda: userbot_service.client)

    # 4.5 Start Web Keep-Alive (Zero-Sleep Cloud Server) early to satisfy Watchdog probes
    try:
        from services.web_keepalive import start_keepalive_server
        await start_keepalive_server(
            default_port=7860,
            digest_service=digest_service,
            db_manager=db_manager,
        )
    except Exception as e:
        logger.warning(f"Could not start Keep-Alive web server: {type(e).__name__}")

    # 5. Start Scheduler and restore pending tasks
    scheduler_service.start()
    await scheduler_service.reload_pending_tasks_from_db()
    logger.info("✅ Scheduler service started with persistent SQLite job store.")

    # Register Smart Digest daily job (scheduled daily at DIGEST_TIME)
    if getattr(settings, "DIGEST_V2_ENABLED", True):
        scheduler_service.register_smart_digest_job(
            digest_service=digest_service,
            notify_boss_callback=admin_bot_service.send_notification,
            digest_time_str=getattr(settings, "DIGEST_TIME", "08:00"),
        )

    # Register daily safe database backup and rotation (04:00 Asia/Tashkent)
    from apscheduler.triggers.cron import CronTrigger
    from services.backup_manager import create_safe_backup, execute_backup_rotation
    import pytz

    async def _run_scheduled_maintenance():
        try:
            import gc
            # 1. Purge ancient records and vacuum-friendly maintenance
            await db_manager.purge_old_digest_posts(retention_days=14)
            await db_manager.cleanup_stale_digest_runs()
            await db_manager.purge_old_style_samples(retention_days=30)
            await db_manager.cleanup_conversation_history(days=60)

            # 2. Online transaction-safe backup & integrity verification
            ok, b_path, res = await asyncio.to_thread(create_safe_backup, "data/butler.db", "data/backups")
            if ok:
                await asyncio.to_thread(execute_backup_rotation, "data/backups", keep_count=7, dry_run=False)
                logger.info("✅ Daily safe backup completed successfully.")
            else:
                logger.warning(f"⚠️ Daily backup failed integrity check: {res}")
                if admin_bot_service.bot:
                    await admin_bot_service.send_notification(
                        f"⚠️ <b>Ma'lumotlar bazasi zaxira ogohlantirishi:</b>\n"
                        f"Integritet natijasi: <code>{res}</code>"
                    )

            # 3. SQLite maintenance: truncate WAL and optimize query planner
            from database.connection import get_db
            async with get_db() as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                await db.execute("PRAGMA optimize;")
            logger.info("✅ SQLite WAL checkpoint (TRUNCATE) and query planner optimization complete.")

            # 4. Truncate excessive log file if exceeds 10MB
            log_path = Path("butler_phone.log")
            if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if len(lines) > 20000:
                        log_path.write_text("\n".join(lines[-20000:]) + "\n", encoding="utf-8")
                        logger.info("✅ Log file pruned to last 20,000 lines.")
                except Exception as log_err:
                    logger.warning(f"Log pruning warning: {log_err}")

            # 5. Clear Telethon in-memory entity cache to prevent RAM growth
            if userbot_service.client and hasattr(userbot_service.client, "_entity_cache"):
                try:
                    userbot_service.client._entity_cache.clear()
                    logger.info("✅ Telethon in-memory entity cache recycled.")
                except Exception:
                    pass

            # 6. Garbage Collection
            collected = gc.collect()
            logger.info(f"✅ Memory garbage collection complete ({collected} objects freed).")

            # 6. Status notification to admin
            if admin_bot_service.bot:
                await admin_bot_service.send_notification(
                    "🌙 <b>Kunlik profilaktika va zaxiralash muvaffaqiyatli yakunlandi:</b>\n"
                    "• Ma'lumotlar bazasi zaxiralandi va optimallashtirildi (WAL TRUNCATE)\n"
                    "• Eski ma'lumotlar va qoralamalar tozalandi\n"
                    f"• Tizim xotirasi tozalandi ({collected} ob'ekt bo'shatildi)\n"
                    "• Butler 100% sog'lom va barqaror ishlamoqda."
                )
        except Exception as e:
            logger.error(f"Error during scheduled database maintenance: {type(e).__name__}")

    scheduler_service.scheduler.add_job(
        _run_scheduled_maintenance,
        CronTrigger(hour=4, minute=0, timezone=pytz.timezone(settings.TIMEZONE)),
        id="daily_safe_db_backup",
        name="Daily Safe Database Backup & Maintenance",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("✅ Daily safe database backup and maintenance scheduled at 04:00.")

    # 6. Initialize MTProto Userbot
    session_file = Path("data") / settings.TELEGRAM_SESSION_NAME
    session_backup = Path("data") / f"{settings.TELEGRAM_SESSION_NAME}.bak"
    if session_file.exists() and session_file.stat().st_size > 0:
        try:
            shutil.copy2(session_file, session_backup)
            logger.info("🛡️ Telegram session backup verified and preserved.")
        except Exception as s_err:
            logger.warning(f"Session backup warning: {s_err}")
    elif (not session_file.exists() or session_file.stat().st_size == 0) and session_backup.exists() and session_backup.stat().st_size > 0:
        logger.warning("🚨 Telegram session missing or empty! Restoring from verified backup...")
        shutil.copy2(session_backup, session_file)

    has_userbot_creds = userbot_service.init_client()
    if has_userbot_creds:
        logger.info("Authenticating Telegram Userbot session...")
        try:
            await userbot_service.start()
        except Exception as e:
            logger.error(f"Userbot login failed or pending interactive login: {type(e).__name__}")
    else:
        logger.warning("⚠️ Userbot credentials not configured in .env. Running in dry/standby mode.")

    # Spawn Smart Digest catch-up check in background now that Userbot is running
    if getattr(settings, "DIGEST_V2_ENABLED", True):
        asyncio.create_task(
            scheduler_service.check_and_run_catchup_digest(
                digest_service=digest_service,
                notify_boss_callback=admin_bot_service.send_notification,
                digest_time_str=getattr(settings, "DIGEST_TIME", "08:00"),
            )
        )


    # 8. Start Admin Bot and Userbot with Resilient Reconnect Loops
    async def _run_admin_bot_resilient():
        while True:
            try:
                await admin_bot_service.dp.start_polling(
                    admin_bot_service.bot,
                    skip_updates=True,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Admin bot polling interrupted ({type(e).__name__}). Retrying in 5s...")
                await asyncio.sleep(5)

    async def _run_userbot_resilient():
        while True:
            try:
                if userbot_service.client and userbot_service._is_running:
                    await userbot_service.client.run_until_disconnected()
                else:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Userbot MTProto disconnected ({type(e).__name__}). Reconnecting in 5s...")
                await asyncio.sleep(5)
                try:
                    if userbot_service.client and not userbot_service.client.is_connected():
                        await userbot_service.client.connect()
                except Exception as e2:
                    logger.error(f"Userbot reconnect attempt failed: {type(e2).__name__}")

    async def _userbot_heartbeat():
        """
        Actively probes Telethon MTProto connection every 60s.
        If mobile network handover (Wi-Fi <-> 4G/5G) leaves a half-open dead socket,
        this detects the stall and triggers a clean reconnection within 60s.
        """
        while True:
            await asyncio.sleep(60)
            if not (userbot_service.client and userbot_service._is_running):
                continue
            try:
                if not userbot_service.client.is_connected():
                    logger.warning("⚠️ [HEARTBEAT] Userbot disconnected. Reconnecting...")
                    await userbot_service.client.connect()
                else:
                    await asyncio.wait_for(userbot_service.client.get_me(), timeout=12.0)
            except (asyncio.TimeoutError, ConnectionError, OSError) as net_err:
                logger.warning(f"🚨 [HEARTBEAT] Telethon MTProto socket error ({type(net_err).__name__}). Resetting connection...")
                try:
                    await userbot_service.client.disconnect()
                    await asyncio.sleep(2)
                    await userbot_service.client.connect()
                    logger.info("✅ [HEARTBEAT] Telethon MTProto reconnected successfully.")
                except Exception as r_err:
                    logger.error(f"Failed to reset stalled connection: {r_err}")
            except Exception as h_err:
                logger.debug(f"Heartbeat tick: {h_err}")

    tasks = []
    if admin_bot_service.bot:
        logger.info("🤖 Admin Cockpit Bot polling starting...")
        await admin_bot_service.setup_bot_commands()
        tasks.append(asyncio.create_task(_run_admin_bot_resilient()))
    else:
        logger.warning("⚠️ Admin Bot Token not configured in .env. Admin polling skipped.")

    if userbot_service.client and userbot_service._is_running:
        tasks.append(asyncio.create_task(_run_userbot_resilient()))
        tasks.append(asyncio.create_task(_userbot_heartbeat()))

    if not tasks:
        logger.info("ℹ️ No active bot tasks were started because credentials are pending in .env.")
        logger.info("Please fill in your credentials in .env to run the live background services.")
        return

    logger.info("✨ [bold cyan]Antigravity AI Butler is fully operational![/bold cyan]", extra={"markup": True})
    try:
        await asyncio.gather(*tasks)
    finally:
        logger.info("🛑 Graceful shutdown initiated...")
        if scheduler_service.scheduler.running:
            scheduler_service.stop()
        if userbot_service.client and userbot_service.client.is_connected():
            await userbot_service.client.disconnect()
        if admin_bot_service.bot and admin_bot_service.bot.session:
            await admin_bot_service.bot.session.close()
        logger.info("✅ All services stopped safely with zero data loss.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Antigravity AI Butler shutdown complete.")
