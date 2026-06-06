"""
utils/helpers.py - Shared formatting and utility helpers.
"""

from datetime import datetime, timezone
from typing import Optional


def format_subscription_end(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return iso_str


def days_remaining(iso_str: Optional[str]) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        diff = dt - datetime.now(timezone.utc)
        return max(0, diff.days)
    except Exception:
        return 0


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def truncate(text: str, max_len: int = 200) -> str:
    return text[:max_len] + "…" if len(text) > max_len else text
