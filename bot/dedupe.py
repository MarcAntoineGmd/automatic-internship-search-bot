"""Stable keys used to recognise the same job across sources and over time."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .location import CITY_TO_PROVINCE
from .textutil import fold, normalize_words

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
TRACKING_PARAMS = {
    "ref", "source", "src", "gh_src", "lever-source", "lever-origin", "lever-source[]", "trk",
    "referrer", "utm", "sid", "jobboard", "board", "iis", "iisn", "codes", "mode", "jobpipeline",
}

COMPANY_ALIASES = {
    "rbc": "royal bank of canada", "rbc royal bank": "royal bank of canada",
    "bmo": "bank of montreal", "bmo financial group": "bank of montreal", "bmo bank of montreal": "bank of montreal",
    "td": "td bank", "td bank group": "td bank", "toronto dominion bank": "td bank", "the toronto dominion bank": "td bank",
    "cibc": "canadian imperial bank of commerce",
    "nbc": "national bank of canada", "bnc": "national bank of canada", "banque nationale": "national bank of canada",
    "banque nationale du canada": "national bank of canada",
    "scotiabank": "bank of nova scotia", "the bank of nova scotia": "bank of nova scotia",
    "desjardins group": "desjardins", "mouvement desjardins": "desjardins",
    "intact financial corporation": "intact", "intact financial": "intact",
    "warner bros games": "warner bros", "wb games": "warner bros", "warner bros discovery": "warner bros",
    "amazon web services": "amazon", "aws": "amazon", "google llc": "google", "alphabet": "google",
    "manulife financial": "manulife", "sun life financial": "sun life",
}
_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|plc|corp|corporation|co|company|group|holdings|"
    r"technologies|technology|labs|canada|ltee|the)\b"
)
_TITLE_NOISE = re.compile(
    r"\b(summer|fall|autumn|winter|spring|ete|automne|hiver|printemps|20\d\d|term|session|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|\d+ (months?|mois)|months?|mois|remote|hybrid|"
    r"canada|onsite|on site|starting|start|position|role|opening|"
    r"ontario|quebec|british columbia|alberta)\b"
)
_CITY_WORDS = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(CITY_TO_PROVINCE, key=len, reverse=True)) + r")\b")


def normalize_company(name: str | None) -> str:
    n = normalize_words(name)
    n = COMPANY_ALIASES.get(n, n)
    stripped = re.sub(r"\s+", " ", _COMPANY_SUFFIXES.sub(" ", n)).strip()
    stripped = COMPANY_ALIASES.get(stripped, stripped)
    return stripped or n


def normalize_title(title: str | None) -> str:
    t = fold(title)
    t = re.sub(r"\bco-?op\b|\bcoop\b", " coop ", t)
    t = re.sub(r"\binternships?\b|\binterns\b|\bstagiaire\b|\bstage\b", " intern ", t)
    t = normalize_words(t)
    t = _CITY_WORDS.sub(" ", t)
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"\b(and|or|the|a|an|of|en|de|du|la|le)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fingerprint_base(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def canonical_url_key(url: str | None) -> str | None:
    """Return an ATS-aware canonical key for an application URL."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = re.sub(r"/+$", "", parts.path)
    query = dict(parse_qsl(parts.query, keep_blank_values=False))
    qlower = {k.lower(): v for k, v in query.items()}

    if "gh_jid" in qlower and qlower["gh_jid"].isdigit():
        return f"greenhouse:{qlower['gh_jid']}"
    if "greenhouse.io" in host:
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"greenhouse:{m.group(1)}"
        if qlower.get("token", "").isdigit():
            return f"greenhouse:{qlower['token']}"
    if host.endswith("lever.co"):
        m = re.search(_UUID, path)
        if m:
            return f"lever:{m.group(0).lower()}"
    if host.endswith("ashbyhq.com"):
        m = re.search(_UUID, path)
        if m:
            return f"ashby:{m.group(0).lower()}"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        tenant = host.split(".")[0]
        last = path.split("/")[-1] if path else ""
        m = re.search(r"_([A-Za-z]*-?\d+)(?:-\d+)?$", last)
        if m:
            return f"workday:{tenant}:{m.group(1).upper()}"
    if "smartrecruiters.com" in host:
        m = re.search(r"/(\d{9,})", path)
        if m:
            return f"smartrecruiters:{m.group(1)}"

    kept = sorted((k, v) for k, v in query.items()
                  if not k.lower().startswith("utm") and k.lower() not in TRACKING_PARAMS)
    q = ("?" + urlencode(kept)) if kept else ""
    return f"url:{host}{path.lower() if host.endswith('github.com') else path}{q}"


def terms_compatible(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True
    a_year, b_year = a.split()[-1], b.split()[-1]
    if len(a.split()) == 1 or len(b.split()) == 1:  # year-only
        return a_year == b_year
    return a == b


def provinces_compatible(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return True
    return bool(set(a) & set(b))
