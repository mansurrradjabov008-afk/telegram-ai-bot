"""
Asynchronous Scheduler Service using APScheduler.
Ensures persistent, reboot-safe message delivery with real-time Boss notifications.
"""

import logging
from datetime import datetime
from typing import Callable, Optional, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import settings
from database.crud import DatabaseManager
from database.models import ScheduledTask

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.tz = pytz.timezone(settings.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)
        self._send_callback: Optional[Callable] = None
        self._notify_boss_callback: Optional[Callable] = None

    def register_callbacks(self, send_callback: Callable, notify_boss_callback: Callable):
        """
        send_callback(target_peer, message_text) -> sends via Userbot
        notify_boss_callback(text) -> sends alert to Admin Bot
        """
        self._send_callback = send_callback
        self._notify_boss_callback = notify_boss_callback

    def start(self):
        """Starts the scheduler loop."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler service started.")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler service stopped.")

    async def schedule_message(
        self,
        target_peer: str,
        message_text: str,
        run_at: datetime,
    ) -> ScheduledTask:
        """
        Persists task in DB and registers job in APScheduler.
        """
        if run_at.tzinfo is None:
            run_at = self.tz.localize(run_at)

        iso_str = run_at.isoformat()
        task = await self.db_manager.create_scheduled_task(
            target_peer=target_peer,
            message_text=message_text,
            scheduled_time=iso_str,
        )

        job_id = f"task_{task.id}"
        self.scheduler.add_job(
            self._execute_task,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            args=[task.id, target_peer, message_text],
            replace_existing=True,
        )
        logger.info(f"Scheduled task #{task.id} for {target_peer} at {iso_str}")
        return task

    async def cancel_task(self, task_id: int) -> bool:
        """Cancels a pending task in scheduler and updates database status."""
        job_id = f"task_{task_id}"
        if self.scheduler.get_job(job_id):
            try:
                self.scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"Could not remove job {job_id}: {type(e).__name__}")

        await self.db_manager.update_task_status(task_id, "cancelled")
        logger.info(f"Task #{task_id} cancelled.")
        return True

    async def execute_now(self, task_id: int) -> bool:
        """Immediately sends a scheduled task without waiting for trigger."""
        task = await self.db_manager.get_task_by_id(task_id)
        if not task:
            return False

        job_id = f"task_{task_id}"
        if self.scheduler.get_job(job_id):
            try:
                self.scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"Could not remove job {job_id}: {type(e).__name__}")

        await self._execute_task(task.id, task.target_peer, task.message_text)
        return True

    async def delete_task(self, task_id: int) -> bool:
        """Cancels and permanently deletes a task from the system."""
        job_id = f"task_{task_id}"
        if self.scheduler.get_job(job_id):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        return await self.db_manager.delete_task(task_id)

    async def _execute_task(self, task_id: int, target_peer: str, message_text: str):
        """Job executor called when trigger fires."""
        logger.info(f"Executing scheduled task #{task_id} to {target_peer}...")
        try:
            if self._send_callback:
                import inspect
                res = self._send_callback(target_peer, message_text)
                if inspect.isawaitable(res):
                    await res

            await self.db_manager.update_task_status(task_id, "executed")

            if self._notify_boss_callback:
                import inspect
                import html
                safe_target = html.escape(target_peer)
                safe_msg = html.escape(message_text)
                msg = (
                    f"✅ <b>Rejalashtirilgan xabar muvaffaqiyatli yuborildi!</b>\n\n"
                    f"👤 <b>Qabul qiluvchi:</b> <code>{safe_target}</code>\n"
                    f"💬 <b>Matn:</b> <i>{safe_msg}</i>"
                )
                res = self._notify_boss_callback(msg)
                if inspect.isawaitable(res):
                    await res

        except Exception as e:
            logger.error(f"Failed to execute scheduled task #{task_id}: {type(e).__name__}")
            await self.db_manager.update_task_status(task_id, "failed", str(e))
            if self._notify_boss_callback:
                import inspect
                import html
                safe_target = html.escape(target_peer)
                safe_err = html.escape(str(e))
                msg = (
                    f"⚠️ <b>Rejalashtirilgan xabarni yuborishda xatolik yuz berdi!</b>\n\n"
                    f"👤 <b>Qabul qiluvchi:</b> <code>{safe_target}</code>\n"
                    f"❌ <b>Xato:</b> <code>{safe_err}</code>"
                )
                res = self._notify_boss_callback(msg)
                if inspect.isawaitable(res):
                    await res

    async def reload_pending_tasks_from_db(self):
        """Reloads tasks on server restart to ensure zero task loss."""
        pending_tasks = await self.db_manager.get_pending_tasks()
        now = datetime.now(self.tz)

        for task in pending_tasks:
            try:
                run_at = datetime.fromisoformat(task.scheduled_time)
                if run_at.tzinfo is None:
                    run_at = self.tz.localize(run_at)

                if run_at <= now:
                    # If scheduled time passed while server was off, run immediately
                    await self._execute_task(task.id, task.target_peer, task.message_text)
                else:
                    job_id = f"task_{task.id}"
                    self.scheduler.add_job(
                        self._execute_task,
                        trigger=DateTrigger(run_date=run_at),
                        id=job_id,
                        args=[task.id, task.target_peer, task.message_text],
                        replace_existing=True,
                    )
            except Exception as e:
                logger.error(f"Error restoring pending task #{task.id}: {type(e).__name__}")

    def register_smart_digest_job(
        self,
        digest_service: Any,
        notify_boss_callback: Optional[Callable] = None,
        digest_time_str: str = "08:00",
    ):
        """
        Registers daily recurring APScheduler cron job for Smart Digest V2.
        Runs daily at DIGEST_TIME (Asia/Tashkent).
        Persists state in digest_runs and guarantees no double send for the same date.
        """
        hour, minute = 8, 0
        try:
            parts = digest_time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            pass

        async def _run_daily_digest():
            logger.info("Triggering scheduled daily digest...")
            now_tashkent = datetime.now(self.tz)
            date_str = now_tashkent.strftime("%Y-%m-%d")
            already_sent = await self.db_manager.has_scheduled_digest_run_today(date_str)
            if already_sent:
                logger.info(f"Scheduled digest for {date_str} already completed today. Skipping.")
                return

            cb = notify_boss_callback or self._notify_boss_callback
            if not getattr(settings, "DIGEST_V2_ENABLED", True):
                logger.info("DIGEST_V2_ENABLED is False: Running legacy V1 digest...")
                summary = await digest_service.generate_and_send_digest()
                if cb and summary:
                    res_cb = cb(summary)
                    import inspect
                    if inspect.isawaitable(res_cb):
                        await res_cb
                return

            res = await digest_service.run_smart_digest(trigger="scheduled")
            messages = res.get("messages", [])
            if cb and messages:
                for msg in messages:
                    res_cb = cb(msg)
                    import inspect
                    if inspect.isawaitable(res_cb):
                        await res_cb

        job_id = "smart_digest_daily_job"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        self.scheduler.add_job(
            _run_daily_digest,
            CronTrigger(hour=hour, minute=minute, timezone=self.tz),
            id=job_id,
            name="Daily Smart Digest V2",
            replace_existing=True,
        )
        logger.info(f"Registered daily Smart Digest job at {hour:02d}:{minute:02d} ({settings.TIMEZONE}).")

    async def check_and_run_catchup_digest(
        self,
        digest_service: Any,
        notify_boss_callback: Optional[Callable] = None,
        digest_time_str: str = "08:00",
    ):
        """
        Catch-up rule:
        If the bot was offline at the scheduled time, send ONE catch-up digest after restart
        (only if less than 12 hours late, otherwise skip and log).
        """
        now_tashkent = datetime.now(self.tz)
        date_str = now_tashkent.strftime("%Y-%m-%d")

        already_sent = await self.db_manager.has_scheduled_digest_run_today(date_str)
        if already_sent:
            logger.info(f"Catch-up check: Digest for {date_str} already completed. No catch-up needed.")
            return

        hour, minute = 8, 0
        try:
            parts = digest_time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            pass

        scheduled_today = now_tashkent.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if now_tashkent > scheduled_today:
            late_hours = (now_tashkent - scheduled_today).total_seconds() / 3600.0
            if late_hours <= 12.0:
                logger.info(f"Catch-up digest: {late_hours:.1f} hours late (<= 12h). Executing catch-up digest...")
                cb = notify_boss_callback or self._notify_boss_callback
                if not getattr(settings, "DIGEST_V2_ENABLED", True):
                    logger.info("Catch-up: DIGEST_V2_ENABLED is False, executing legacy V1 digest...")
                    summary = await digest_service.generate_and_send_digest()
                    if cb and summary:
                        res_cb = cb(summary)
                        import inspect
                        if inspect.isawaitable(res_cb):
                            await res_cb
                    return

                res = await digest_service.run_smart_digest(trigger="scheduled")
                messages = res.get("messages", [])
                if cb and messages:
                    for msg in messages:
                        res_cb = cb(msg)
                        import inspect
                        if inspect.isawaitable(res_cb):
                            await res_cb
            else:
                logger.info(f"Catch-up digest skipped: {late_hours:.1f} hours late (> 12h cutoff).")
