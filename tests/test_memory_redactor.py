"""
Unit tests for Pure Redaction Module (services/memory_redactor.py).
Covers 25+ real positive examples (cards with Luhn, PINFL, passports, bank accounts, OTPs)
and negative safety examples (dates, prices, counts, quantities, phone numbers).
"""

import pytest
from services.memory_redactor import (
    luhn_checksum,
    redact_text,
    redact_cards,
    redact_bank_accounts,
    redact_pinfl,
    redact_passports,
    redact_otps,
)


class TestLuhnChecksum:
    def test_valid_16_digit_card(self):
        # 8600123456789012 passes Luhn
        assert luhn_checksum("8600123456789012") is True

    def test_valid_humo_card(self):
        # 9860123456789015 passes Luhn
        assert luhn_checksum("9860123456789015") is True

    def test_invalid_card_checksum(self):
        # 8600123456789013 fails Luhn
        assert luhn_checksum("8600123456789013") is False

    def test_too_short_digits(self):
        # Phone numbers or short codes (<13 digits) fail
        assert luhn_checksum("998901234567") is False

    def test_too_long_digits(self):
        # > 19 digits fail
        assert luhn_checksum("123456789012345678901") is False


class TestCardRedaction:
    def test_uzcard_spaced(self):
        text = "Mening kartam: 8600 1234 5678 9012 raqamiga o'tkazing."
        expected = "Mening kartam: [REDACTED] raqamiga o'tkazing."
        assert redact_text(text) == expected

    def test_humo_hyphenated(self):
        text = "Karta raqami: 9860-1234-5678-9015."
        expected = "Karta raqami: [REDACTED]."
        assert redact_text(text) == expected

    def test_visa_contiguous(self):
        # 4532015112830366 is a valid Luhn 16-digit Visa
        text = "To'lov kartasi 4532015112830366 orqali amalga oshirildi."
        expected = "To'lov kartasi [REDACTED] orqali amalga oshirildi."
        assert redact_text(text) == expected

    def test_invalid_card_not_redacted(self):
        # Does not pass Luhn checksum
        text = "Hisob kodi: 8600 1234 5678 9019 tekshirildi."
        assert redact_text(text) == text


class TestBankAccountRedaction:
    def test_uzbek_20_digit_account(self):
        text = "Hisob raqamimiz: 20208000100123456789 filialda ochilgan."
        expected = "Hisob raqamimiz: [REDACTED] filialda ochilgan."
        assert redact_text(text) == expected

    def test_iban_account(self):
        text = "IBAN: GB29NWBK60161331926819 to'lov uchun."
        expected = "IBAN: [REDACTED] to'lov uchun."
        assert redact_text(text) == expected


class TestPinflRedaction:
    def test_pinfl_14_digits(self):
        text = "JShShIR (PINFL): 31204951234567 tasdiqlandi."
        expected = "JShShIR (PINFL): [REDACTED] tasdiqlandi."
        assert redact_text(text) == expected


class TestPassportRedaction:
    def test_uzbek_passport_contiguous(self):
        text = "Pasport seriyasi: AA1234567 berilgan sana 2020."
        expected = "Pasport seriyasi: [REDACTED] berilgan sana 2020."
        assert redact_text(text) == expected

    def test_uzbek_passport_spaced(self):
        text = "Mening pasportim FA 7654321 nusxasi."
        expected = "Mening pasportim [REDACTED] nusxasi."
        assert redact_text(text) == expected

    def test_cis_passport_with_keyword(self):
        text = "Pasport: 4508 123456 egasiga tegishli."
        expected = "Pasport: [REDACTED] egasiga tegishli."
        assert redact_text(text) == expected


class TestOtpPasswordRedaction:
    def test_uzbek_latin_tasdiqlash_kodi(self):
        text = "Sizning tasdiqlash kodingiz: 5920. Hech kimga bermang."
        expected = "Sizning tasdiqlash kodingiz: [REDACTED]. Hech kimga bermang."
        assert redact_text(text) == expected

    def test_uzbek_latin_backward_code(self):
        text = "84920 - tasdiqlash kodi"
        expected = "[REDACTED] - tasdiqlash kodi"
        assert redact_text(text) == expected

    def test_uzbek_latin_sms_kod(self):
        text = "SMS-kod: 123456"
        expected = "SMS-kod: [REDACTED]"
        assert redact_text(text) == expected

    def test_uzbek_latin_parol(self):
        text = "parol: MySecretPass123!"
        expected = "parol: [REDACTED]"
        assert redact_text(text) == expected

    def test_uzbek_latin_maxfiy_kod(self):
        text = "maxfiy kod: 778899"
        expected = "maxfiy kod: [REDACTED]"
        assert redact_text(text) == expected

    def test_russian_kod_podtverzhdeniya(self):
        text = "Код подтверждения: 994821"
        expected = "Код подтверждения: [REDACTED]"
        assert redact_text(text) == expected

    def test_russian_odnorazoviy_parol(self):
        text = "Одноразовый пароль: 3829"
        expected = "Одноразовый пароль: [REDACTED]"
        assert redact_text(text) == expected

    def test_russian_pin_kod(self):
        text = "Пин-код: 1234"
        expected = "Пин-код: [REDACTED]"
        assert redact_text(text) == expected

    def test_english_otp(self):
        text = "Your OTP is 492011"
        expected = "Your OTP is [REDACTED]"
        assert redact_text(text) == expected

    def test_english_verification_code(self):
        text = "Verification code: 884123"
        expected = "Verification code: [REDACTED]"
        assert redact_text(text) == expected

    def test_english_temp_password(self):
        text = "temp password: TempPass#1"
        expected = "temp password: [REDACTED]"
        assert redact_text(text) == expected


class TestNegativeSafetyRules:
    """Ensures dates, prices, quantities, normal words, and phone numbers are NEVER redacted."""

    def test_phone_number_with_plus(self):
        text = "Mening telefon raqamim +998901234567, aloqaga chiqing."
        assert redact_text(text) == text

    def test_phone_number_without_plus(self):
        text = "Telefon: 998901234567 ga yuboring."
        assert redact_text(text) == text

    def test_phone_number_formatted(self):
        text = "Tel: +998 90 123 45 67 yoki (90) 123-45-67."
        assert redact_text(text) == text

    def test_date_iso(self):
        text = "Bugungi sana 2026-10-09 deb belgilandi."
        assert redact_text(text) == text

    def test_date_dots(self):
        text = "Tug'ilgan sana: 15.08.1995."
        assert redact_text(text) == text

    def test_date_slashes(self):
        text = "Hujjat muddati: 12/05/2028."
        assert redact_text(text) == text

    def test_price_uzbek_som(self):
        text = "Mahsulot narxi 150 000 so'm, yana biri 250000 som."
        assert redact_text(text) == text

    def test_price_dollars(self):
        text = "Oylik to'lov $500 yoki 1500 USD."
        assert redact_text(text) == text

    def test_price_uzs(self):
        text = "Jami 75 000 UZS to'landi."
        assert redact_text(text) == text

    def test_quantities_and_counts(self):
        text = "Do'kondan 10 ta kitob, 5 dona ruchka va 3 kishi uchun chipta oldim."
        assert redact_text(text) == text

    def test_normal_sentence_with_kod(self):
        text = "Bugun dasturchilar bilan yangi kod yozdim va test qildim."
        assert redact_text(text) == text

    def test_normal_sentence_with_parol(self):
        text = "Eski parolni eslolmayapman, tizimga kira olmadim."
        assert redact_text(text) == text

    def test_empty_and_none(self):
        assert redact_text("") == ""
        assert redact_text(None) == ""
