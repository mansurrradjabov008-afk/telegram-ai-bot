"""
Writing Style Guide Renderer (D1, D5, D6, D7, Phase 4).
Renders short, structured style guidelines from pure code statistics for system prompt injection.
"""

import re
from typing import Optional, Dict, Any, List
from config import settings
from services.sensitive_guard import check_sensitive_keywords

_URL_REGEX = re.compile(r"https?://[^\s]+|t\.me/[^\s]+", re.IGNORECASE)

# Additional conservative local keywords for D5 few-shot protection
_SENSITIVE_FEW_SHOT_WORDS = {
    # Money / numbers / cards
    "pul", "so'm", "som", "dollar", "dollor", "usd", "rubl", "karta", "hisob",
    # Promises / commitments
    "va'da", "vada", "so'z beraman", "aniq bitadi", "kafolat",
    # Meeting / time / scheduling
    "uchrashuv", "ko'rishamiz", "korishamiz", "soat", "ertalab", "kechqurun", "kelaman", "boraman",
}


def filter_few_shot_examples(
    candidates: List[str],
    max_examples: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> List[str]:
    """
    Filters candidate few-shot messages according to Rule D5:
    - Same thread, owner-written.
    - Length <= STYLE_EXAMPLE_MAX_CHARS (default 120).
    - No digits or URLs.
    - Passes sensitive keyword guard (money, promises, meeting, personal data).
    - Up to STYLE_MAX_EXAMPLES (default 3).
    """
    cap_count = max_examples or getattr(settings, "STYLE_MAX_EXAMPLES", 3)
    cap_chars = max_chars or getattr(settings, "STYLE_EXAMPLE_MAX_CHARS", 120)

    clean_examples: List[str] = []

    for text in candidates:
        raw = (text or "").strip()
        if not raw or len(raw) > cap_chars:
            continue

        # No digits
        if any(c.isdigit() for c in raw):
            continue

        # No URLs or @ handles
        if _URL_REGEX.search(raw) or "@" in raw:
            continue

        # Sensitive guard pure keyword check
        reasons = check_sensitive_keywords(raw)
        if reasons:
            continue

        # Conservative local keywords check
        lower_raw = raw.lower()
        if any(w in lower_raw for w in _SENSITIVE_FEW_SHOT_WORDS):
            continue

        clean_examples.append(raw)
        if len(clean_examples) >= cap_count:
            break

    return clean_examples


def render_style_guide(
    profile: Optional[Dict[str, Any]] = None,
    examples: Optional[List[str]] = None,
    owner_note: Optional[str] = None,
    target_lang: str = "uz-lat",
    target_register: Optional[str] = None,
) -> str:
    """
    Renders concise style instructions (< STYLE_GUIDE_MAX_TOKENS ~250 tokens) for LLM.
    """
    lines: List[str] = ["✍️ XO'JAYINNING YOZISH USLUBI VA USLUB KO'RSATMASI (WRITING STYLE):"]

    stats = profile.get("stats", {}) if profile else {}
    phrases = profile.get("phrases", []) if profile else []

    # 1. Length & Hard Cap
    p90_chars = stats.get("p90_chars", 80.0)
    min_hard_cap = getattr(settings, "STYLE_HARD_MAX_CHARS_MIN", 300)
    hard_cap = max(min_hard_cap, int(3 * p90_chars))
    avg_words = stats.get("avg_words", 7.0)
    avg_chars = stats.get("avg_chars", 40.0)

    lines.append(
        f"• Uzunlik: o'rtacha {int(avg_words)} so'z ({int(avg_chars)} belgi). Qat'iy chegara: eng ko'pi bilan {hard_cap} belgi."
    )

    # 2. Casing & Punctuation
    lowercase_rate = stats.get("all_lowercase_rate", 0.0)
    if lowercase_rate > 0.60:
        casing_rule = "Barcha so'zlarni kichik harflar bilan yozing (kichik harflar uslubi)."
    else:
        casing_rule = "Standart bosh harf bilan yozing."

    single_excl = stats.get("single_excl_rate", 0.0)
    multi_excl = stats.get("multi_excl_rate", 0.0)
    if (single_excl + multi_excl) < 0.10:
        punct_rule = "Undov (!) belgilarini ishlatmang."
    elif multi_excl > 0.20:
        punct_rule = "Hissiyotli undov (!) yoki (!!) ishlatilishi mumkin."
    else:
        punct_rule = "Me'yorda undov (!) ishlatilishi mumkin."

    lines.append(f"• Harflar va tinish belgilari: {casing_rule} {punct_rule}")

    # 3. Emoji Rule
    avg_emojis = stats.get("avg_emojis", 0.0)
    top_emojis = stats.get("top_emojis", [])
    if avg_emojis < 0.20 or not top_emojis:
        lines.append("• Emoji (smaylik): umuman ishlatmang.")
    else:
        em_list = [e[0] for e in top_emojis[:3]]
        em_str = ", ".join(em_list)
        lines.append(f"• Emoji (smaylik): har xabarda ko'pi bilan 1 ta, xabar oxirida ({em_str}).")

    # 4. Habitual Phrases
    if phrases:
        quoted = [f'"{p}"' for p in phrases[:6]]
        lines.append(f"• Odatiy iboralar (tabiiy o'rinda ishlating, majburlamang): {', '.join(quoted)}.")

    # 5. Few-Shot Examples (D5)
    if examples:
        lines.append(
            "• Uslub namunalari (DIQQAT: namunalar faqat yozish USLUBINI ko'rsatadi, ulardagi mavzu yoki ma'lumotlarni ASLO ko'chirmang):"
        )
        for ex in examples[:3]:
            lines.append(f'  - "{ex}"')

    # 6. Natural Language and Human Flow (D6)
    lines.append("• Muloqot tili: suhbatdosh yozgan tilda javob bering. Suhbatni xuddi tirik insondek mutlaqo tabiiy, jonli va boy so'z boyligi bilan olib boring, sun'iy bot kabi gapirmang.")

    # 7. Owner Note below safety boundary (D7)
    if owner_note and owner_note.strip():
        clean_note = owner_note.strip()[:200]
        lines.append(
            "🛡️ XAVFSIZLIK CHEGARASI: Tizim xavfsizlik qoidalari har qanday uslubiy ko'rsatmadan 100% ustun turadi.\n"
            f'Xo\'jayinning uslubiy eslatmasi (xavfsizlik qoidalariga bo\'ysunadi): "{clean_note}"'
        )

    guide_text = "\n".join(lines)
    return guide_text


def validate_owner_note(note_text: Optional[str]) -> Optional[str]:
    """
    Validates and sanitizes owner style notes according to Rule D7:
    - Length-capped at 200 chars.
    - Rejects prompt injections / jailbreak instructions.
    - Rejects commitments, promises, financial or sensitive topics.
    Returns cleaned note string, or None if rejected.
    """
    if not note_text:
        return None
    raw = note_text.strip()
    if not raw:
        return None
    if len(raw) > 200:
        raw = raw[:200]

    # 1. Injection check
    from services.memory_injection import contains_injection_patterns, is_forbidden_topic
    is_inj, _ = contains_injection_patterns(raw)
    if is_inj:
        return None

    # 2. Forbidden topic check
    is_forb, _ = is_forbidden_topic(raw, "identity")
    if is_forb:
        return None

    # 3. Commitments / sensitive guard keywords
    sens = check_sensitive_keywords(raw)
    if sens:
        return None

    return raw
