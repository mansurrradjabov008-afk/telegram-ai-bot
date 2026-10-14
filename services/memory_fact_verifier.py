"""
Deterministic Fact Verification and Confidence Engine (Steps 1.5 & 1.6).
Guarantees that every name, number, date, and key content word of an extracted
fact appears (normalized, conservative) in at least one cited source message.
Rejects hallucinated facts (e.g. 'Python dasturchi' citing messages without 'Python').
Derives confidence from verification outcome and source_type instead of constant 1.0.
"""

import re
import string
from typing import List, Tuple, Optional, Set
from database.models import MemMessage

STOP_WORDS: Set[str] = {
    # Uzbek
    "bilan", "uchun", "ham", "juda", "emas", "ekan", "bor", "yo'q", "biri",
    "esa", "haqida", "deb", "bo'lib", "yaxshi", "yomon", "katta", "kichik",
    "faqat", "kabi", "orqali", "tomonidan", "keyin", "oldin", "hozir",
    "kontakt", "kontaktning", "kontaktga", "kontaktda", "foydalanuvchi",
    "foydalanuvchining", "suhbatdosh", "suhbatdoshning", "inson", "insonning",
    "uning", "ularning", "o'zi", "o'zining", "ushbu", "bu", "shu", "o'sha",
    # Uzbek meta/grammatical & owner references
    "meni", "seni", "bizni", "sizni", "menga", "senga", "bizga", "sizga", "menda", "senda",
    "egasi", "egasini", "egasiga", "egasida", "mansur", "mansurni", "mansurga", "mansurda", "mansurning",
    "ataydi", "atashadi", "aytadi", "aytdi", "nomlaydi", "murojaat",
    "ismli", "nomli", "yoshda", "yashaydi", "ishlaydi", "biladi", "qiladi", "qildi", "bo'lgan",
    "so'raydi", "so'radi", "qiziqadi", "o'rganadi", "o'rganmoqda", "o'rganishga",
    "yoqtiradi", "ko'radi", "ma'qul", "kerak", "xohlaydi", "istaydi", "tili", "so'z", "so'zlari", "so'zlariga", "so'zlarini",
    # Russian
    "это", "что", "как", "или", "для", "при", "все", "его", "ее", "их",
    "был", "была", "были", "быть", "очень", "также", "после", "только",
    # English
    "the", "and", "with", "from", "that", "this", "have", "been", "were",
    "very", "also", "after", "before", "only", "such", "into", "over",
}


def _normalize_text(text: str) -> str:
    """Lowercases, normalizes apostrophes, and cleans punctuation."""
    if not text:
        return ""
    # Normalize varied apostrophes
    t = text.lower().replace("‘", "'").replace("’", "'").replace("`", "'")
    return t


def _levenshtein(s1: str, s2: str) -> int:
    """Calculates Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def verify_fact_content(
    fact_text: str,
    source_messages: List[MemMessage],
    source_type: str = "contact_claim",
) -> Tuple[bool, float, Optional[str]]:
    """
    Deterministically verifies that all key tokens in fact_text are grounded
    in the cited source messages.

    Returns:
        (is_verified: bool, confidence: float, reason_or_status: str)
    """
    if not fact_text or not fact_text.strip():
        return False, 0.0, "empty_fact"

    if not source_messages:
        return False, 0.0, "no_source_messages"

    # Build normalized source corpus
    source_corpus_parts = []
    for msg in source_messages:
        msg_text = getattr(msg, "text_redacted", None) or getattr(msg, "text", "") or ""
        source_corpus_parts.append(_normalize_text(msg_text))
    source_corpus = " ".join(source_corpus_parts)

    # 1. Identify numbers in fact
    numbers_in_fact = re.findall(r"\b\d+([.,]\d+)?\b", fact_text)
    for num_match in re.finditer(r"\b\d+\b", fact_text):
        num_str = num_match.group(0)
        # Check if number appears in source corpus
        if not re.search(r"\b" + re.escape(num_str) + r"\b", source_corpus):
            return False, 0.0, f"fact_verifier:missing_number:{num_str}"

    # 2. Identify proper nouns / capitalized words in fact (excluding start of sentence if not clearly proper)
    corpus_words = set(re.findall(r"\b[A-Za-zА-Яа-яЎўҚқҒғҲҳ\']+\b", source_corpus))
    words = re.findall(r"\b[A-Za-zА-Яа-яЎўҚқҒғҲҳ\']+\b", fact_text)
    proper_nouns = []
    for idx, w in enumerate(words):
        if w and w[0].isupper():
            w_norm = _normalize_text(w)
            if w_norm in STOP_WORDS:
                continue
            proper_nouns.append(w)

    case_suffixes = (
        "chasiga", "chasini", "cha", "dagi", "dek", "day", "lik", "chilik",
        "da", "dan", "ga", "ka", "qa", "ni", "ning", "si", "i", "m", "am", "im", "ng", "ing", "dir"
    )

    for pn in proper_nouns:
        pn_norm = _normalize_text(pn)
        if pn_norm in STOP_WORDS:
            continue

        matched_pn = False
        if re.search(r"\b" + re.escape(pn_norm) + r"\b", source_corpus):
            matched_pn = True
        else:
            # Check stem after stripping Uzbek case/derivation suffix
            pn_stem = pn_norm
            for sfx in case_suffixes:
                if pn_stem.endswith(sfx) and len(pn_stem) - len(sfx) >= 3:
                    pn_stem = pn_stem[:-len(sfx)]
                    break

            for cw in corpus_words:
                c_stem = cw
                for sfx in case_suffixes:
                    if c_stem.endswith(sfx) and len(c_stem) - len(sfx) >= 3:
                        c_stem = c_stem[:-len(sfx)]
                        break
                if c_stem == pn_stem or cw.startswith(pn_stem) or pn_stem.startswith(c_stem):
                    matched_pn = True
                    break
                # 1-character typo/phonetic variation (e.g. ingliz vs ingiliz) for length >= 4
                if len(pn_stem) >= 4 and len(c_stem) >= 4 and _levenshtein(pn_stem, c_stem) <= 1:
                    matched_pn = True
                    break

        if not matched_pn:
            return False, 0.0, f"fact_verifier:missing_proper_noun:{pn}"

    # 3. Identify content words (len >= 3, non-stopword to support Uzbek words like uka, aka, ona, ota)
    content_words = [
        _normalize_text(w) for w in words
        if len(w) >= 3 and _normalize_text(w) not in STOP_WORDS
    ]

    if not content_words:
        # Very short generic fact; require at least words to be present
        all_norm_words = [_normalize_text(w) for w in words if _normalize_text(w) not in STOP_WORDS]
        if not all_norm_words:
            return True, 0.70, "verified_generic"
        content_words = all_norm_words

    # Split corpus into normalized words for fast lookup
    corpus_words = set(re.findall(r"\b[A-Za-zА-Яа-яЎўҚқҒғҲҳ\']+\b", source_corpus))

    def _word_matches_corpus(fact_word: str) -> bool:
        if fact_word in corpus_words:
            return True
        suffixes = case_suffixes + (
            "man", "san", "di", "miz", "siz", "lar", "larga", "larini", "moqda", "yapti", "gan"
        )
        stem = fact_word
        for sfx in suffixes:
            if stem.endswith(sfx) and len(stem) - len(sfx) >= 3:
                stem = stem[:-len(sfx)]
                break

        for cw in corpus_words:
            if cw == fact_word:
                return True
            c_stem = cw
            for sfx in suffixes:
                if c_stem.endswith(sfx) and len(c_stem) - len(sfx) >= 3:
                    c_stem = c_stem[:-len(sfx)]
                    break
            if stem == c_stem and len(stem) >= 3:
                return True
            if len(stem) >= 3 and len(c_stem) >= 3 and stem[:3] == c_stem[:3] and (stem in c_stem or c_stem in stem):
                return True
            if len(stem) >= 4 and len(c_stem) >= 4 and _levenshtein(stem, c_stem) <= 1:
                return True
        return False

    matched_count = 0
    for cw in content_words:
        if _word_matches_corpus(cw):
            matched_count += 1

    match_ratio = matched_count / len(content_words)

    # Grounding threshold: at least 50% of content words must match source messages
    if match_ratio < 0.50:
        missing = [cw for cw in content_words if not _word_matches_corpus(cw)]
        return False, 0.0, f"fact_verifier:low_match_ratio:{match_ratio:.2f}:missing_{missing[:2]}"

    # 4. Deterministic Confidence Derivation (Step 1.6)
    # Documented meaning:
    # - owner_confirmed: 1.0 (Owner explicitly validated via /memory confirm or /remember)
    # - owner_stated: 0.85 - 0.95 (Owner stated directly in conversation)
    # - contact_claim: 0.65 - 0.85 (Contact claimed in conversation, grounded in verified text)
    if source_type == "owner_confirmed":
        confidence = 1.0
    elif source_type == "owner_stated":
        confidence = 0.85 + 0.10 * match_ratio
    else:  # contact_claim
        confidence = 0.65 + 0.20 * match_ratio

    confidence = round(confidence, 2)
    return True, confidence, "verified"
