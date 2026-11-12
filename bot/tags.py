"""Visual tags so Québec vs Ontario postings and winter / summer / fall terms are obvious at a glance."""
from __future__ import annotations

# Province -> (emoji, short label, embed colour)
PROVINCE_STYLE: dict[str, tuple[str, str, int]] = {
    "QC": ("🔵", "QC", 0x1F6FEB),
    "ON": ("🔴", "ON", 0xD1242F),
    "BC": ("🟢", "BC", 0x1A7F37),
    "AB": ("🟡", "AB", 0xBF8700),
}
OTHER_PROVINCE = ("⚪", None, 0x8C959F)
# Season (as stored in the DB) -> (emoji, French label)
SEASON_STYLE: dict[str, tuple[str, str]] = {
    "Winter": ("❄️", "Hiver"),
    "Spring": ("🌱", "Printemps"),
    "Summer": ("☀️", "Été"),
    "Fall": ("🍂", "Automne"),
}
SEASON_KEYS = {"hiver": "Winter", "printemps": "Spring", "ete": "Summer", "été": "Summer", "automne": "Fall"}


def province_tags(provinces: list[str], country: str | None, workplace: str | None = None) -> list[str]:
    tags = []
    for p in provinces[:3]:
        emoji, label, _ = PROVINCE_STYLE.get(p, OTHER_PROVINCE)
        tags.append(f"{emoji} {label or p}")
    if not tags:
        if country == "Canada":
            tags.append("🌐 Canada (télétravail)" if workplace == "remote" else "🍁 Canada")
        else:
            tags.append("❔ Lieu non précisé")
    return tags


def season_of(term: str | None) -> str | None:
    """'Summer 2027' -> 'Summer'; year-only or unknown -> None."""
    if not term:
        return None
    first = term.split()[0]
    return first if first in SEASON_STYLE else None


def season_tag(term: str | None) -> str:
    season = season_of(term)
    if season:
        emoji, label = SEASON_STYLE[season]
        return f"{emoji} {label} {term.split()[-1]}"
    if term and term.isdigit():
        return f"📅 {term}"
    return "❔ Session non précisée"


def tag_line(provinces: list[str], country: str | None, term: str | None, workplace: str | None = None) -> str:
    return " · ".join(province_tags(provinces, country, workplace) + [season_tag(term)])


def province_color(provinces: list[str], country: str | None) -> int:
    if provinces:
        return PROVINCE_STYLE.get(provinces[0], OTHER_PROVINCE)[2]
    return 0x8C959F
