"""Internship detection, SWE relevance, Québec/Ontario location priority, remote eligibility, terms."""
from __future__ import annotations

import pytest

from bot.classify import FilterConfig, classify, detect_terms_in_text, normalize_term
from bot.location import parse_location
from tests.conftest import NOW, make_job

CFG = FilterConfig()


def c(title, location="Toronto, ON, Canada", **kw):
    return classify(make_job(title=title, location=location, **kw), CFG, now=NOW)


# ------------------------------------------------------------------ internship keyword detection
@pytest.mark.parametrize("title", [
    "Software Engineer Intern", "Software Developer Co-op", "Software Engineering Internship",
    "Coop - Développeur logiciel", "Stagiaire en développement logiciel", "Stage - Développeur Full Stack",
    "Student Software Developer", "Co-op/Intern, DevOps - Winter 2027", "PEY Software Developer",
])
def test_internship_titles_detected(title):
    r = c(title)
    assert r.internship_type in ("internship", "co-op", "student"), r
    assert r.included, r.exclusion_reason


@pytest.mark.parametrize("title", [
    "Internal Tools Engineer", "International Payments Software Engineer", "Software Engineer, Early Stage Products",
    "Senior Software Engineer", "Staff Backend Developer",
])
def test_non_internships_rejected(title):
    r = c(title)
    assert not r.included
    assert "internship" in r.exclusion_reason or "senior" in r.exclusion_reason


def test_internship_from_employment_type_and_description():
    r = c("Software Developer", employment_type="Intern")
    assert r.included and r.internship_type == "internship"
    r2 = c("Développeur logiciel", description="Ce stage de 4 mois s'adresse aux étudiants. Stage d'été 2027 en Python, Java et SQL.")
    assert r2.included, r2.exclusion_reason
    assert r2.score < c("Développeur logiciel - Stagiaire").score  # description-only evidence scores lower


def test_coop_type_label():
    assert c("Software Developer Co-op").internship_type == "co-op"


def test_new_grad_optional():
    assert not c("Software Engineer, New Grad 2027").included
    cfg = FilterConfig(include_new_grad=True)
    r = classify(make_job(title="Software Engineer, New Grad 2027"), cfg, now=NOW)
    assert r.included and r.internship_type == "new_grad"


def test_unpaid_excluded_by_default():
    r = c("Software Developer Intern", description="This is an unpaid internship for academic credit.")
    assert not r.included and "unpaid" in r.exclusion_reason
    r2 = classify(make_job(title="Software Developer Intern", description="This is an unpaid internship."),
                  FilterConfig(allow_unpaid=True), now=NOW)
    assert r2.included


# ------------------------------------------------------------------ SWE relevance
@pytest.mark.parametrize("title,category", [
    ("Software Engineer Intern", "software"), ("Backend Developer Intern", "software"),
    ("Frontend Developer Co-op", "software"), ("Full Stack Developer Intern", "software"),
    ("Platform Engineer Intern", "platform"), ("Infrastructure Engineer Intern", "platform"),
    ("Cloud Engineer Co-op", "platform"), ("DevOps Engineer Intern", "platform"),
    ("Systems Software Engineer Intern", "software"), ("Machine Learning Engineer Intern", "ai_ml"),
    ("AI Engineer Co-op", "ai_ml"), ("Data Engineer Intern", "data_engineering"),
    ("Security Engineer Intern", "security"), ("Embedded Firmware Intern", "platform"),
    ("Gameplay Programmer Intern", "software"), ("Stagiaire - Programmeur(se) outils", "software"),
])
def test_swe_categories(title, category):
    r = c(title)
    assert r.included, r.exclusion_reason
    assert r.category == category


@pytest.mark.parametrize("title", [
    "Sales Development Intern", "Marketing Intern", "IT Support Co-op", "Help Desk Technician Intern",
    "Business Analyst Intern", "Product Manager Intern", "UX Designer Intern", "Accounting Co-op",
    "Recruiting Coordinator Intern", "Analog Design Intern", "Mechanical Engineering Co-op",
    "Customer Success Intern", "Quantitative Trading Analyst Co-op",
])
def test_unrelated_roles_excluded(title):
    r = c(title)
    assert not r.included
    assert "unrelated" in r.exclusion_reason


def test_unusual_title_kept_when_description_is_software():
    desc = ("You will write code in Python and TypeScript, build REST APIs and microservices, "
            "use Git, Docker and Kubernetes, and do code reviews.")
    r = c("Innovation Lab Intern", description=desc)
    assert r.included, r.exclusion_reason
    assert r.category == "software"
    assert any("description" in x for x in r.reasons)


def test_unusual_title_without_signals_is_low_or_excluded():
    r = c("Innovation Lab Intern", description="Help organize events and newsletters.")
    assert not r.included or r.priority == "Low"


def test_strong_title_beats_exclusion_words():
    r = c("Software Engineer Intern, Finance Technology")
    assert r.included and r.category == "software"


def test_phd_penalty():
    assert c("Software Engineer Intern - PhD").score < c("Software Engineer Intern").score


# ------------------------------------------------------------------ location: Québec & Ontario priority
def test_quebec_and_ontario_rank_above_other_provinces():
    qc = c("Software Engineer Intern", "Montréal, QC, Canada")
    on = c("Software Engineer Intern", "Waterloo, ON")
    bc = c("Software Engineer Intern", "Vancouver, BC, Canada")
    ab = c("Software Engineer Intern", "Calgary, Alberta")
    assert qc.province == "QC" and on.province == "ON" and bc.province == "BC" and ab.province == "AB"
    assert qc.score > bc.score and on.score > ab.score


def test_quebec_city_and_french_names():
    r = c("Développeur logiciel - Stagiaire", "Ville de Québec, Québec")
    assert r.province == "QC" and r.country == "Canada"
    assert parse_location("Québec City, QC").provinces == ["QC"]


def test_multi_location_prefers_target_province():
    r = c("Software Engineer Intern", "Vancouver, BC | Toronto, ON | Montreal, QC")
    assert r.province == "QC"
    assert set(r.provinces) == {"QC", "ON", "BC"}


@pytest.mark.parametrize("loc,country", [
    ("San Jose, CA", "Foreign"), ("Toronto, ON, CA", "Canada"), ("Vancouver, WA", "Foreign"),
    ("London, ON", "Canada"), ("London, United Kingdom", "Foreign"), ("Bengaluru, India", "Foreign"),
    ("New York, NY", "Foreign"), ("TORONTO, Ontario, Canada", "Canada"), ("Kanata, Ottawa, ON, Canada", "Canada"),
])
def test_country_detection(loc, country):
    assert parse_location(loc).country == country


def test_foreign_positions_excluded():
    r = c("Software Engineer Intern", "San Francisco, CA")
    assert not r.included and r.exclusion_reason == "outside Canada"


def test_us_and_canada_multi_location_kept():
    r = c("Software Engineer Intern", "New York, NY | Toronto, ON")
    assert r.included and r.province == "ON"


# ------------------------------------------------------------------ Canadian remote eligibility
@pytest.mark.parametrize("loc", ["Remote in Canada", "Remote - Canada", "Canada (Remote)", "Remote (US or Canada)",
                                 "Remote, Canada"])
def test_remote_canada_eligible(loc):
    r = c("Software Engineer Intern", loc)
    assert r.included, r.exclusion_reason
    assert r.country == "Canada" and r.remote_scope == "canada" and r.workplace == "remote"
    assert "Remote, open to Canada" in (r.eligibility or "")


@pytest.mark.parametrize("loc", ["Remote - US", "Remote in USA", "United States (Remote)"])
def test_remote_us_only_excluded(loc):
    assert not c("Software Engineer Intern", loc).included


def test_remote_unknown_kept_with_lower_score():
    r = c("Software Engineer Intern", "Remote")
    assert r.included and r.country == "Unknown"
    assert r.score < c("Software Engineer Intern", "Remote in Canada").score


def test_remote_north_america_kept():
    r = c("Software Engineer Intern", "Remote - North America")
    assert r.included and r.remote_scope == "north_america"


def test_canada_only_board_assumption():
    r = classify(make_job(title="Software Developer Intern", location="Hybrid", assume_country="Canada"), CFG, now=NOW)
    assert r.country == "Canada" and r.included


def test_us_work_authorization_excludes_unknown_location():
    r = c("Software Engineer Intern", "Remote", description="Must be authorized to work in the United States.")
    assert not r.included


# ------------------------------------------------------------------ terms
@pytest.mark.parametrize("text,expected", [
    ("Software Engineer Intern (Summer 2027)", ["Summer 2027"]),
    ("Stagiaire - Été 2027", ["Summer 2027"]),
    ("2027 Summer Analyst", ["Summer 2027"]),
    ("Intern (January 2027)", ["Winter 2027"]),
    ("Co-op Summer/Fall 2027", ["Summer 2027", "Fall 2027"]),
    ("Automne 2027 - Stage", ["Fall 2027"]),
])
def test_term_detection(text, expected):
    assert detect_terms_in_text(text) == expected


def test_term_normalization_and_priority():
    assert normalize_term("summer 2027") == "Summer 2027"
    assert normalize_term("N/A") is None
    s27 = c("Software Engineer Intern (Summer 2027)")
    f27 = c("Software Engineer Intern (Fall 2027)")
    s26 = c("Software Engineer Intern (Summer 2026)")
    assert s27.term == "Summer 2027" and s27.score > f27.score > s26.score
    assert s26.included  # past term is penalised, not silently dropped


def test_terms_from_source_hint():
    r = classify(make_job(title="Software Engineer Intern", terms_hint=["Winter 2027", "Summer 2027"]), CFG, now=NOW)
    assert r.term == "Summer 2027" and set(r.terms) == {"Winter 2027", "Summer 2027"}


def test_unknown_fields_stay_unknown():
    r = c("Software Engineer Intern", "Toronto")
    assert r.term is None and r.workplace is None and r.eligibility is None
