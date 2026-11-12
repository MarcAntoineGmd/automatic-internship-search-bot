"""Deterministic location parsing focused on Canada vs. elsewhere.

Handles the classic traps:
  * "San Jose, CA" is California, but "Toronto, ON, CA" is Canada.
  * "Vancouver, WA" is the US, "Vancouver, BC" is Canada.
  * "London, ON" is Canada, plain "London" is the UK.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textutil import fold

PROVINCE_NAMES: dict[str, list[str]] = {
    "QC": ["quebec", "province de quebec"],
    "ON": ["ontario"],
    "BC": ["british columbia", "colombie-britannique", "colombie britannique"],
    "AB": ["alberta"],
    "MB": ["manitoba"],
    "SK": ["saskatchewan"],
    "NS": ["nova scotia", "nouvelle-ecosse", "nouvelle ecosse"],
    "NB": ["new brunswick", "nouveau-brunswick", "nouveau brunswick"],
    "NL": ["newfoundland", "terre-neuve"],
    "PE": ["prince edward island", "ile-du-prince-edouard"],
    "YT": ["yukon"],
    "NT": ["northwest territories"],
    "NU": ["nunavut"],
}
PROVINCE_CODES = set(PROVINCE_NAMES)

# Canadian cities that are unambiguous enough to imply Canada on their own.
CITY_TO_PROVINCE: dict[str, str] = {
    # Québec
    "montreal": "QC", "quebec city": "QC", "ville de quebec": "QC", "laval": "QC",
    "gatineau": "QC", "sherbrooke": "QC", "longueuil": "QC", "brossard": "QC",
    "saint-laurent": "QC", "st-laurent": "QC", "levis": "QC", "trois-rivieres": "QC",
    "boucherville": "QC", "pointe-claire": "QC", "dorval": "QC", "terrebonne": "QC",
    "blainville": "QC", "mirabel": "QC", "saguenay": "QC", "rimouski": "QC",
    "drummondville": "QC", "granby": "QC", "saint-hyacinthe": "QC", "varennes": "QC",
    "kirkland": "QC", "boisbriand": "QC", "bromont": "QC", "candiac": "QC",
    # Ontario
    "toronto": "ON", "waterloo": "ON", "kitchener": "ON", "ottawa": "ON",
    "mississauga": "ON", "markham": "ON", "hamilton": "ON", "oakville": "ON",
    "burlington": "ON", "vaughan": "ON", "richmond hill": "ON", "guelph": "ON",
    "kanata": "ON", "brampton": "ON", "north york": "ON", "scarborough": "ON",
    "etobicoke": "ON", "thornhill": "ON", "pickering": "ON", "ajax": "ON",
    "whitby": "ON", "oshawa": "ON", "barrie": "ON", "sudbury": "ON",
    "thunder bay": "ON", "peterborough": "ON", "sarnia": "ON", "st. catharines": "ON",
    "milton": "ON", "newmarket": "ON", "concord, on": "ON",
    # British Columbia
    "vancouver": "BC", "burnaby": "BC", "north vancouver": "BC", "kelowna": "BC",
    "coquitlam": "BC", "new westminster": "BC", "nanaimo": "BC", "kamloops": "BC",
    "abbotsford": "BC",
    # Prairies / Atlantic
    "calgary": "AB", "edmonton": "AB", "lethbridge": "AB", "red deer": "AB",
    "winnipeg": "MB", "saskatoon": "SK", "regina": "SK",
    "halifax": "NS", "fredericton": "NB", "moncton": "NB", "saint john": "NB",
    "st. john's": "NL", "st john's": "NL", "charlottetown": "PE",
}
CITY_DISPLAY = {
    "montreal": "Montréal", "quebec city": "Québec City", "ville de quebec": "Québec City",
    "levis": "Lévis", "trois-rivieres": "Trois-Rivières",
}

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
}
US_WORDS = [
    "united states", "usa", "u.s.", "u.s.a", "california", "new york", "texas", "washington",
    "massachusetts", "illinois", "georgia", "colorado", "virginia", "florida", "oregon",
    "pennsylvania", "new jersey", "north carolina", "michigan", "minnesota", "arizona", "utah",
    "ohio", "maryland", "tennessee", "wisconsin", "connecticut", "missouri", "indiana",
    "san francisco", "seattle", "austin", "boston", "chicago", "los angeles", "palo alto",
    "mountain view", "menlo park", "sunnyvale", "san jose", "santa clara", "redmond",
    "bellevue", "atlanta", "denver", "pittsburgh", "philadelphia", "miami", "dallas",
    "houston", "san diego", "irvine", "salt lake city", "raleigh", "nashville", "phoenix",
    "detroit", "minneapolis", "foster city", "san mateo", "redwood city", "cupertino",
    "brooklyn", "manhattan", "jersey city", "ann arbor", "madison", "durham", "plano",
]
FOREIGN_WORDS = [
    "united kingdom", "england", "scotland", "ireland", "dublin", "germany", "berlin",
    "munich", "france", "paris", "netherlands", "amsterdam", "switzerland", "zurich",
    "sweden", "stockholm", "spain", "madrid", "barcelona", "portugal", "lisbon", "poland",
    "warsaw", "krakow", "israel", "tel aviv", "india", "bangalore", "bengaluru", "hyderabad",
    "pune", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "chennai", "singapore",
    "japan", "tokyo", "korea", "seoul", "australia", "sydney", "melbourne", "china",
    "shanghai", "beijing", "shenzhen", "hong kong", "taiwan", "taipei", "mexico", "brazil",
    "sao paulo", "argentina", "buenos aires", "colombia", "bogota", "philippines", "manila",
    "vietnam", "romania", "bucharest", "czech", "prague", "denmark", "copenhagen", "finland",
    "helsinki", "norway", "oslo", "italy", "milan", "belgium", "brussels", "austria",
    "vienna", "uae", "dubai", "emea", "apac", "latam", "europe", "costa rica", "chile",
    "south africa", "nigeria", "egypt", "turkey", "istanbul", "malaysia", "indonesia",
    "new zealand", "london", "uk",
]

_SPLIT_RE = re.compile(r"\s*(?:;|\||\n|<br\s*/?>|</br>| / | or |\s•\s)\s*", re.I)
_PROV_CODE_RE = re.compile(r"(?:^|[,(\-–\s])(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)(?=$|[\s,)\-–.])")
_US_CODE_RE = re.compile(r",\s*([A-Z]{2})(?=$|[\s,)\-–.])")
_REMOTE_RE = re.compile(r"\b(remote|teletravail|work from home|wfh|anywhere|distributed|virtual)\b")
_HYBRID_RE = re.compile(r"\b(hybrid|hybride)\b")
_ONSITE_RE = re.compile(r"\b(on-?site|in[- ]office|sur place|en personne|in person)\b")


def _word_in(word: str, text: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text) is not None


@dataclass
class LocationInfo:
    country: str = "Unknown"  # "Canada" | "Foreign" | "Unknown"
    provinces: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    remote: bool = False
    remote_scope: str | None = None  # canada | north_america | global | us | foreign | None
    workplace: str | None = None

    @property
    def is_canada(self) -> bool:
        return self.country == "Canada"


@dataclass
class _Segment:
    canada: bool = False
    foreign: bool = False
    us: bool = False
    remote: bool = False
    north_america: bool = False
    global_remote: bool = False
    provinces: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)


def _parse_segment(raw: str) -> _Segment:
    seg = _Segment()
    text = fold(raw)
    if not text.strip():
        return seg

    # Canadian province codes (case-sensitive on original text).
    for m in _PROV_CODE_RE.finditer(raw):
        code = m.group(1)
        after = raw[m.end(1): m.end(1) + 5].upper()
        if code == "ON" and re.match(r"[\s-]?SITE", after):
            continue  # "ON-SITE"
        seg.provinces.append(code)
    for code, names in PROVINCE_NAMES.items():
        for name in names:
            if _word_in(name, text):
                # "New Brunswick, NJ" is a US city, ignore when a US state code follows.
                if code == "NB" and re.search(r"new brunswick,\s*nj", text):
                    continue
                seg.provinces.append(code)
    if "canada" in text or "canadian" in text:
        seg.canada = True

    us_codes = [c for c in _US_CODE_RE.findall(raw) if c in US_STATE_CODES]
    # "Toronto, ON, CA": trailing CA after a Canadian province code means Canada.
    if us_codes and (seg.provinces or seg.canada):
        us_codes = [c for c in us_codes if c != "CA"]
    for city, prov in CITY_TO_PROVINCE.items():
        if _word_in(city, text):
            if us_codes:  # e.g. "Vancouver, WA"
                continue
            if city == "waterloo" and re.search(r"waterloo,\s*(ia|iowa)", text):
                continue
            seg.provinces.append(prov)
            seg.cities.append(CITY_DISPLAY.get(city, city.title()))

    if seg.provinces:
        seg.canada = True
    if us_codes and not seg.canada:
        seg.us = True
    if not seg.canada:
        if any(_word_in(w, text) for w in US_WORDS) or re.search(r"(?<![a-z])us(?![a-z])", text):
            seg.us = True
        elif re.search(r"\bUS\b", raw):
            seg.us = True
        if any(_word_in(w, text) for w in FOREIGN_WORDS):
            seg.foreign = True
    if seg.us:
        seg.foreign = True

    if _REMOTE_RE.search(text):
        seg.remote = True
    if "north america" in text or "americas" in text:
        seg.north_america = True
    if seg.remote and re.search(r"\b(anywhere|worldwide|global|globally)\b", text):
        seg.global_remote = True
    # Dedupe preserving order
    seg.provinces = list(dict.fromkeys(seg.provinces))
    seg.cities = list(dict.fromkeys(seg.cities))
    return seg


def split_locations(raw: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(raw or "") if p and p.strip()]
    return parts or ([raw.strip()] if raw and raw.strip() else [])


def parse_location(
    raw: str | list[str] | None,
    countries: list[str] | None = None,
    workplace: str | None = None,
    province_priority: list[str] | None = None,
) -> LocationInfo:
    """Parse a raw location (string or list of strings) plus structured country hints."""
    if isinstance(raw, list):
        segments_raw: list[str] = []
        for item in raw:
            segments_raw.extend(split_locations(item))
    else:
        segments_raw = split_locations(raw or "")

    segs = [_parse_segment(s) for s in segments_raw]
    info = LocationInfo()
    full_text = fold(" | ".join(segments_raw))

    provinces: list[str] = []
    cities: list[str] = []
    any_canada = any_foreign = any_remote = any_na = any_global = False
    unknown_segments = 0
    for s in segs:
        provinces.extend(s.provinces)
        cities.extend(s.cities)
        any_canada |= s.canada
        any_foreign |= s.foreign
        any_remote |= s.remote
        any_na |= s.north_america
        any_global |= s.global_remote
        if not (s.canada or s.foreign or s.north_america or s.global_remote):
            unknown_segments += 1

    for c in countries or []:
        fc = fold(c).strip()
        if fc in ("canada", "ca", "can"):
            any_canada = True
        elif fc:
            any_foreign = True

    info.provinces = _order_provinces(list(dict.fromkeys(provinces)), province_priority)
    info.cities = list(dict.fromkeys(cities))
    info.remote = any_remote or (workplace or "").lower() == "remote"

    if any_canada:
        info.country = "Canada"
    elif any_global:
        info.country = "Unknown"
    elif any_foreign and not any_na:
        info.country = "Foreign"
    else:
        info.country = "Unknown"

    if info.remote:
        if any_canada and (not info.provinces or any(s.remote and s.canada for s in segs)):
            info.remote_scope = "canada"
        elif any_canada:
            info.remote_scope = "canada"
        elif any_global:
            info.remote_scope = "global"
        elif any_na:
            info.remote_scope = "north_america"
        elif any_foreign:
            info.remote_scope = "us" if any(s.us for s in segs) else "foreign"
    elif any_na and not any_canada:
        info.remote_scope = "north_america"

    wp = (workplace or "").lower() or None
    if wp in ("remote", "hybrid", "onsite"):
        info.workplace = wp
    elif _HYBRID_RE.search(full_text):
        info.workplace = "hybrid"
    elif info.remote:
        info.workplace = "remote"
    elif _ONSITE_RE.search(full_text):
        info.workplace = "onsite"
    return info


def _order_provinces(provs: list[str], priority: list[str] | None) -> list[str]:
    if not priority:
        return provs
    rank = {p: i for i, p in enumerate(priority)}
    return sorted(provs, key=lambda p: (rank.get(p, len(rank)), provs.index(p)))


def text_mentions_canada(text: str) -> bool:
    t = fold(text)
    if re.search(r"\bcanada\b|\bcanadian\b", t):
        return True
    return any(_word_in(c, t) for c in ("toronto", "montreal", "waterloo", "ottawa", "vancouver", "quebec city"))
