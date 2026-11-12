"""Deterministic relevance filtering and scoring (no LLM).

Pipeline for one job:
  1. Internship / co-op / (optional) new-grad detection.
  2. Role category from the title (+ description fallback for unusual titles).
  3. Location (Canada / Québec / Ontario priority, remote eligibility).
  4. Term detection (Summer 2027 highest priority).
  5. Score 0-100 + exclusion decision. Uncertain jobs are kept with a lower score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .location import LocationInfo, parse_location, text_mentions_canada
from .models import Classification, Job
from .textutil import fold, utcnow

# --------------------------------------------------------------------------- internship
INTERN_TITLE_RE = re.compile(
    r"\b(intern|interns|internship|internships|co-?op|coop|stagiaire|apprentice|apprenticeship|"
    r"working student|student|students|trainee|industrial placement|placement year|pey|"
    r"summer analyst|summer associate|alternance|alternant)\b"
    r"|\bstage\s*(?:en|d['’]|de|ete|hiver|automne|printemps|\d|[-–(:])"
    r"|[-–(,/|]\s*stage\b",
    re.I,
)
COOP_RE = re.compile(r"\b(co-?op|coop)\b", re.I)
NEW_GRAD_RE = re.compile(
    r"\b(new[\s-]?grad(uate)?s?|recent graduate|entry[\s-]level|early career|university graduate|"
    r"graduate (program|programme|software|developer|engineer)|nouveaux? dipl[oô]m[ée]s?|finissant)\b",
    re.I,
)
EMPLOYMENT_INTERN_RE = re.compile(r"intern|co-?op|stage|stagiaire|student|apprentice|trainee", re.I)
DESC_INTERN_RE = re.compile(
    r"\b(this (is an?|internship|co-?op)|our (internship|co-?op) program|internship (program|position|opportunity|term)|"
    r"co-?op (term|position|placement|program|student|opportunity)|work term|summer student|"
    r"students? currently enrolled|currently enrolled|returning to (school|studies|university)|"
    r"stage (de|d['’]|en)|stagiaires?)\b"
    r"|\b(a|an|our|paid|this)\s+(?:[\w-]+\s+){0,3}(internship|co-?op|work term)\b"
    r"|\bfor (current |enrolled )?(university|college|post-secondary|undergraduate) students\b",
    re.I,
)
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|chief|"
    r"architect|distinguished|fellow|supervisor|superviseur|gestionnaire|directeur|directrice|"
    r"experienced|expert)\b|\b(ii|iii|iv)\b",
    re.I,
)
YEARS_EXP_RE = re.compile(r"\b([3-9]|1\d)\+?\s*(?:-\s*\d+\s*)?(?:years|yrs|ans)\b[^.\n]{0,40}\bexperience", re.I)

# --------------------------------------------------------------------------- categories
# (category, weight, regex) evaluated in order. Strong software terms win over exclusions.
STRONG_CATEGORIES: list[tuple[str, int, re.Pattern]] = [
    ("software", 45, re.compile(
        r"\b(software|swe|sde|developer|developers|development engineer|backend|back-end|back end|"
        r"frontend|front-end|front end|full[\s-]?stack|web (developer|engineer|development)|"
        r"mobile (developer|engineer|development)|ios|android|application (developer|engineer)|"
        r"programmer|programmeur|programmeuse|programming|gameplay|d[ée]veloppeu(r|se)|"
        r"d[ée]veloppement (logiciel|web|d['’]applications|informatique)|logiciel|"
        r"g[ée]nie logiciel|engineer,? (software|backend|frontend|full[\s-]?stack|platform|infrastructure)|"
        r"application development|applications development|app development|systems development|"
        r"engineering (and|&) data|dev degree|developer degree)\b",
        re.I)),
    ("platform", 40, re.compile(
        r"\b(platform engineer(ing)?|infrastructure|cloud (engineer|engineering|developer|administrator|infrastructure)|devops|"
        r"development operations|"
        r"dev ops|devsecops|site reliability|sre|systems? software|embedded|firmware|kernel|"
        r"compiler|distributed systems|reliability engineer|network software|robotics software|"
        r"graphics (programmer|engineer)|rendering engineer|production engineer|build engineer|"
        r"release engineer|tools engineer|systems engineer(ing)? intern)\b", re.I)),
    ("ai_ml", 40, re.compile(
        r"\b((machine learning|ml|ai|artificial intelligence|deep learning|computer vision|nlp|llm|"
        r"genai|generative ai|mlops) (engineer|engineering|developer|software)|mlops|"
        r"ai/ml engineer|ml engineer|ai engineer)\b", re.I)),
    ("data_engineering", 40, re.compile(
        r"\b(data engineer(ing)?|data platform|data infrastructure|etl developer|analytics engineer)\b", re.I)),
    ("security", 40, re.compile(
        r"\b((security|cybersecurity|cyber security|application security|appsec|product security|"
        r"cloud security|offensive security|detection) (engineer|engineering|developer|software)|"
        r"security engineer)\b", re.I)),
    ("security", 32, re.compile(
        r"\b(cybersecurity|cyber security|security operations|soc analyst|security analyst|"
        r"cyber[- ]?s[ée]curit[ée])\b", re.I)),
    ("ai_ml", 34, re.compile(
        r"\b(ai|ml|machine learning|artificial intelligence|deep learning|computer vision|nlp|llm)\s*"
        r"(/\s*ml\s*)?(intern|interns|internship|co-?op|student|research intern)\b", re.I)),
    ("qa", 32, re.compile(
        r"\b(sdet|test automation|qa automation|automation (engineer|developer)|software (test|quality)|"
        r"quality assurance|qa (engineer|analyst|developer|intern|co-?op)|test (engineer|developer))\b", re.I)),
]
EXCLUDE_CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("it_support", re.compile(
        r"\b((it|technical|tech|customer|desktop|end[- ]user|application) support|support (analyst|specialist|"
        r"technician|agent|associate|engineer)|help ?desk|service ?desk|technicien(ne)?|technician|"
        r"soutien technique|support informatique)\b", re.I)),
    ("hardware", re.compile(
        r"\b(asic|fpga|rtl|vlsi|analog|mixed[- ]signal|layout|pcb|hardware|electrical|mechanical|"
        r"mechatronics|civil|chemical|process engineer|manufacturing|industrial engineer(ing)?|structural|"
        r"rf|antenna|optical|photonics|materials|aerospace|biomedical|quality engineer|"
        r"[ée]lectrique|m[ée]canique|design verification|silicon|semiconductor|packaging|"
        r"power electronics|circuit|thermal|ic|soc|ic design|digital design|serdes|validation engineer|"
        r"system validation|camera design|controls engineer|certification engineer|applications engineer(ing)?|"
        r"hvp|battery|test technician)\b", re.I)),
    ("analytics", re.compile(
        r"\b(data analyst|business intelligence|bi analyst|reporting analyst|analytics (intern|co-?op|analyst)|"
        r"insights analyst|analyste de donn[ée]es|data analytics|business analyst|business systems analyst|"
        r"credit risk|risk analytics|market risk|model validation|market insights|quantitative (equity |research )?analyst|"
        r"equity analyst|business management|project analyst|"
        r"analyste d['’]affaires|financial analyst|risk analyst|credit analyst|investment analyst)\b", re.I)),
    ("product_design", re.compile(
        r"\b(product manag\w*|program manag\w*|project manag\w*|product owner|chef de produit|"
        r"gestion de projet|scrum master|product (marketing|operations|analyst)|"
        r"(ux|ui|product|graphic|visual|interaction|content|game|level|motion|industrial) design(er)?|"
        r"designer|design intern|technical writer|r[ée]dact)\b", re.I)),
    ("non_engineering", re.compile(
        r"\b(sales|vente|ventes|account (executive|manager)|business development|bdr|sdr|marketing|"
        r"communications?|social media|public relations|brand|recruit\w*|talent acquisition|"
        r"human resources|hr|people (operations|partner)|ressources humaines|accounting|accountant|"
        r"finance|audit|tax|treasury|actuarial|comptab\w*|investment banking|wealth management|"
        r"underwriting|sales & trading|sales and trading|trading|trader|equity research|private equity|"
        r"asset management|legal|lawyer|paralegal|counsel|juridique|compliance|operations|"
        r"supply chain|logistics|procurement|customer success|customer service|consultant|"
        r"consulting|strategy|strat[ée]gie|administrative|administrator|assistant|coordinator|"
        r"coordonnat\w*|receptionist|event|policy|merchandising|retail|store|warehouse|driver|"
        r"nurse|clinical|pharmac\w*|teacher|tutor|journalis\w*|editor|copywriter|photograph\w*|"
        r"video|animator|artist|art|audio|sound|music|qa tester|game tester|localization tester|"
        r"real estate|insurance|claims|banking associate|relationship)\b", re.I)),
]
MEDIUM_CATEGORIES: list[tuple[str, int, re.Pattern]] = [
    ("quant_dev", 32, re.compile(r"\b(quantitative|quant) (developer|engineer|software|technologist)\b", re.I)),
    ("data_science", 28, re.compile(
        r"\b(data scien\w*|machine learning|ml|ai|artificial intelligence|computer vision|deep learning|"
        r"nlp|llm|intelligence artificielle|apprentissage automatique)\b", re.I)),
    ("security", 28, re.compile(r"\b(security|cyber\w*|s[ée]curit[ée])\b", re.I)),
    ("research", 24, re.compile(r"\b(research engineer|research scientist|research intern|researcher|research)\b", re.I)),
    ("general", 26, re.compile(
        r"\b(technology (analyst|associate|intern|co-?op|program|developer)|tech analyst|it developer|"
        r"computer (science|engineering)|informatique|automation|robotics|solutions? engineer|"
        r"integration engineer|systems engineer|devops|product engineer|technologist|"
        r"d[ée]veloppement|programmation|linux|unix)\b", re.I)),
    ("general", 8, re.compile(r"\b(engineer|engineering|ing[ée]nieur\w*|g[ée]nie|technolog\w*|tech|it|digital|data|systems?|technical)\b", re.I)),
]
SWE_DESC_SIGNALS = [
    r"\bpython\b", r"\bjava\b", r"\bc\+\+", r"\bc#", r"\bjavascript\b", r"\btypescript\b", r"\breact\b",
    r"\bnode(\.js)?\b", r"\bgolang\b", r"\brust\b", r"\bkotlin\b", r"\bswift\b", r"\bsql\b", r"\bgit\b",
    r"\brest(ful)? apis?\b", r"\bapis?\b", r"\bmicroservices?\b", r"\bkubernetes\b", r"\bdocker\b",
    r"\baws\b", r"\bazure\b", r"\bgcp\b", r"\bci/cd\b", r"\blinux\b", r"\balgorithms?\b",
    r"\bdata structures\b", r"\bobject[- ]oriented\b", r"software development", r"write (clean )?code",
    r"\bcoding\b", r"\bunit tests?\b", r"\bcode reviews?\b", r"\bbackend\b", r"\bfrontend\b",
    r"\bfull[- ]?stack\b", r"\bdistributed systems\b", r"d[ée]veloppement logiciel",
]
SIMPLIFY_CATEGORY_HINTS = {
    "software": ("software", 34),
    "software engineering": ("software", 34),
    "ai/ml/data": ("data_science", 26),
    "data science, ai & machine learning": ("data_science", 26),
    "quant": ("quant_dev", 12),
    "hardware": ("hardware", 0),
    "product": ("product_design", 0),
    "product management": ("product_design", 0),
}

UNPAID_RE = re.compile(
    r"\b(unpaid|non[- ]r[ée]mun[ée]r[ée]e?|b[ée]n[ée]vole|volunteer (position|role|opportunity)|"
    r"no (compensation|salary|pay)|for (academic )?credit only|commission[- ]only)\b",
    re.I,
)
PHD_RE = re.compile(r"\b(ph\.?d\.?|doctoral|doctorate)\b", re.I)
MASTERS_RE = re.compile(r"\b(master'?s|msc|m\.sc|mba)\b", re.I)
MBA_RE = re.compile(r"\bmba\b", re.I)

ELIGIBILITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Open to currently enrolled students", re.compile(
        r"\b(currently enrolled|enrolled (full[- ]time )?in an? (accredited )?(university|college|post-secondary|degree)|"
        r"returning to (school|studies|university)|must be a (current )?student|co-?op program|"
        r"inscrit(e)? (à|a) (temps plein|l['’]universit[ée]))\b", re.I)),
    ("Must be eligible to work in Canada", re.compile(
        r"\b(eligible|authori[sz]ed|entitled|legally able) to work in canada\b", re.I)),
    ("Requires US work authorization", re.compile(
        r"\b(authori[sz]ed to work in the (u\.?s\.?|united states)|u\.?s\.? citizen(ship)?|us person|"
        r"green card|security clearance)\b", re.I)),
    ("Canadian citizenship/PR may be required", re.compile(
        r"\b(canadian citizen(ship)?|permanent resident)\b", re.I)),
]

# --------------------------------------------------------------------------- terms
SEASON_MAP = {
    "summer": "Summer", "ete": "Summer", "fall": "Fall", "autumn": "Fall", "automne": "Fall",
    "winter": "Winter", "hiver": "Winter", "spring": "Spring", "printemps": "Spring",
}
_SEASON_WORDS = "summer|fall|autumn|winter|spring|ete|automne|hiver|printemps"
SEASON_CHAIN_RE = re.compile(
    rf"\b((?:(?:{_SEASON_WORDS})\s*(?:/|,|&|\+|and|or|et|ou|-|–)?\s*)+)(?:term\s*|semester\s*|session\s*|of\s*|de\s*|d['’]\s*)?(20\d\d)\b"
)
YEAR_SEASON_RE = re.compile(rf"\b(20\d\d)\s*(?:-\s*)?({_SEASON_WORDS})\b")
MONTHS = {
    "january": 1, "jan": 1, "janvier": 1, "february": 2, "feb": 2, "fevrier": 2, "march": 3, "mar": 3,
    "mars": 3, "april": 4, "apr": 4, "avril": 4, "may": 5, "mai": 5, "june": 6, "jun": 6, "juin": 6,
    "july": 7, "jul": 7, "juillet": 7, "august": 8, "aug": 8, "aout": 8, "september": 9, "sep": 9,
    "sept": 9, "septembre": 9, "october": 10, "oct": 10, "octobre": 10, "november": 11, "nov": 11,
    "novembre": 11, "december": 12, "dec": 12, "decembre": 12,
}
MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?,?\s+(20\d\d)\b")
START_CONTEXT_RE = re.compile(r"(start|starting|begin|beginning|commenc|from|d[ée]but|a partir|term|stage|internship|co-?op)")
YEAR_ONLY_RE = re.compile(r"(?<![\d/-])(20[2-3]\d)(?![\d/-])")
SEASON_START_MONTH = {"Winter": 1, "Spring": 3, "Summer": 5, "Fall": 9}


def _month_to_season(month: int) -> str:
    if month <= 4:
        return "Winter"
    if month <= 8:
        return "Summer"
    return "Fall"


def normalize_term(value: str) -> str | None:
    """'summer 2027' / 'Été 2027' / '2027 Summer' -> 'Summer 2027'."""
    found = detect_terms_in_text(value, allow_month=True, allow_year_only=False)
    return found[0] if found else None


def detect_terms_in_text(text: str, allow_month: bool = True, allow_year_only: bool = False,
                         require_start_context: bool = False) -> list[str]:
    if not text:
        return []
    t = fold(text)
    out: list[str] = []
    for m in SEASON_CHAIN_RE.finditer(t):
        year = m.group(2)
        for word in re.findall(_SEASON_WORDS, m.group(1)):
            out.append(f"{SEASON_MAP[word]} {year}")
    for m in YEAR_SEASON_RE.finditer(t):
        out.append(f"{SEASON_MAP[m.group(2)]} {m.group(1)}")
    if allow_month:
        for m in MONTH_YEAR_RE.finditer(t):
            if require_start_context:
                ctx = t[max(0, m.start() - 45): m.start()]
                if not START_CONTEXT_RE.search(ctx):
                    continue
            out.append(f"{_month_to_season(MONTHS[m.group(1)])} {m.group(2)}")
    if allow_year_only and not out:
        years = YEAR_ONLY_RE.findall(t)
        out.extend(years)
    return list(dict.fromkeys(out))


def term_start(term: str) -> datetime | None:
    parts = term.split()
    try:
        if len(parts) == 2:
            return datetime(int(parts[1]), SEASON_START_MONTH[parts[0]], 1, tzinfo=utcnow().tzinfo)
        if len(parts) == 1:
            return datetime(int(parts[0]), 12, 31, tzinfo=utcnow().tzinfo)
    except (KeyError, ValueError):
        return None
    return None


# --------------------------------------------------------------------------- settings view
@dataclass
class FilterConfig:
    target_provinces: list[str] = field(default_factory=lambda: ["QC", "ON"])
    target_cities: list[str] = field(default_factory=lambda: ["Montreal", "Quebec City", "Toronto", "Waterloo", "Ottawa"])
    target_terms: list[str] = field(default_factory=lambda: ["Summer 2027", "Fall 2027", "Winter 2027"])
    included_categories: list[str] = field(default_factory=lambda: [
        "software", "platform", "ai_ml", "data_engineering", "security", "qa", "quant_dev",
        "data_science", "research", "general"])
    excluded_keywords: list[str] = field(default_factory=lambda: ["unpaid", "volunteer", "bénévole"])
    include_new_grad: bool = False
    allow_unpaid: bool = False
    include_unknown_location: bool = True
    min_score: int = 40

    def signature(self) -> str:
        return "|".join([
            ",".join(self.target_provinces), ",".join(self.target_cities), ",".join(self.target_terms),
            ",".join(sorted(self.included_categories)), ",".join(sorted(self.excluded_keywords)),
            str(self.include_new_grad), str(self.allow_unpaid), str(self.include_unknown_location),
            str(self.min_score), "v1",
        ])


def priority_label(score: int) -> str:
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    return "Low"


# --------------------------------------------------------------------------- main entry
def _role_category(title: str, description: str, category_hint: str | None) -> tuple[str | None, int, list[str], bool]:
    """Return (category, weight, reasons, excluded_by_title)."""
    reasons: list[str] = []
    for cat, weight, rx in STRONG_CATEGORIES:
        if rx.search(title):
            # A strong title can still be a hardware role ("Hardware Developer")
            if cat in ("software", "platform") and re.search(r"\b(hardware|asic|fpga|rtl|analog|pcb)\b", title, re.I) \
                    and not re.search(r"\b(software|firmware|embedded software)\b", title, re.I):
                return "hardware", 0, ["hardware role"], True
            return cat, weight, [f"title matches {cat}"], False
    for cat, rx in EXCLUDE_CATEGORIES:
        if rx.search(title):
            return cat, 0, [f"title indicates {cat.replace('_', ' ')}"], True
    best_cat, best_w = None, 0
    for cat, weight, rx in MEDIUM_CATEGORIES:
        if rx.search(title) and weight > best_w:
            best_cat, best_w = cat, weight
    if best_cat:
        reasons.append(f"title suggests {best_cat}")
    if category_hint:
        hint = SIMPLIFY_CATEGORY_HINTS.get(category_hint.strip().lower())
        if hint:
            hcat, hw = hint
            if hw == 0 and not best_cat:
                return hcat, 0, [f"source category {category_hint}"], True
            if hw > best_w:
                best_cat, best_w = hcat, hw
                reasons.append(f"source category {category_hint}")
    # Unusual title: look at description for clear software-engineering work.
    if description and best_w < 30:
        d = description.lower()
        hits = sum(1 for p in SWE_DESC_SIGNALS if re.search(p, d))
        if hits >= 4:
            desc_w = 30 if hits >= 7 else 24
            if desc_w > best_w:
                best_cat = best_cat if best_cat not in (None, "general") else "software"
                best_w = desc_w
                reasons.append(f"description shows software work ({hits} signals)")
    return best_cat, best_w, reasons, False


def classify(job: Job, cfg: FilterConfig, now: datetime | None = None) -> Classification:
    now = now or utcnow()
    title = job.title or ""
    desc = job.description or ""
    reasons: list[str] = []
    excluded: str | None = None

    loc: LocationInfo = parse_location(
        job.location, countries=job.countries, workplace=job.workplace, province_priority=cfg.target_provinces
    )
    if loc.country == "Unknown" and job.assume_country and fold(job.assume_country) == "canada":
        loc.country = "Canada"
        if loc.remote and not loc.remote_scope:
            loc.remote_scope = "canada"
        reasons.append("board is Canada-only")
    elif loc.country == "Unknown" and desc and text_mentions_canada(desc[:3000]) and not loc.remote_scope == "us":
        loc.country = "Canada"
        reasons.append("description mentions Canada")

    # ---- internship detection
    title_intern = bool(INTERN_TITLE_RE.search(title))
    emp_intern = bool(job.employment_type and EMPLOYMENT_INTERN_RE.search(job.employment_type))
    is_new_grad = bool(NEW_GRAD_RE.search(title))
    senior = bool(SENIOR_RE.search(title))
    internship_type: str | None = None
    intern_points = 0
    if title_intern:
        intern_points = 20
        reasons.append("internship/co-op in title")
    elif emp_intern:
        intern_points = 18
        reasons.append(f"employment type '{job.employment_type}'")
    elif job.assume_internship:
        intern_points = 15
        reasons.append("listed by internship tracker")
    elif desc and not senior and DESC_INTERN_RE.search(desc) and not YEARS_EXP_RE.search(desc):
        intern_points = 10
        reasons.append("description describes an internship")
    if intern_points:
        blob = f"{title} {job.employment_type or ''}"
        if COOP_RE.search(blob):
            internship_type = "co-op"
        elif re.search(r"\bstudent\b", blob, re.I) and not re.search(r"\bintern", blob, re.I):
            internship_type = "student"
        else:
            internship_type = "internship"
    elif is_new_grad and not senior:
        if cfg.include_new_grad:
            internship_type = "new_grad"
            intern_points = 10
            reasons.append("new-grad role (optional category enabled)")
        else:
            excluded = "new-grad role (category disabled)"
    if not intern_points and not excluded:
        excluded = "senior/experienced role" if senior else "not an internship/co-op"
    if senior and not title_intern and not excluded:
        excluded = "senior/experienced role"

    # ---- role
    category, role_w, role_reasons, title_excluded = _role_category(title, desc, job.category_hint)
    reasons.extend(role_reasons)
    if not excluded:
        if title_excluded:
            excluded = f"unrelated role ({category.replace('_', ' ') if category else 'non-engineering'})"
        elif category is None or role_w == 0:
            excluded = "no software-engineering signal"
        elif category not in cfg.included_categories:
            excluded = f"category '{category}' not included"
    if MBA_RE.search(title) and not excluded:
        excluded = "MBA role"

    # ---- keywords / pay
    lowered = fold(f"{title} {desc[:4000]}")
    for kw in cfg.excluded_keywords:
        k = fold(kw).strip()
        if k and re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", fold(title)):
            excluded = excluded or f"excluded keyword '{kw}'"
    if not cfg.allow_unpaid and UNPAID_RE.search(lowered):
        excluded = excluded or "unpaid position"

    # ---- location
    loc_points = 0
    target_cities_f = [fold(c) for c in cfg.target_cities]
    if loc.country == "Canada":
        if loc.provinces and loc.provinces[0] in cfg.target_provinces:
            loc_points = 20
            reasons.append(f"located in {loc.provinces[0]}")
            if any(fold(c) in target_cities_f or fold(c).replace("é", "e") in target_cities_f for c in loc.cities):
                loc_points += 3
        elif loc.provinces:
            loc_points = 12
            reasons.append(f"Canada ({loc.provinces[0]})")
        elif loc.remote:
            loc_points = 15
            reasons.append("remote in Canada")
        else:
            loc_points = 12
            reasons.append("Canada (province unknown)")
    elif loc.country == "Foreign":
        excluded = excluded or "outside Canada"
    else:
        if loc.remote_scope == "global":
            loc_points = 6
            reasons.append("remote, worldwide")
        elif loc.remote_scope == "north_america":
            loc_points = 6
            reasons.append("remote, North America")
        elif not cfg.include_unknown_location:
            excluded = excluded or "location unknown"
        else:
            loc_points = -12
            reasons.append("location unknown")

    # ---- terms
    terms: list[str] = []
    for t in job.terms_hint:
        nt = normalize_term(t)
        if nt:
            terms.append(nt)
    terms += detect_terms_in_text(title, allow_month=True, allow_year_only=False)
    if desc:
        terms += detect_terms_in_text(desc[:6000], allow_month=True, require_start_context=True)
    terms = list(dict.fromkeys(terms))
    year_only = detect_terms_in_text(title, allow_month=False, allow_year_only=True) if not terms else []
    target_terms = [normalize_term(t) or t for t in cfg.target_terms]
    term_points = 3
    primary_term: str | None = None
    if terms:
        ranked = sorted(terms, key=lambda t: target_terms.index(t) if t in target_terms else 99)
        primary_term = ranked[0]
        if primary_term in target_terms:
            idx = target_terms.index(primary_term)
            term_points = 12 if idx == 0 else 8
            reasons.append(f"term {primary_term}")
        else:
            start = term_start(primary_term)
            if start and (now - start).days > 45:
                term_points = -15
                reasons.append(f"term {primary_term} already started")
            else:
                term_points = 5
                reasons.append(f"term {primary_term}")
    elif year_only:
        primary_term = year_only[0]
        target_years = {t.split()[-1] for t in target_terms}
        term_points = 6 if primary_term in target_years else (-10 if int(primary_term) < now.year else 3)

    # ---- eligibility
    elig: list[str] = []
    student_bonus = 0
    for label, rx in ELIGIBILITY_PATTERNS:
        if rx.search(desc[:8000]):
            elig.append(label)
    penalty = 0
    if "Open to currently enrolled students" in elig:
        student_bonus = 4
    if "Requires US work authorization" in elig and loc.country != "Canada":
        excluded = excluded or "requires US work authorization"
    elif "Requires US work authorization" in elig:
        penalty += 10
    if loc.remote and loc.remote_scope == "canada":
        elig.append("Remote, open to Canada")
    if PHD_RE.search(title):
        penalty += 15
        reasons.append("PhD-level role")
    elif MASTERS_RE.search(title):
        penalty += 8
        reasons.append("graduate-degree role")

    score = role_w + intern_points + loc_points + term_points + student_bonus - penalty
    score = max(0, min(100, score))
    if not excluded and score < cfg.min_score:
        excluded = f"score {score} below minimum {cfg.min_score}"

    return Classification(
        included=excluded is None,
        score=score,
        priority=priority_label(score),
        category=category,
        reasons=reasons,
        exclusion_reason=excluded,
        country=loc.country,
        province=loc.provinces[0] if loc.provinces else None,
        provinces=loc.provinces,
        city=loc.cities[0] if loc.cities else None,
        workplace=loc.workplace,
        remote_scope=loc.remote_scope,
        internship_type=internship_type,
        term=primary_term,
        terms=terms,
        eligibility="; ".join(dict.fromkeys(elig)) or None,
    )
