"""
Smart, multi-layer scheduling parser for Antigravity AI Butler.
Provides deterministic high-speed parsing (sub-millisecond) for Uzbek, Russian,
and English time expressions, with AI semantic fallback.
"""

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Any, Tuple, List
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


def is_valid_peer(peer_str: Optional[str]) -> bool:
    """Checks if a string is a valid Telegram peer (not an unresolved Uzbek inflected word)."""
    if not peer_str:
        return False
    p = peer_str.strip()
    if p.startswith("+") and p[1:].isdigit() and len(p) >= 8:
        return True
    if p.isdigit() and len(p) >= 5:
        return True
    if p.startswith("@") and re.match(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$", p):
        # Disallow synthetic handles made from Uzbek inflected nouns (e.g. @mansurga)
        raw_token = p[1:].lower()
        if re.search(r"(?:ga|ka|qa|da|dan|ni|ning|yga|yda|ydan)$", raw_token):
            return False
        return True
    return False


def resolve_target_peer(
    peer_str: Optional[str],
    contacts: Optional[List[Any]] = None,
    raw_query: str = "",
) -> Optional[str]:
    """
    Resolves a peer string to normalized @username, phone, or Telegram ID.
    Strips Uzbek case suffixes (-ga, -ka, -da, -dan, -ni, etc.) and matches CRM contacts.
    If peer cannot be resolved and had no explicit '@' in raw_query, returns None (ambiguous).
    """
    if not peer_str:
        return None
    raw = peer_str.strip()
    clean_handle = raw.lstrip("@").lower()

    if contacts:
        # 1. Exact username or ID match
        for c in contacts:
            c_u = (getattr(c, "username", "") or "").lower().lstrip("@")
            if c_u and c_u == clean_handle:
                return f"@{c.username}"
            if str(getattr(c, "user_id", "")) == raw:
                return str(c.user_id)

        # 2. Suffix stripping + contact match
        stem = re.sub(r"(?:ga|ka|qa|da|dan|ni|ning|yga|yda|ydan)$", "", clean_handle, flags=re.IGNORECASE)
        for c in contacts:
            aliases = generate_contact_aliases(getattr(c, "username", None), getattr(c, "full_name", None))
            for a in aliases:
                if stem == a.lower() or clean_handle == a.lower():
                    return f"@{c.username}" if c.username else str(c.user_id)

    # 3. Explicit @ in user prompt or explicit @ handle
    had_explicit_at = f"@{clean_handle}" in raw_query or (
        raw.startswith("@") and not re.search(r"(?:ga|ka|qa|da|dan|ni|ning|yga|yda|ydan)$", clean_handle)
    )
    if had_explicit_at and re.match(r"^[A-Za-z0-9_]{3,32}$", clean_handle):
        return f"@{clean_handle}"

    # 4. Numeric Telegram ID or Phone number
    if (raw.startswith("+") and raw[1:].isdigit() and len(raw) >= 8) or (raw.isdigit() and len(raw) >= 5):
        return raw

    return None


ALIAS_STOPWORDS = {
    "deb", "aka", "oka", "opa", "jon", "bek", "jura", "jo'ra", "siz", "sen",
    "yoz", "ayt", "sura", "so'ra", "bormi", "qil", "ber", "edi", "bilan",
    "uchun", "ham", "dev", "bot", "user", "admin", "test", "va", "da", "ga",
    "dan", "ni", "ning", "yoq", "yo'q", "ha", "nima", "qayerga", "qayga",
    "qachon", "kim", "qanday", "nechta", "bugun", "ertaga"
}


def generate_contact_aliases(username: Optional[str], full_name: Optional[str]) -> List[str]:
    """
    Generates a rich, phonetically and morphologically normalized list of alias candidates
    for a contact, supporting Latin/Cyrillic transliteration, CamelCase usernames,
    Uzbek honorifics ('aka', 'opa', 'jon', 'bek', 'jura'), and 'x'/'h' / 'v'/'b' orthographic variants.
    """
    from services.digest_cleaner import transliterate_uzbek_cyrillic_to_latin
    raw_candidates = set()

    if username:
        u = username.lower().lstrip("@").strip()
        if u:
            raw_candidates.add(u)
            no_digits = re.sub(r"\d+$", "", u)
            if len(no_digits) >= 3:
                raw_candidates.add(no_digits)
            # CamelCase / word segment splitting: e.g. SuhrobOmonov12 -> ['Suhrob', 'Omonov']
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)", username)
            for p in parts:
                p_clean = p.lower()
                if len(p_clean) >= 3:
                    raw_candidates.add(p_clean)

    if full_name:
        fn = full_name.strip()
        if fn:
            fn_lower = fn.lower()
            raw_candidates.add(fn_lower)
            lat = transliterate_uzbek_cyrillic_to_latin(fn).lower()
            raw_candidates.add(lat)
            for src in [fn_lower, lat]:
                words = re.split(r"[\s_]+", src)
                for w in words:
                    w_clean = re.sub(r"[^a-zа-яё\']", "", w)
                    if len(w_clean) >= 3:
                        raw_candidates.add(w_clean)
                        variants = [w_clean]
                        if "x" in w_clean:
                            variants.append(w_clean.replace("x", "h"))
                        if "h" in w_clean:
                            variants.append(w_clean.replace("h", "x"))
                        for var in list(variants):
                            raw_candidates.add(var)
                            if var.endswith("v"):
                                raw_candidates.add(var[:-1] + "b")
                            if var.endswith("b"):
                                raw_candidates.add(var[:-1] + "v")
                            if var.endswith("rov"):
                                raw_candidates.add(var[:-3] + "rob")
                            if var.endswith("rob"):
                                raw_candidates.add(var[:-3] + "rov")

    # Filter out stopwords from base aliases
    clean_base = {a for a in raw_candidates if a not in ALIAS_STOPWORDS and len(a) >= 3}
    final_aliases = set(clean_base)

    # Add common Uzbek honorifics and suffixes
    for a in clean_base:
        final_aliases.add(f"{a} aka")
        final_aliases.add(f"{a} oka")
        final_aliases.add(f"{a} opa")
        final_aliases.add(f"{a}jon")
        final_aliases.add(f"{a}bek")
        final_aliases.add(f"{a} jura")
        final_aliases.add(f"{a} jo'ra")

    # Sort descending by length so most specific candidates match first
    return sorted(final_aliases, key=len, reverse=True)


def clean_message_text(text: str) -> str:
    """Cleans enclosing quotes, leading time particles ('da', 'ga'), and trailing Uzbek/Russian dispatch suffixes."""
    cleaned = text.strip()
    # Strip quotes
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    # Strip leading connectors: "da ", "ga ", "в "
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

    return cleaned.strip()


def transform_indirect_to_direct_uzbek(text: str) -> str:
    """
    Transforms indirect reported speech into natural direct first-person speech in Uzbek.
    Examples:
    - 'men borayotganimni uni yoniga' -> 'Yoningga boryapman'
    - 'borayotganimni uni yoniga' -> 'Yoningga boryapman'
    - 'kechikayotganimni ayt' -> 'Biroz kechikaman'
    - 'yetib kelganimni' -> 'Yetib keldim'
    """
    if not text:
        return text
    orig = text.strip()
    s = orig
    # Specific common full clauses
    s = re.sub(r"(?i)\b(?:men\s+)?borayotganimni\s+(?:uni\s+|uning\s+)?yoniga\b", "yoningga boryapman", s)
    s = re.sub(r"(?i)\b(?:uni\s+|uning\s+)?yoniga\s+(?:men\s+)?borayotganimni\b", "yoningga boryapman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?borishimni\s+(?:uni\s+|uning\s+)?yoniga\b", "yoningga boraman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?borayotganimni\b", "boryapman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?borishimni\b", "boraman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?kelayotganimni\b", "kelyapman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?kelishimni\b", "kelaman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?kechikayotganimni\b", "kechikayapman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?kechikishimni\b", "kechikaman", s)
    s = re.sub(r"(?i)\b(?:men\s+)?yetib\s+borganimni\b", "yetib bordim", s)
    s = re.sub(r"(?i)\b(?:men\s+)?yetib\s+kelganimni\b", "yetib keldim", s)
    s = re.sub(r"(?i)\b(?:uni\s+|uning\s+)?yoniga\b", "yoningga", s)

    s = re.sub(r"\s+", " ", s).strip()
    if s != orig and s:
        s = s[0].upper() + s[1:]
    return s


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


DELEGATION_SIGNALS = [
    r"\bmenga\s+(?:yetkaz|ayt|xabar\s+ber|bildir|ma['`’‘]?lum\s+qil|top|jo['`’‘]nat)\b",
    r"\b(?:bilib|aniqlab|ko['`’‘]rib|so['`’‘]rab|surishtir(?:ib)?)\s+ber\b",
    r"\b(?:shug['`’‘]?ullan|band)\w*\b",
    r"\bvaqti\s+bor\w*\b",
    r"\brej[ae]lari\b",
    r"\bnima(?:lar)?\s+qilyapti\b",
    r"\bhol-?ahvol\b",
    r"\bxabar\s+ol\w*\b",
    r"\b\d{1,2}:\d{2}\s*(?:gacha|до|until)\b",
]


def parse_deterministic_schedule(
    text: str,
    tz_name: str = "Asia/Tashkent",
    contacts: Optional[List[Any]] = None,
    default_peer: Optional[str] = None,
) -> Optional[ScheduleParseResult]:
    """
    High-speed deterministic parser covering:
    0. Flexible sentence-wide relative time + contact matching (e.g. 'mansur akaga kola kerakmi shuni sura 5 minutdan keyin')
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
    trimmed = text.strip()

    TIME_RELATIVE_UNITS = r"(?:daqiqa|minut|минут|daq|min|soat|соат|час|hour|minute)[\w]*"
    TIME_RELATIVE_POSTFIXES = r"(?:keyin|so['`’‘]?ng|song|sung|сунг|сўнг|o['`’‘]?tib|otib|utib|o‘tib|ichida|ichra|кеин|кейин)"

    # -------------------------------------------------------------
    # Pattern 0: Flexible Sentence-Wide Relative Time & Contact Matching
    # Handles:
    # "mansur akaga kola kearkmi shuni sura 5 minutdan keyin"
    # "@mansurjon_07 ga kola kerakmi deb so'ra 5 minutdan keyin"
    # "5 minutdan keyin mansur akaga kola kerakmi deb sura"
    # "mansur akaga 10 daqiqadan so'ng yoz"
    # "mansur yaxshimisan deb 2 minutdan sung yoz"
    # "mansurga 1 minutdan sung yozib quy  men borayotganimni uni yoniga"
    # -------------------------------------------------------------
    rel_anywhere = re.search(
        rf"\b(\d+)\s*({TIME_RELATIVE_UNITS})\s*{TIME_RELATIVE_POSTFIXES}\b",
        trimmed,
        re.IGNORECASE,
    )
    if rel_anywhere:
        num = int(rel_anywhere.group(1))
        unit_str = rel_anywhere.group(2).lower()
        delta = timedelta(hours=num) if any(u in unit_str for u in ["soat", "соат", "час", "hour"]) else timedelta(minutes=num)
        run_at = now + delta
        human = f"{num} daqiqadan so'ng" if delta.seconds < 3600 else f"{num} soatdan so'ng"

        rem = trimmed[:rel_anywhere.start()] + " " + trimmed[rel_anywhere.end():]
        rem = rem.strip()

        matched_peer = None
        p_match = re.search(r"(@[a-zA-Z0-9_]{3,32}|\+?\d{7,15})", rem)
        if p_match:
            matched_peer = p_match.group(1)
            rem = rem[:p_match.start()] + " " + rem[p_match.end():]
        elif contacts:
            for c in contacts:
                candidates = generate_contact_aliases(getattr(c, "username", None), getattr(c, "full_name", None))
                for cand in candidates:
                    cand_pat = rf"\b{re.escape(cand)}(?:ga|ka|qa|da|dan|ni|ning|yga|yda)?\b"
                    m_cand = re.search(cand_pat, rem, re.IGNORECASE)
                    if m_cand:
                        matched_peer = f"@{c.username}" if c.username else str(c.user_id)
                        rem = rem[:m_cand.start()] + " " + rem[m_cand.end():]
                        break
                if matched_peer:
                    break

        if not matched_peer and default_peer:
            matched_peer = default_peer

        if matched_peer:
            # 1. Strip leading dispatch phrases: 'yozib quy', 'yoz', 'aytib quy', 'ayt', etc.
            rem = re.sub(
                r"^(?:deb\s+)?(?:xabar\s+)?"
                r"(?:yoz(?:ib\s+(?:qo['`’‘]?y|quy|qoy|tashla|yubor))?|yoz(?:ing)?|"
                r"jo['`’‘]nat(?:ib\s+(?:yubor|qo['`’‘]?y|quy))?|jo['`’‘]nat(?:ing)?|"
                r"yubor(?:ib\s+(?:qo['`’‘]?y|quy))?|yubor(?:ing)?|"
                r"ayt(?:ib\s+(?:qo['`’‘]?y|quy|qoy|tashla|yubor))?|ayt(?:ing)?)\s+",
                "",
                rem.strip(),
                flags=re.IGNORECASE,
            )
            # 2. Strip trailing dispatch phrases
            rem = re.sub(
                r"\s+(?:deb\s+)?(?:xabar\s+)?"
                r"(?:yoz(?:ib\s+(?:qo['`’‘]?y|quy|qoy|tashla|yubor))?|yoz(?:ing)?|"
                r"jo['`’‘]nat(?:ib\s+(?:yubor|qo['`’‘]?y|quy))?|jo['`’‘]nat(?:ing)?|"
                r"yubor(?:ib\s+(?:qo['`’‘]?y|quy))?|yubor(?:ing)?|"
                r"ayt(?:ib\s+(?:qo['`’‘]?y|quy|qoy|tashla|yubor))?|ayt(?:ing)?)\.?$",
                "",
                rem.strip(),
                flags=re.IGNORECASE,
            )
            rem = re.sub(r"^(?:ga|da|dan|ni|ning)\s+", "", rem.strip(), flags=re.IGNORECASE)
            rem = re.sub(r"\b(?:shuni\s+)?(?:sura|so['`’‘]?ra|sorachi|surachi|so['`’‘]?rachi)\b", "", rem, flags=re.IGNORECASE)
            rem = re.sub(r"\b(?:deb|shuni|iltimos|ekan)\b", "", rem, flags=re.IGNORECASE)
            rem = re.sub(r"\bkearkmi\b", "kerakmi", rem, flags=re.IGNORECASE)
            rem = re.sub(r"\buiyapsiz\b", "o'qiyapsiz", rem, flags=re.IGNORECASE)
            rem = re.sub(r"\buqiyapti\b", "o'qiyapti", rem, flags=re.IGNORECASE)
            clean_msg = clean_message_text(rem)
            clean_msg = transform_indirect_to_direct_uzbek(clean_msg)
            if not clean_msg:
                clean_msg = "Salom, yaxshimisiz?"

            resolved_p = resolve_target_peer(matched_peer, contacts=contacts, raw_query=trimmed) or clean_target_peer(matched_peer)
            return ScheduleParseResult(
                target_peer=resolved_p,
                run_at=run_at,
                message_text=clean_msg,
                human_time=human,
            )

    # Guard: never misinterpret delegation, investigation, or assistant status inquiries as peer schedules
    if not trimmed.lower().startswith("/schedule"):
        if any(re.search(pat, trimmed, re.IGNORECASE) for pat in DELEGATION_SIGNALS):
            return None

    s = trimmed
    s = re.sub(r"^/schedule(?:@[a-zA-Z0-9_]+)?\s+", "", s, flags=re.IGNORECASE).strip()

    # -------------------------------------------------------------
    # Pattern 1: Relative minutes / hours:
    # "@sah_60 15 daqiqadan keyin xabar" OR "15 daqiqadan keyin @sah_60 ga xabar"
    # -------------------------------------------------------------
    # 1a: peer N daqiqa/minutdan keyin [text]
    m = re.match(
        rf"^(@?[a-zA-Z][a-zA-Z0-9_]{{3,31}}|\+?\d{{7,15}})\s+(?:ga\s+)?(\d+)\s+{TIME_RELATIVE_UNITS}\s+{TIME_RELATIVE_POSTFIXES}\s+(.+)$",
        s,
        re.IGNORECASE,
    )

    if not m:
        # 1b: N daqiqa/minutdan keyin peer [ga] [text]
        m2 = re.match(
            rf"^(\d+)\s+{TIME_RELATIVE_UNITS}\s+{TIME_RELATIVE_POSTFIXES}\s+(@?[a-zA-Z][a-zA-Z0-9_]{{3,31}}|\+?\d{{7,15}})(?:\s+ga)?\s+(.+)$",
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
            resolved_p = resolve_target_peer(peer, contacts=contacts, raw_query=trimmed) or clean_target_peer(peer)
            clean_msg = clean_message_text(msg)
            clean_msg = transform_indirect_to_direct_uzbek(clean_msg)
            return ScheduleParseResult(
                target_peer=resolved_p,
                run_at=run_at,
                message_text=clean_msg,
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
        resolved_p = resolve_target_peer(peer, contacts=contacts, raw_query=trimmed) or clean_target_peer(peer)
        clean_msg = clean_message_text(msg)
        clean_msg = transform_indirect_to_direct_uzbek(clean_msg)
        return ScheduleParseResult(
            target_peer=resolved_p,
            run_at=run_at,
            message_text=clean_msg,
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
        resolved_peer = peer_match.group(1) if peer_match else default_peer
        if time_match and resolved_peer:
            peer = resolved_peer
            time_str = time_match.group(1)
            hour, minute = map(int, time_str.split(":"))

            is_tomorrow = day_word in ("ertaga", "завтра")
            target_date = now.date() + timedelta(days=1) if is_tomorrow else now.date()
            target_dt = tz.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0))
            if not is_tomorrow and target_dt <= now:
                target_dt += timedelta(days=1)

            # Extract message text by removing peer, day word, time and connectors
            rem = s
            if peer_match:
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
    contacts: Optional[List[Any]] = None,
    default_peer: Optional[str] = None,
) -> Optional[ScheduleParseResult]:
    """
    Hybrid schedule parser:
    1. First tries deterministic regex parsing (instant, zero network latency, 100% reliable).
    2. If text is free-form or complex natural language, falls back to AI model.
    """
    # 1. Deterministic local pass
    deterministic_result = parse_deterministic_schedule(
        raw_text,
        tz_name=tz_name,
        contacts=contacts,
        default_peer=default_peer,
    )
    if deterministic_result:
        logger.info(
            f"Deterministic schedule parsed successfully: {deterministic_result.target_peer} at {deterministic_result.human_time}"
        )
        return deterministic_result


    # 2. AI semantic fallback pass
    trimmed = (raw_text or "").strip()
    if not trimmed.lower().startswith("/schedule"):
        if any(re.search(pat, trimmed, re.IGNORECASE) for pat in DELEGATION_SIGNALS):
            return None

    if ai_engine and hasattr(ai_engine, "parse_schedule_intent"):
        try:
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            ai_data = await ai_engine.parse_schedule_intent(raw_text, now, tz_name, contacts=contacts)
            if ai_data and ai_data.get("is_schedule") and ai_data.get("scheduled_iso"):
                run_at = datetime.fromisoformat(ai_data["scheduled_iso"])
                if run_at.tzinfo is None:
                    run_at = tz.localize(run_at)

                raw_target = ai_data.get("target_peer", "")
                resolved = resolve_target_peer(raw_target, contacts=contacts, raw_query=raw_text)
                if not resolved and default_peer:
                    resolved = default_peer

                raw_msg = ai_data.get("message_text", "Salom!")
                clean_msg = clean_message_text(raw_msg)
                clean_msg = transform_indirect_to_direct_uzbek(clean_msg)

                return ScheduleParseResult(
                    target_peer=resolved or raw_target,
                    run_at=run_at,
                    message_text=clean_msg,
                    human_time=ai_data.get("human_time", run_at.strftime("%Y-%m-%d %H:%M")),
                )
        except Exception as e:
            logger.warning(f"AI schedule parsing fallback failed: {type(e).__name__}")

    return None
