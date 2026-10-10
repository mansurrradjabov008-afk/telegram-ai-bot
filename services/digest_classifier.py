"""
Smart Digest V2 - Phase 3: Post Classifier.
Batches posts, queries Gemini with strict JSON schema at temperature 0.0,
enforces topic taxonomy, precedence rules, extracts structured data for D & F,
and validates results with retry-once and strict anti-hallucination checks.
"""

import inspect
import json
import logging
from typing import List, Dict, Any, Optional, Set
from pydantic import BaseModel, Field

from config import settings
from database.models import DigestPost
from services.ai_engine import AIEngine

logger = logging.getLogger(__name__)


def clean_json_text(raw_text: str) -> str:
    """Strips markdown code fences (```json ... ```) from raw AI output."""
    if not isinstance(raw_text, str):
        return ""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class TopicDExtracted(BaseModel):
    """Structured fields for Topic D: Kasblar narxlari (salaries and rates)."""
    profession: str = Field(default="ko'rsatilmagan", description="Kasb nomi, masalan: Python dasturchi")
    amount: str = Field(default="ko'rsatilmagan", description="Aniq matndagi summa, masalan: $3000 yoki 15 000 000 so'm")
    currency: str = Field(default="ko'rsatilmagan", description="Valyuta, masalan: USD, UZS, EUR")
    period: str = Field(default="ko'rsatilmagan", description="To'lov davri (oyiga, soatiga) yoki ko'rsatilmagan")
    location: str = Field(default="ko'rsatilmagan", description="Ish joyi yoki ko'rsatilmagan")
    employer: str = Field(default="ko'rsatilmagan", description="Ish beruvchi tashkilot yoki ko'rsatilmagan")

    model_config = {"extra": "ignore"}


class TopicFExtracted(BaseModel):
    """Structured fields for Topic F: Imkoniyatlar (opportunities with deadlines)."""
    title: str = Field(default="ko'rsatilmagan", description="Imkoniyat, grant yoki kurs nomi")
    deadline: str = Field(default="ko'rsatilmagan", description="Oxirgi muddat (nisbiy so'zlar o'zgartirilmaydi)")
    how_to_apply: str = Field(default="ko'rsatilmagan", description="Ariza topshirish tartibi yoki havola")

    model_config = {"extra": "ignore"}


class ClassifiedPostItem(BaseModel):
    """Schema for individual post classification result."""
    post_id: int
    topic: str = Field(description="A, B, C, D, E, F yoki other")
    importance: int = Field(default=1, ge=1, le=5, description="1 dan 5 gacha muhimlik darajasi")
    is_advertisement: bool = Field(default=False, description="Tijoriy reklama ekanligi")
    extracted: Optional[Dict[str, Any]] = Field(default=None, description="Topic D yoki Topic F uchun ajratilgan ma'lumotlar")

    model_config = {"extra": "ignore"}


class BatchClassificationResponse(BaseModel):
    """Schema for entire batch classification response."""
    items: List[ClassifiedPostItem]

    model_config = {"extra": "ignore"}


CLASSIFICATION_SYSTEM_PROMPT = """Siz — Telegram Smart Digest V2 uchun professional tahlilchi va klassifikatorsiz.
Sizga berilgan Telegram postlarini o'rganib chiqib, har bir post uchun yagona asosiy mavzu (topic), muhimlik darajasi (importance), reklama belgisi (is_advertisement) va kerakli strukturalangan ma'lumotlarni (extracted) aniqlang.

MAVZULAR (TOPICS):
A. Siyosiy yangiliklar (O'zbekiston va jahon siyosati, qonunchilik, davlat boshqaruvi)
B. Tashqi siyosat va muzokaralar (diplomatiya, tashqi ishlar, xalqaro sammitlar, rasmiy muzokaralar, bitimlar, xalqaro tashkilotlar: BMT, ShHT, YeOII va b.)
C. TSUE yangiliklari (Toshkent Davlat Iqtisodiyot Universiteti - TDIU / TSUE: yangiliklar, qabullar, tadbirlar, anjumanlar, talabalar hayoti)
D. Kasblar narxlari (maoshlar va stavkalar). FAQAT postning ASOSIY mazmuni maosh yoki ish haqi ko'rsatkichlari bo'lgan e'lonlar uchun. Ustuvorlik: AI/ML muhandislari, dasturchilar, diplomatlar/xalqaro munosabatlar rollari; boshqa kasblar faqat summa ko'rsatilgan taqdirda.
E. Sun'iy intellekt (AI): BARCHA sun'iy intellekt yangiliklari: yangi modellar (LLM, computer vision va b.), mahsulotlar, ilmiy tadqiqotlar, vositalar, AI kompaniyalari, regulyatsiya, kurslar, grantlar.
F. Imkoniyatlar (oxirgi muddati/deadline'i bo'lgan imkoniyatlar: kurslar, grantlar, tanlovlar, stajirovkalar, stipendiyalar, konferensiyalar).
other. Yuqoridagi 6 toifaga kirmaydigan yoki umumiy xabarlar.

QAT'IY USTUVORLIK TARTIBI (Precedence Order):
Agar post bir vaqtning o'zida bir nechta mavzuga to'g'ri kelsa, QAT'IY ravishda quyidagi tartibda yagona bitta asosiy mavzuni tanlang:
F (agar ariza topshirish yoki aniq deadline/muddat bo'lsa) >
C (agar TSUE/TDIU haqida bo'lsa) >
E (agar AI/sun'iy intellekt haqida bo'lsa) >
B (agar diplomatiya/tashqi siyosat haqida bo'lsa) >
A (siyosat) >
D (kasblar narxlari).
DIQQAT: 'D' toifasi FAQAT postning asosiy mazmuni maosh/ish haqi ko'rsatkichlari bo'lgandagina tanlanadi.

MUHIMLIK DARAJASI (Importance: 1 dan 5 gacha):
5: Xo'jayin uchun bevosita dolzarb, shoshilinch deadline yoki juda yirik tarixiy voqea/yangilik.
4: Yuqori ahamiyatga ega jiddiy yangilik yoki manfaatli imkoniyat.
3: O'rtacha qiziqarli yangilik.
2: Kam ahamiyatli yoki umumiy gaplar.
1: Marginal, arzimas yoki aralash umumiy gap.

STRUKTURALANGAN MAYDONLAR (extracted):
- Agar topic == 'D' bo'lsa:
  "extracted": {
    "profession": "kasb nomi (matndagi aniq nom)",
    "amount": "matndagi ANIQ summa (masalan: '$3000' yoki '15 000 000 so'm')",
    "currency": "valyuta (USD, UZS, EUR, so'm, dollar)",
    "period": "to'lov davri (oyiga, soatiga, loyihaga) yoki 'ko'rsatilmagan'",
    "location": "joylashuv yoki 'ko'rsatilmagan'",
    "employer": "ish beruvchi yoki kompaniya yoki 'ko'rsatilmagan'"
  }
- Agar topic == 'F' bo'lsa:
  "extracted": {
    "title": "imkoniyat yoki grant nomi",
    "deadline": "oxirgi muddat (DIQQAT: agar matnda nisbiy so'zlar: 'ertaga', 'bugun', 'shu juma', 'завтра' bo'lsa, ularni AYNAN O'ZIDEK yozing, o'zingiz mutlaq sanaga aylantirmang!) yoki 'ko'rsatilmagan'",
    "how_to_apply": "ariza topshirish tartibi yoki havola yoki 'ko'rsatilmagan'"
  }
- Boshqa mavzularda: "extracted": null bo'lishi mumkin.

ANTI-HALLUCINATION VA REKLAMA QOIDALARI:
- Faqat post matnida mavjud bo'lgan ma'lumotlarni yozing. Hech narsani to'qimang.
- is_advertisement: agar post toza mahsulot/xizmat sotish reklamasi bo'lsa true, aks holda false.
- FAQAT quyidagi JSON formatida javob bering, boshqa hech qanday izoh qo'shmang:
{
  "items": [
    {
      "post_id": 123,
      "topic": "E",
      "importance": 4,
      "is_advertisement": false,
      "extracted": null
    }
  ]
}
"""


class DigestClassifier:
    """
    Handles Phase 3 batch classification of digest posts.
    """

    ALLOWED_TOPICS: Set[str] = {"A", "B", "C", "D", "E", "F", "other"}

    def __init__(self, ai_engine: AIEngine):
        self.ai_engine = ai_engine

    def build_batch_prompt(self, posts: List[DigestPost]) -> str:
        """Constructs prompt containing posts for Gemini input."""
        posts_text_list = []
        for p in posts:
            post_id = p.id if p.id is not None else 0
            # Truncate to MAX_POST_CHARS for AI input, keeping full text in DB
            truncated_text = p.text[:settings.MAX_POST_CHARS] if p.text else ""
            source_info = f"Manba: {p.chat_title} (@{p.chat_username or 'yopiq'})"
            if p.original_source:
                source_info += f" | Forward: {p.original_source}"
            posts_text_list.append(
                f"[POST ID: {post_id}]\n"
                f"{source_info}\n"
                f"Sana: {p.posted_at or 'noma`lum'}\n"
                f"Matn:\n{truncated_text}\n"
            )

        batch_body = "\n---\n".join(posts_text_list)
        return (
            f"{CLASSIFICATION_SYSTEM_PROMPT}\n\n"
            f"TAHLIL QILINADIGAN POSTLAR RO'YXATI:\n\n"
            f"{batch_body}\n\n"
            f"Barcha yuqoridagi postlar uchun to'liq JSON javob qaytaring:"
        )

    def parse_and_validate_response(
        self,
        raw_text: str,
        expected_ids: Set[int],
    ) -> Optional[List[ClassifiedPostItem]]:
        """
        Parses JSON response and validates against Pydantic schema.
        Enforces Anti-Hallucination verification: referenced post_ids must exist in input batch.
        """
        if not raw_text:
            return None

        clean_text = clean_json_text(raw_text)
        try:
            data = json.loads(clean_text)
        except Exception as e:
            logger.warning(f"Classification JSON parse error: {type(e).__name__}")
            return None

        try:
            batch_resp = BatchClassificationResponse.model_validate(data)
        except Exception as e:
            logger.warning(f"Classification Pydantic validation error: {type(e).__name__}")
            return None

        validated_items: List[ClassifiedPostItem] = []
        for item in batch_resp.items:
            # Code verification: post_id must exist in expected_ids
            if item.post_id not in expected_ids:
                logger.warning(
                    f"Discarding hallucinated post_id {item.post_id} not in batch {expected_ids}"
                )
                continue
            validated_items.append(item)

        return validated_items

    async def classify_batch_with_retry(
        self,
        batch: List[DigestPost],
    ) -> Optional[List[ClassifiedPostItem]]:
        """
        Sends batch to AI engine with retry-once on invalid output.
        Never invents a fallback result if both attempts fail.
        """
        expected_ids = {p.id for p in batch if p.id is not None}
        prompt = self.build_batch_prompt(batch)
        temp = getattr(settings, "DIGEST_AI_TEMPERATURE", 0.0)

        # Attempt 1
        res = self.ai_engine.generate_json_response(prompt, temperature=temp)
        if inspect.iscoroutine(res):
            raw_response = await res
        else:
            raw_response = res
        if not isinstance(raw_response, str):
            raw_response = ""

        validated = self.parse_and_validate_response(raw_response, expected_ids)
        if validated is not None:
            return validated

        # Retry once
        logger.warning(f"AI classification failed for batch {expected_ids}. Retrying once...")
        res_retry = self.ai_engine.generate_json_response(prompt, temperature=temp)
        if inspect.iscoroutine(res_retry):
            raw_retry = await res_retry
        else:
            raw_retry = res_retry
        if not isinstance(raw_retry, str):
            raw_retry = ""

        validated_retry = self.parse_and_validate_response(raw_retry, expected_ids)
        if validated_retry is not None:
            return validated_retry

        # Both attempts failed: return None without hallucinating fallback
        logger.error(
            f"AI classification failed after retry for batch {expected_ids}. Skipping batch without fallback."
        )
        return None

    async def classify_posts(
        self,
        posts: List[DigestPost],
    ) -> Dict[str, Any]:
        """
        Processes posts in batches, adhering to BATCH_SIZE and MAX_AI_CALLS_PER_RUN.
        Sorts and prioritizes posts if quota limit is exceeded.
        Updates post fields: status, topic, importance, discard_reason, extracted_json.
        """
        if not posts:
            return {
                "classified_posts": [],
                "discarded_posts": [],
                "topic_counts": {},
                "ai_calls_made": 0,
                "failed_batches_count": 0,
                "skipped_posts_count": 0,
                "total_classified": 0,
                "total_discarded": 0,
            }

        batch_size = max(1, getattr(settings, "BATCH_SIZE", 10))
        max_ai_calls = max(1, getattr(settings, "MAX_AI_CALLS_PER_RUN", 10))
        min_importance = getattr(settings, "DIGEST_IMPORTANCE_MIN_THRESHOLD", 2)

        # Quota prioritization: channels first, then groups, then by date descending
        # When limit is reached, process highest-priority sources first
        prioritized_posts = sorted(
            posts,
            key=lambda p: (
                0 if p.chat_username else 1,  # Public channels first
                p.posted_at or "",
            ),
            reverse=True,
        )

        max_posts_allowed = batch_size * max_ai_calls
        posts_to_process = prioritized_posts[:max_posts_allowed]
        skipped_posts = prioritized_posts[max_posts_allowed:]

        classified_posts: List[DigestPost] = []
        discarded_posts: List[DigestPost] = []
        topic_counts: Dict[str, int] = {}
        ai_calls_made = 0
        failed_batches_count = 0

        # Mark quota-skipped posts as discarded
        for sp in skipped_posts:
            sp.status = "discarded"
            sp.discard_reason = "quota_limit_skipped"
            discarded_posts.append(sp)

        # Process in batches
        for i in range(0, len(posts_to_process), batch_size):
            if ai_calls_made >= max_ai_calls:
                # Extra safeguard
                for remaining in posts_to_process[i:]:
                    remaining.status = "discarded"
                    remaining.discard_reason = "quota_limit_skipped"
                    discarded_posts.append(remaining)
                break

            current_batch = posts_to_process[i : i + batch_size]
            ai_calls_made += 1

            classified_items = await self.classify_batch_with_retry(current_batch)

            if classified_items is None:
                # Batch failed after retry - discard batch without fallback
                failed_batches_count += 1
                for bp in current_batch:
                    bp.status = "discarded"
                    bp.discard_reason = "ai_classification_failed"
                    discarded_posts.append(bp)
                continue

            # Map returned items by post_id
            items_by_id = {item.post_id: item for item in classified_items}

            for bp in current_batch:
                item = items_by_id.get(bp.id)
                if not item:
                    # AI omitted this post from response
                    bp.status = "discarded"
                    bp.discard_reason = "omitted_by_classifier"
                    discarded_posts.append(bp)
                    continue

                # Topic validation
                topic = item.topic.strip() if item.topic else "other"
                if topic not in self.ALLOWED_TOPICS:
                    topic = "other"

                # Importance clamping
                importance = max(1, min(5, item.importance))

                # Drop 'other', importance <= 1, or advertisements
                if topic == "other" or importance < min_importance or item.is_advertisement:
                    bp.status = "discarded"
                    bp.topic = topic
                    bp.importance = importance
                    if item.is_advertisement:
                        bp.discard_reason = "advertisement"
                    else:
                        bp.discard_reason = f"topic {topic} or importance {importance}"
                    discarded_posts.append(bp)
                else:
                    bp.status = "classified"
                    bp.topic = topic
                    bp.importance = importance
                    bp.discard_reason = None

                    # Extract structured fields for D and F
                    if item.extracted:
                        try:
                            if topic == "D":
                                ext_model = TopicDExtracted.model_validate(item.extracted)
                                bp.extracted_json = json.dumps(ext_model.model_dump(), ensure_ascii=False)
                            elif topic == "F":
                                ext_model = TopicFExtracted.model_validate(item.extracted)
                                bp.extracted_json = json.dumps(ext_model.model_dump(), ensure_ascii=False)
                            else:
                                bp.extracted_json = json.dumps(item.extracted, ensure_ascii=False)
                        except Exception as e:
                            logger.warning(f"Error validating extracted fields for post {bp.id}: {type(e).__name__}")
                            bp.extracted_json = json.dumps(item.extracted, ensure_ascii=False)
                    else:
                        bp.extracted_json = None

                    classified_posts.append(bp)
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1

        return {
            "classified_posts": classified_posts,
            "discarded_posts": discarded_posts,
            "topic_counts": topic_counts,
            "ai_calls_made": ai_calls_made,
            "failed_batches_count": failed_batches_count,
            "skipped_posts_count": len(skipped_posts),
            "total_classified": len(classified_posts),
            "total_discarded": len(discarded_posts),
        }
