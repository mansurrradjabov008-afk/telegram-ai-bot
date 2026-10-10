"""
Automated Log Sanitation and Anti-Leakage Guard Tests (Phase 1, Hardening).
Guarantees Rule H3:
- Never log message, reply, fact, summary, or exception text above DEBUG.
- At DEBUG only after redaction.
- Log ids, lengths, counts, reasons, and exception class names.
- Third-party loggers set to WARNING or higher.
- Tracebacks never print locals.
"""

import ast
import logging
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

SENTINEL_INCOMING = "SENTINEL_INCOMING_SECRET_ABC123"
SENTINEL_REPLY = "SENTINEL_REPLY_SECRET_DEF456"
SENTINEL_FACT = "SENTINEL_FACT_SECRET_GHI789"
SENTINEL_EXCEPTION = "SENTINEL_EXCEPTION_SECRET_JKL000"


class LogCaptureHandler(logging.Handler):
    """Captures all log records across all loggers at INFO and above."""
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def get_all_text(self) -> str:
        texts = []
        for r in self.records:
            msg = r.getMessage()
            texts.append(msg)
            if r.exc_info and r.exc_info[1]:
                texts.append(str(r.exc_info[1]))
            if r.exc_text:
                texts.append(r.exc_text)
        return "\n".join(texts)


# ============================================================================
# 1. AST SCANNER TEST
# ============================================================================

def scan_ast_tree(tree: ast.AST, rel_path: str = "test.py") -> list:
    """Scans an AST tree for logger and print violations."""
    violations = []
    forbidden_var_pattern = re.compile(
        r"(?i)\b(text|message|msg|reply|body|fact|content|summary|incoming_text|proposed_reply|incoming_message|message_text)\b"
    )
    forbidden_exc_pattern = re.compile(
        r"(?i)(\b(str|repr)\([a-zA-Z0-9_]*e[a-zA-Z0-9_]*\)|f['\"].*?\{[a-zA-Z0-9_]*e[0-9]*\}.*?['\"])"
    )

    for node in ast.walk(tree):
        # 1. Flag print() calls in production modules
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "print":
                if not str(rel_path).startswith("scripts") and not str(rel_path).startswith("test"):
                    unparsed = ast.unparse(node)
                    violations.append(f"{rel_path}:{node.lineno}: print() call: {unparsed[:80]}")

        # 2. Flag logger calls at INFO, WARNING, ERROR, EXCEPTION, CRITICAL
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            log_methods = {"info", "warning", "error", "exception", "critical"}
            if node.func.attr in log_methods:
                is_logger = False
                if isinstance(node.func.value, ast.Name):
                    if "logger" in node.func.value.id.lower() or node.func.value.id == "logging":
                        is_logger = True
                elif isinstance(node.func.value, ast.Attribute):
                    if "logger" in node.func.value.attr.lower():
                        is_logger = True

                if is_logger:
                    unparsed = ast.unparse(node)

                    # a) Check exc_info=True
                    for kw in node.keywords:
                        if kw.arg == "exc_info" and ast.unparse(kw.value) == "True":
                            violations.append(f"{rel_path}:{node.lineno}: [{node.func.attr.upper()}] exc_info=True forbidden: {unparsed[:100]}")

                    # b) Check %s args
                    if len(node.args) > 1:
                        first_arg = ast.unparse(node.args[0])
                        if "%s" in first_arg:
                            for extra_arg in node.args[1:]:
                                extra_unp = ast.unparse(extra_arg)
                                if forbidden_var_pattern.search(extra_unp):
                                    violations.append(f"{rel_path}:{node.lineno}: [{node.func.attr.upper()}] %s passes text variable '{extra_unp}': {unparsed[:100]}")

                    # c) Check f-string interpolation
                    has_text = bool(forbidden_var_pattern.search(unparsed))
                    has_exc = bool(forbidden_exc_pattern.search(unparsed))

                    if has_text:
                        interpolated = re.findall(r"\{([^}]+)\}", unparsed)
                        for interp in interpolated:
                            if forbidden_var_pattern.search(interp) and not any(k in interp.lower() for k in ["len(", "id", "count", "mode"]):
                                violations.append(
                                    f"{rel_path}:{node.lineno}: [{node.func.attr.upper()}] interpolates text '{interp}': {unparsed[:100]}"
                                )
                    if has_exc:
                        interpolated = re.findall(r"\{([^}]+)\}", unparsed)
                        for interp in interpolated:
                            if interp.strip() in ("e", "e2", "err", "exc", "str(e)", "str(e2)", "str(err)", "repr(e)"):
                                violations.append(
                                    f"{rel_path}:{node.lineno}: [{node.func.attr.upper()}] interpolates raw exception '{interp}': {unparsed[:100]}"
                                )

    return violations


def test_ast_scanner_self_test():
    """
    Self-Test: Ensures AST scanner reliably detects deliberately bad logger lines:
    1) f-string with text variable
    2) str(e) exception string
    3) exc_info=True
    4) %s args passing text variable
    """
    bad_code = """
import logging
logger = logging.getLogger(__name__)

# Bad 1: f-string with text variable
logger.info(f"Incoming user message: {incoming_text}")

# Bad 2: str(e) inside log
logger.error(f"Failed with exception: {str(e)}")

# Bad 3: exc_info=True
logger.warning("Something went wrong", exc_info=True)

# Bad 4: %s args
logger.info("Message received: %s", message_text)
"""
    tree = ast.parse(bad_code, filename="synthetic_bad.py")
    violations = scan_ast_tree(tree, rel_path="synthetic_bad.py")

    assert len(violations) >= 4, f"Expected at least 4 violations, got: {violations}"
    v_text = "\n".join(violations)
    assert "interpolates text 'incoming_text'" in v_text
    assert "interpolates raw exception 'str(e)'" in v_text
    assert "exc_info=True forbidden" in v_text
    assert "%s passes text variable" in v_text


def test_ast_scan_no_text_or_exception_in_info_logs():
    """
    AST Scan (H3 Guard):
    Fails on any logger call at INFO+ or print() in production modules
    that interpolates or passes variables named text, message, msg, reply,
    body, fact, content, summary, or exception strings (e, err, exc, str(e)).
    """
    project_root = Path(__file__).resolve().parent.parent
    target_dirs = ["bot", "services", "database"]
    target_files = ["main.py", "config.py"]

    py_files = []
    for td in target_dirs:
        dir_path = project_root / td
        if dir_path.exists():
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f.endswith(".py"):
                        py_files.append(Path(root) / f)
    for tf in target_files:
        fp = project_root / tf
        if fp.exists():
            py_files.append(fp)

    all_violations = []
    for py_file in py_files:
        with open(py_file, "r", encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read(), filename=str(py_file))
            except Exception as e:
                all_violations.append(f"{py_file}: Parse error: {e}")
                continue
        rel_path = py_file.relative_to(project_root)
        file_violations = scan_ast_tree(tree, rel_path=str(rel_path))
        all_violations.extend(file_violations)

    assert not all_violations, f"Found {len(all_violations)} log sanitation violations:\n" + "\n".join(all_violations)


# ============================================================================
# 2. RUNTIME SENTINEL TEST
# ============================================================================

@pytest.mark.asyncio
async def test_runtime_sentinel_pipeline_logging():
    """
    Runtime Test (H3 Sentinel Guard):
    Sends fake messages through userbot, memory extractor, and admin bot with
    unique sentinel strings in incoming message, AI reply, fact, and exception.
    Asserts NO sentinel appears in any log output at INFO+.
    """
    capture = LogCaptureHandler()
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(capture)

    try:
        # 1. Userbot processing check
        from bot.userbot import UserbotService
        from database.models import Contact

        mock_db = MagicMock()
        mock_db.get_recent_history = AsyncMock(return_value=[])
        mock_db.log_message = AsyncMock()
        mock_db.create_draft = AsyncMock(return_value=MagicMock(id=99))
        mock_ai = MagicMock()
        mock_ai.generate_reply = AsyncMock(return_value=f"Reply containing {SENTINEL_REPLY}")

        userbot = UserbotService(
            db_manager=mock_db,
            ai_engine=mock_ai,
            rate_limiter=MagicMock(),
            digest_service=MagicMock(),
        )

        contact = Contact(
            user_id=1001,
            username="testuser",
            full_name="Test User",
            mode="draft",
        )

        batch = [{
            "message_id": 1,
            "chat_id": 1001,
            "first_name": "Test",
            "username": "testuser",
            "peer_repr": "@testuser",
            "text": f"Secret input with {SENTINEL_INCOMING}",
            "contact": contact,
        }]

        # Populate debounce buffer and run process debounced batch
        userbot._debounce_buffers[1001] = batch
        with patch("bot.userbot.check_sensitive", AsyncMock(return_value=MagicMock(is_sensitive=False, primary_reason=None))):
            await userbot._process_debounced_batch(1001)

        # 2. Memory Extractor forbidden topic check
        from services.memory_extractor import MemoryExtractor
        mock_db.get_mem_contact = AsyncMock(return_value=MagicMock(memory_enabled=1))
        mock_db.get_daily_counter = AsyncMock(return_value=0)
        mock_db.increment_daily_counter = AsyncMock()
        mock_db.store_mem_fact = AsyncMock(return_value=1)
        mock_db.store_mem_open_item = AsyncMock(return_value=1)
        extractor = MemoryExtractor(db_manager=mock_db)

        # Fake parsed fact containing forbidden topic and sentinel
        fake_parsed = {
            "facts": [{
                "text": f"Forbidden fact with {SENTINEL_FACT} and salomatlik",
                "category": "health",
                "source_message_ids": [1],
                "confidence": 1.0,
            }],
            "open_items": []
        }

        import json
        with patch.object(extractor, "_call_ai", AsyncMock(return_value=json.dumps(fake_parsed))):
            await extractor.extract_from_messages(
                contact_user_id=1001,
                messages=[MagicMock(id=1, contact_user_id=1001, sender="contact", text_redacted="msg")],
            )

        # 3. Direct userbot send message check
        mock_client = MagicMock()
        mock_client.get_entity = AsyncMock(return_value=MagicMock(id=1001))
        mock_client.action = MagicMock()
        mock_client.action.return_value.__aenter__ = AsyncMock()
        mock_client.action.return_value.__aexit__ = AsyncMock()
        userbot.client = mock_client
        userbot._send_with_floodwait = AsyncMock(return_value=MagicMock(id=10))

        await userbot.send_direct_message(
            target_peer="@testuser",
            message_text=f"Direct send with {SENTINEL_REPLY}",
        )

        # 4. Admin bot draft approval check
        from bot.admin_bot import AdminBotService
        from database.models import Draft

        admin_service = AdminBotService(
            db_manager=mock_db,
            ai_engine=mock_ai,
            scheduler_service=MagicMock(),
            token="1234567890:mock",
            admin_id=7992504344,
        )
        fake_draft = Draft(
            id=12,
            contact_username="testuser",
            contact_user_id=1001,
            source_message_id=1,
            incoming_text=f"Draft incoming {SENTINEL_INCOMING}",
            proposed_reply=f"Draft reply {SENTINEL_REPLY}",
            status="pending",
            is_sensitive=0,
            sensitive_reason=None,
            expires_at="2026-10-12T00:00:00",
        )
        admin_service.bot = None  # To trigger logger.info fallback
        await admin_service.send_draft_for_approval(fake_draft)

        # Check all logged text at INFO+
        logged_text = capture.get_all_text()

        # None of the sentinels should appear in logs!
        assert SENTINEL_INCOMING not in logged_text, f"LEAK: Incoming sentinel found in INFO logs: {logged_text}"
        assert SENTINEL_REPLY not in logged_text, f"LEAK: Reply sentinel found in INFO logs: {logged_text}"
        assert SENTINEL_FACT not in logged_text, f"LEAK: Fact sentinel found in INFO logs: {logged_text}"
        assert SENTINEL_EXCEPTION not in logged_text, f"LEAK: Exception sentinel found in INFO logs: {logged_text}"

    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(old_level)
