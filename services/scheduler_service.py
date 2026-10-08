"""
Asynchronous Scheduler Service using APScheduler.
Ensures persistent, reboot-safe message delivery with real-time Boss notifications.
"""

import logging
from datetime import datetime
from typing import Callable, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
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
                logger.warning(f"Could not remove job {job_id}: {e}")

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
                logger.warning(f"Could not remove job {job_id}: {e}")

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
            logger.error(f"Failed to execute scheduled task #{task_id}: {e}")
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
                logger.error(f"Error restoring pending task #{task.id}: {e}")
