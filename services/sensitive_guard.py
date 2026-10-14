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

# 2. Currency amounts (e.g. 50 000 so'm, 100$, $500, 5000 rubl, 200 USD, 70+ mln som, 10-15 mln)
AMOUNT_PATTERN = re.compile(
    r"(?:(?:(?<!\w)\d+[\d\s.,+~-]*\s*(?:so['`’‘]m|som|сўм|сум|usd|dollar|доллар|\$|rubl|рубл|руб|евро|euro|eur|ming|минг|mln|млн|million|миллион))(?!\w)|(?:(?<!\w)[$€£]\s*\d+[\d\s.,+~-]*(?!\w)))",
    re.IGNORECASE,
)

# ------------------ CATEGORIZED KEYWORD DICTIONARIES ------------------
# Multi-lingual coverage: Uzbek Latin, Uzbek Cyrillic, Russian, English

SENSITIVE_CATEGORIES: Dict[str, List[str]] = {
    "money_payments": [
        # Uzbek Latin
        "pul", "to'lov", "to‘lov", "to`lov", "tolov", "so'm", "som", "so’m", "karta", "kartaga",
        "kartangiz", "kartam", "kartamga", "kartangizga", "kartasi", "kartasiga",
        "karta raqam", "karta raqami", "kartangiz raqami", "karta raqamingiz", "kartangiz raqamini",
        "shot", "hisob raqam", "narxi", "narx", "narxi qancha", "qancha bo'ladi", "qancha to'lash",
        "qarz", "dollor", "dollar", "usd", "summa", "to'ladim", "toladim", "click", "payme",
        "uzum", "uzum bank", "tranzaksiya", "otpravil", "oplata", "daromad", "daromadi", "maosh",
        "maoshi", "oylik", "oyligi", "ish haqi", "bepul", "pullik", "million", "mln",
        "kurs", "valyuta", "rubl", "yevro", "investitsiya",
        # Uzbek Cyrillic
        "пул", "тўлов", "толов", "сўм", "сом", "карта", "картага", "картангиз", "картам",
        "карта рақам", "карта рақами", "картангиз рақами", "карта рақамингиз",
        "ҳисоб рақам", "нархи", "нарх", "нархи қанча", "қанча бўлади", "қарз", "доллар",
        "сумма", "тўладим", "толадим", "транзакция", "даромад", "даромади", "маош", "ойлик",
        "иш ҳақи", "бепул", "пуллик", "миллион", "млн", "курс", "валюта", "рубль",
        # Russian
        "деньги", "денег", "оплата", "платеж", "перевод", "цена", "стоимость", "сколько стоит",
        "карта", "номер карты", "счет", "транзакция", "доллар", "оплатил", "перевел", "долг",
        "рубль", "рублей", "переведи", "доход", "зарплата", "оклад", "бесплатно", "миллион",
        # English
        "money", "payment", "pay", "transfer", "price", "how much", "credit card", "debit card",
        "bank account", "invoice", "bill", "debt", "cash", "transaction", "fee", "salary", "income",
    ],
    "personal_data": [
        # Uzbek Latin
        "parol", "password", "pin", "sms kod", "tasdiqlash kodi", "pasport",
        "passport", "pinfl", "jshshir", "seriya", "karta raqam", "cvv", "cvc",
        "kartangiz raqami", "karta raqamingiz", "kartangiz raqamini", "karta raqami",
        "kod keldimi", "kodni ayt", "maxfiy kod", "token", "maxfiy",
        "telefon raqam", "telefon raqami", "telefon raqamingiz", "tel raqam",
        "raqamizni bering", "nomerizni bering", "telefonizni bering", "telefoningizni bering",
        "nomer tashlang", "raqam tashlang",
        # Uzbek Cyrillic
        "парол", "пин", "смс код", "тасдиқлаш коди", "паспорт", "пинфл", "жшшир",
        "серия", "карта рақам", "махфий код", "токен", "телефон рақам", "тел рақам",
        "карта рақами", "картангиз рақами", "карта рақамингиз",
        "рақамизни беринг", "номеризни беринг", "телефонингизни беринг",
        # Russian
        "пароль", "пин", "смс код", "код подтверждения", "паспорт", "номер карты",
        "свв", "сививи", "секретный код", "токен", "логин", "номер телефона", "твой номер",
        # English
        "password", "passcode", "pin", "sms code", "verification code", "otp", "passport",
        "ssn", "card number", "cvv", "cvc", "secret key", "secret token", "credentials",
        "phone number", "your number", "contact number",
    ],
    "promises_commitments": [
        # Uzbek Latin
        "va'da", "vada", "va'da beraman", "so'z beraman", "aniq qilaman", "aniq bitadi",
        "garantiya", "kafolat", "kafolatlayman", "100% bo'ladi", "so'z berdim", "ishonchim komil",
        "ish bilan ta'minlash", "ishga joylash",
        # Uzbek Cyrillic
        "ваъда", "вада", "ваъда бераман", "сўз бераман", "аниқ қиламан", "гарантия",
        "кафолат", "кафолатлайман", "100% бўлади", "сўз бердим", "иш билан таъминлаш",
        # Russian
        "обещаю", "гарантирую", "точно сделаю", "слово даю", "ручаюсь", "100% будет",
        "клянусь", "гарантия", "обязуюсь", "трудоустройство",
        # English
        "promise", "guarantee", "swear", "i will definitely", "commit", "warranty",
        "assure", "100% sure", "i promise", "employment guarantee",
    ],
    "meeting_schedule": [
        # Uzbek Latin
        "uchrashuv", "uchrashamiz", "ko'rishamiz", "korishamiz", "qayerda uchrashamiz",
        "soat nechada", "lokatsiya", "joylashuv", "manzil", "manzilingiz",
        "uchrashamizmi", "ko'rishaylik",
        # Uzbek Cyrillic
        "учрашув", "учрашамиз", "кўришамиз", "коришамиз", "қаерда учрашамиз", "соат нечада",
        "локация", "манзил", "манзилингиз", "учрашамизми",
        # Russian
        "встреча", "встретимся", "где встретимся", "увидимся", "во сколько", "локация",
        "адрес", "место встречи", "давай встретимся",
        # English
        "meeting", "meet up", "let's meet", "where to meet", "what time", "location",
        "address", "rendezvous", "see you at",
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
    hard_sensitive: bool = False
    categories: List[str] = field(default_factory=list)

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
            "hard_sensitive": self.hard_sensitive,
            "categories": self.categories,
        }

    def __getitem__(self, item: str) -> Any:
        if item in ("sensitive", "is_sensitive"):
            return self.is_sensitive
        elif item == "reasons":
            return self.reasons
        elif item == "primary_reason":
            return self.primary_reason
        elif item == "hard_sensitive":
            return self.hard_sensitive
        elif item == "categories":
            return self.categories
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


def check_sensitive_keywords_detailed(text: str) -> Dict[str, Any]:
    """
    Evaluates text against regex patterns and keyword dictionaries.
    Returns dict with reasons, matched categories, and whether hard critical sensitivity was hit.
    """
    if not text:
        return {"reasons": [], "categories": [], "hard_sensitive": False}

    lower_text = text.lower()
    detected_reasons: List[str] = []
    detected_categories: List[str] = []
    is_hard = False

    # Check 13-19 digit card numbers
    if check_card_number(text):
        detected_reasons.append("Bank karta raqami aniqlandi")
        detected_categories.append("cards")
        is_hard = True

    # Check currency amounts
    if AMOUNT_PATTERN.search(text):
        detected_reasons.append("Pul/Summa miqdori aniqlandi")
        detected_categories.append("amounts")
        is_hard = True

    category_labels = {
        "money_payments": "Pul va to'lov mavzusi",
        "personal_data": "Shaxsiy ma'lumotlar / Parol / Kod",
        "promises_commitments": "Va'da / Kafolat berish",
        "meeting_schedule": "Uchrashuv vaqti yoki manzili",
        "legal_official": "Huquqiy / Rasmiy masala",
        "suspicious_instruction": "Shubhali ko'rsatma / Prompt injection",
    }

    # Hard categories that must never be auto-handled
    HARD_CATEGORIES = {
        "money_payments",
        "personal_data",
        "promises_commitments",
        "legal_official",
        "suspicious_instruction",
    }

    for category_name, keywords in SENSITIVE_CATEGORIES.items():
        for kw in keywords:
            if _contains_keyword(lower_text, kw):
                label = category_labels.get(category_name, category_name)
                if label not in detected_reasons:
                    detected_reasons.append(label)
                if category_name not in detected_categories:
                    detected_categories.append(category_name)
                if category_name in HARD_CATEGORIES:
                    is_hard = True
                break

    return {
        "reasons": detected_reasons,
        "categories": detected_categories,
        "hard_sensitive": is_hard,
    }


def check_sensitive_keywords(text: str) -> List[str]:
    """
    Evaluates text against regex patterns (cards, currencies) and multi-lingual keyword dictionaries.
    Returns list of triggered reason descriptions in Uzbek.
    """
    return check_sensitive_keywords_detailed(text)["reasons"]


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
    Also tracks hard_sensitive to distinguish hard security threats from routine chitchat.
    """
    reasons: List[str] = []
    categories: List[str] = []
    hard_sensitive = False

    # Layer a: Check incoming text and proposed reply with keyword layer
    in_detail = check_sensitive_keywords_detailed(incoming_text)
    out_detail = check_sensitive_keywords_detailed(proposed_reply)

    reasons.extend(in_detail["reasons"])
    categories.extend(in_detail["categories"])
    if in_detail["hard_sensitive"]:
        hard_sensitive = True

    # Proposed reply only forces hard sensitive if it contains HARD sensitive keywords (money, cards, credentials, guarantees)
    if out_detail["hard_sensitive"]:
        hard_sensitive = True
    if out_detail["reasons"]:
        for r in out_detail["reasons"]:
            reasons.append(f"Javobda: {r}")
        for c in out_detail["categories"]:
            if c not in categories:
                categories.append(f"reply_{c}")

    # If keyword layer already detected sensitivity
    if reasons:
        return SensitiveCheckResult(
            is_sensitive=True,
            reasons=reasons,
            hard_sensitive=hard_sensitive,
            categories=categories,
        )

    # Layer b: AI Classifier layer if AI engine is provided
    if ai_engine and hasattr(ai_engine, "classify_sensitive_topic"):
        try:
            ai_result = await ai_engine.classify_sensitive_topic(incoming_text, proposed_reply)
            if isinstance(ai_result, dict) and ai_result.get("sensitive"):
                ai_reasons = ai_result.get("reasons", ["AI tomonidan nozik mavzu aniqlandi"])
                reasons.extend(ai_reasons)
                categories.append("ai_classifier")

                # Differentiate hard security threats from soft scheduling/coordination
                is_ai_hard = bool(ai_result.get("hard_sensitive", False))
                cat = str(ai_result.get("category", "")).lower()
                reasons_str = " ".join(ai_reasons).lower()

                # If category or reasons are purely meeting/scheduling or soft chitchat without money/credentials
                if any(w in cat for w in ["meeting", "casual", "chitchat", "schedule"]) or any(
                    w in reasons_str for w in ["uchrashuv", "joyi", "vaqti", "kutubxona", "choyxona"]
                ):
                    if not any(k in reasons_str for k in ["pul", "karta", "parol", "sud", "shartnoma", "kod"]):
                        is_ai_hard = False
                elif not any(k in reasons_str for k in ["pul", "karta", "parol", "sud", "shartnoma", "kod", "inj"]):
                    if not in_detail["hard_sensitive"] and not out_detail["hard_sensitive"]:
                        is_ai_hard = False

                if is_ai_hard:
                    hard_sensitive = True

                return SensitiveCheckResult(
                    is_sensitive=True,
                    reasons=reasons,
                    hard_sensitive=hard_sensitive,
                    categories=categories,
                )
        except Exception as e:
            # FAIL-CLOSED: Any failure, timeout, or invalid output forces sensitive flag,
            # but only marks hard_sensitive if keyword layer detected true critical boundaries.
            logger.warning(f"Sensitive guard AI classifier failed ({type(e).__name__}). Enforcing fail-closed sensitive flag.")
            is_fail_hard = bool(in_detail.get("hard_sensitive") or out_detail.get("hard_sensitive"))
            return SensitiveCheckResult(
                is_sensitive=True,
                reasons=[f"AI xavfsizlik tekshiruvi xatosi (fail-closed): {type(e).__name__}"],
                hard_sensitive=is_fail_hard,
                categories=["fail_closed_error"],
            )

    return SensitiveCheckResult(
        is_sensitive=bool(reasons),
        reasons=reasons,
        hard_sensitive=hard_sensitive,
        categories=categories,
    )
