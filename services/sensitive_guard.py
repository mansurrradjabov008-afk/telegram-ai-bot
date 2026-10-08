"""
Sensitive-Topic Guard module for Antigravity AI Butler.
Provides multi-layered detection (keyword/regex + fail-closed AI classification)
covering Uzbek (Latin & Cyrillic), Russian, and English across 6 critical categories:
1. Money / Payments
2. Personal Data / Credentials
3. Promises / Commitments
4. Meeting / Scheduling
5. Legal / Official Matters
6. Suspicious Instructions / Prompt Injection
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# ------------------ REGEX PATTERNS ------------------

# 1. 13-19 digit card-like sequences (with optional spaces or dashes)
CARD_PATTERN_CANDIDATE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# 2. Currency amounts (e.g. 50 000 so'm, 100$, $500, 5000 rubl, 200 USD, 15 mln som)
AMOUNT_PATTERN = re.compile(
    r"(?:(?:(?<!\w)\d+[\d\s.,]*(?:\s*(?:so['`’‘]m|som|сўм|сум|usd|dollar|доллар|\$|rubl|рубл|руб|евро|euro|eur|ming|минг|mln|млн|million|миллион))(?!\w))|(?:(?<!\w)[$€£]\s*\d+[\d\s.,]*(?!\w)))",
    re.IGNORECASE,
)

# ------------------ CATEGORIZED KEYWORD DICTIONARIES ------------------
# Multi-lingual coverage: Uzbek Latin, Uzbek Cyrillic, Russian, English

SENSITIVE_CATEGORIES: Dict[str, List[str]] = {
    "money_payments": [
        # Uzbek Latin
        "pul", "to'lov", "to‘lov", "to`lov", "tolov", "so'm", "som", "karta", "kartaga",
        "karta raqam", "shot", "hisob", "hisob raqam", "narxi", "qancha", "qancha bo'ladi",
        "qarz", "dollor", "dollar", "usd", "summa", "to'ladim", "toladim", "click", "payme",
        "uzum", "uzum bank", "tranzaksiya", "otpravil", "oplata",
        # Uzbek Cyrillic
        "пул", "тўлов", "толов", "сўм", "сом", "карта", "картага", "карта рақам",
        "ҳисоб", "ҳисоб рақам", "нархи", "қанча", "қанча бўлади", "қарз", "доллар",
        "сумма", "тўладим", "толадим", "транзакция",
        # Russian
        "деньги", "денег", "оплата", "платеж", "перевод", "цена", "стоимость", "сколько стоит",
        "карта", "номер карты", "счет", "транзакция", "доллар", "оплатил", "перевел", "долг",
        "рубль", "рублей", "переведи",
        # English
        "money", "payment", "pay", "transfer", "price", "how much", "credit card", "debit card",
        "bank account", "invoice", "bill", "debt", "cash", "transaction", "fee",
    ],
    "personal_data": [
        # Uzbek Latin
        "parol", "password", "kod", "pin", "sms kod", "tasdiqlash kodi", "pasport",
        "passport", "pinfl", "jshshir", "seriya", "karta raqam", "cvv", "cvc",
        "kod keldimi", "kodni ayt", "maxfiy kod", "token", "maxfiy",
        # Uzbek Cyrillic
        "парол", "код", "пин", "смс код", "тасдиқлаш коди", "паспорт", "пинфл", "жшшир",
        "серия", "карта рақам", "махфий код", "токен",
        # Russian
        "пароль", "код", "пин", "смс код", "код подтверждения", "паспорт", "номер карты",
        "свв", "сививи", "секретный код", "токен", "логин",
        # English
        "password", "passcode", "pin", "sms code", "verification code", "otp", "passport",
        "ssn", "card number", "cvv", "cvc", "secret key", "secret token", "credentials",
    ],
    "promises_commitments": [
        # Uzbek Latin
        "va'da", "vada", "va'da beraman", "so'z beraman", "aniq qilaman", "aniq bitadi",
        "garantiya", "kafolat", "kafolatlayman", "100% bo'ladi", "so'z berdim", "ishonchim komil",
        # Uzbek Cyrillic
        "ваъда", "вада", "ваъда бераман", "сўз бераман", "аниқ қиламан", "гарантия",
        "кафолат", "кафолатлайман", "100% бўлади", "сўз бердим",
        # Russian
        "обещаю", "гарантирую", "точно сделаю", "слово даю", "ручаюсь", "100% будет",
        "клянусь", "гарантия", "обязуюсь",
        # English
        "promise", "guarantee", "swear", "i will definitely", "commit", "warranty",
        "assure", "100% sure", "i promise",
    ],
    "meeting_schedule": [
        # Uzbek Latin
        "uchrashuv", "uchrashamiz", "ko'rishamiz", "korishamiz", "qayerda uchrashamiz",
        "soat nechada", "lokatsiya", "joylashuv", "manzil", "manzilingiz", "qayerdasan",
        "kelasanmi", "borasanmi", "uchrashamizmi", "ko'rishaylik",
        # Uzbek Cyrillic
        "учрашув", "учрашамиз", "кўришамиз", "коришамиз", "қаерда учрашамиз", "соат нечада",
        "локация", "манзил", "манзилингиз", "қаердасан", "келасанми", "борасанми", "учрашамизми",
        # Russian
        "встреча", "встретимся", "где встретимся", "увидимся", "во сколько", "локация",
        "адрес", "где ты", "приедешь", "подъедешь", "место встречи", "давай встретимся",
        # English
        "meeting", "meet up", "let's meet", "where to meet", "what time", "location",
        "address", "where are you", "rendezvous", "see you at",
    ],
    "legal_official": [
        # Uzbek Latin
        "shartnoma", "shartnoma tuzamiz", "sud", "prokuratura", "militsiya", "iib",
        "yurist", "advokat", "rasmiy hujjat", "muhr", "imzo", "qonun", "jarima",
        # Uzbek Cyrillic
        "шартнома", "суд", "прокуратура", "милиция", "ииб", "юрист", "адвокат",
        "расмий ҳужжат", "муҳр", "имзо", "қонун", "жарима",
        # Russian
        "договор", "суд", "прокуратура", "полиция", "юрист", "адвокат", "документ",
        "печать", "подпись", "закон", "штраф", "иск", "судебный",
        # English
        "contract", "court", "lawsuit", "prosecutor", "police", "lawyer", "attorney",
        "official document", "stamp", "signature", "fine", "penalty", "legal agreement",
    ],
    "suspicious_instruction": [
        # Uzbek Latin
        "ignore previous instructions", "ignore all instructions", "system prompt",
        "act as", "forget instructions", "developer mode", "jailbreak",
        "oldingi buyruqlarni unut", "tizim ko'rsatmasi", "sen kimsan", "barcha qoidalarni buz",
        "promptni ko'rsat", "ko'rsatmalaringni tashla", "barcha yo'riqnomani ber",
        # Uzbek Cyrillic
        "олдинги буйруқларни унут", "тизим кўрсатмаси", "сен кимсан", "барча қоидаларни буз",
        "промптни кўрсат", "кўрсатмаларингни ташла", "барча йўриқномани бер",
        # Russian
        "забудь все инструкции", "игнорируй предыдущие инструкции", "системный промпт",
        "действуй как", "притворись", "режим разработчика", "покажи промпт",
        "раскрой инструкции", "обойди правила",
        # English
        "ignore previous instructions", "ignore all instructions", "system prompt",
        "act as", "roleplay as", "forget instructions", "developer mode", "jailbreak",
        "disregard instructions", "show prompt", "reveal prompt", "bypass rules",
    ],
}


@dataclass
class SensitiveCheckResult:
    """
    Structured result of sensitive check.
    Supports attribute access, dictionary indexing, and json serialization.
    """
    is_sensitive: bool
    reasons: List[str] = field(default_factory=list)

    @property
    def sensitive(self) -> bool:
        """Alias for is_sensitive."""
        return self.is_sensitive

    @property
    def primary_reason(self) -> Optional[str]:
        if not self.reasons:
            return None
        return ", ".join(self.reasons)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sensitive": self.is_sensitive,
            "reasons": self.reasons,
            "primary_reason": self.primary_reason,
        }

    def __getitem__(self, item: str) -> Any:
        if item in ("sensitive", "is_sensitive"):
            return self.is_sensitive
        elif item == "reasons":
            return self.reasons
        elif item == "primary_reason":
            return self.primary_reason
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except KeyError:
            return default


def check_card_number(text: str) -> bool:
    """Detects 13-19 digit card-like numeric sequences."""
    candidates = CARD_PATTERN_CANDIDATE.findall(text)
    for cand in candidates:
        digits_only = re.sub(r"\D", "", cand)
        if 13 <= len(digits_only) <= 19:
            return True
    return False


def _contains_keyword(text_lower: str, keyword: str) -> bool:
    """Matches keyword honoring Unicode word/punctuation boundaries and apostrophes."""
    pattern = r"(?:(?<=[\s\W_])|^)" + re.escape(keyword) + r"(?:(?=[\s\W_])|$)"
    return bool(re.search(pattern, text_lower))


def check_sensitive_keywords(text: str) -> List[str]:
    """
    Evaluates text against regex patterns (cards, currencies) and multi-lingual keyword dictionaries.
    Returns list of triggered reason descriptions in Uzbek.
    """
    if not text:
        return []

    lower_text = text.lower()
    detected_reasons: List[str] = []

    # Check 13-19 digit card numbers
    if check_card_number(text):
        detected_reasons.append("Bank karta raqami aniqlandi")

    # Check currency amounts
    if AMOUNT_PATTERN.search(text):
        detected_reasons.append("Pul/Summa miqdori aniqlandi")

    category_labels = {
        "money_payments": "Pul va to'lov mavzusi",
        "personal_data": "Shaxsiy ma'lumotlar / Parol / Kod",
        "promises_commitments": "Va'da / Kafolat berish",
        "meeting_schedule": "Uchrashuv vaqti yoki manzili",
        "legal_official": "Huquqiy / Rasmiy masala",
        "suspicious_instruction": "Shubhali ko'rsatma / Prompt injection",
    }

    # Check keyword categories
    for category_name, keywords in SENSITIVE_CATEGORIES.items():
        for kw in keywords:
            if _contains_keyword(lower_text, kw):
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
    Layer a: Keyword & Regex inspection (Uzbek Latin, Uzbek Cyrillic, Russian, English).
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
            if isinstance(ai_result, dict) and ai_result.get("sensitive"):
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
