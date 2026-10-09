"""
Smart Digest V2 - Phase 4: Event Grouping and Summarization Service.
Groups posts by event, generates 1-2 sentence neutral summaries in Uzbek (Latin)
strictly based on cited posts, enforces Anti-Hallucination verification by code,
deterministically builds Section D and sorts Section F by deadline.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Set
from pydantic import BaseModel, Field

from config import settings
from database.models import DigestPost
from services.ai_engine import AIEngine
from services.digest_cleaner import DigestCleaner
from services.digest_classifier import clean_json_text
from services.digest_verifier import DigestVerifier

logger = logging.getLogger(__name__)


@dataclass
class DigestItem:
    """Represents a verified, formatted item in the digest."""
    topic: str
    title: str
    summary: str
    source_links: List[Tuple[str, str]]  # [(source_title, url), ...]
    post_date: str
    labels: List[str] = field(default_factory=list)  # e.g. ["tasdiqlanmagan", "ma'lumotlar farq qiladi"]
    post_ids_used: List[int] = field(default_factory=list)
    deadline: Optional[str] = None
    is_suspicious_job: bool = False


class AISummaryResponse(BaseModel):
    """Schema for AI event summarization."""
    title: str = Field(description="Qisqa, aniq sarlavha (3-7 so'z)")
    summary: str = Field(description="1-2 gapdan iborat neytral xulosa (O'zbek tilida)")
    post_ids_used: List[int] = Field(description="Ushbu xulosada foydalanilgan post ID lari")
    unverified: bool = Field(default=False, description="Yagona norasmiy manbadan olingan bo'lsa true")
    has_conflicts: bool = Field(default=False, description="Manbalar ma'lumoti bir-biriga zid bo'lsa true")

    model_config = {"extra": "ignore"}


SUMMARIZATION_SYSTEM_PROMPT = """Siz — Telegram Smart Digest V2 uchun professional yangiliklar redaktorisiz.
Sizga bitta voqea haqidagi bitta yoki bir nechta Telegram postlari beriladi.
Ushbu postlar asosida Xo'jayin uchun 1-2 gapdan iborat ixcham, neytral va xolis brifing xulosasi (summary) yozing.

QAT'IY QOIDALAR (ANTI-HALLUCINATION):
1. Faqat va faqat berilgan post matnlarida mavjud bo'lgan faktlar asosida yozing. Tashqi bilimlardan foydalanmang, taxmin qilmang.
2. Ismlar, tashkilot nomlari, raqamlar, summalar va birliklarni manbadagidek AYNAN o'zgartirmasdan qoldiring.
3. Ruscha yoki inglizcha matnlarni o'zbek tiliga (lotin alifbosida) chiroyli tarjima qiling, lekin atoqli otlar (nomlar) va raqamlarga aslo tegmang.
4. Agar postda nisbiy vaqt so'zlari ("ertaga", "bugun", "shu juma", "завтра") bo'lsa, ularni AYNAN O'ZIDEK yozing. Hech qachon mutlaq sanaga aylantirmang!
5. Agar bir nechta manba ma'lumotlari farq qilsa, ularni bitta faktga birlashtirmang; ikkala ma'lumotni ham ko'rsatib, has_conflicts: true qiling.
6. Agar ma'lumot yagona norasmiy/anonim kanaldan bo'lsa, unverified: true qiling.

Javobni FAQAT quyidagi JSON formatida qaytaring:
{
  "title": "Qisqa sarlavha",
  "summary": "1-2 gapdan iborat ixcham neytral xulosa matni.",
  "post_ids_used": [101],
  "unverified": false,
  "has_conflicts": false
}
"""


class DigestSummarizer:
    """
    Orchestrates Phase 4: Event clustering, summarization, verification, and topic assembly.
    """

    TOPIC_ORDER = ["F", "E", "C", "B", "A", "D"]

    def __init__(self, ai_engine: AIEngine):
        self.ai_engine = ai_engine
        self.verifier = DigestVerifier()
        self.cleaner = DigestCleaner()

    def get_topic_limit(self, topic: str) -> int:
        """Returns max items limit for topic from config."""
        if topic == "E":
            return getattr(settings, "DIGEST_TOPIC_LIMIT_AI", 8)
        return getattr(settings, "DIGEST_TOPIC_LIMIT_DEFAULT", 5)

    def group_posts_by_event(self, posts: List[DigestPost]) -> List[List[DigestPost]]:
        """
        Groups posts discussing the same event within a topic.
        Uses token similarity threshold (0.70) to detect same-event clusters.
        """
        if not posts:
            return []

        groups: List[List[DigestPost]] = []
        for post in posts:
            placed = False
            for group in groups:
                # Compare with leader of group
                sim = self.cleaner.calculate_token_similarity(post.text or "", group[0].text or "")
                if sim >= 0.70:
                    group.append(post)
                    placed = True
                    break
            if not placed:
                groups.append([post])

        return groups

    async def summarize_event_group(
        self,
        group: List[DigestPost],
    ) -> Optional[AISummaryResponse]:
        """
        Calls Gemini with temperature 0.0 to summarize a group of posts with retry-once logic.
        """
        posts_text = []
        for p in group:
            p_id = p.id if p.id is not None else 0
            truncated = p.text[:settings.MAX_POST_CHARS] if p.text else ""
            posts_text.append(
                f"[POST ID: {p_id}] Manba: {p.chat_title} (@{p.chat_username or 'yopiq'})\n"
                f"Sana: {p.posted_at or 'noma`lum'}\n"
                f"Matn: {truncated}"
            )

        prompt = (
            f"{SUMMARIZATION_SYSTEM_PROMPT}\n\n"
            f"VOQEA POSTLARI:\n" + "\n---\n".join(posts_text) + "\n\nJSON javob:"
        )

        temp = getattr(settings, "DIGEST_AI_TEMPERATURE", 0.0)

        # Attempt 1
        raw = await self.ai_engine.generate_json_response(prompt, temperature=temp)
        parsed = self._parse_summary_json(raw)
        if parsed is not None:
            return parsed

        # Retry once
        logger.warning(f"Summarization JSON parse failed. Retrying once...")
        raw_retry = await self.ai_engine.generate_json_response(prompt, temperature=temp)
        parsed_retry = self._parse_summary_json(raw_retry)
        if parsed_retry is not None:
            return parsed_retry

        logger.error(f"Summarization failed after retry. Skipping event group.")
        return None

    def _parse_summary_json(self, raw_text: Optional[str]) -> Optional[AISummaryResponse]:
        """Parses and validates AISummaryResponse."""
        if not raw_text or not isinstance(raw_text, str):
            return None
        clean_text = clean_json_text(raw_text)
        try:
            data = json.loads(clean_text)
            return AISummaryResponse.model_validate(data)
        except Exception as e:
            logger.warning(f"Error parsing AISummaryResponse: {e}")
            return None

    def build_section_d_items(self, posts: List[DigestPost]) -> List[DigestItem]:
        """
        Builds Section D items directly from verified extracted fields (no free-form text).
        Format: 'Kasb - summa/davr - joy - manba' with label 'e'londa ko'rsatilgan, tasdiqlanmagan'.
        If suspicious_job: 'gumonli e'lon, tasdiqlanmagan'.
        """
        items: List[DigestItem] = []
        for p in posts:
            ext_data = {}
            if p.extracted_json:
                try:
                    ext_data = json.loads(p.extracted_json)
                except Exception:
                    pass

            profession = ext_data.get("profession") or "Mutaxassis"
            amount = ext_data.get("amount") or "Ko'rsatilmagan"
            period = ext_data.get("period") or "ko'rsatilmagan"
            location = ext_data.get("location") or "ko'rsatilmagan"

            is_suspicious = False
            if p.spam_reasons and "suspicious_job" in p.spam_reasons:
                is_suspicious = True

            label = "gumonli e'lon, tasdiqlanmagan" if is_suspicious else "e'londa ko'rsatilgan, tasdiqlanmagan"

            summary = f"{profession} — {amount}/{period} — Joylashuv: {location}"

            links = [(p.chat_title, p.link or "")]
            # Also add merged links if available
            if p.original_source and p.original_source.startswith("http"):
                links.append(("Qo'shimcha manba", p.original_source))

            item = DigestItem(
                topic="D",
                title=profession,
                summary=summary,
                source_links=links,
                post_date=p.posted_at or "",
                labels=[label],
                post_ids_used=[p.id] if p.id else [],
                is_suspicious_job=is_suspicious,
            )
            items.append(item)

        return items

    def sort_section_f_items(self, items: List[DigestItem]) -> List[DigestItem]:
        """
        Sorts Section F items by nearest deadline first.
        Relative deadlines stay with post date.
        """
        def deadline_sort_key(item: DigestItem) -> Tuple[int, str]:
            # Priority: 0 if valid ISO date, 1 if relative/string date, 2 if None/unspecified
            dl = item.deadline or ""
            if not dl or dl == "ko'rsatilmagan":
                return (2, item.post_date)
            # Check if dl starts with year e.g. 2026-
            if dl[:4].isdigit():
                return (0, dl)
            return (1, item.post_date)

        return sorted(items, key=deadline_sort_key)

    async def summarize_topic_posts(
        self,
        topic: str,
        posts: List[DigestPost],
    ) -> Tuple[List[DigestItem], List[DigestPost], int]:
        """
        Processes and summarizes posts for a single topic.
        Returns (approved_items, discarded_posts, skipped_count).
        """
        if not posts:
            return ([], [], 0)

        # Sort posts by importance DESC, then posted_at DESC
        sorted_posts = sorted(
            posts,
            key=lambda p: (p.importance, p.posted_at or ""),
            reverse=True,
        )

        limit = self.get_topic_limit(topic)
        eligible_posts = sorted_posts[:limit]
        skipped_count = len(sorted_posts) - len(eligible_posts)

        # Topic D is deterministic - no AI summarization
        if topic == "D":
            items = self.build_section_d_items(eligible_posts)
            return (items, [], skipped_count)

        # Group posts by event
        event_groups = self.group_posts_by_event(eligible_posts)

        approved_items: List[DigestItem] = []
        discarded_posts: List[DigestPost] = []

        for group in event_groups:
            summary_resp = await self.summarize_event_group(group)
            if summary_resp is None:
                for p in group:
                    p.status = "discarded"
                    p.discard_reason = "summarization_ai_failed"
                    discarded_posts.append(p)
                continue

            # Code verification: Rule 3
            post_ids = summary_resp.post_ids_used or [p.id for p in group if p.id]
            is_valid, cleaned_summary, discard_reason = self.verifier.verify_item(
                summary_text=summary_resp.summary,
                source_posts=group,
                post_ids_used=post_ids,
            )

            if not is_valid:
                logger.warning(
                    f"Digest item discarded by code verification: {discard_reason} | Summary: {cleaned_summary}"
                )
                for p in group:
                    p.status = "discarded"
                    p.discard_reason = f"verification_failed: {discard_reason}"
                    discarded_posts.append(p)
                continue

            # Item passed verification!
            labels: List[str] = []
            if summary_resp.has_conflicts:
                labels.append("ma'lumotlar farq qiladi")
            if summary_resp.unverified:
                labels.append("tasdiqlanmagan")

            # Collect source links and date
            source_links = []
            for p in group:
                if p.link:
                    source_links.append((p.chat_title, p.link))
                if p.original_source and p.original_source.startswith("http"):
                    source_links.append(("Qo'shimcha manba", p.original_source))

            post_date = group[0].posted_at or ""

            # Deadline handling for Topic F
            deadline = None
            if topic == "F":
                for p in group:
                    if p.extracted_json:
                        try:
                            ext_f = json.loads(p.extracted_json)
                            dl = ext_f.get("deadline")
                            if dl and dl != "ko'rsatilmagan":
                                # If relative words e.g. "ertaga", append post date
                                if any(rw in dl.lower() for rw in ["ertaga", "bugun", "shu juma", "завтра"]):
                                    deadline = f"{dl} (post sanasi: {p.posted_at[:10] if p.posted_at else ''})"
                                else:
                                    deadline = dl
                                break
                        except Exception:
                            pass

            item = DigestItem(
                topic=topic,
                title=summary_resp.title,
                summary=cleaned_summary,
                source_links=source_links,
                post_date=post_date,
                labels=labels,
                post_ids_used=post_ids,
                deadline=deadline,
            )
            approved_items.append(item)

            # Mark posts as included
            for p in group:
                p.status = "included"
                p.discard_reason = None

        if topic == "F":
            approved_items = self.sort_section_f_items(approved_items)

        return (approved_items, discarded_posts, skipped_count)

    async def process_all_topics(
        self,
        classified_posts: List[DigestPost],
    ) -> Dict[str, Any]:
        """
        Processes all topics in TOPIC_ORDER [F, E, C, B, A, D].
        Maintains topic isolation (failure in one topic doesn't block others).
        """
        # Bucket posts by topic
        posts_by_topic: Dict[str, List[DigestPost]] = {t: [] for t in self.TOPIC_ORDER}
        for p in classified_posts:
            if p.topic in posts_by_topic:
                posts_by_topic[p.topic].append(p)

        sections: Dict[str, List[DigestItem]] = {}
        all_discarded_posts: List[DigestPost] = []
        failed_topics: List[str] = []
        total_skipped = 0

        for topic in self.TOPIC_ORDER:
            topic_posts = posts_by_topic.get(topic, [])
            try:
                items, discarded, skipped = await self.summarize_topic_posts(topic, topic_posts)
                sections[topic] = items
                all_discarded_posts.extend(discarded)
                total_skipped += skipped
            except Exception as e:
                logger.error(f"Error processing topic {topic}: {e}", exc_info=True)
                failed_topics.append(topic)
                sections[topic] = []

        total_selected = sum(len(items) for items in sections.values())

        return {
            "sections": sections,
            "topic_items_count": {t: len(items) for t, items in sections.items()},
            "discarded_by_verification": all_discarded_posts,
            "discarded_by_verification_count": len(all_discarded_posts),
            "failed_topics": failed_topics,
            "skipped_by_limit_count": total_skipped,
            "raw_posts_analyzed": len(classified_posts),
            "total_items_selected": total_selected,
        }
