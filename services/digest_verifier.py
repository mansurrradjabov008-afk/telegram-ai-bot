"""
Smart Digest V2 - Phase 4: Anti-Hallucination Code Verifier.
Implements Rule 3 of Anti-Hallucination Guidelines:
- Verification is done strictly by CODE, not by the AI.
- Rule 3a: Every referenced post_id exists in the input batch.
- Rule 3b: Every number (amounts, percentages, years, dates, times) appears in at least one cited post after normalization.
- Rule 3c: Every URL in output appears in cited posts or is generated link; strips all other URLs.
- Rule 3d: Phone numbers are stripped (the source link is enough).
- Rule 3e: Person and organization names appear in cited posts (case-insensitive with Latin/Cyrillic transliteration equivalence).
- Discards on failure without AI fallback fixing.
"""

import re
import logging
from typing import List, Set, Tuple, Optional, Dict
from database.models import DigestPost
from services.digest_cleaner import DigestCleaner

logger = logging.getLogger(__name__)

# Stopwords to ignore when extracting capitalized candidate entities at start of sentences
CAPITAL_STOPWORDS = {
    "bu", "shu", "ushbu", "yangi", "bugun", "ertaga", "hozir", "hozirda",
    "shuningdek", "lekin", "ammo", "biroq", "chunki", "agar", "barcha",
    "har", "bir", "ikki", "uch", "toshkent", "ozbekiston", "uzbekiston",
    "mamlakat", "davlat", "prezident", "hukumat", "vazirlik", "qaror",
    "qonun", "talabalar", "universitet", "institut", "xabar", "malumot",
    "diqqat", "muhim", "e'lon", "elon", "manba", "rasmiy", "the", "in",
    "on", "at", "by", "for", "with", "from", "and", "or", "to", "of",
    "a", "an", "is", "are", "was", "were", "this", "that", "these", "those"
}


class NumberNormalizer:
    """
    Extracts and normalizes numbers, currency amounts, percentages, years, and times.
    Enforces conservative equivalence.
    """

    # Thousand separators pattern: e.g. "300 000", "300,000", "300.000"
    THOUSAND_SEP_RE = re.compile(r'\b\d{1,3}(?:[ ,.]\d{3})+\b')

    # Multiplier pattern: e.g. "300k", "300 ming", "300 тыс", "5 mln", "1.5 million"
    MULTIPLIER_RE = re.compile(
        r'(\d+(?:[.,]\d+)?)\s*(k|ming|тыс|тысяч|млн|mln|million|миллион|млрд|mlrd|milliard|миллиард|m|b)\b',
        re.IGNORECASE
    )

    # General numbers (including decimals)
    PLAIN_NUMBER_RE = re.compile(r'\b\d+(?:[.,]\d+)?\b')

    # Time pattern: e.g. "10:00", "18:30", "09.45"
    TIME_RE = re.compile(r'\b\d{1,2}[:.]\d{2}\b')

    # Multiplier factors
    FACTORS = {
        "k": 1000,
        "ming": 1000,
        "тыс": 1000,
        "тысяч": 1000,
        "m": 1_000_000,
        "mln": 1_000_000,
        "million": 1_000_000,
        "млн": 1_000_000,
        "миллион": 1_000_000,
        "b": 1_000_000_000,
        "mlrd": 1_000_000_000,
        "milliard": 1_000_000_000,
        "млрд": 1_000_000_000,
        "миллиард": 1_000_000_000,
    }

    @classmethod
    def extract_numbers(cls, text: str) -> Set[str]:
        """
        Extracts all numeric representations from text normalized to string form.
        Includes both raw strings and calculated integer/float values.
        """
        if not text:
            return set()

        results: Set[str] = set()

        # 1. Times (from original text before collapsing)
        for match in cls.TIME_RE.finditer(text):
            results.add(match.group(0).replace(".", ":"))

        # 2. Pre-collapse thousand-separated numbers so PLAIN_NUMBER_RE doesn't split them
        # e.g. "300 000" -> "300000", "300,000" -> "300000", "300.000" -> "300000"
        collapsed_text = cls.THOUSAND_SEP_RE.sub(
            lambda m: m.group(0).replace(" ", "").replace(",", "").replace(".", ""),
            text
        )

        # 3. Multipliers (e.g. "300 ming", "300k", "5 mln")
        for match in cls.MULTIPLIER_RE.finditer(collapsed_text):
            num_str = match.group(1).replace(",", ".")
            suffix = match.group(2).lower()
            try:
                base_num = float(num_str)
                mult = cls.FACTORS.get(suffix, 1)
                val = int(base_num * mult) if (base_num * mult).is_integer() else base_num * mult
                results.add(str(val))
                # Also add base number
                int_base = int(base_num) if base_num.is_integer() else base_num
                results.add(str(int_base))
            except Exception:
                pass

        # 4. Plain digits and decimals from collapsed text
        for match in cls.PLAIN_NUMBER_RE.finditer(collapsed_text):
            raw = match.group(0).replace(",", ".")
            try:
                val = float(raw)
                val_str = str(int(val)) if val.is_integer() else str(val)
                results.add(val_str)
            except Exception:
                pass

        return results


class DigestVerifier:
    """
    Enforces Anti-Hallucination Rule 3 via code verification.
    """

    # URL regex
    URL_RE = re.compile(r'https?://[^\s<>"]+|t\.me/[^\s<>"]+', re.IGNORECASE)

    # Phone number regex (Uzbek and international)
    PHONE_RE = re.compile(
        r'(?:\+?998[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}\b'
        r'|\b\+?\d{1,4}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3}[\s.-]?\d{3,4}\b'
    )

    # Capitalized word/entity pattern
    PROPER_ENTITY_RE = re.compile(
        r'\b[A-ZА-ЯЁO‘O`G‘G`][a-zа-яёo‘o`g‘g`\']+(?:\s+[A-ZА-ЯЁO‘O`G‘G`][a-zа-яёo‘o`g‘g`\']+)*\b'
        r'|\b[A-ZА-ЯЁ]{2,}\b'
    )

    def __init__(self):
        self.cleaner = DigestCleaner()

    def strip_unapproved_urls(
        self,
        summary_text: str,
        approved_urls: Set[str],
    ) -> str:
        """
        Rule 3c: Every URL in output must appear in cited source post or be generated link.
        Strips any unapproved URL.
        """
        def replace_url(match):
            url = match.group(0).strip(".,!?;:()")
            for app in approved_urls:
                if url in app or app in url:
                    return match.group(0)
            logger.info(f"Stripping unapproved URL from summary: {url}")
            return ""

        cleaned = self.URL_RE.sub(replace_url, summary_text)
        # Normalize double spaces created by removal
        return re.sub(r'\s+', ' ', cleaned).strip()

    def strip_phone_numbers(self, summary_text: str) -> str:
        """
        Rule 3d: Phone numbers are never included in digest text (source link is enough).
        """
        cleaned = self.PHONE_RE.sub("", summary_text)
        return re.sub(r'\s+', ' ', cleaned).strip()

    def extract_proper_entities(self, text: str) -> Set[str]:
        """
        Extracts candidate proper nouns (person names, organizations, places).
        Filters out common sentence-starting words.
        """
        entities: Set[str] = set()
        matches = self.PROPER_ENTITY_RE.findall(text)
        for m in matches:
            clean_m = m.strip().strip(".,!?;:\"'()[]")
            clean_m_lower = clean_m.lower()
            if len(clean_m) < 3:
                continue
            if clean_m_lower in CAPITAL_STOPWORDS:
                continue
            entities.add(clean_m)
        return entities

    def verify_numbers(
        self,
        summary_text: str,
        source_posts: List[DigestPost],
    ) -> Tuple[bool, Optional[str]]:
        """
        Rule 3b: Every number in summary must appear in at least one cited source post.
        """
        summary_numbers = NumberNormalizer.extract_numbers(summary_text)
        if not summary_numbers:
            return (True, None)

        combined_source_text = " ".join([p.text or "" for p in source_posts])
        source_numbers = NumberNormalizer.extract_numbers(combined_source_text)

        # Also add numbers from post dates (year, month, day)
        for p in source_posts:
            if p.posted_at:
                source_numbers.update(NumberNormalizer.extract_numbers(p.posted_at))

        for num in summary_numbers:
            if num not in source_numbers:
                return (False, f"unverified_number: {num}")

        return (True, None)

    def verify_proper_entities(
        self,
        summary_text: str,
        source_posts: List[DigestPost],
    ) -> Tuple[bool, Optional[str]]:
        """
        Rule 3e: Person and organization names must appear in cited source posts
        (case-insensitive, with Latin/Cyrillic transliteration equivalence).
        """
        candidate_entities = self.extract_proper_entities(summary_text)
        if not candidate_entities:
            return (True, None)

        # Build transliterated source corpus (both Latin and Cyrillic forms)
        raw_source = " ".join([
            f"{p.text or ''} {p.chat_title or ''} {p.original_source or ''}"
            for p in source_posts
        ])
        latin_source = self.cleaner.transliterate_uzbek_cyrillic_to_latin(raw_source).lower()

        def word_in_source(w: str) -> bool:
            if w in latin_source:
                return True
            # Transliteration equivalence for "yoy" / "yo" (e.g. Mirziyoyev vs Mirziyoev)
            if w.replace("yoy", "yo") in latin_source or w.replace("yo", "yoy") in latin_source:
                return True
            # Apostrophe variants: o' vs oʻ vs о'
            clean_w = w.replace("'", "").replace("`", "").replace("‘", "").replace("’", "")
            clean_src = latin_source.replace("'", "").replace("`", "").replace("‘", "").replace("’", "")
            if clean_w in clean_src:
                return True
            return False

        for entity in candidate_entities:
            latin_entity = self.cleaner.transliterate_uzbek_cyrillic_to_latin(entity).lower()
            words = [w for w in latin_entity.split() if len(w) >= 3 and w not in CAPITAL_STOPWORDS]
            if not words:
                continue

            entity_found = (latin_entity in latin_source) or all(word_in_source(w) for w in words)

            if not entity_found:
                logger.warning(f"Unverified entity '{entity}' not found in cited sources.")
                return (False, f"unverified_entity: {entity}")

        return (True, None)

    def verify_item(
        self,
        summary_text: str,
        source_posts: List[DigestPost],
        post_ids_used: List[int],
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Full Rule 3 Verification:
        - 3a: Post IDs exist
        - 3c: Strip unapproved URLs
        - 3d: Strip phone numbers
        - 3b: Verify numbers
        - 3e: Verify proper entities
        Returns (is_valid, cleaned_summary, discard_reason).
        """
        # Rule 3a: Verify every post_id exists
        source_id_map = {p.id: p for p in source_posts if p.id is not None}
        for pid in post_ids_used:
            if pid not in source_id_map:
                return (False, summary_text, f"referenced_post_id_missing: {pid}")

        cited_posts = [source_id_map[pid] for pid in post_ids_used if pid in source_id_map]
        if not cited_posts:
            cited_posts = source_posts

        # Approved URLs from sources
        approved_urls: Set[str] = set()
        for p in cited_posts:
            if p.link:
                approved_urls.add(p.link)
            if p.text:
                for match in self.URL_RE.finditer(p.text):
                    approved_urls.add(match.group(0))

        # Rule 3c: Strip unapproved URLs
        cleaned = self.strip_unapproved_urls(summary_text, approved_urls)

        # Rule 3d: Strip phone numbers
        cleaned = self.strip_phone_numbers(cleaned)

        # Rule 3b: Verify numbers
        num_ok, num_err = self.verify_numbers(cleaned, cited_posts)
        if not num_ok:
            return (False, cleaned, num_err)

        # Rule 3e: Verify entities
        ent_ok, ent_err = self.verify_proper_entities(cleaned, cited_posts)
        if not ent_ok:
            return (False, cleaned, ent_err)

        return (True, cleaned, None)
