"""
Category Enum and Normalizer for Contact Memory (Step 1.3).
Enforces the 7 spec categories:
- identity
- preference
- interest
- important_date
- relationship_note
- ongoing_topic
- other
"""

from typing import Set, Dict

ALLOWED_CATEGORIES: Set[str] = {
    "identity",
    "preference",
    "interest",
    "important_date",
    "relationship_note",
    "ongoing_topic",
    "other",
}

CANONICAL_CATEGORIES: Set[str] = ALLOWED_CATEGORIES

CATEGORY_SYNONYMS: Dict[str, str] = {
    "kasb": "identity",
    "manzil": "identity",
    "location": "identity",
    "work": "identity",
    "shahar": "identity",
    "professiya": "identity",
    "shaxsiyat": "identity",
    "ism": "identity",
    "familiya": "identity",
    "qiziqish": "interest",
    "hobbi": "interest",
    "mashgulot": "interest",
    "afzallik": "preference",
    "yoqtirish": "preference",
    "yoqtirishi": "preference",
    "sana": "important_date",
    "tugilgan_kun": "important_date",
    "bayram": "important_date",
    "munosabat": "relationship_note",
    "aloqa": "relationship_note",
    "dostlik": "relationship_note",
    "mavzu": "ongoing_topic",
    "loyiha": "ongoing_topic",
    "ish": "ongoing_topic",
    "faoliyat": "ongoing_topic",
    "biznes": "ongoing_topic",
    "hamkorlik": "ongoing_topic",
    "eksport": "ongoing_topic",
}


def normalize_category(raw_category: str | None) -> str:
    """
    Validates and maps category to the canonical enum.
    Returns canonical category string ('identity', 'preference', 'interest',
    'important_date', 'relationship_note', 'ongoing_topic', 'other').
    """
    if not raw_category:
        return "other"
    cleaned = raw_category.strip().lower()
    if cleaned in ALLOWED_CATEGORIES:
        return cleaned
    return CATEGORY_SYNONYMS.get(cleaned, "other")
