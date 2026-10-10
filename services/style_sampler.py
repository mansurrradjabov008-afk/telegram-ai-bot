"""
Writing Style Sampler and Hygiene Filter (D2, D3).
Applies pure code statistics and hygiene filters for style sample extraction.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Optional, Set, List
from config import settings

_MEDIA_MARKER_REGEX = re.compile(
    r"^\[(?:rasm|ovozli xabar|dumaloq video|video|hujjat|stiker|fayl)\]$",
    re.IGNORECASE,
)
_URL_REGEX = re.compile(r"https?://[^\s]+|t\.me/[^\s]+", re.IGNORECASE)

# Lexicons for language/script detection
_UZ_CYR_LETTERS = set("ўқғҳЎҚҒҲ")
_RU_SPECIFIC_LETTERS = set("ыэъщьЫЭЪЩЬ")

_UZ_WORDS = {
    "assalomu", "alaykum", "va", "bilan", "uchun", "ham", "yaxshi", "rahmat",
    "qalay", "ishlar", "nima", "gap", "boldi", "boladi", "siz", "sizga", "sizni",
    "sen", "senga", "senni", "aka", "uka", "jigar", "zor", "ha", "yoq", "bormi",
    "kelyapsan", "kelyapsiz", "qilyapsan", "qilyapsiz", "aytganingizdek",
    "ogayni", "xop", "mayli", "albatta", "dustim", "dostim", "salom",
}

_UZ_CYR_WORDS = {
    "ассалому", "алайкум", "ва", "билан", "учун", "ҳам", "хам", "яхши", "рахмат",
    "раҳмат", "қалай", "калай", "ишлар", "нима", "гап", "бўлди", "болди", "бўлади",
    "сиз", "сизга", "сизни", "сен", "сенга", "сени", "ака", "ука", "жигар", "зўр",
    "ҳа", "ха", "йўқ", "йок", "борми", "келяпсиз", "қиляпсиз", "хўп", "майли", "салом",
}

_RU_WORDS = {
    "привет", "здравствуйте", "как", "дела", "что", "это", "для", "ваши", "спасибо",
    "хорошо", "пожалуйста", "да", "нет", "будет", "когда", "где", "почему", "меня",
    "тебя", "вас", "добрый", "день", "вечер", "утро", "сегодня", "завтра",
}

_EN_WORDS = {
    "hello", "hi", "how", "are", "you", "doing", "today", "thanks", "thank",
    "good", "fine", "what", "is", "up", "the", "and", "for", "with", "see",
    "later", "yes", "no", "okay", "ok", "please", "tomorrow", "tonight",
}

_SIZ_PATTERNS = [
    r"\bsiz\b", r"\bsizga\b", r"\bsizni\b", r"\bsizda\b", r"\bsizdan\b",
    r"\b(yaxshimisiz|salomatmisiz|bormisiz|qandaysiz|charchamayapsizmi)\b",
    r"\b\w+(?:yapsiz|yapsizmi|yapsizlar|dingiz|dingizmi|ingiz|ingizni|ingizga)\b",
    r"\b(rahmat|tashakkur)\b",
]
_SEN_PATTERNS = [
    r"\bsen\b", r"\bsenga\b", r"\bsenni\b", r"\bsenda\b", r"\bsendan\b",
    r"\b(qalaysan|yaxshimisan|nima gap|bormisan|qandaysan)\b",
    r"\b\w+(?:yapsan|yapsanmi|ding|dingmi|san|sanmi)\b",
    r"\b(bro|jigar|og'ayni|ogayni|o'rtoq|ortoq)\b",
]


@dataclass
class HygieneCheckResult:
    passed: bool
    reason: str


def normalize_sample_text(text: str) -> str:
    """Normalizes text by lowercasing, standardizing apostrophes, stripping punctuation, and collapsing whitespace."""
    if not text:
        return ""
    # Standardize apostrophes
    t = re.sub(r"[`’‘ʼ']", "'", text.lower())
    # Remove punctuation except letters and numbers
    t = re.sub(r"[^\w\s']", " ", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_hygienic_sample(
    text: str,
    sent_at: Optional[datetime] = None,
    is_forward: bool = False,
    via_bot: bool = False,
    is_service: bool = False,
    is_media_only: bool = False,
    max_chars: Optional[int] = None,
    window_days: Optional[int] = None,
    seen_normalized: Optional[Set[str]] = None,
    bot_sent_normalized: Optional[Set[str]] = None,
) -> HygieneCheckResult:
    """
    Applies pure code hygiene rules (Rule D2) to filter out non-owner or inappropriate messages.
    """
    if is_forward:
        return HygieneCheckResult(passed=False, reason="forwarded")
    if via_bot:
        return HygieneCheckResult(passed=False, reason="via_bot")
    if is_service:
        return HygieneCheckResult(passed=False, reason="service")
    if is_media_only:
        return HygieneCheckResult(passed=False, reason="media_only")

    raw_text = (text or "").strip()
    if not raw_text:
        return HygieneCheckResult(passed=False, reason="media_only")

    if _MEDIA_MARKER_REGEX.match(raw_text):
        return HygieneCheckResult(passed=False, reason="media_marker")

    if "[REDACTED]" in raw_text:
        return HygieneCheckResult(passed=False, reason="redacted")

    cap = max_chars or getattr(settings, "STYLE_MAX_SAMPLE_CHARS", 400)
    if len(raw_text) > cap:
        return HygieneCheckResult(passed=False, reason="max_chars")

    # Command check: starts with "/"
    if raw_text.startswith("/"):
        return HygieneCheckResult(passed=False, reason="command")

    # URL-heavy check
    urls = _URL_REGEX.findall(raw_text)
    if len(urls) > 1:
        return HygieneCheckResult(passed=False, reason="url_heavy")
    if urls:
        url_chars = sum(len(u) for u in urls)
        if len(raw_text) > 0 and (url_chars / len(raw_text)) > 0.30:
            return HygieneCheckResult(passed=False, reason="url_heavy")

    # Time window check
    win = window_days or getattr(settings, "STYLE_WINDOW_DAYS", 180)
    if sent_at is not None:
        cutoff = datetime.now() - timedelta(days=win)
        if sent_at < cutoff:
            return HygieneCheckResult(passed=False, reason="outside_window")

    # Normalized text checks (duplicates & bot collisions)
    norm = normalize_sample_text(raw_text)
    if not norm:
        return HygieneCheckResult(passed=False, reason="empty_normalized")

    if seen_normalized is not None and norm in seen_normalized:
        return HygieneCheckResult(passed=False, reason="duplicate")

    if bot_sent_normalized is not None and norm in bot_sent_normalized:
        return HygieneCheckResult(passed=False, reason="bot_collision")

    return HygieneCheckResult(passed=True, reason="ok")


def detect_language_script(text: str) -> str:
    """
    Detects language and script of text: 'uz-lat', 'uz-cyr', 'ru', 'en', or 'other'.
    """
    if not text:
        return "other"

    cyrillic_chars = len(re.findall(r"[\u0400-\u04FF]", text))
    latin_chars = len(re.findall(r"[a-zA-Z]", text))

    total_letters = cyrillic_chars + latin_chars
    if total_letters < 3:
        return "other"

    words = [w.strip("'") for w in re.findall(r"[\w']+", text.lower())]
    if not words:
        return "other"

    if cyrillic_chars > latin_chars:
        # Cyrillic script: uz-cyr vs ru
        if any(c in _UZ_CYR_LETTERS for c in text):
            return "uz-cyr"
        if any(c in _RU_SPECIFIC_LETTERS for c in text):
            return "ru"

        uz_cyr_hits = sum(1 for w in words if w in _UZ_CYR_WORDS)
        ru_hits = sum(1 for w in words if w in _RU_WORDS)
        if uz_cyr_hits > ru_hits:
            return "uz-cyr"
        if ru_hits > uz_cyr_hits:
            return "ru"
        return "uz-cyr"

    else:
        # Latin script: uz-lat vs en vs ru
        uz_hits = sum(1 for w in words if w in _UZ_WORDS)
        en_hits = sum(1 for w in words if w in _EN_WORDS)

        # Look for apostrophes specific to Uzbek Latin (o', g')
        if any(w in text.lower() for w in ["o'", "g'", "o‘", "g‘", "o`", "g`"]):
            return "uz-lat"

        if en_hits > uz_hits:
            return "en"
        return "uz-lat"


def detect_register(text: str) -> Optional[str]:
    """
    Detects addressing register: 'siz', 'sen', or None.
    """
    if not text:
        return None

    lower_text = text.lower()
    siz_score = sum(len(re.findall(p, lower_text)) for p in _SIZ_PATTERNS)
    sen_score = sum(len(re.findall(p, lower_text)) for p in _SEN_PATTERNS)

    if siz_score > sen_score:
        return "siz"
    elif sen_score > siz_score:
        return "sen"
    return None
