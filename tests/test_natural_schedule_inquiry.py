import pytest
from datetime import datetime, timedelta
import pytz
from services.schedule_parser import parse_deterministic_schedule, parse_schedule_intent_hybrid
from services.ai_engine import AIEngine
from database.models import Contact

class MockContact:
    def __init__(self, uid, username, full_name):
        self.user_id = uid
        self.username = username
        self.full_name = full_name

@pytest.mark.asyncio
async def test_natural_schedule_with_contact_alias_and_end_time():
    contacts = [
        MockContact(8036556250, "mansurjon_07", "Mansur"),
        MockContact(6152742837, "umid_namozov", "Umid jura"),
    ]

    # Exact user query from screenshot
    query = "mansur akaga kola kearkmi shuni sura 5 minutdan keyin"
    res = parse_deterministic_schedule(query, tz_name="Asia/Tashkent", contacts=contacts)

    assert res is not None
    assert res.target_peer == "@mansurjon_07"
    assert "kola kerakmi" in res.message_text.lower()
    assert "5 daqiqadan so'ng" in res.human_time

    # Verify time is approximately 5 minutes in future
    tz = pytz.timezone("Asia/Tashkent")
    now = datetime.now(tz)
    assert 4 * 60 <= (res.run_at - now).total_seconds() <= 6 * 60

@pytest.mark.asyncio
async def test_natural_schedule_time_at_start():
    contacts = [
        MockContact(8036556250, "mansurjon_07", "Mansur"),
    ]

    query = "5 minutdan keyin mansur akaga kola kerakmi deb sura"
    res = parse_deterministic_schedule(query, tz_name="Asia/Tashkent", contacts=contacts)

    assert res is not None
    assert res.target_peer == "@mansurjon_07"
    assert "kola kerakmi" in res.message_text.lower()

@pytest.mark.asyncio
async def test_inquiry_topic_aware_fallback():
    engine = AIEngine(api_key="")

    res = await engine.analyze_contact_inquiry(
        contact_name="Mansur",
        username="mansurjon_07",
        messages=[],
        user_query="mansur aka qanaqa kitob uqiyapti ekan surachi",
        addressing_style="siz",
        facts=[],
    )
    assert res is not None
    assert "kitob" in res["suggested_outreach"].lower()
    assert "o'qiyapsiz" in res["suggested_outreach"].lower()

@pytest.mark.asyncio
async def test_ask_jarvis_no_canned_dodging():
    engine = AIEngine(api_key="")

    reply = await engine.ask_jarvis(
        user_query="mansur akaga kola kearkmi shuni sura 5 minutdan keyin",
        context={"boss_name": "Radjabov Mansur"},
    )

    # Must NOT contain the old useless canned phrase!
    assert "Barcha tizimlar to'liq avtonom rejimda nazoratim ostida" not in reply
    assert "topshirig'ingiz qabul qilindi" in reply.lower() or "schedule" in reply.lower()


@pytest.mark.asyncio
async def test_natural_schedule_cyrillic_contact_suhrob():
    """Verifies that Latin 'Suhrobdan ... 2minutdan keyin yoz' matches Cyrillic contact 'Сухров Омонов'."""
    contacts = [
        MockContact(8747692548, "SuhrobOmonov12", "Сухров Омонов"),
        MockContact(265367488, "ravshanov_dev", "ravshanov_dev"),
    ]

    query = "Suhrobdan surachi nima gaplar ekan 2minutdan keyin yoz"
    res = parse_deterministic_schedule(query, tz_name="Asia/Tashkent", contacts=contacts)

    assert res is not None
    assert res.target_peer == "@SuhrobOmonov12"
    assert "nima gaplar" in res.message_text.lower()
    assert "2 daqiqadan so'ng" in res.human_time


@pytest.mark.asyncio
async def test_contextual_schedule_after_preview_without_peer():
    """Verifies that contextual schedule command 'Bugun qayerga boramiz deb 5minutdan keyin yoz' uses default_peer."""
    contacts = [
        MockContact(8747692548, "SuhrobOmonov12", "Сухров Омонов"),
        MockContact(265367488, "ravshanov_dev", "ravshanov_dev"),
    ]

    query = "Bugun qayerga boramiz deb 5minutdan keyin yoz"
    res = parse_deterministic_schedule(
        query,
        tz_name="Asia/Tashkent",
        contacts=contacts,
        default_peer="@SuhrobOmonov12",
    )

    assert res is not None
    assert res.target_peer == "@SuhrobOmonov12"
    assert "bugun qayerga boramiz" in res.message_text.lower()
    assert "5 daqiqadan so'ng" in res.human_time


@pytest.mark.asyncio
async def test_no_false_alias_from_stopwords():
    """Verifies that ravshanov_dev (dev -> deb) does not accidentally match 'deb' in schedule sentences."""
    contacts = [
        MockContact(265367488, "ravshanov_dev", "ravshanov_dev"),
    ]

    query = "Ertaga soat 10:00 da dars boshlanadi deb yoz"
    res = parse_deterministic_schedule(query, tz_name="Asia/Tashkent", contacts=contacts)
    # Since neither ravshanov nor @ravshanov_dev is in query, it should NOT match ravshanov_dev
    if res is not None:
        assert res.target_peer != "@ravshanov_dev"
