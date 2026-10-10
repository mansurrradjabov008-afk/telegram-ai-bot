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
    await memory_service.purge_retention()

    # 3. Initialize Bot Services
    admin_bot_service = AdminBotService(
        db_manager=db_manager,
        ai_engine=ai_engine,
        scheduler_service=scheduler_service,
        digest_service=digest_service,
        memory_service=memory_service,
    )
    userbot_service = UserbotService(
        db_manager=db_manager,
        ai_engine=ai_engine,
        rate_limiter=rate_limiter,
        digest_service=digest_service,
        memory_service=memory_service,
    )

    # 4. Wire cross-service callbacks
    userbot_service.set_escalation_callback(admin_bot_service.notify_boss_escalation)
    userbot_service.set_draft_approval_callback(admin_bot_service.send_draft_for_approval)
    userbot_service.set_notification_callback(admin_bot_service.send_notification)
    admin_bot_service.set_userbot_sender(userbot_service.send_direct_message)
    admin_bot_service.set_manual_reply_checker(userbot_service.check_owner_manually_replied)
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

    # 7. Start Admin Bot Polling
    tasks = []
    if admin_bot_service.bot:
        logger.info("🤖 Admin Cockpit Bot polling starting...")
        await admin_bot_service.setup_bot_commands()
        tasks.append(
            asyncio.create_task(
                admin_bot_service.dp.start_polling(
                    admin_bot_service.bot,
                    skip_updates=True,
                )
            )
        )
    else:
        logger.warning("⚠️ Admin Bot Token not configured in .env. Admin polling skipped.")

    if userbot_service.client and userbot_service._is_running:
        tasks.append(
            asyncio.create_task(userbot_service.client.run_until_disconnected())
        )

    if not tasks:
        logger.info("ℹ️ No active bot tasks were started because credentials are pending in .env.")
        logger.info("Please fill in your credentials in .env to run the live background services.")
        return

    logger.info("✨ [bold cyan]Antigravity AI Butler is fully operational![/bold cyan]", extra={"markup": True})
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Antigravity AI Butler shutting down gracefully.")
