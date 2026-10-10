"""
Code Statistics & Profile Engine for Writing Style (D1, D3, D4, Phase 3).
Pure code calculation of owner writing characteristics without any AI calls.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
import json
import math
import re
from typing import List, Dict, Any, Optional, Set, Tuple
from config import settings
from services.style_sampler import normalize_sample_text, detect_register

# Regex for emojis (Unicode emoji range + common symbols)
_EMOJI_REGEX = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]",
    re.UNICODE,
)
_URL_REGEX = re.compile(r"https?://[^\s]+|t\.me/[^\s]+", re.IGNORECASE)


def _percentile(data: List[float], p: float) -> float:
    """Computes p-th percentile (0 <= p <= 100) using nearest-rank / linear interpolation."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    d0 = s[int(f)] * (c - k)
    d1 = s[int(c)] * (k - f)
    return float(d0 + d1)


@dataclass
class StyleStatistics:
    total_samples: int
    avg_chars: float
    p50_chars: float
    p90_chars: float
    min_chars: int
    max_chars: int
    avg_words: float
    p50_words: float
    p90_words: float
    min_words: int
    max_words: int
    avg_sentences: float
    p50_sentences: float
    single_excl_rate: float
    multi_excl_rate: float
    ellipsis_rate: float
    question_rate: float
    avg_emojis: float
    p90_emojis: float
    top_emojis: List[Tuple[str, int]]
    emoji_at_end_rate: float
    all_lowercase_rate: float
    standard_sentence_case_rate: float
    siz_ratio: float
    sen_ratio: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_style_statistics(samples: List[Dict[str, Any]]) -> StyleStatistics:
    """
    Computes statistical profile of text length, punctuation, emojis, capitalization, and register.
    """
    n = len(samples)
    if n == 0:
        return StyleStatistics(
            total_samples=0,
            avg_chars=0.0, p50_chars=0.0, p90_chars=0.0, min_chars=0, max_chars=0,
            avg_words=0.0, p50_words=0.0, p90_words=0.0, min_words=0, max_words=0,
            avg_sentences=0.0, p50_sentences=0.0,
            single_excl_rate=0.0, multi_excl_rate=0.0, ellipsis_rate=0.0, question_rate=0.0,
            avg_emojis=0.0, p90_emojis=0.0, top_emojis=[], emoji_at_end_rate=0.0,
            all_lowercase_rate=0.0, standard_sentence_case_rate=0.0,
            siz_ratio=0.0, sen_ratio=0.0,
        )

    char_lens: List[float] = []
    word_lens: List[float] = []
    sentence_counts: List[float] = []
    emoji_counts: List[float] = []

    single_excl_count = 0
    multi_excl_count = 0
    ellipsis_count = 0
    question_count = 0

    all_emojis: Counter = Counter()
    emoji_at_end_count = 0
    all_lowercase_count = 0
    standard_sentence_case_count = 0
    siz_count = 0
    sen_count = 0

    for s in samples:
        text = s.get("text_redacted", "")
        c_len = len(text)
        words = text.split()
        w_len = len(words)

        char_lens.append(float(c_len))
        word_lens.append(float(w_len))

        # Sentence count
        raw_sentences = [p for p in re.split(r"[.!?]+", text) if p.strip()]
        sentence_counts.append(float(max(1, len(raw_sentences))))

        # Punctuation profile
        has_multi_excl = bool(re.search(r"!{2,}", text))
        has_single_excl = bool("!" in text) and not has_multi_excl
        if has_multi_excl:
            multi_excl_count += 1
        elif has_single_excl:
            single_excl_count += 1

        if "..." in text or "…" in text:
            ellipsis_count += 1
        if "?" in text:
            question_count += 1

        # Emoji profile
        emojis = _EMOJI_REGEX.findall(text)
        emoji_counts.append(float(len(emojis)))
        for em in emojis:
            all_emojis[em] += 1

        if emojis:
            # Check if emoji is near end (in trailing 30% of characters)
            trailing_slice = text[int(len(text) * 0.70):]
            if any(em in trailing_slice for em in emojis):
                emoji_at_end_count += 1

        # Capitalization
        has_alpha = bool(re.search(r"[a-zA-Zа-яА-ЯўқғҳЎҚҒҲ]", text))
        if has_alpha:
            if text == text.lower():
                all_lowercase_count += 1
            elif text[0].isupper() and not text.isupper():
                standard_sentence_case_count += 1

        # Formality / register
        reg = s.get("register") or detect_register(text)
        if reg == "siz":
            siz_count += 1
        elif reg == "sen":
            sen_count += 1

    return StyleStatistics(
        total_samples=n,
        avg_chars=round(sum(char_lens) / n, 2),
        p50_chars=round(_percentile(char_lens, 50), 2),
        p90_chars=round(_percentile(char_lens, 90), 2),
        min_chars=int(min(char_lens)),
        max_chars=int(max(char_lens)),
        avg_words=round(sum(word_lens) / n, 2),
        p50_words=round(_percentile(word_lens, 50), 2),
        p90_words=round(_percentile(word_lens, 90), 2),
        min_words=int(min(word_lens)),
        max_words=int(max(word_lens)),
        avg_sentences=round(sum(sentence_counts) / n, 2),
        p50_sentences=round(_percentile(sentence_counts, 50), 2),
        single_excl_rate=round(single_excl_count / n, 2),
        multi_excl_rate=round(multi_excl_count / n, 2),
        ellipsis_rate=round(ellipsis_count / n, 2),
        question_rate=round(question_count / n, 2),
        avg_emojis=round(sum(emoji_counts) / n, 2),
        p90_emojis=round(_percentile(emoji_counts, 90), 2),
        top_emojis=all_emojis.most_common(5),
        emoji_at_end_rate=round(emoji_at_end_count / max(1, sum(1 for c in emoji_counts if c > 0)), 2),
        all_lowercase_rate=round(all_lowercase_count / n, 2),
        standard_sentence_case_rate=round(standard_sentence_case_count / n, 2),
        siz_ratio=round(siz_count / n, 2),
        sen_ratio=round(sen_count / n, 2),
    )


def extract_candidate_phrases(text: str) -> List[str]:
    """
    Extracts candidate openers (first 1-3 words) and closers (last 1-3 words) after stripping punctuation.
    """
    if not text:
        return []

    # Clean punctuation and normalize apostrophes
    clean = re.sub(r"[^\w\s']", " ", text)
    words = clean.split()
    if not words:
        return []

    candidates: List[str] = []

    # Opener phrases (1, 2, 3 words)
    if len(words) >= 2:
        candidates.append(normalize_sample_text(" ".join(words[:2])))
    candidates.append(normalize_sample_text(words[0]))
    if len(words) >= 3:
        candidates.append(normalize_sample_text(" ".join(words[:3])))

    # Closer phrases (last 1-2 words if different)
    if len(words) >= 2:
        candidates.append(normalize_sample_text(words[-1]))
        if len(words) >= 3:
            candidates.append(normalize_sample_text(" ".join(words[-2:])))

    # Deduplicate while preserving order
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)

    return result


def filter_and_rank_phrases(
    candidate_counts: Counter,
    contact_spread: Dict[str, Set[int]],
    forbidden_entities: Set[str],
    min_count: int,
    min_contacts: int,
    eligible_contacts_count: int,
    min_eligible_contacts: int,
    max_phrases: Optional[int] = None,
) -> List[str]:
    """
    Filters candidate phrases according to Rule D4:
    - If eligible_contacts_count < min_eligible_contacts, phrases are disabled.
    - Drops phrases with digits, URLs, @, emails, mid-sentence caps.
    - Drops phrases containing any token in forbidden_entities (contact names/usernames, mem_facts entities).
    - Requires count >= min_count and distinct contacts >= min_contacts.
    """
    if eligible_contacts_count < min_eligible_contacts:
        return []

    cap = max_phrases or getattr(settings, "STYLE_MAX_PHRASES", 8)
    lower_forbidden = {e.lower().strip() for e in forbidden_entities if e}

    qualified: List[Tuple[str, int]] = []

    for phrase, count in candidate_counts.most_common():
        if count < min_count:
            continue
        spread = len(contact_spread.get(phrase, set()))
        if spread < min_contacts:
            continue

        # Reject digits
        if any(c.isdigit() for c in phrase):
            continue

        # Reject URLs, web protocols, domains, @handles, emails
        if (
            _URL_REGEX.search(phrase)
            or any(w in phrase.lower() for w in ["http", "https", "www.", ".com", ".uz", ".ru", ".org", "t.me"])
            or "@" in phrase
        ):
            continue

        # Check tokens against forbidden entities
        tokens = phrase.split()
        if any(t in lower_forbidden for t in tokens):
            continue

        qualified.append((phrase, count))
        if len(qualified) >= cap:
            break

    return [p[0] for p in qualified]


def build_style_profile(
    scope: str,
    lang: str,
    samples: List[Dict[str, Any]],
    forbidden_entities: Set[str],
    min_samples: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Builds structured style profile for scope and language.
    Returns None if sample count is below min_samples threshold.
    """
    req_samples = min_samples or (
        getattr(settings, "STYLE_MIN_CONTACT_SAMPLES", 40)
        if scope.startswith("contact:")
        else getattr(settings, "STYLE_MIN_LANG_SAMPLES", 60)
    )

    if len(samples) < req_samples:
        return None

    stats = compute_style_statistics(samples)

    # Phrase extraction
    candidate_counts: Counter = Counter()
    contact_spread: Dict[str, Set[int]] = defaultdict(set)
    unique_contacts: Set[int] = set()

    for s in samples:
        cid = s.get("contact_user_id") or 0
        unique_contacts.add(cid)
        phrases = extract_candidate_phrases(s.get("text_redacted", ""))
        for p in phrases:
            candidate_counts[p] += 1
            contact_spread[p].add(cid)

    is_contact_scope = scope.startswith("contact:")
    if is_contact_scope:
        # Per-contact scope: single thread
        min_cnt = 3
        min_c = 1
        min_elig = 1
        elig_count = 1
    else:
        min_cnt = getattr(settings, "STYLE_PHRASE_MIN_COUNT", 5)
        min_c = getattr(settings, "STYLE_PHRASE_MIN_CONTACTS", 3)
        min_elig = getattr(settings, "STYLE_PHRASE_MIN_ELIGIBLE_CONTACTS", 5)
        elig_count = len(unique_contacts)

    ranked_phrases = filter_and_rank_phrases(
        candidate_counts=candidate_counts,
        contact_spread=contact_spread,
        forbidden_entities=forbidden_entities,
        min_count=min_cnt,
        min_contacts=min_c,
        eligible_contacts_count=elig_count,
        min_eligible_contacts=min_elig,
    )

    return {
        "scope": scope,
        "lang": lang,
        "stats": stats.to_dict(),
        "phrases": ranked_phrases,
        "sample_count": len(samples),
    }
