"""Job normalization helpers and normalized Job structure."""
from __future__ import annotations

from datetime import timezone

from bot.config import database_path_from_url, load_settings
from bot.dedupe import canonical_url_key, fingerprint_base, normalize_company, normalize_title
from bot.textutil import fold, parse_datetime, strip_html, truncate
from tests.conftest import make_job


def test_strip_html_handles_double_escaped_greenhouse_content():
    raw = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    text = strip_html(raw)
    assert "Hello & welcome" in text and "Python" in text and "<" not in text


def test_parse_datetime_variants():
    assert parse_datetime("2026-11-06T13:58:49-05:00").tzinfo == timezone.utc
    assert parse_datetime(1794442230).year == 2026            # unix seconds (Simplify)
    assert parse_datetime(1793145600000).year == 2026         # unix ms (Lever)
    assert parse_datetime("2026-11-08").day == 8              # Workday startDate
    assert parse_datetime(None) is None and parse_datetime("garbage") is None and parse_datetime(0) is None


def test_fold_and_truncate():
    assert fold("Montréal Québec") == "montreal quebec"
    assert truncate("a" * 50, 10).endswith("…") and len(truncate("a" * 50, 10)) <= 10


def test_company_normalization_aliases():
    assert normalize_company("RBC") == normalize_company("Royal Bank of Canada")
    assert normalize_company("BMO Financial Group") == normalize_company("Bank of Montreal")
    assert normalize_company("Shopify Inc.") == normalize_company("Shopify")
    assert normalize_company("Desjardins Group") == "desjardins"


def test_title_normalization_strips_terms_and_cities():
    a = normalize_title("Software Engineer Intern (Summer 2027) - Toronto")
    b = normalize_title("Software Engineer Internship")
    assert a == b == "software engineer intern"
    assert normalize_title("Software Developer Co-op (4 months)") == normalize_title("Software Developer Coop")
    # role specifics are preserved so different roles do not merge
    assert normalize_title("Software Engineer Intern - Backend") != normalize_title("Software Engineer Intern - Frontend")


def test_fingerprint_base():
    assert fingerprint_base("RBC", "Developer Intern (Summer 2027)") == fingerprint_base("Royal Bank of Canada", "Developer Intern")


def test_canonical_url_keys():
    assert canonical_url_key("https://boards.greenhouse.io/lyft/jobs/123?utm_source=x") == "greenhouse:123"
    assert canonical_url_key("https://app.careerpuck.com/job-board/lyft/job/123?gh_jid=123") == "greenhouse:123"
    assert canonical_url_key("https://jobs.lever.co/waabi/5514238A-8301-49f1-8116-26d38ea0900c/apply") == \
        "lever:5514238a-8301-49f1-8116-26d38ea0900c"
    assert canonical_url_key("https://jobs.ashbyhq.com/cohere/73bd3e2b-6597-4124-b64b-1e5dbc32e785/application?embed=true") == \
        "ashby:73bd3e2b-6597-4124-b64b-1e5dbc32e785"
    assert canonical_url_key("https://jobs.smartrecruiters.com/Ubisoft2/744000160900001-stagiaire") == \
        "smartrecruiters:744000160900001"
    assert canonical_url_key("https://www.example.com/careers/123/?utm_source=Simplify&ref=Simplify") == \
        "url:example.com/careers/123"
    assert canonical_url_key("") is None and canonical_url_key(None) is None


def test_job_uid_and_defaults():
    j = make_job(source="greenhouse:lyft", source_job_id="42")
    assert j.uid == "greenhouse:lyft:42"
    assert j.date_posted is None and j.workplace is None  # unknown stays unknown


def test_settings_from_env():
    s = load_settings(env={
        "DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": "123", "DISCORD_USER_ID": "456", "SCAN_INTERVAL_MINUTES": "15",
        "DATABASE_URL": "sqlite:////data/jobs.db", "TARGET_PROVINCES": "on,qc", "INCLUDE_NEW_GRAD": "true",
        "MIN_RELEVANCE_SCORE": "50", "TARGET_TERMS": "Summer 2027,Fall 2027",
    })
    assert s.discord_guild_id == 123 and s.discord_user_id == 456 and s.scan_interval_minutes == 15
    assert s.database_path == "/data/jobs.db"
    assert s.filters.target_provinces == ["ON", "QC"] and s.filters.include_new_grad
    assert s.filters.min_score == 50 and s.notify_min_score == 55
    assert s.filters.target_terms == ["Summer 2027", "Fall 2027"]
    defaults = load_settings(env={})
    assert defaults.scan_interval_minutes == 30 and defaults.discord_bot_token is None
    assert defaults.daily_summary_enabled is False


def test_database_url_parsing():
    assert database_path_from_url("sqlite:///./jobs.db") == "./jobs.db"
    assert database_path_from_url("sqlite:////data/jobs.db") == "/data/jobs.db"
