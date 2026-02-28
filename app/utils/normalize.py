import re
from datetime import date, datetime


def normalize_phone(raw: str | None) -> str | None:
    """Strip to digits, prepend +1 if 10 digits."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return digits if digits else None


def normalize_name(raw: str | None) -> str:
    """Title-case and strip whitespace."""
    if not raw:
        return ""
    return " ".join(raw.strip().split()).title()


def days_since(date_str: str | None) -> int | None:
    """Calculate days between a date string and today. Tries multiple formats."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt).date()
            return (date.today() - dt).days
        except ValueError:
            continue
    return None


def is_lead_status(status_text: str | None) -> bool:
    """Check if a status string indicates lead/prospect (not active member)."""
    if not status_text:
        return False
    lower = status_text.lower().strip()
    return lower in {"lead", "prospect", "inquiry", "trial", "intro", "guest"}
