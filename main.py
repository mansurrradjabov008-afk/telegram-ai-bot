"""
Master runner for Antigravity Telegram AI Executive Butler.
Coordinates Database, AI Brain, Scheduler, Admin Bot Cockpit, and MTProto Userbot.
"""

import asyncio
import logging
import signal
import sys
from rich.logging import RichHandler

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, tracebacks_show_locals=False)],
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

    # 5. Start Scheduler and restore pending tasks
    scheduler_service.start()
    await scheduler_service.reload_pending_tasks_from_db()
    logger.info("✅ Scheduler service started with persistent SQLite job store.")

    # Register Smart Digest daily job and run catch-up if needed
    if getattr(settings, "DIGEST_V2_ENABLED", True):
        scheduler_service.register_smart_digest_job(
            digest_service=digest_service,
            notify_boss_callback=admin_bot_service.send_notification,
            digest_time_str=getattr(settings, "DIGEST_TIME", "08:00"),
        )
        await scheduler_service.check_and_run_catchup_digest(
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
            # 1. Online transaction-safe backup & integrity verification
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

            # 2. SQLite maintenance: checkpoint WAL and optimize query planner
            from database.connection import get_db
            async with get_db() as db:
                await db.execute("PRAGMA wal_checkpoint(PASSIVE);")
                await db.execute("PRAGMA optimize;")
            logger.info("✅ SQLite WAL checkpoint and query planner optimization complete.")
        except Exception as e:
            logger.error(f"Error during scheduled database maintenance: {type(e).__name__}")

    scheduler_service.scheduler.add_job(
        _run_scheduled_maintenance,
        CronTrigger(hour=4, minute=0, timezone=pytz.timezone(settings.TIMEZONE)),
        id="daily_safe_db_backup",
        name="Daily Safe Database Backup & Maintenance",
        replace_existing=True,
    )
    logger.info("✅ Daily safe database backup and maintenance scheduled at 04:00.")

    # 6. Initialize MTProto Userbot
    has_userbot_creds = userbot_service.init_client()
    if has_userbot_creds:
        logger.info("Authenticating Telegram Userbot session...")
        try:
            await userbot_service.start()
        except Exception as e:
            logger.error(f"Userbot login failed or pending interactive login: {type(e).__name__}")
    else:
        logger.warning("⚠️ Userbot credentials not configured in .env. Running in dry/standby mode.")

    # 7. Start Web Keep-Alive (Zero-Sleep Cloud Server)
    from services.web_keepalive import start_keepalive_server
    try:
        await start_keepalive_server(default_port=7860)
    except Exception as e:
        logger.warning(f"Could not start Keep-Alive web server: {type(e).__name__}")

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

    tasks = []
    if admin_bot_service.bot:
        logger.info("🤖 Admin Cockpit Bot polling starting...")
        await admin_bot_service.setup_bot_commands()
        tasks.append(asyncio.create_task(_run_admin_bot_resilient()))
    else:
        logger.warning("⚠️ Admin Bot Token not configured in .env. Admin polling skipped.")

    if userbot_service.client and userbot_service._is_running:
        tasks.append(asyncio.create_task(_run_userbot_resilient()))

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
