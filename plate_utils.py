from __future__ import annotations

import re
from datetime import datetime, timezone


_STRICT_PLATE_PATTERNS = (
    re.compile(r"^[A-Z]{4}[0-9]{3}$"),
    re.compile(r"^[A-Z]{2}[0-9]{5}$"),
)


def normalize_plate_read(value: object | None) -> str | None:
    if value is None:
        return None

    normalized = "".join(character for character in str(value).upper() if character.isalnum())
    if not normalized:
        return None

    if any(pattern.fullmatch(normalized) for pattern in _STRICT_PLATE_PATTERNS):
        return normalized
    return None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
