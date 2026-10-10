"""
Prompt Injection and Forbidden Topic Detection Engine for Antigravity Butler.
Provides a shared, unit-tested detector across 4 languages (Uzbek Latin, Uzbek Cyrillic,
Russian, English) to protect against prompt injection (U2) and filter forbidden topics (H6).
"""

import logging
import re
from typing import Optional, Tuple
from config import settings

logger = logging.getLogger(__name__)


def contains_injection_patterns(text: str) -> Tuple[bool, Optional[str]]:
    """
    Detects prompt injection and jailbreak attempt patterns in untrusted contact text.
    Checks keyword lists in settings across Uzbek (Latin/Cyrillic), Russian, and English (U2).
    Returns (is_suspected, matched_pattern).
    """
    if not text or not text.strip():
        return False, None

    normalized = text.lower().strip()

    # 1. Check keyword lists from config
    all_keywords = (
        getattr(settings, "MEM_INJECTION_KEYWORDS_UZ_LATIN", [])
        + getattr(settings, "MEM_INJECTION_KEYWORDS_UZ_CYRILLIC", [])
        + getattr(settings, "MEM_INJECTION_KEYWORDS_RU", [])
        + getattr(settings, "MEM_INJECTION_KEYWORDS_EN", [])
    )

    for kw in all_keywords:
        clean_kw = kw.lower().strip()
        if clean_kw and clean_kw in normalized:
            logger.warning(f"🚨 [U2 INJECTION DETECTED] Matched keyword '{clean_kw}' in text.")
            return True, clean_kw

    # 2. Structural instruction-like regex patterns
    regex_patterns = [
        r"\b(ignore\s+(all\s+)?(previous|prior)\s+instructions?)\b",
        r"\b(system\s+prompt|developer\s+mode|dan\s+mode)\b",
        r"\b(act\s+as\s+(an?|a)\s+\w+)\b",
        r"\b(you\s+must\s+always\s+(reply|answer))\b",
        r"\b(always\s+reply\s+with)\b",
        r"\b(disregard\s+(all\s+)?rules)\b",
        r"\b(har\s+doim\s+shunday\s+javob\s+ber)\b",
        r"\b(barcha\s+qoidalarni\s+bekor\s+qil)\b",
        r"\b(ko'rsatmalarni\s+bekor\s+qil|qoidalarni\s+bekor\s+qil)\b",
        r"\b(забудь\s+(все\s+)?инструкции)\b",
    ]

    for pattern in regex_patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            matched_str = match.group(0)
            logger.warning(f"🚨 [U2 INJECTION DETECTED] Matched regex pattern '{pattern}' (chars={len(matched_str)}).")
            return True, matched_str

    return False, None


def is_forbidden_topic(text: str, category: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Enforces Rule H6:
    NEVER extract as facts: health, religion, ethnicity, political views, sexual life,
    finances/debts/income, criminal matters.
    Returns (is_forbidden, reason).
    """
    if not text:
        return False, None

    # Check category name
    if category:
        cat_lower = category.lower().strip()
        forbidden_cats = [c.lower() for c in getattr(settings, "MEM_FORBIDDEN_CATEGORIES", [])]
        if cat_lower in forbidden_cats:
            return True, f"forbidden_category:{category}"

    normalized = text.lower()
    forbidden_kws = getattr(settings, "MEM_FORBIDDEN_KEYWORDS", [])

    for kw in forbidden_kws:
        clean_kw = kw.lower().strip()
        if not clean_kw:
            continue
        if len(clean_kw) <= 3:
            pattern = r"\b" + re.escape(clean_kw) + r"\b"
        else:
            # Word-start match to catch inflectional suffixes (e.g. byudjeti, maoshi, moliyaviy)
            pattern = r"\b" + re.escape(clean_kw)
        if re.search(pattern, normalized):
            return True, f"forbidden_keyword:{clean_kw}"

    return False, None


class MemoryInjectionDetector:
    """Wrapper class for prompt injection and forbidden topic detection."""

    @staticmethod
    def detect_injection(text: str) -> bool:
        is_suspected, _ = contains_injection_patterns(text)
        return is_suspected

    @staticmethod
    def detect_forbidden_topics(text: str, category: Optional[str] = None) -> bool:
        is_forbidden, _ = is_forbidden_topic(text, category)
        return is_forbidden
