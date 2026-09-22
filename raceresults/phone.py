"""Phone numbers: clean up what people type (or what Excel does to it) and validate for SMS."""

import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

_SEPARATORS = re.compile(r"[\s\-./()]")


def normalise(raw, region="GB"):
    """Return (e164, None) for a textable number, or (None, reason).

    Handles 07..., +44 (0)7..., 0044..., 447... and 10-digit 7... numbers whose
    leading zero was dropped by a spreadsheet (including "7400123456.0").
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None, "No phone number"
    cleaned = re.sub(r"\.0+$", "", text)        # 7400123456.0 from a spreadsheet
    cleaned = cleaned.replace("(0)", "")        # +44 (0)7400 ...
    cleaned = _SEPARATORS.sub("", cleaned)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not re.fullmatch(r"\+?\d{6,17}", cleaned):
        return None, f"Invalid phone number: {text}"
    try:
        number = phonenumbers.parse(cleaned, region or "GB")
    except NumberParseException:
        return None, f"Invalid phone number: {text}"
    if not phonenumbers.is_valid_number(number):
        return None, f"Invalid phone number: {text}"
    if phonenumbers.number_type(number) == PhoneNumberType.FIXED_LINE:
        return None, f"Landline, can't receive texts: {text}"
    return phonenumbers.format_number(number, PhoneNumberFormat.E164), None
