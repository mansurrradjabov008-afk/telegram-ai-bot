"""
Sensitive-Topic Guard module for Antigravity AI Butler.
Provides multi-layered detection (keyword/regex + fail-closed AI classification)
to prevent the AI from automatically sending sensitive, financial, or legally binding replies.
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)

# ------------------ KEYWORD & REGEX PATTERNS ------------------

# 1. 13-19 digit card-like sequences (with optional spaces or dashes)
CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# 2. Money / Currency amounts (e.g. 50 000 so'm, 100$, 5000 rubl, 200 USD)
AMOUNT_PATTERN = re.compile(
    r"\b\d+[\d\s.,]*(?:\s*(?:so['`’‘]m|som|сум|usd|dollar|\$|rubl|руб|евро|euro|eur|ming|mln|million|минг))\b",
    re.IGNORECASE,
)

# 3. Categorized keyword dictionaries (Uzbek Latin, Uzbek Cyrillic, Russian, English)
SENSITIVE_CATEGORIES = {
    "money_payments": [
        "pul", "to'lov", "to‘lov", "to`lov", "tolov", "so'm", "som", "сум", "деньги",
        "оплата", "перевод", "narxi", "qancha bo'ladi", "qancha", "click", "payme",
        "uzum bank", "karta", "kartaga", "karta raqam", "shot", "hisob raqam", "tranzaksiya",
        "dollar", "usd", "summa", "to'ladim", "toladim", "otpravil", "oplata",
    ],
    "personal_data": [
        "parol", "password", "kod", "pin", "sms kod", "tasdiqlash kodi", "pasport",
        "passport", "pinfl", "jshshir", "seriya", "karta raqam", "cvv", "cvc",
        "kod keldimi", "kodni ayt", "пароль", "код", "смс код", "паспорт",
    ],
    "promises_commitments": [
        "va'da", "vada", "va'da beraman", "so'z beraman", "aniq qilaman",
        "garantiya", "kafolat", "kafolatlayman", "100% bo'ladi",
        "обещаю", "гарантирую", "точно сделаю", "слово даю", "promise", "guarantee",
    ],
    "meeting_schedule": [
        "uchrashuv", "uchrashamiz", "ko'rishamiz", "korishamiz", "qayerda uchrashamiz",
        "soat nechada", "lokatsiya", "joylashuv", "manzil", "manzilingiz", "qayerdasan",
        "kelasanmi", "borasanmi", "встреча", "встретимся", "где встретимся", "локация", "адрес",
    ],
    "legal_official": [
        "shartnoma", "shartnoma tuzamiz", "sud", "prokuratura", "militsiya", "iib",
        "yurist", "advokat", "rasmiy hujjat", "muhr", "imzo", "qonun", "jarima",
        "договор", "суд", "прокуратура", "полиция", "юрист", "адвокат", "документ",
    ],
    "suspicious_instruction": [
        "ignore previous instructions", "ignore all instructions", "system prompt",
        "act as", "forget instructions", "developer mode", "jailbreak",
        "oldingi buyruqlarni unut", "tizim ko'rsatmasi", "sen kimsan", "barcha qoidalarni buz",
        "promptni ko'rsat", "ko'rsatmalaringni tashla", "barcha yo'riqnomani ber",
    ],
}


@dataclass
class SensitiveCheckResult:
    """Structured result of sensitive check."""
    is_sensitive: bool
    reasons: List[str] = field(default_factory=list)

    @property
    def primary_reason(self) -> Optional[str]:
        if not self.reasons:
            return None
        return ", ".join(self.reasons)


def check_sensitive_keywords(text: str) -> List[str]:
    """
    Evaluates text against regex patterns and keyword dictionaries.
    Returns list of triggered reason categories.
    """
    if not text:
        return []

    lower_text = text.lower()
    detected_reasons = []

    # Check 13-19 digit card numbers
    if CARD_PATTERN.search(text):
        detected_reasons.append("Bank karta raqami aniqlandi")

    # Check currency amounts
    if AMOUNT_PATTERN.search(text):
        detected_reasons.append("Pul/Summa miqdori aniqlandi")

    # Check keyword categories
    for category_name, keywords in SENSITIVE_CATEGORIES.items():
        for kw in keywords:
            pattern = r"(?:\b|_|^)" + re.escape(kw) + r"(?:\b|_|$)"
            if re.search(pattern, lower_text):
                category_labels = {
                    "money_payments": "Pul va to'lov mavzusi",
                    "personal_data": "Shaxsiy ma'lumotlar / Parol / Kod",
                    "promises_commitments": "Va'da / Kafolat berish",
                    "meeting_schedule": "Uchrashuv vaqti yoki manzili",
                    "legal_official": "Huquqiy / Rasmiy masala",
                    "suspicious_instruction": "Shubhali ko'rsatma / Prompt injection",
                }
                label = category_labels.get(category_name, category_name)
                if label not in detected_reasons:
                    detected_reasons.append(label)
                break

    return detected_reasons


async def check_sensitive(
    incoming_text: str,
    proposed_reply: str = "",
    ai_engine: Optional[Any] = None,
) -> SensitiveCheckResult:
    """
    Dual-layer sensitive-topic guard:
    Layer a: Fast Keyword & Regex inspection (Latin, Cyrillic, Russian, English)
    Layer b: AI Classifier inspection with strict JSON output (temperature 0, fail-closed).

    Combined with OR: if either layer triggers, or if AI call fails, result is marked sensitive.
    """
    reasons: List[str] = []

    # Layer a: Check incoming text and proposed reply with keyword layer
    in_reasons = check_sensitive_keywords(incoming_text)
    out_reasons = check_sensitive_keywords(proposed_reply)
    reasons.extend(in_reasons)
    for r in out_reasons:
        if r not in reasons:
            reasons.append(f"Javobda: {r}")

    # If keyword layer already detected sensitivity, fail-safe early
    if reasons:
        return SensitiveCheckResult(is_sensitive=True, reasons=reasons)

    # Layer b: AI Classifier layer if AI engine is provided
    if ai_engine and hasattr(ai_engine, "classify_sensitive_topic"):
        try:
            ai_result = await ai_engine.classify_sensitive_topic(incoming_text, proposed_reply)
            if ai_result.get("sensitive"):
                ai_reasons = ai_result.get("reasons", ["AI tomonidan nozik mavzu aniqlandi"])
                reasons.extend(ai_reasons)
                return SensitiveCheckResult(is_sensitive=True, reasons=reasons)
        except Exception as e:
            # FAIL-CLOSED: Any failure, timeout, or invalid output forces sensitive draft
            logger.warning(f"Sensitive guard AI classifier failed ({e}). Enforcing fail-closed sensitive flag.")
            return SensitiveCheckResult(
                is_sensitive=True,
                reasons=[f"AI xavfsizlik tekshiruvi xatosi (fail-closed): {str(e)[:50]}"],
            )

    return SensitiveCheckResult(is_sensitive=bool(reasons), reasons=reasons)
