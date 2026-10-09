"""
Smart Digest V2 - Phase 5: Formatting and Presentation Engine.
Formats header, strictly ordered sections (F, E, C, B, A, D), deterministic Section D,
deadline-sorted Section F, footer, and splits messages safely under 4096 chars
without breaking HTML tags or item boundaries.
"""

import html
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import pytz

from config import settings
from database.models import DigestPost
from services.digest_summarizer import DigestItem

logger = logging.getLogger(__name__)

MONTH_NAMES_UZ = {
    1: "yanvar",
    2: "fevral",
    3: "mart",
    4: "aprel",
    5: "may",
    6: "iyun",
    7: "iyul",
    8: "avgust",
    9: "sentabr",
    10: "oktabr",
    11: "noyabr",
    12: "dekabr",
}

SECTION_METADATA = {
    "F": {
        "title": "IMKONIYATLAR (Grantlar, stipendiyalar, tanlovlar)",
        "icon": "🎓",
    },
    "E": {
        "title": "SUN'IY INTELLEKT (AI)",
        "icon": "🤖",
    },
    "C": {
        "title": "TSUE (TDIU) YANGILIKLARI",
        "icon": "🏛",
    },
    "B": {
        "title": "TASHQI SIYOSAT VA MUZOKARALAR",
        "icon": "🌐",
    },
    "A": {
        "title": "SIYOSIY YANGILIKLAR",
        "icon": "📰",
    },
    "D": {
        "title": "KASBLAR NARXLARI (MAOSHLAR VA STAVKALAR)",
        "icon": "💼",
    },
}


class DigestFormatter:
    """
    Renders Smart Digest V2 into beautifully formatted Telegram HTML messages.
    """

    TOPIC_ORDER = ["F", "E", "C", "B", "A", "D"]

    @classmethod
    def get_tashkent_now(cls) -> datetime:
        """Returns current localized Tashkent datetime."""
        tz = pytz.timezone(settings.TIMEZONE)
        return datetime.now(tz)

    @classmethod
    def format_header(
        cls,
        analyzed_count: int,
        selected_count: int,
        target_date: Optional[datetime] = None,
    ) -> str:
        """Formats standard Smart Digest V2 header."""
        dt = target_date or cls.get_tashkent_now()
        month_name = MONTH_NAMES_UZ.get(dt.month, "")
        date_str = f"{dt.day}-{month_name}, {dt.year}-yil"

        return (
            f"📅 <b>{date_str}</b>\n"
            f"📊 <i>{analyzed_count} ta post tahlil qilindi, {selected_count} ta tanlandi</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    @classmethod
    def format_source_links(cls, links: List[Tuple[str, str]]) -> str:
        """Formats HTML links for an item."""
        if not links:
            return ""

        formatted_links = []
        for title, url in links:
            safe_title = html.escape(title or "Manba")
            if url:
                formatted_links.append(f'<a href="{url}">[{safe_title}]</a>')
            else:
                formatted_links.append(f'[{safe_title}]')

        return ", ".join(formatted_links)

    @classmethod
    def format_item(cls, item: DigestItem) -> str:
        """Formats an individual DigestItem with bold title, summary, link, and date."""
        safe_title = html.escape(item.title)
        safe_summary = html.escape(item.summary)

        # Labels formatting
        labels_str = ""
        if item.labels:
            safe_labels = ", ".join([html.escape(l) for l in item.labels])
            labels_str = f" (<i>{safe_labels}</i>)"

        # Date formatting
        date_display = item.post_date[:10] if item.post_date else ""
        date_part = f" | 📅 {date_display}" if date_display else ""

        links_part = cls.format_source_links(item.source_links)
        links_str = f"🔗 {links_part}" if links_part else ""

        # Section D format (Kasblar narxlari)
        if item.topic == "D":
            return (
                f"• <b>{safe_title}</b>\n"
                f"{safe_summary}\n"
                f"{links_str}{date_part}{labels_str}"
            )

        # Section F format with deadline
        deadline_str = ""
        if item.topic == "F" and item.deadline and item.deadline != "ko'rsatilmagan":
            safe_dl = html.escape(item.deadline)
            deadline_str = f"⏳ <b>Muddati:</b> {safe_dl}\n"

        return (
            f"• <b>{safe_title}</b>\n"
            f"{safe_summary}\n"
            f"{deadline_str}"
            f"{links_str}{date_part}{labels_str}"
        )

    @classmethod
    def format_section(cls, topic: str, items: List[DigestItem]) -> str:
        """Formats a single topic section with header and empty fallback."""
        meta = SECTION_METADATA.get(topic, {"title": f"BO'LIM {topic}", "icon": "📌"})
        header = f"\n\n{meta['icon']} <b>{meta['title']}</b>\n"

        if not items:
            return header + "<i>Bu mavzuda yangilik topilmadi</i>"

        formatted_items = [cls.format_item(it) for it in items]
        return header + "\n\n".join(formatted_items)

    @classmethod
    def format_footer(
        cls,
        spam_count: int = 0,
        discarded_by_verification: int = 0,
        skipped_by_limit: int = 0,
        failed_batches: int = 0,
        failed_topics: Optional[List[str]] = None,
    ) -> str:
        """Formats Smart Digest V2 footer."""
        lines = [
            "\n━━━━━━━━━━━━━━━━━━━━",
            f"🛡 <i>Reklama/spam filtrlandi: {spam_count} ta</i>",
            f"🔍 <i>Tekshiruvdan o'tmagan: {discarded_by_verification} ta</i>",
        ]
        if skipped_by_limit > 0:
            lines.append(f"⚠️ <i>Limit sababli kiritilmagan: {skipped_by_limit} ta</i>")
        if failed_batches > 0:
            lines.append(f"⚠️ <i>Muvaffaqiyatsiz batch'lar: {failed_batches} ta</i>")

        if failed_topics:
            safe_failed = ", ".join([html.escape(t) for t in failed_topics])
            lines.append(f"⚠️ <i>Muvaffaqiyatsiz mavzular: {safe_failed}</i>")

        return "\n".join(lines)

    @classmethod
    def format_full_digest(
        cls,
        sections: Dict[str, List[DigestItem]],
        analyzed_count: int,
        selected_count: int,
        spam_count: int = 0,
        discarded_by_verification: int = 0,
        skipped_by_limit: int = 0,
        failed_batches: int = 0,
        failed_topics: Optional[List[str]] = None,
        target_date: Optional[datetime] = None,
    ) -> List[str]:
        """
        Formats complete digest and splits across multiple Telegram messages
        safely under 4000 characters without breaking HTML tags or items.
        Splits at section or item boundaries.
        """
        header = cls.format_header(analyzed_count, selected_count, target_date)
        footer = cls.format_footer(
            spam_count=spam_count,
            discarded_by_verification=discarded_by_verification,
            skipped_by_limit=skipped_by_limit,
            failed_batches=failed_batches,
            failed_topics=failed_topics,
        )

        messages: List[str] = []
        current_msg = header

        for topic in cls.TOPIC_ORDER:
            meta = SECTION_METADATA.get(topic, {"title": f"BO'LIM {topic}", "icon": "📌"})
            sec_header = f"\n\n{meta['icon']} <b>{meta['title']}</b>\n"
            items = sections.get(topic, [])

            if not items:
                empty_sec = sec_header + "<i>Bu mavzuda yangilik topilmadi</i>"
                if len(current_msg) + len(empty_sec) <= 3800:
                    current_msg += empty_sec
                else:
                    if current_msg.strip():
                        messages.append(current_msg.strip())
                    current_msg = empty_sec.strip()
            else:
                first_item_text = cls.format_item(items[0])
                if len(current_msg) + len(sec_header) + len(first_item_text) <= 3800:
                    current_msg += sec_header + first_item_text
                    remaining_items = items[1:]
                else:
                    if current_msg.strip():
                        messages.append(current_msg.strip())
                    current_msg = sec_header.strip() + "\n" + first_item_text
                    remaining_items = items[1:]

                for it in remaining_items:
                    it_text = "\n\n" + cls.format_item(it)
                    if len(current_msg) + len(it_text) <= 3800:
                        current_msg += it_text
                    else:
                        if current_msg.strip():
                            messages.append(current_msg.strip())
                        current_msg = f"{meta['icon']} <b>{meta['title']}</b> (davomi):\n\n" + cls.format_item(it)

        # Add footer to last message or separate
        if len(current_msg) + len("\n" + footer) <= 3900:
            current_msg += "\n" + footer
            messages.append(current_msg.strip())
        else:
            if current_msg.strip():
                messages.append(current_msg.strip())
            messages.append(footer.strip())

        return [m for m in messages if m.strip()]

    @classmethod
    def format_single_topic(
        cls,
        topic: str,
        items: List[DigestItem],
        analyzed_count: int,
        target_date: Optional[datetime] = None,
    ) -> List[str]:
        """Formats a single topic view for /digest topic <A-F>, safely splitting under 4000 chars."""
        dt = target_date or cls.get_tashkent_now()
        month_name = MONTH_NAMES_UZ.get(dt.month, "")
        date_str = f"{dt.day}-{month_name}, {dt.year}-yil"

        meta = SECTION_METADATA.get(topic.upper(), {"title": f"Mavzu {topic}", "icon": "📌"})
        header = (
            f"📅 <b>{date_str}</b>\n"
            f"📌 <b>{meta['icon']} {meta['title']}</b> (<i>{len(items)} ta tanlandi</i>)\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        footer = f"\n━━━━━━━━━━━━━━━━━━━━\n<i>Smart Digest V2 — Mavzu {topic.upper()}</i>"

        if not items:
            return [f"{header}\n\n<i>Bu mavzuda yangilik topilmadi</i>\n{footer}"]

        messages: List[str] = []
        current_msg = header + "\n\n" + cls.format_item(items[0])

        for it in items[1:]:
            it_text = "\n\n" + cls.format_item(it)
            if len(current_msg) + len(it_text) <= 3800:
                current_msg += it_text
            else:
                if current_msg.strip():
                    messages.append(current_msg.strip())
                current_msg = f"📌 <b>{meta['icon']} {meta['title']}</b> (davomi):\n\n" + cls.format_item(it)

        if len(current_msg) + len("\n" + footer) <= 3900:
            current_msg += "\n" + footer
            messages.append(current_msg.strip())
        else:
            if current_msg.strip():
                messages.append(current_msg.strip())
            messages.append(footer.strip())

        return [m for m in messages if m.strip()]

    @classmethod
    def format_debug_posts_page(
        cls,
        posts: List[DigestPost],
        page: int,
        total_count: int,
        page_size: int = 10,
    ) -> str:
        """Formats a page of excluded/discarded posts for /digest debug."""
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        start_idx = page * page_size + 1
        end_idx = min(total_count, start_idx + len(posts) - 1)

        header = (
            f"🔍 <b>SMART DIGEST V2 — CHETLATILGAN POSTLAR (DEBUG)</b>\n"
            f"📄 <b>Sahifa:</b> <code>{page + 1} / {total_pages}</code> (Jami: <code>{total_count}</code> ta)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )

        if not posts:
            return header + "\n<i>Chetlatilgan postlar mavjud emas.</i>"

        lines = []
        for i, p in enumerate(posts, start=start_idx):
            post_id = p.id or p.message_id
            safe_source = html.escape(p.chat_title or "Noma'lum manba")
            safe_user = f"@{p.chat_username}" if p.chat_username else "yopiq"
            reason = p.discard_reason or p.status
            safe_reason = html.escape(reason or "filtrlangan")

            # Truncate post text to ~100 chars
            raw_text = p.text or ""
            truncated = (raw_text[:100] + "...") if len(raw_text) > 100 else raw_text
            safe_text = html.escape(truncated.replace("\n", " "))

            lines.append(
                f"<b>{i}. ID: {post_id}</b> | 📢 {safe_source} (<code>{safe_user}</code>)\n"
                f"🚫 <b>Sabab:</b> <code>{safe_reason}</code>\n"
                f"📝 <i>{safe_text}</i>"
            )

        return header + "\n\n".join(lines)
