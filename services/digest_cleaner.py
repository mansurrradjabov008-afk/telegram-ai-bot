"""
Smart Digest V2 - Cleaning, Normalization, Deduplication and Spam Scoring Module.
Pure code, deterministic, unit-testable without AI dependencies.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Set

from config import settings
from database.models import DigestPost

logger = logging.getLogger(__name__)

# Uzbek Cyrillic to Latin transliteration mapping
CYRILLIC_TO_LATIN_DIGRAPHS = [
    ("ш", "sh"), ("Ш", "Sh"),
    ("ч", "ch"), ("Ч", "Ch"),
    ("ю", "yu"), ("Ю", "Yu"),
    ("я", "ya"), ("Я", "Ya"),
    ("ё", "yo"), ("Ё", "Yo"),
    ("ў", "o'"), ("Ў", "O'"),
    ("ғ", "g'"), ("Ғ", "G'"),
    ("ц", "ts"), ("Ц", "Ts"),
    ("щ", "sh"), ("Щ", "Sh"),
    ("ж", "j"),  ("Ж", "J"),
]

CYRILLIC_TO_LATIN_SINGLE = {
    "а": "a", "А": "A",
    "б": "b", "Б": "B",
    "в": "v", "В": "V",
    "г": "g", "Г": "G",
    "д": "d", "Д": "D",
    "е": "e", "Е": "E",
    "з": "z", "З": "Z",
    "и": "i", "И": "I",
    "й": "y", "Й": "Y",
    "к": "k", "К": "K",
    "л": "l", "Л": "L",
    "м": "m", "М": "M",
    "н": "n", "Н": "N",
    "о": "o", "О": "O",
    "п": "p", "П": "P",
    "р": "r", "Р": "R",
    "с": "s", "С": "S",
    "т": "t", "Т": "T",
    "у": "u", "У": "U",
    "ф": "f", "Ф": "F",
    "х": "x", "Х": "X",
    "ҳ": "h", "Ҳ": "H",
    "қ": "q", "Қ": "Q",
    "ы": "i", "Ы": "I",
    "э": "e", "Э": "E",
    "ь": "'", "ъ": "'",
    "Ъ": "'", "Ь": "'",
}


def transliterate_uzbek_cyrillic_to_latin(text: str) -> str:
    """
    Transliterates Uzbek Cyrillic text to Uzbek Latin script.
    Guarantees cross-script equivalence for hashing and comparison.
    """
    if not text:
        return ""

    result = text
    # Replace multi-character digraphs first
    for cyr, lat in CYRILLIC_TO_LATIN_DIGRAPHS:
        result = result.replace(cyr, lat)

    # Replace single characters
    chars = [CYRILLIC_TO_LATIN_SINGLE.get(c, c) for c in result]
    return "".join(chars)


def normalize_text_for_comparison(text: str) -> str:
    """
    Cleans and normalizes text for exact hash and token-set similarity comparison:
    1. Transliterates Cyrillic -> Latin.
    2. Strips URLs and telegram links.
    3. Standardizes apostrophes to standard single quote.
    4. Removes emojis and special punctuation symbols.
    5. Lowercases and condenses multiple whitespace.
    """
    if not text:
        return ""

    # 1. Transliterate to Latin
    latin_text = transliterate_uzbek_cyrillic_to_latin(text)

    # 2. Strip URLs (http, https, t.me, @usernames)
    cleaned = re.sub(r"https?://\S+|t\.me/\S+", " ", latin_text)
    cleaned = re.sub(r"@\w+", " ", cleaned)

    # 3. Standardize apostrophes
    cleaned = re.sub(r"[`’‘ʻʼ]", "'", cleaned)

    # 4. Remove emojis and symbols, keeping alphanumeric, apostrophes, and basic whitespace
    cleaned = re.sub(r"[^\w\s\']", " ", cleaned)

    # 5. Lowercase and collapse whitespace
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def compute_normalized_hash(text: str) -> str:
    """Computes SHA-256 hash of normalized text for exact deduplication."""
    norm = normalize_text_for_comparison(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def tokenize_text(text: str) -> Set[str]:
    """Extracts informative word tokens (length >= 2) from normalized text."""
    norm = normalize_text_for_comparison(text)
    words = norm.split()
    return {w for w in words if len(w) >= 2}


def calculate_text_similarity(text1: str, text2: str) -> float:
    """
    Calculates token-set similarity between two texts:
    Uses Sorensen-Dice coefficient and token containment.
    Returns score between 0.0 and 1.0.
    """
    tokens1 = tokenize_text(text1)
    tokens2 = tokenize_text(text2)

    if not tokens1 or not tokens2:
        return 0.0

    intersection = tokens1 & tokens2
    if not intersection:
        return 0.0

    # Sorensen-Dice coefficient
    dice = (2.0 * len(intersection)) / (len(tokens1) + len(tokens2))

    # Containment: if one post is an excerpt/subset of another
    min_len = min(len(tokens1), len(tokens2))
    containment = len(intersection) / min_len if min_len > 0 else 0.0

    if min_len >= 12 and containment >= 0.88:
        return max(dice, containment)

    return dice


class DigestCleaner:
    """
    Deterministic cleaning, deduplication, and spam/scam/ad scoring engine.
    Loads keyword lists from configurable data file.
    """

    @staticmethod
    def transliterate_uzbek_cyrillic_to_latin(text: str) -> str:
        return transliterate_uzbek_cyrillic_to_latin(text)

    @staticmethod
    def calculate_token_similarity(text1: str, text2: str) -> float:
        return calculate_text_similarity(text1, text2)

    def __init__(self, rules_file_path: Optional[Path] = None):
        self.rules_path = rules_file_path or (settings.BASE_DIR / settings.SPAM_RULES_FILE)
        self.rules: Dict[str, List[str]] = self._load_rules()

    def _load_rules(self) -> Dict[str, List[str]]:
        """Loads filter rules from JSON file, with safe fallback defaults."""
        if self.rules_path.exists():
            try:
                with open(self.rules_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"Loaded spam filter rules from {self.rules_path}")
                    return data
            except Exception as e:
                logger.error(f"Failed to read spam rules file {self.rules_path}: {e}")

        logger.warning("Using built-in fallback spam filter rules.")
        return {
            "get_rich_quick": ["tez pul topish", "kuniga daromad", "tez boyib ketish", "быстрый заработок", "make money fast"],
            "crypto_investment_doubling": ["pulni 2 barobar", "daromad 100%", "удвоение депозита", "double your money"],
            "giveaways_lotteries": ["yutuqli o'yin", "iphone yutib", "розыгрыш", "giveaway"],
            "loan_offers": ["garovsiz kredit", "tezkor qarz", "микроқарз", "займ онлайн", "payday loan"],
            "pure_goods_services": ["arzon narxlarda sotiladi", "yetkazib berish", "dastavka", "такси", "скидки", "discount"],
            "referral_invite_spam": ["do'stlaringizni taklif qiling", "referal havola", "реферальная ссылка", "referral link"],
            "suspicious_job_markers": ["tajriba talab qilinmaydi, yuqori daromad", "oldindan to'lov", "опыт не требуется, высокий доход"],
            "legitimate_opportunity_markers": ["vakansiya", "ishga taklif", "talablar:", "grant", "stipendiya", "kurs", "deadline"],
        }

    def score_post(self, post: DigestPost) -> Tuple[float, List[str], bool, bool]:
        """
        Calculates spam score and flags suspicious job postings.
        Returns:
            spam_score (float, 0.0 - 1.0)
            spam_reasons (List[str])
            is_spam (bool)
            is_suspicious_job (bool)
        """
        raw_text = post.text or ""
        norm_text = normalize_text_for_comparison(raw_text)
        lower_raw = raw_text.lower()

        spam_score = 0.0
        reasons: List[str] = []
        is_suspicious_job = False

        # 1. Check get-rich-quick patterns
        for kw in self.rules.get("get_rich_quick", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.70
                reasons.append("reklama: tez boyib ketish / shubhali daromad")
                break

        # 2. Check crypto doubling / piramida
        for kw in self.rules.get("crypto_investment_doubling", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.80
                reasons.append("firibgarlik: kripto / 2 barobar daromad va'dasi")
                break

        # 3. Check giveaways / lotteries
        for kw in self.rules.get("giveaways_lotteries", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.70
                reasons.append("reklama: yutuqli o'yin / giveaway / lotereya")
                break

        # 4. Check loan offers
        for kw in self.rules.get("loan_offers", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.70
                reasons.append("reklama: garovsiz kredit / mikroqarz")
                break

        # 5. Check pure goods/services ads (taxi, delivery, discounts)
        for kw in self.rules.get("pure_goods_services", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.65
                reasons.append("reklama: tovar / xizmat / chegirma sotuvi")
                break

        # 6. Check referral / invite spam
        for kw in self.rules.get("referral_invite_spam", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                spam_score += 0.70
                reasons.append("spam: do'stlarni taklif qilish / referal havola")
                break

        # 7. Check high density of phone numbers & prices without opportunity context
        phone_matches = re.findall(r"(?:\+?998|8)[\s\-]?[0-9]{2}[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}", raw_text)
        has_multiple_phones = len(phone_matches) >= 2
        price_matches = re.findall(r"(?i)\b\d+(?:[\s\.,]\d+)?\s*(?:so'm|сум|сўм|uzs|\$|usd|dollar)\b", raw_text)
        has_prices = len(price_matches) >= 2

        if has_multiple_phones and has_prices:
            spam_score += 0.55
            reasons.append("reklama: narxlar va telefon raqamlari katalogi")

        # 8. Check for Legitimate Opportunities (Safelist Protection)
        # Legitimate vacancies, courses, grants, competitions are NOT spam even with phone numbers/prices!
        opportunity_matched = False
        for kw in self.rules.get("legitimate_opportunity_markers", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                opportunity_matched = True
                break

        # 9. Check for Suspicious Job Ads
        # Unrealistic pay claims, "no experience, high income", asks for upfront payment
        for kw in self.rules.get("suspicious_job_markers", []):
            clean_kw = normalize_text_for_comparison(kw)
            if clean_kw in norm_text or kw.lower() in lower_raw:
                is_suspicious_job = True
                reasons.append("gumonli e'lon: asossiz yuqori daromad yoki oldindan to'lov")
                break

        # Apply safelist protection if legitimate opportunity
        if opportunity_matched and not is_suspicious_job:
            # Significant score reduction to guarantee it is kept
            spam_score = max(0.0, spam_score - 0.75)
            if "reklama: tovar / xizmat / chegirma sotuvi" in reasons:
                reasons.remove("reklama: tovar / xizmat / chegirma sotuvi")
            if "reklama: narxlar va telefon raqamlari katalogi" in reasons:
                reasons.remove("reklama: narxlar va telefon raqamlari katalogi")

        # Clamp spam_score between 0.0 and 1.0
        final_score = min(1.0, max(0.0, spam_score))
        is_spam = final_score >= settings.SPAM_SCORE_THRESHOLD and not is_suspicious_job

        return final_score, reasons, is_spam, is_suspicious_job

    def deduplicate_posts(
        self,
        posts: List[DigestPost],
        similarity_threshold: Optional[float] = None,
    ) -> Tuple[List[DigestPost], List[DigestPost]]:
        """
        Deduplicates a list of posts using exact normalized hash + near-duplicate token similarity.
        Merges source links into the surviving primary item.
        Returns:
            (unique_clean_posts, duplicate_posts)
        """
        threshold = similarity_threshold if similarity_threshold is not None else settings.DEDUPLICATION_SIMILARITY_THRESHOLD

        unique_posts: List[DigestPost] = []
        duplicate_posts: List[DigestPost] = []

        # Map hash to primary post
        hash_map: Dict[str, DigestPost] = {}

        for post in posts:
            post_hash = compute_normalized_hash(post.text)
            post.normalized_hash = post_hash

            # 1. Exact hash duplicate
            if post_hash in hash_map:
                primary = hash_map[post_hash]
                post.status = "filtered"
                post.discard_reason = f"duplicate: matches post #{primary.message_id} from {primary.chat_title} ({primary.link})"
                # Merge source links: keep all source links
                if post.link and post.link not in (primary.original_source or ""):
                    extra_source = f"{post.chat_title}: {post.link}"
                    if primary.original_source:
                        primary.original_source = f"{primary.original_source} | {extra_source}"
                    else:
                        primary.original_source = extra_source
                duplicate_posts.append(post)
                continue

            # 2. Near-duplicate detection against already accepted unique posts
            is_near_dup = False
            for primary in unique_posts:
                sim = calculate_text_similarity(primary.text, post.text)
                if sim >= threshold:
                    is_near_dup = True
                    post.status = "filtered"
                    post.discard_reason = f"duplicate: near-duplicate ({sim:.2f}) matches post #{primary.message_id} from {primary.chat_title} ({primary.link})"
                    # Merge source link
                    if post.link and post.link not in (primary.original_source or ""):
                        extra_source = f"{post.chat_title}: {post.link}"
                        if primary.original_source:
                            primary.original_source = f"{primary.original_source} | {extra_source}"
                        else:
                            primary.original_source = extra_source
                    duplicate_posts.append(post)
                    break

            if not is_near_dup:
                hash_map[post_hash] = post
                unique_posts.append(post)

        return unique_posts, duplicate_posts

    def process_and_filter_batch(
        self,
        posts: List[DigestPost],
    ) -> Dict[str, Any]:
        """
        Executes full Phase 2 pipeline on a list of posts:
        1. Exact and near-duplicate detection (merging source links).
        2. Spam/scam/ad scoring with reasons.
        3. Suspicious job flagging.
        Returns:
            {
                "clean_posts": List[DigestPost],
                "duplicate_posts": List[DigestPost],
                "spam_posts": List[DigestPost],
                "suspicious_jobs": List[DigestPost],
                "stats": {
                    "total_input": int,
                    "clean_remaining": int,
                    "duplicates_count": int,
                    "spam_count": int,
                    "suspicious_jobs_count": int,
                }
            }
        """
        # Step 1: Deduplicate
        unique_posts, duplicate_posts = self.deduplicate_posts(posts)

        clean_posts: List[DigestPost] = []
        spam_posts: List[DigestPost] = []
        suspicious_jobs: List[DigestPost] = []

        # Step 2: Score spam and ads on unique posts
        for post in unique_posts:
            score, reasons, is_spam, is_suspicious_job = self.score_post(post)
            post.spam_score = score
            post.spam_reasons = ", ".join(reasons) if reasons else None

            if is_suspicious_job:
                # Kept for Topic D with suspicious label
                post.status = "new"
                post.discard_reason = "gumonli e'lon"
                suspicious_jobs.append(post)
                clean_posts.append(post)
            elif is_spam:
                post.status = "filtered"
                post.discard_reason = f"spam: {post.spam_reasons}"
                spam_posts.append(post)
            else:
                post.status = "new"
                clean_posts.append(post)

        return {
            "clean_posts": clean_posts,
            "duplicate_posts": duplicate_posts,
            "spam_posts": spam_posts,
            "suspicious_jobs": suspicious_jobs,
            "stats": {
                "total_input": len(posts),
                "clean_remaining": len(clean_posts),
                "duplicates_count": len(duplicate_posts),
                "spam_count": len(spam_posts),
                "suspicious_jobs_count": len(suspicious_jobs),
            },
        }
