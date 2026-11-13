"""Source adapters: Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Eightfold, GitHub listings, Markdown."""
from __future__ import annotations

import httpx
import pytest

from bot.dedupe import canonical_url_key
from bot.http import SourceFetchError
from bot.sources import build_sources
from bot.sources.ashby import AshbySource
from bot.sources.base import FetchContext
from bot.sources.eightfold import EightfoldSource
from bot.sources.github_listings import GitHubListingsSource
from bot.sources.greenhouse import GreenhouseSource
from bot.sources.lever import LeverSource
from bot.sources.markdown_table import MarkdownTableSource, parse_markdown_table
from bot.sources.smartrecruiters import SmartRecruitersSource
from bot.sources.workday import WorkdaySource, parse_posted_on
from tests.conftest import load_fixture, make_http


def gh_detail(request: httpx.Request):
    job_id = request.url.path.rsplit("/", 1)[-1]
    details = load_fixture("greenhouse_details.json")
    base = next(j for j in load_fixture("greenhouse_jobs.json")["jobs"] if str(j["id"]) == job_id)
    return {**base, **details[job_id]}


# ------------------------------------------------------------------ Greenhouse
async def test_greenhouse_parsing(router):
    router.add("GET", r"boards/hootsuite/jobs$", load_fixture("greenhouse_jobs.json"))
    router.add("GET", r"boards/hootsuite/jobs/\d+$", gh_detail)
    src = GreenhouseSource(board="hootsuite", company="Hootsuite")
    jobs = await src.fetch(make_http(router), FetchContext())
    titles = {j.title for j in jobs}
    # Senior role is dropped before any detail request; intern-like titles are kept.
    assert "Senior Software Engineer, Platform" not in titles
    assert {"Co-op/Intern, DevOps - Winter 2027", "Software Developer Co-op Intern (Summer 2027)",
            "Marketing Intern - Summer 2027"} == titles
    devops = next(j for j in jobs if "DevOps" in j.title)
    assert devops.source == "greenhouse:hootsuite"
    assert devops.uid == "greenhouse:hootsuite:8359242"
    assert devops.company == "Hootsuite"
    assert devops.url == "https://job-boards.greenhouse.io/hootsuite/jobs/8359242"
    assert "Alberta" in devops.location and "British Columbia, Canada" in devops.location
    assert "Kubernetes" in devops.description and "&lt;" not in devops.description and "<p>" not in devops.description
    assert devops.date_posted is not None and devops.date_posted.isoformat().startswith("2026-11-06")
    assert "greenhouse:8359242" in devops.extra_keys
    assert router.count(r"jobs/8000001") == 0  # no detail call for the senior role


async def test_greenhouse_skips_detail_for_known_jobs(router):
    router.add("GET", r"boards/hootsuite/jobs$", load_fixture("greenhouse_jobs.json"))
    router.add("GET", r"boards/hootsuite/jobs/\d+$", gh_detail)
    src = GreenhouseSource(board="hootsuite")
    ctx = FetchContext(is_known=lambda uid: True)
    jobs = await src.fetch(make_http(router), ctx)
    assert len(jobs) == 3
    assert router.count(r"jobs/\d+$") == 0
    assert all(not j.details_fetched for j in jobs)


async def test_greenhouse_bad_board_raises(router):
    router.add("GET", r"boards/doesnotexist/jobs", httpx.Response(404, json={"status": 404}))
    with pytest.raises(SourceFetchError, match="404"):
        await GreenhouseSource(board="doesnotexist").fetch(make_http(router), FetchContext())


# ------------------------------------------------------------------ Lever
async def test_lever_parsing(router):
    router.add("GET", r"api\.lever\.co/v0/postings/kepler", load_fixture("lever_postings.json"))
    jobs = await LeverSource(company_id="kepler", company="Kepler Communications").fetch(make_http(router), FetchContext())
    assert len(jobs) == 3
    j = jobs[0]
    assert j.title == "Software Engineering Intern (January 2027)"
    assert j.company == "Kepler Communications"
    assert j.location == "Toronto, Ontario"
    assert j.countries == ["Canada"]
    assert j.workplace == "onsite"
    assert j.employment_type == "Internship"
    assert j.url.startswith("https://jobs.lever.co/kepler/5514238a")
    assert "Currently enrolled" in j.description and "Eligible to work in Canada" in j.description
    assert j.date_posted is not None and j.date_posted.year == 2026
    assert j.extra_keys == ["lever:5514238a-8301-49f1-8116-26d38ea0900c"]
    assert jobs[2].countries == ["United States"]


async def test_lever_error_payload(router):
    router.add("GET", r"postings/nope", {"ok": False, "error": "Document not found"})
    with pytest.raises(SourceFetchError):
        await LeverSource(company_id="nope").fetch(make_http(router), FetchContext())


# ------------------------------------------------------------------ Ashby
async def test_ashby_parsing(router):
    router.add("GET", r"posting-api/job-board/cohere", load_fixture("ashby_board.json"))
    jobs = await AshbySource(org="cohere", company="Cohere").fetch(make_http(router), FetchContext())
    assert [j.title for j in jobs] == ["Research Internship (Winter 2027)",
                                       "Software Engineer Intern - Backend (Summer 2027)", "Staff Software Engineer"]
    research = jobs[0]
    assert research.employment_type == "Intern"
    assert research.workplace == "remote"
    assert "Canada" in research.countries and "United States" in research.countries
    assert "Toronto, Ontario, Canada" in research.location and "Montreal, Quebec, Canada" in research.location
    assert research.date_posted.isoformat().startswith("2026-05-01")
    backend = jobs[1]
    assert backend.workplace == "hybrid"
    assert backend.location == "Toronto, Ontario, Canada"
    assert backend.extra_keys == ["ashby:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]


async def test_ashby_unexpected_payload(router):
    router.add("GET", r"job-board/broken", {"unexpected": True})
    with pytest.raises(SourceFetchError):
        await AshbySource(org="broken").fetch(make_http(router), FetchContext())


# ------------------------------------------------------------------ Workday
async def test_workday_parsing(router):
    router.add("POST", r"/wday/cxs/rbc/RBCEARLYTALENT1/jobs$", load_fixture("workday_list.json"))
    router.add("GET", r"/wday/cxs/rbc/RBCEARLYTALENT1/job/", load_fixture("workday_detail.json"))
    src = WorkdaySource(company="Royal Bank of Canada", host="rbc.wd3.myworkdayjobs.com", site="RBCEARLYTALENT1", search=[""])
    jobs = await src.fetch(make_http(router), FetchContext())
    titles = [j.title for j in jobs]
    assert "Senior Director, Technology" not in titles  # pre-filtered, no detail request
    dev = next(j for j in jobs if j.title.startswith("Developer"))
    assert dev.source_job_id == "R-0000191193"
    assert dev.countries == ["Canada"]
    assert "Java and React" in dev.description
    assert dev.date_posted.isoformat().startswith("2026-11-08")
    assert dev.extra_keys == ["workday:rbc:R-0000191193"]
    sent = [r for r in router.calls if r.method == "POST"]
    assert sent and b'"limit": 20' in sent[0].content.replace(b'"limit":20', b'"limit": 20')


def test_workday_posted_on_parsing():
    assert parse_posted_on("Posted Today") is not None
    assert parse_posted_on("Posted 3 Days Ago") is not None
    assert parse_posted_on("Posted 30+ Days Ago") is None  # unknown: never invent a date
    assert parse_posted_on(None) is None


def test_workday_url_key_matches_aggregator_url():
    # The same requisition posted on two Workday sites with different suffixes is one job.
    a = canonical_url_key("https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1/job/T/XMLNAME-2027-AI-Engineer_R-0000187401-1")
    b = canonical_url_key("https://rbc.wd3.myworkdayjobs.com/rbcglobal1/job/T/XMLNAME-2027-AI-Engineer_R-0000187401-2?utm_source=Simplify")
    assert a == b == "workday:rbc:R-0000187401"


def test_workday_url_key_for_newer_requisition_formats():
    # Autodesk's ids (26WD102151) have no "R-" prefix and were missed before.
    assert canonical_url_key("https://autodesk.wd1.myworkdayjobs.com/Ext/job/Toronto-ON-CAN/"
                             "Software-Developer-Intern_26WD102151-1") == "workday:autodesk:26WD102151"
    assert canonical_url_key("https://autodesk.wd1.myworkdayjobs.com/en-US/uni/job/Montreal/"
                             "Intern_26WD102151") == "workday:autodesk:26WD102151"
    assert canonical_url_key("https://arlo.wd1.myworkdayjobs.com/Arlo/job/Ottawa/Intern_JR100445") == "workday:arlo:JR100445"
    assert canonical_url_key("https://semtech.wd1.myworkdayjobs.com/x/job/y/Intern_REQ3617-2") == "workday:semtech:REQ3617"
    # A title slug with no requisition id is not mistaken for one.
    assert canonical_url_key("https://x.wd3.myworkdayjobs.com/s/job/Toronto/Software-Intern").startswith("url:")


# ------------------------------------------------------------------ Eightfold
async def test_eightfold_parsing(router):
    router.add("GET", r"jobs\.autodesk\.com/api/pcsx/search", load_fixture("eightfold_search.json"))
    router.add("GET", r"jobs\.autodesk\.com/api/pcsx/position_details", load_fixture("eightfold_detail.json"))
    src = EightfoldSource(name="autodesk", company="Autodesk", host="jobs.autodesk.com", domain="autodesk.com",
                          queries=["intern"], ats_key_prefix="workday:autodesk")
    jobs = await src.fetch(make_http(router), FetchContext())
    titles = [j.title for j in jobs]
    assert "Principal Software Architect" not in titles  # pre-filtered, no detail request
    dev = next(j for j in jobs if j.title == "Software Developer Intern")
    assert dev.url == "https://jobs.autodesk.com/careers/job/1443154491171"
    assert dev.source == "eightfold:autodesk" and dev.source_job_id == "1443154491171"
    assert dev.extra_keys == ["workday:autodesk:26WD102151"]
    assert dev.location == "Toronto, ON, Canada" and dev.workplace == "hybrid"
    assert "C++ and Python" in dev.description and "<b>" not in dev.description
    assert dev.date_posted.isoformat().startswith("2025-11-12")
    search = [r for r in router.calls if "/search" in str(r.url)]
    assert search and search[0].url.params["location"] == "Canada" and search[0].url.params["domain"] == "autodesk.com"


async def test_eightfold_skips_details_for_known_jobs(router):
    router.add("GET", r"/api/pcsx/search", load_fixture("eightfold_search.json"))
    router.add("GET", r"/api/pcsx/position_details", load_fixture("eightfold_detail.json"))
    src = EightfoldSource(name="autodesk", company="Autodesk", host="jobs.autodesk.com", domain="autodesk.com")
    jobs = await src.fetch(make_http(router), FetchContext(is_known=lambda uid: True))
    assert len(jobs) == 2 and not any("position_details" in str(r.url) for r in router.calls)
    assert jobs[0].url.startswith("https://jobs.autodesk.com/careers/job/")


async def test_eightfold_unexpected_payload(router):
    router.add("GET", r"/api/pcsx/search", {"status": 403, "message": "nope"})
    src = EightfoldSource(company="X", host="careers.x.example", domain="x.example")
    with pytest.raises(SourceFetchError):
        await src.fetch(make_http(router), FetchContext())


# ------------------------------------------------------------------ SmartRecruiters
async def test_smartrecruiters_parsing(router):
    router.add("GET", r"companies/Ubisoft2/postings/\d+", load_fixture("smartrecruiters_detail.json"))
    router.add("GET", r"companies/Ubisoft2/postings\?", load_fixture("smartrecruiters_postings.json"))
    jobs = await SmartRecruitersSource(company_id="Ubisoft2", company="Ubisoft").fetch(make_http(router), FetchContext())
    assert len(jobs) == 1  # permanent "Tools Programmer" is not student-like
    j = jobs[0]
    assert j.title.startswith("Stagiaire en programmation")
    assert j.countries == ["Canada"]
    assert j.workplace == "hybrid"
    assert "internship" in (j.employment_type or "")
    assert "C++" in j.description
    assert j.url == "https://jobs.smartrecruiters.com/Ubisoft2/744000160900001"
    assert "country=ca" in str(router.calls[0].url)


# ------------------------------------------------------------------ GitHub listings / Markdown
async def test_github_listings_parsing_and_prefilter(router):
    url = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
    router.add("GET", r"listings\.json", httpx.Response(200, json=load_fixture("simplify_listings.json"), headers={"ETag": '"v1"'}))
    src = GitHubListingsSource(name="simplify", url=url)
    jobs = await src.fetch(make_http(router), FetchContext())
    companies = [j.company for j in jobs]
    assert "Lightmatter" not in companies  # Mountain View, CA (California) filtered out
    assert "Closed Co" not in companies  # inactive
    lyft = next(j for j in jobs if j.company == "Lyft")
    assert lyft.terms_hint == ["Summer 2027"]
    assert lyft.assume_internship is True
    assert lyft.category_hint == "Software"
    assert lyft.date_posted is not None


async def test_github_listings_etag_cache(router):
    data = load_fixture("simplify_listings.json")

    def respond(request):
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=data, headers={"ETag": '"v1"'})

    router.add("GET", r"listings\.json", respond)
    http = make_http(router)
    src = GitHubListingsSource(name="simplify", url="https://raw.githubusercontent.com/x/y/dev/listings.json")
    first = await src.fetch(http, FetchContext())
    second = await src.fetch(http, FetchContext())
    assert [j.uid for j in first] == [j.uid for j in second]
    assert router.calls[1].headers.get("If-None-Match") == '"v1"'


def test_markdown_table_parsing():
    rows = parse_markdown_table(load_fixture("canadian_internships_readme.md"))
    assert len(rows) == 4
    assert rows[1]["company"] == "Botpress Technologies"  # "↳" repeats previous company
    assert rows[2]["closed"] is True
    assert rows[3]["url"].startswith("https://rbc.wd3.myworkdayjobs.com/")
    assert rows[0]["url"] == "https://apply.workable.com/botpress/j/16C68A50A7/"  # not the badge image
    assert rows[0]["date"].year == 2026 and rows[0]["date"].month == 11


async def test_markdown_source_skips_closed(router):
    router.add("GET", r"README\.md", load_fixture("canadian_internships_readme.md"))
    src = MarkdownTableSource(name="cti", url="https://raw.githubusercontent.com/n/r/main/README.md", assume_country="Canada")
    jobs = await src.fetch(make_http(router), FetchContext())
    assert len(jobs) == 3
    assert all(j.assume_country == "Canada" and j.assume_internship for j in jobs)
    assert not any("Data Analyst" in j.title for j in jobs)


# ------------------------------------------------------------------ registry
def test_build_sources_from_config_and_errors():
    cfg = {
        "greenhouse": [{"board": "lyft", "company": "Lyft"}, {"board": "off", "enabled": False}],
        "lever": [{"company_id": "waabi"}],
        "workday": [{"company": "X", "host": "x.wd3.myworkdayjobs.com"}],  # missing site -> config error
        "bogus": [{"x": 1}],
    }
    sources, errors = build_sources(cfg)
    assert [s.id for s in sources] == ["greenhouse:lyft", "lever:waabi"]
    assert any("workday" in e for e in errors) and any("bogus" in e for e in errors)


def test_shipped_sources_file_is_valid():
    from bot.config import load_sources_config

    sources, errors = build_sources(load_sources_config("sources.toml"))
    assert errors == []
    kinds = {s.kind for s in sources}
    assert {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "eightfold", "github_listings",
            "markdown_table"} <= kinds
    assert len(sources) >= 50


def test_vansh_season_without_year_uses_repo_year():
    src = GitHubListingsSource(name="v", url="https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json")
    job = src.parse_job({"id": "1", "title": "Software Engineer Intern", "company_name": "X", "url": "https://go.vansh.nyc/a1",
                         "locations": ["Toronto, ON"], "season": "Summer", "active": True})
    assert job.terms_hint == ["Summer 2027"]
