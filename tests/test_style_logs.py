"""
Runtime Sentinel and Log Sanitation Tests for Writing Style Modules.
Ensures Rule E8 and H3 compliance across all style services:
- Sentinels inside a sample text
- Sentinels inside an import message
- Sentinels inside a style note
- Sentinels inside a forced exception
Never logged at INFO or above.
"""

from datetime import datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.style_service import StyleService
from services.style_importer import StyleImporter
from services.style_renderer import render_style_guide, filter_few_shot_examples
from database.crud import DatabaseManager


SENTINEL_SAMPLE = "SENTINEL_STYLE_SAMPLE_SECRET_ALPHA"
SENTINEL_IMPORT = "SENTINEL_STYLE_IMPORT_SECRET_BBB222"
SENTINEL_NOTE = "SENTINEL_STYLE_NOTE_SECRET_CCC333"
SENTINEL_EXCEPTION = "SENTINEL_STYLE_EXC_SECRET_DDD444"


class StyleLogCaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def get_all_text(self) -> str:
        texts = []
        for r in self.records:
            texts.append(r.getMessage())
            if r.exc_info and r.exc_info[1]:
                texts.append(str(r.exc_info[1]))
            if r.exc_text:
                texts.append(r.exc_text)
        return "\n".join(texts)


@pytest.mark.asyncio
async def test_style_runtime_sentinels_no_leak_at_info():
    """
    Asserts that sample text, import text, owner note, and exception text
    never appear in any INFO+ logs across style modules.
    """
    root_logger = logging.getLogger()
    handler = StyleLogCaptureHandler()
    root_logger.addHandler(handler)

    try:
        mock_db = MagicMock(spec=DatabaseManager)
        service = StyleService(mock_db)

        # 1. Processing sample / few-shot candidate with sentinel
        few_shots = filter_few_shot_examples([f"Salom do'stim {SENTINEL_SAMPLE}"])
        assert len(few_shots) == 1

        # 2. Rendering guide with note containing sentinel
        note = f"Owner directive {SENTINEL_NOTE} qisqa yozing"
        profile = {
            "scope": "global",
            "stats": {"p50_chars": 20, "p90_chars": 40, "emoji_rate": 0.0},
            "phrases": ["raxmat"],
        }
        guide = render_style_guide(profile, owner_note=note)
        assert SENTINEL_NOTE in guide  # Guide string contains note for prompt

        # 3. Import simulation with sentinel in message
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 99
        mock_msg.out = True
        mock_msg.raw_text = f"Tarixiy xabar {SENTINEL_IMPORT} uz-lat"
        mock_msg.text = f"Tarixiy xabar {SENTINEL_IMPORT} uz-lat"
        mock_msg.fwd_from = None
        mock_msg.via_bot_id = None
        mock_msg.action = None
        mock_msg.media = None
        mock_msg.date = datetime(2026, 10, 1, 10, 0, 0)

        async def fake_iter(*args, **kwargs):
            yield mock_msg

        mock_client.iter_messages = fake_iter
        mock_contact = MagicMock()
        mock_contact.user_id = 12345
        mock_contact.mode = "auto"
        mock_db.list_contacts = AsyncMock(return_value=[mock_contact])
        mock_db.get_bot_sent_texts_for_contact = AsyncMock(return_value=set())
        mock_db.get_style_samples = AsyncMock(return_value=[])
        mock_db.insert_style_sample = AsyncMock(return_value=1)

        importer = StyleImporter(db_manager=mock_db, telethon_client=mock_client, owner_id=12345)
        cutoff = datetime(2026, 10, 8, 5, 26, 14)

        res = await importer.import_history(
            caller_user_id=12345,
            custom_cutoff=cutoff,
        )
        assert res["samples_imported"] == 1

        # 4. Forced exception with sentinel in get_style_context
        with patch.object(service, "is_style_enabled_for_contact", AsyncMock(return_value=True)):
            with patch.object(
                service,
                "_fetch_internal",
                AsyncMock(side_effect=ValueError(f"Forced DB error with {SENTINEL_EXCEPTION}")),
            ):
                ctx = await service.get_style_context(12345, f"Incoming {SENTINEL_SAMPLE}")
                assert ctx == ""  # fails open gracefully

        # Inspect captured logs
        all_logs = handler.get_all_text()

        assert SENTINEL_SAMPLE not in all_logs, f"LEAK: Sample sentinel found in logs: {all_logs}"
        assert SENTINEL_IMPORT not in all_logs, f"LEAK: Import sentinel found in logs: {all_logs}"
        assert SENTINEL_NOTE not in all_logs, f"LEAK: Note sentinel found in logs: {all_logs}"
        assert SENTINEL_EXCEPTION not in all_logs, f"LEAK: Exception sentinel found in logs: {all_logs}"

    finally:
        root_logger.removeHandler(handler)
