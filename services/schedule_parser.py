"""
Smart, multi-layer scheduling parser for Antigravity AI Butler.
Provides deterministic high-speed parsing (sub-millisecond) for Uzbek, Russian,
and English time expressions, with AI semantic fallback.
"""

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Any, Tuple
import pytz

logger = logging.getLogger(__name__)


@dataclass
class ScheduleParseResult:
    """Structured result of schedule parsing."""
    target_peer: str
    run_at: datetime
    message_text: str
    human_time: str
    is_schedule: bool = True

    @property
    def scheduled_iso(self) -> str:
        return self.run_at.isoformat()


def clean_target_peer(peer_str: str) -> str:
    """Normalizes target peer representation (@username, phone, or id)."""
    peer = peer_str.strip()
    if peer.startswith("@"):
        return peer
    if peer.startswith("+") and peer[1:].isdigit():
        return peer
    if peer.isdigit():
        return peer
    # If alphabetic username without @, add @
    if re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", peer):
        return f"@{peer}"
    return peer


def clean_message_text(text: str) -> str:
    """Cleans enclosing quotes, leading time particles ('da', 'ga'), and trailing Uzbek/Russian dispatch suffixes."""
    cleaned = text.strip()
    # Strip quotes
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    # Strip leading connectors: "da ", "ga "
    cleaned = re.sub(r"^(?:da\s+|ga\s+|в\s+)", "", cleaned, flags=re.IGNORECASE).strip()

    # Strip Uzbek suffixes: "... deb yoz", "... deb jo'nat", "... deb xabar yoz", "... deb", "... deb yozib quy/qo'y/qoy"
    suffix_pattern = (
        r"\s+deb(?:\s+(?:xabar\s+)?"
        r"(?:yoz(?:ib\s+(?:qo['`’‘]?y|quy|qoy|tashla))?|yoz(?:ing)?|"
        r"jo['`’‘]nat(?:ib\s+(?:yubor|qo['`’‘]?y|quy))?|jo['`’‘]nat(?:ing)?|"
        r"yubor(?:ib\s+(?:qo['`’‘]?y|quy))?|yubor(?:ing)?))?\.?$"
    )
    cleaned = re.sub(suffix_pattern, "", cleaned, flags=re.IGNORECASE).strip()

    # Strip Russian suffixes: "... напиши", "... отправь"
    ru_suffix = r"\s+(?:напиши|отправь|передай)\.?$"
    cleaned = re.sub(ru_suffix, "", cleaned, flags=re.IGNORECASE).strip()

    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    return cleaned


def extract_peer_and_text_without_time(text: str) -> Optional[Tuple[str, str]]:
    """
    Extracts target peer and message text when user provides recipient and text but forgets the time.
    Example: '/schedule @sah_60 uqi gandon deb yozib quy' -> ('@sah_60', 'uqi gandon')
    """
    if not text:
        return None
    s = re.sub(r"^/schedule(?:@[a-zA-Z0-9_]+)?\s+", "", text.strip(), flags=re.IGNORECASE).strip()
    parts = s.split(maxsplit=1)
    if len(parts) >= 2 and _is_peer_token(parts[0]):
        peer = clean_target_peer(parts[0])
        msg = clean_message_text(parts[1])
        if msg:
            return peer, msg
    return None


def _is_peer_token(token: str) -> bool:
    """Checks if a string token looks like a valid peer."""
    t = token.strip()
    if t.startswith("@") and len(t) > 1:
        return True
    if t.startswith("+") and t[1:].isdigit():
        return True
    if t.isdigit() and len(t) >= 5:
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", t):
        return True
    return False


def _parse_time_token(token: str) -> Optional[Tuple[int, int]]:
    """Extracts (hour, minute) from tokens like '22:07' or '22:07da'."""
    t = re.sub(r"(?i)(?:da|да)$", "", token.strip())
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mn < 60:
            return h, mn
    return None


def parse_deterministic_schedule(text: str, tz_name: str = "Asia/Tashkent") -> Optional[ScheduleParseResult]:
    """
    High-speed deterministic parser covering:
    1. @username HH:MM [text] (or username HH:MM [text], HH:MMda ...)
    2. HH:MM @username [text]
    3. @username YYYY-MM-DD HH:MM [text] / DD.MM.YYYY
    4. @username N daqiqa/minut/soatdan keyin [text]
    5. N daqiqa/minut/soatdan keyin @username [ga] [text]
    6. ertaga / bugun soat HH:MM da @username [ga] [text]
    7. @username [ga] ertaga / bugun soat HH:MM da [text]
    """
    if not text:
        return None

    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    s = text.strip()

    # Remove optional leading /schedule or /schedule@botname
    s = re.sub(r"^/schedule(?:@[a-zA-Z0-9_]+)?\s+", "", s, flags=re.IGNORECASE).strip()

    # -------------------------------------------------------------
    # Pattern 1: Relative minutes / hours:
    # "@sah_60 15 daqiqadan keyin xabar" OR "15 daqiqadan keyin @sah_60 ga xabar"
    # -------------------------------------------------------------
    # 1a: peer N daqiqa/minutdan keyin [text]
    m = re.match(
        r"^(@?[a-zA-Z][a-zA-Z0-9_]{3,31}|\+?\d{7,15})\s+(?:ga\s+)?(\d+)\s+(?:daqiqa|minut|минут|daq|min|soat|соат|час|hour|minute)[\w]*\s+keyin\s+(.+)$",
        s,
        re.IGNORECASE,
    )
    if not m:
        # 1b: N daqiqa/minutdan keyin peer [ga] [text]
        m2 = re.match(
            r"^(\d+)\s+(?:daqiqa|minut|минут|daq|min|soat|соат|час|hour|minute)[\w]*\s+keyin\s+(@?[a-zA-Z][a-zA-Z0-9_]{3,31}|\+?\d{7,15})(?:\s+ga)?\s+(.+)$",
            s,
            re.IGNORECASE,
        )
        if m2:
            num = int(m2.group(1))
            unit_str = m2.group(0).lower()
            peer = m2.group(2)
            msg = m2.group(3)
            delta = timedelta(hours=num) if any(u in unit_str.split()[1] for u in ["soat", "соат", "час", "hour"]) else timedelta(minutes=num)
            run_at = now + delta
            human = f"{num} daqiqadan so'ng" if delta.seconds < 3600 else f"{num} soatdan so'ng"
            return ScheduleParseResult(
                target_peer=clean_target_peer(peer),
                run_at=run_at,
                message_text=clean_message_text(msg),
                human_time=human,
            )

    if m:
        peer = m.group(1)
        num = int(m.group(2))
        unit_str = s.lower()
        msg = m.group(3)
        delta = timedelta(hours=num) if any(u in unit_str for u in ["soat", "соат", "час", "hour"]) else timedelta(minutes=num)
        run_at = now + delta
        human = f"{num} daqiqadan so'ng" if delta.seconds < 3600 else f"{num} soatdan so'ng"
        return ScheduleParseResult(
            target_peer=clean_target_peer(peer),
            run_at=run_at,
            message_text=clean_message_text(msg),
            human_time=human,
        )

    # -------------------------------------------------------------
    # Pattern 2: Explicit Date + Time:
    # "@username 2026-10-09 14:30 [text]" OR "@username 09.10.2026 14:30 [text]"
    # -------------------------------------------------------------
    m_date = re.match(
        r"^(@?[a-zA-Z][a-zA-Z0-9_]{3,31}|\+?\d{7,15})\s+(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})\s+(.+)$",
        s,
    )
    if m_date:
        peer = m_date.group(1)
        date_str = m_date.group(2)
        time_str = m_date.group(3)
        msg = m_date.group(4)
        hour, minute = map(int, time_str.split(":"))
        if "-" in date_str:
            y, mth, d = map(int, date_str.split("-"))
        else:
            d, mth, y = map(int, date_str.split("."))
        target_dt = tz.localize(datetime(y, mth, d, hour, minute, 0))
        return ScheduleParseResult(
            target_peer=clean_target_peer(peer),
            run_at=target_dt,
            message_text=clean_message_text(msg),
            human_time=f"{d:02d}.{mth:02d}.{y} soat {hour:02d}:{minute:02d} da",
        )

    # -------------------------------------------------------------
    # Pattern 3: "Ertaga" / "Bugun" phrases:
    # "ertaga soat 10:00 da @username ga [text]" OR "@username ertaga 10:00 [text]"
    # -------------------------------------------------------------
    m_day = re.search(r"(?i)\b(ertaga|bugun|завтра|сегодня)\b", s)
    if m_day:
        day_word = m_day.group(1).lower()
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", s)
        peer_match = re.search(r"(@[a-zA-Z0-9_]{3,32}|\+?\d{7,15})", s)
        if time_match and peer_match:
            peer = peer_match.group(1)
            time_str = time_match.group(1)
            hour, minute = map(int, time_str.split(":"))

            is_tomorrow = day_word in ("ertaga", "завтра")
            target_date = now.date() + timedelta(days=1) if is_tomorrow else now.date()
            target_dt = tz.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0))
            if not is_tomorrow and target_dt <= now:
                target_dt += timedelta(days=1)

            # Extract message text by removing peer, day word, time and connectors
            rem = s
            rem = rem.replace(peer_match.group(0), "")
            rem = rem.replace(time_match.group(0), "")
            rem = re.sub(rf"(?i)\b{day_word}\b", "", rem)
            rem = re.sub(r"(?i)\b(?:soat|в|at|da|ga|deb|yoz|jo'nat)\b", "", rem)
            msg = clean_message_text(rem)
            day_label = "Ertaga" if is_tomorrow else "Bugun"
            return ScheduleParseResult(
                target_peer=clean_target_peer(peer),
                run_at=target_dt,
                message_text=msg or "Salom!",
                human_time=f"{day_label} soat {hour:02d}:{minute:02d} da",
            )

    # -------------------------------------------------------------
    # Pattern 4 & 5: Classic format: "peer HH:MM text" or "HH:MM peer text"
    # Example: "@sah_60 22:07 menga yoz biron nima deb"
    # -------------------------------------------------------------
    parts = s.split(maxsplit=2)
    if len(parts) >= 3:
        # Check if parts[0] is peer and parts[1] is time
        is_p0 = _is_peer_token(parts[0])
        time_01 = _parse_time_token(parts[1])
        if is_p0 and time_01:
            hour, minute = time_01
            peer = parts[0]
            msg = parts[2]
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_dt <= now:
                target_dt += timedelta(days=1)
                day_str = "Ertaga"
            else:
                day_str = "Bugun"

            return ScheduleParseResult(
                target_peer=clean_target_peer(peer),
                run_at=target_dt,
                message_text=clean_message_text(msg),
                human_time=f"{day_str} soat {hour:02d}:{minute:02d} da",
            )

        # Check if parts[0] is time and parts[1] is peer
        time_00 = _parse_time_token(parts[0])
        is_p1 = _is_peer_token(parts[1])
        if time_00 and is_p1:
            hour, minute = time_00
            peer = parts[1]
            msg = parts[2]
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_dt <= now:
                target_dt += timedelta(days=1)
                day_str = "Ertaga"
            else:
                day_str = "Bugun"

            return ScheduleParseResult(
                target_peer=clean_target_peer(peer),
                run_at=target_dt,
                message_text=clean_message_text(msg),
                human_time=f"{day_str} soat {hour:02d}:{minute:02d} da",
            )

    return None


async def parse_schedule_intent_hybrid(
    raw_text: str,
    ai_engine: Any,
    tz_name: str = "Asia/Tashkent",
) -> Optional[ScheduleParseResult]:
    """
    Hybrid schedule parser:
    1. First tries deterministic regex parsing (instant, zero network latency, 100% reliable).
    2. If text is free-form or complex natural language, falls back to AI model.
    """
    # 1. Deterministic local pass
    deterministic_result = parse_deterministic_schedule(raw_text, tz_name=tz_name)
    if deterministic_result:
        logger.info(
            f"Deterministic schedule parsed successfully: {deterministic_result.target_peer} at {deterministic_result.human_time}"
        )
        return deterministic_result

    # 2. AI semantic fallback pass
    if ai_engine and hasattr(ai_engine, "parse_schedule_intent"):
        try:
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            ai_data = await ai_engine.parse_schedule_intent(raw_text, now, tz_name)
            if ai_data and ai_data.get("is_schedule") and ai_data.get("target_peer") and ai_data.get("scheduled_iso"):
                run_at = datetime.fromisoformat(ai_data["scheduled_iso"])
                if run_at.tzinfo is None:
                    run_at = tz.localize(run_at)

                return ScheduleParseResult(
                    target_peer=clean_target_peer(ai_data["target_peer"]),
                    run_at=run_at,
                    message_text=clean_message_text(ai_data.get("message_text", "Salom!")),
                    human_time=ai_data.get("human_time", run_at.strftime("%Y-%m-%d %H:%M")),
                )
        except Exception as e:
            logger.warning(f"AI schedule parsing fallback failed: {e}")

    return None
