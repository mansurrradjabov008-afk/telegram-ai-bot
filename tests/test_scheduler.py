"""
Unit tests for APScheduler and execution callbacks.
"""

import asyncio
from datetime import datetime, timedelta
import pytest
import pytest_asyncio
import pytz
from pathlib import Path

from database.connection import init_db
from database.crud import DatabaseManager
from services.scheduler_service import SchedulerService


@pytest_asyncio.fixture
async def scheduler_fixture(tmp_path: Path):
    db_file = tmp_path / "test_scheduler.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    service = SchedulerService(manager)
    service.start()
    yield service, manager
    service.stop()


@pytest.mark.asyncio
async def test_scheduler_execution_flow(scheduler_fixture):
    service, manager = scheduler_fixture

    executed_messages = []
    notified_boss = []

    async def fake_send(target, text):
        executed_messages.append((target, text))

    async def fake_notify(text):
        notified_boss.append(text)

    service.register_callbacks(send_callback=fake_send, notify_boss_callback=fake_notify)

    tz = pytz.timezone("Asia/Tashkent")
    # Schedule 1 second in future
    run_at = datetime.now(tz) + timedelta(seconds=1)

    task = await service.schedule_message(
        target_peer="@friend",
        message_text="Test xabar",
        run_at=run_at,
    )

    assert task.status == "pending"

    # Wait for job to trigger
    await asyncio.sleep(2.0)

    assert len(executed_messages) == 1
    assert executed_messages[0] == ("@friend", "Test xabar")
    assert len(notified_boss) == 1
    assert "muvaffaqiyatli yuborildi" in notified_boss[0]

    # Verify status in database
    pending = await manager.get_pending_tasks()
    assert len(pending) == 0
