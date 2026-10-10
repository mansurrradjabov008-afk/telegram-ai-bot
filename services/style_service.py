"""
Central Writing Style Service (D1, D6, D7, D8, D10, Phase 5).
Coordinates style enablement, honesty check, profile caching, few-shot retrieval, and prompt guide assembly.
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
import re
from typing import Optional, Dict, Any, List, Set
from config import settings
from database.crud import DatabaseManager
from services.style_sampler import detect_language_script, detect_register
from services.style_features import build_style_profile
from services.style_renderer import render_style_guide, filter_few_shot_examples
from services.sensitive_guard import check_sensitive_keywords

logger = logging.getLogger(__name__)

# Honesty Check Lexicon (D6) - Config lexicon in uz-lat, uz-cyr, ru, en
_HONESTY_PATTERNS = [
    # Uzbek Latin
    r"\b(?:botmisan|robotmisan|sun'iy intellekt|suniy intellekt|ai misan|odammisan)\b",
    r"\b(?:haqiqiy\s+\w+\b|\bhaqiqatdan\s+ham\s+\w+\b|\brostingni\s+ayt\b)",
    r"\b(?:o'zing\s+(?:yozayapsanmi|yozvosanmi|yozdingmi)|ozing\s+yozvosanmi)\b",
    r"\b(?:avto[\s-]?javobmi|avtomatmi|sen\s+botsanmi|siz\s+botsizmi)\b",
    r"\b(?:odam\s+javob\s+beryaptimi|bot\s+javob\s+beryaptimi)\b",
    # Uzbek Cyrillic
    r"\b(?:ботмисан|роботмисан|сунъий интеллект|одаммисан|ростингни айт)\b",
    r"\b(?:ўзинг\s+ёзяпсанми|ўзингмисан|сен\s+ботсанми|авто[\s-]?жавобми)\b",
    # Russian
    r"\b(?:ты\s+бот|ты\s+робот|ты\s+ии|ты\s+человек|это\s+бот|это\s+робот)\b",
    r"\b(?:это\s+автоответчик|сам\s+пишешь|ты\s+настоящий|бот\s+отвечает|ты\s+нейросеть)\b",
    # English
    r"\b(?:are\s+you\s+a\s+bot|are\s+you\s+an\s+ai|are\s+you\s+real|are\s+you\s+human)\b",
    r"\b(?:is\s+this\s+an?\s+ai|is\s+this\s+a\s+bot|is\s+this\s+automated|auto[\s-]?reply)\b",
    r"\b(?:am\s+i\s+talking\s+to\s+a\s+bot|who\s+is\s+responding)\b",
]

_HONESTY_REGEXES = [re.compile(p, re.IGNORECASE) for p in _HONESTY_PATTERNS]


class StyleService:
    def __init__(self, db_manager: Optional[DatabaseManager]):
        self.db_manager = db_manager

    def check_honesty_question(self, incoming_text: str) -> bool:
        """
        Pure code honesty check (Rule D6):
        Detects if incoming message sincerely asks whether they are talking to an AI/bot
        or whether it is really the owner.
        """
        if not incoming_text:
            return False
        clean_text = incoming_text.strip()
        for rx in _HONESTY_REGEXES:
            if rx.search(clean_text):
                return True
        return False

    async def is_style_enabled_for_contact(self, contact_user_id: int) -> bool:
        """
        Rollout check (Rule D8):
        Returns True if globally enabled or if enabled per-contact (/style on @user).
        """
        if not self.db_manager:
            return False

        # Check per-contact toggle first
        contact_state = await self.db_manager.get_style_note(f"contact:{contact_user_id}:state")
        if contact_state == "on":
            return True
        elif contact_state == "off":
            return False

        # Fall back to global toggle
        global_state = await self.db_manager.get_style_note("global:state")
        if global_state == "on":
            return True
        elif global_state == "off":
            return False

        return getattr(settings, "STYLE_ENABLED", False)

    async def set_contact_enabled(self, contact_user_id: int, enabled: bool) -> None:
        """Sets per-contact style toggle state ('on' or 'off')."""
        if self.db_manager:
            await self.db_manager.upsert_style_note(
                f"contact:{contact_user_id}:state", "on" if enabled else "off"
            )

    async def set_global_enabled(self, enabled: bool) -> None:
        """Sets global style toggle state ('on' or 'off')."""
        if self.db_manager:
            await self.db_manager.upsert_style_note(
                "global:state", "on" if enabled else "off"
            )

    async def get_style_context(
        self,
        contact_user_id: int,
        incoming_text: str,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Fetches and renders the style guide with a strict timeout (STYLE_CONTEXT_TIMEOUT_SECONDS).
        Fails open to empty string ("") if timed out or on any error.
        """
        if not await self.is_style_enabled_for_contact(contact_user_id):
            return ""

        effective_timeout = timeout or getattr(settings, "STYLE_CONTEXT_TIMEOUT_SECONDS", 1.0)
        try:
            return await asyncio.wait_for(
                self._fetch_internal(contact_user_id, incoming_text),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Style context fetch timed out after {effective_timeout}s for contact {contact_user_id}; failing open."
            )
            return ""
        except Exception as e:
            logger.error(f"Error fetching style context for contact {contact_user_id}: {type(e).__name__}")
            return ""

    async def _fetch_internal(self, contact_user_id: int, incoming_text: str) -> str:
        """Internal helper to load profiles, few-shots, notes and render the guide."""
        if not self.db_manager:
            return ""

        lang = detect_language_script(incoming_text)
        reg = detect_register(incoming_text)

        # 1. Profile retrieval hierarchy: contact:<id> -> register:<reg> -> global
        profile = None
        contact_prof = await self.db_manager.get_style_profile(f"contact:{contact_user_id}", lang)
        if contact_prof and contact_prof.get("stats_json"):
            profile = {
                "scope": f"contact:{contact_user_id}",
                "lang": lang,
                "stats": json.loads(contact_prof["stats_json"]),
                "phrases": json.loads(contact_prof.get("phrases_json", "[]")),
                "sample_count": contact_prof.get("sample_count", 0),
            }
        elif reg:
            reg_prof = await self.db_manager.get_style_profile(f"register:{reg}", lang)
            if reg_prof and reg_prof.get("stats_json"):
                profile = {
                    "scope": f"register:{reg}",
                    "lang": lang,
                    "stats": json.loads(reg_prof["stats_json"]),
                    "phrases": json.loads(reg_prof.get("phrases_json", "[]")),
                    "sample_count": reg_prof.get("sample_count", 0),
                }

        if not profile:
            glob_prof = await self.db_manager.get_style_profile("global", lang)
            if not glob_prof:
                glob_prof = await self.db_manager.get_style_profile("global", "uz-lat")
            if glob_prof and glob_prof.get("stats_json"):
                profile = {
                    "scope": "global",
                    "lang": glob_prof["lang"],
                    "stats": json.loads(glob_prof["stats_json"]),
                    "phrases": json.loads(glob_prof.get("phrases_json", "[]")),
                    "sample_count": glob_prof.get("sample_count", 0),
                }

        # 2. Few-shot retrieval from same contact thread
        samples = await self.db_manager.get_style_samples(contact_user_id=contact_user_id, limit=20)
        raw_candidates = [s["text_redacted"] for s in samples if s.get("text_redacted")]
        few_shots = filter_few_shot_examples(raw_candidates)

        # 3. Owner notes (contact specific or global)
        note = await self.db_manager.get_style_note(f"contact:{contact_user_id}")
        if not note and reg:
            note = await self.db_manager.get_style_note(f"register:{reg}")
        if not note:
            note = await self.db_manager.get_style_note("global")

        guide = render_style_guide(
            profile=profile,
            examples=few_shots,
            owner_note=note,
            target_lang=lang,
            target_register=reg,
        )
        return guide

    async def rebuild_all_profiles(self) -> Dict[str, int]:
        """
        Rebuilds structured profiles from active style_samples and stores in style_profiles.
        """
        if not self.db_manager:
            return {"built": 0}

        # Gather forbidden entities
        contacts = await self.db_manager.list_contacts()
        forbidden_entities: Set[str] = set()
        for c in contacts:
            if c.full_name:
                forbidden_entities.update(c.full_name.lower().split())
            if c.username:
                forbidden_entities.update(c.username.lower().split())

        # Fetch all samples inside window
        win_days = getattr(settings, "STYLE_WINDOW_DAYS", 180)
        all_samples = await self.db_manager.get_style_samples(window_days=win_days)

        profiles_built = 0

        # 1. Global by language
        samples_by_lang: Dict[str, List[Dict[str, Any]]] = {}
        for s in all_samples:
            l = s.get("lang", "uz-lat")
            samples_by_lang.setdefault(l, []).append(s)

        for l, l_samples in samples_by_lang.items():
            min_req = getattr(settings, "STYLE_MIN_LANG_SAMPLES", 60)
            prof = build_style_profile("global", l, l_samples, forbidden_entities, min_samples=min_req)
            if prof:
                await self.db_manager.upsert_style_profile(
                    scope="global",
                    lang=l,
                    stats_json=json.dumps(prof["stats"]),
                    phrases_json=json.dumps(prof["phrases"]),
                    sample_count=prof["sample_count"],
                )
                profiles_built += 1

        # 2. Register (siz / sen)
        for reg in ("siz", "sen"):
            reg_samples = [s for s in all_samples if s.get("register") == reg]
            if len(reg_samples) >= getattr(settings, "STYLE_MIN_REGISTER_SAMPLES", 60):
                prof = build_style_profile(f"register:{reg}", "uz-lat", reg_samples, forbidden_entities, min_samples=60)
                if prof:
                    await self.db_manager.upsert_style_profile(
                        scope=f"register:{reg}",
                        lang="uz-lat",
                        stats_json=json.dumps(prof["stats"]),
                        phrases_json=json.dumps(prof["phrases"]),
                        sample_count=prof["sample_count"],
                    )
                    profiles_built += 1

        # 3. Eligible contacts (>= STYLE_MIN_CONTACT_SAMPLES)
        samples_by_contact: Dict[int, List[Dict[str, Any]]] = {}
        for s in all_samples:
            cid = s.get("contact_user_id")
            if cid:
                samples_by_contact.setdefault(cid, []).append(s)

        min_contact = getattr(settings, "STYLE_MIN_CONTACT_SAMPLES", 40)
        for cid, c_samples in samples_by_contact.items():
            if len(c_samples) >= min_contact:
                l = c_samples[0].get("lang", "uz-lat")
                prof = build_style_profile(f"contact:{cid}", l, c_samples, forbidden_entities, min_samples=min_contact)
                if prof:
                    await self.db_manager.upsert_style_profile(
                        scope=f"contact:{cid}",
                        lang=l,
                        stats_json=json.dumps(prof["stats"]),
                        phrases_json=json.dumps(prof["phrases"]),
                        sample_count=prof["sample_count"],
                    )
                    profiles_built += 1

        return {"built": profiles_built, "samples_evaluated": len(all_samples)}

    async def purge_retention(self) -> int:
        """
        Purges style_samples older than STYLE_SAMPLE_RETENTION_DAYS (D10).
        """
        if not self.db_manager:
            return 0
        try:
            purged = await self.db_manager.purge_old_style_samples()
            if purged > 0:
                logger.info(f"Purged {purged} expired style samples based on retention.")
            return purged
        except Exception as e:
            logger.warning(f"Style retention purge failed: {type(e).__name__}")
            return 0

