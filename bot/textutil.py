"""Small text helpers (HTML stripping, accent folding, date parsing)."""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)\b[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n+")


def strip_html(value: str | None) -> str:
    """Convert (possibly entity-escaped) HTML to plain text."""
    if not value:
        return ""
    text = html.unescape(value)
    if "&lt;" in text or "&gt;" in text:  # double-escaped (Greenhouse content)
        text = html.unescape(text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


def fold(value: str | None) -> str:
    """Lowercase and strip accents: 'Montréal' -> 'montreal'."""
    if not value:
        return ""
    nfkd = unicodedata.normalize("NFKD", value)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def normalize_words(value: str | None) -> str:
    text = fold(value).replace("&", " and ")
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(value: str | None, limit: int) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) <= limit:
        return value
    cut = value[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def parse_datetime(value) -> datetime | None:
    """Parse ISO strings, unix seconds or unix milliseconds into aware UTC datetimes."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            if ts <= 0:
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        text = str(value).strip()
        if re.fullmatch(r"\d{9,13}", text):
            return parse_datetime(int(text))
        text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
