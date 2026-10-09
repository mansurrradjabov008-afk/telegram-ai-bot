"""
Pure redaction module for Contact Memory.
Redacts sensitive identifiers before persistence in SQLite:
- Payment card numbers (13-19 digits verified with Luhn checksum algorithm)
- One-time codes / PINs / passwords (OTPs in Uzbek Latin, Uzbek Cyrillic, Russian, English)
- Passport series and numbers (Uzbek 2-letter + 7-digit, and CIS passport formats)
- 14-digit PINFL (JShShIR)
- 20-digit Bank Account numbers & IBANs

Guarantees negative safety: dates, prices, quantities, and phone numbers are NEVER redacted.
"""

import re
from typing import Optional
from config import settings


def luhn_checksum(card_number_str: str) -> bool:
    """
    Validates candidate card number against standard ISO/IEC 7812 Luhn algorithm.
    Only numbers of 13 to 19 digits are considered.
    """
    digits = [int(d) for d in card_number_str if d.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False

    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = digit * 2
            checksum += (doubled - 9) if doubled > 9 else doubled
        else:
            checksum += digit
    return checksum % 10 == 0


def redact_cards(text: str, mask: str = "[REDACTED]") -> str:
    """
    Redacts credit/debit card numbers that satisfy the Luhn algorithm.
    Supports spaced, hyphenated, and contiguous numbers (13 to 19 digits).
    Does NOT match international phone numbers prefixed with '+'.
    """
    def _card_repl(m):
        raw = m.group(0)
        digits = re.sub(r"[\s-]", "", raw)
        return mask if luhn_checksum(digits) else raw

    # Candidate 13-19 digit sequences, not preceded by '+' or digit, not followed by digit
    card_pattern = r"(?<!\+)(?<!\d)\d(?:[\s-]?\d){12,18}(?!\d)"
    return re.sub(card_pattern, _card_repl, text)


def redact_bank_accounts(text: str, mask: str = "[REDACTED]") -> str:
    """
    Redacts 20-digit bank settlement accounts and IBAN identifiers.
    """
    # 20 consecutive digits (Uzbek settlement accounts)
    text = re.sub(r"(?<!\d)\d{20}(?!\d)", mask, text)
    # IBAN identifiers (2 letters + 2 digits + alphanumeric)
    text = re.sub(r"\b[A-Z]{2}\d{2}[A-Za-z0-9]{11,30}\b", mask, text)
    return text


def redact_pinfl(text: str, mask: str = "[REDACTED]") -> str:
    """
    Redacts 14-digit Uzbek PINFL / JShShIR identifiers.
    Must not match phone numbers (which are 9 or 12 digits, or start with '+').
    """
    pinfl_pattern = r"(?<!\+)(?<!\d)[1-6]\d{13}(?!\d)"
    return re.sub(pinfl_pattern, mask, text)


def redact_passports(text: str, mask: str = "[REDACTED]") -> str:
    """
    Redacts Uzbek passport numbers (2 Latin letters + 7 digits, e.g. AA1234567, FA 7654321)
    and CIS passport series/numbers preceded by passport keywords.
    """
    # Uzbek passport: 2 letters + 7 digits (with optional space between letters and digits)
    text = re.sub(r"\b[A-Za-z]{2}\s*\d{7}\b", mask, text)

    # Explicit passport keywords followed by 10-digit series (4 digits + 6 digits)
    text = re.sub(
        r"(?i)(?:pasport|паспорт|passport)\s*[:=№#–-]?\s*(\d{4}\s*\d{6})",
        lambda m: m.group(0).replace(m.group(1), mask),
        text,
    )
    return text


def redact_otps(text: str, mask: str = "[REDACTED]") -> str:
    """
    Redacts one-time codes, PINs, passwords, and verification tokens across
    Uzbek Latin, Uzbek Cyrillic, Russian, and English.
    Preserves regular vocabulary words and numbers without code-like context.
    """
    otp_kws = [
        r"tasdiqlash\s+kodi(?:ngiz)?",
        r"bir\s+martalik\s+parol(?:ingiz)?",
        r"maxfiy\s+kod(?:ingiz)?",
        r"sms[-\s]?kod(?:ingiz)?",
        r"kod(?:ingiz)?",
        r"parol(?:ingiz)?",
        r"код\s+подтверждения",
        r"одноразовый\s+пароль",
        r"смс[-\s]?код",
        r"код",
        r"пароль",
        r"тасдиқлаш\s+коди",
        r"verification\s+code",
        r"one[-\s]?time\s+(?:code|password)",
        r"security\s+code",
        r"temp(?:orary)?\s+password",
        r"secret\s+code",
        r"password",
        r"otp",
        r"pin[-\s]?kod",
        r"пин[-\s]?код",
        r"pin",
        r"пин",
    ]
    kw_re = "|".join(otp_kws)

    # 1. Backward pattern: '84920 - tasdiqlash kodi' or '123456 sms kod'
    def _back_repl(m):
        code = m.group(1)
        return m.group(0).replace(code, mask)

    text = re.sub(
        rf"(?i)\b(\d{{4,8}})\b\s*(?:[-–=:]|\bbu\b)?\s*(?:boshqa\s+hech\s+kimga\s+bermang\s+)?(?:\b(?:{kw_re})\b)",
        _back_repl,
        text,
    )

    # 2. Forward pattern: keyword followed by separator and secret
    def _fwd_repl(m):
        full = m.group(0)
        sep = m.group(1) or ""
        secret = m.group(2)
        has_delim = bool(sep.strip() in (":", "=", "-"))
        has_digits = any(c.isdigit() for c in secret)

        # If pure alphabetic and no delimiter, treat as regular dictionary phrase
        if not has_digits and not has_delim:
            return full

        # Candidate secret identified
        if has_digits or has_delim:
            return full.replace(secret, mask)
        return full

    text = re.sub(
        rf"(?i)(?:\b(?:{kw_re})\b)\s*([:=–-]|is|\bbu\b)?\s*([A-Za-z0-9@#$%^&*!_-]{{3,25}})",
        _fwd_repl,
        text,
    )
    return text


def redact_text(text: Optional[str], mask: Optional[str] = None) -> str:
    """
    Main redaction entry point. Redacts payment cards, bank accounts,
    PINFL, passports, and one-time secrets while preserving dates, prices,
    quantities, and phone numbers.
    """
    if not text:
        return "" if text is None else text

    active_mask = mask or getattr(settings, "MEM_REDACTION_MASK", "[REDACTED]")

    result = text
    # 1. Cards (Luhn validated)
    result = redact_cards(result, active_mask)
    # 2. Bank Accounts & IBAN
    result = redact_bank_accounts(result, active_mask)
    # 3. PINFL (14 digits)
    result = redact_pinfl(result, active_mask)
    # 4. Passports
    result = redact_passports(result, active_mask)
    # 5. OTPs, passwords, and verification tokens
    result = redact_otps(result, active_mask)

    return result
