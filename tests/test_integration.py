"""End-to-end scan with mocked public APIs (real adapters, real classifier, real SQLite)."""
from __future__ import annotations

import httpx

from bot.db import Database
from bot.notify import Notifier
from bot.scanner import Scanner
from bot.sources.ashby import AshbySource
from bot.sources.github_listings import GitHubListingsSource
from bot.sources.greenhouse import GreenhouseSource
from bot.sources.lever import LeverSource
from bot.sources.markdown_table import MarkdownTableSource
from bot.sources.workday import WorkdaySource
from tests.conftest import NOW, Router, load_fixture, make_http

LYFT_LIST = {"jobs": [
    {"id": 8796054002, "title": "Software Engineer Intern - Backend (Summer 2027)", "location": {"name": "Toronto, Canada"},
     "absolute_url": "https://app.careerpuck.com/job-board/lyft/job/8796054002?gh_jid=8796054002",
     "first_published": "2026-11-05T10:00:00-05:00", "company_name": "Lyft"},
    {"id": 8797376002, "title": "Data Engineer Intern (Summer 2027)", "location": {"name": "Toronto, Canada"},
     "absolute_url": "https://app.careerpuck.com/job-board/lyft/job/8797376002?gh_jid=8797376002", "company_name": "Lyft"},
    {"id": 8800000001, "title": "Content Design Intern (Summer 2027)", "location": {"name": "Toronto, Canada"},
     "absolute_url": "https://app.careerpuck.com/job-board/lyft/job/8800000001?gh_jid=8800000001", "company_name": "Lyft"},
    {"id": 8800000002, "title": "Senior Software Engineer, Payments", "location": {"name": "Toronto, Canada"},
     "absolute_url": "https://app.careerpuck.com/job-board/lyft/job/8800000002?gh_jid=8800000002", "company_name": "Lyft"},
]}


def gh_detail(request: httpx.Request):
    board = request.url.path.split("/")[3]
    job_id = request.url.path.rsplit("/", 1)[-1]
    if board == "hootsuite":
        base = next(j for j in load_fixture("greenhouse_jobs.json")["jobs"] if str(j["id"]) == job_id)
        return {**base, **load_fixture("greenhouse_details.json")[job_id]}
    base = next(j for j in LYFT_LIST["jobs"] if str(j["id"]) == job_id)
    return {**base, "content": "&lt;p&gt;You will build services in Python and Go and work with SQL.&lt;/p&gt;"}


def workday_detail(request: httpx.Request):
    if "Developer" in request.url.path:
        return load_fixture("workday_detail.json")
    return {"jobPostingInfo": {"title": "2028 Capital Markets, Global Markets Sales & Trading Summer Analyst",
                               "location": "New York, New York, United States of America",
                               "jobDescription": "<p>Sales and trading summer analyst.</p>",
                               "jobRequisitionLocation": {"country": {"alpha2Code": "US"}}}}


def build_router() -> Router:
    r = Router()
    r.add("GET", r"boards-api\.greenhouse\.io/v1/boards/lyft/jobs$", LYFT_LIST)
    r.add("GET", r"boards-api\.greenhouse\.io/v1/boards/hootsuite/jobs$", load_fixture("greenhouse_jobs.json"))
    r.add("GET", r"boards-api\.greenhouse\.io/v1/boards/\w+/jobs/\d+$", gh_detail)
    r.add("GET", r"api\.lever\.co/v0/postings/kepler", load_fixture("lever_postings.json"))
    r.add("GET", r"api\.ashbyhq\.com/posting-api/job-board/cohere", load_fixture("ashby_board.json"))
    r.add("POST", r"rbc\.wd3\.myworkdayjobs\.com/wday/cxs/rbc/RBCEARLYTALENT1/jobs$", load_fixture("workday_list.json"))
    r.add("GET", r"rbc\.wd3\.myworkdayjobs\.com/wday/cxs/rbc/RBCEARLYTALENT1/job/", workday_detail)
    r.add("GET", r"raw\.githubusercontent\.com/.+/listings\.json",
          lambda req: httpx.Response(200, json=load_fixture("simplify_listings.json")))
    r.add("GET", r"raw\.githubusercontent\.com/.+/README\.md", load_fixture("canadian_internships_readme.md"))
    r.add("GET", r"boards-api\.greenhouse\.io/v1/boards/brokenboard/jobs$", httpx.Response(500, text="oops"))
    return r


def build_sources():
    return [
        GreenhouseSource(board="lyft", company="Lyft"),
        GreenhouseSource(board="hootsuite", company="Hootsuite"),
        GreenhouseSource(board="brokenboard", company="Broken"),
        LeverSource(company_id="kepler", company="Kepler Communications"),
        AshbySource(org="cohere", company="Cohere"),
        WorkdaySource(company="Royal Bank of Canada", host="rbc.wd3.myworkdayjobs.com", site="RBCEARLYTALENT1", search=[""]),
        GitHubListingsSource(name="simplify-summer2027",
                             url="https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"),
        MarkdownTableSource(name="cti-2027", url="https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2027/main/README.md",
                            assume_country="Canada"),
    ]


EXPECTED_NEW = {
    ("Lyft", "Software Engineer Intern - Backend (Summer 2027)"),
    ("Lyft", "Data Engineer Intern (Summer 2027)"),
    ("Hootsuite", "Co-op/Intern, DevOps - Winter 2027"),
    ("Hootsuite", "Software Developer Co-op Intern (Summer 2027)"),
    ("Kepler Communications", "Software Engineering Intern (January 2027)"),
    ("Cohere", "Research Internship (Winter 2027)"),
    ("Cohere", "Software Engineer Intern - Backend (Summer 2027)"),
    ("Royal Bank of Canada", "Developer (RBC Amplify 2027) - Toronto"),
    ("SOTI", "Software Developer Intern"),
    ("Botpress Technologies", "Software Development Intern (Winter 2027)"),
    ("Botpress Technologies", "Software Developer Intern (Winter 2027)"),
}


async def test_full_scan_end_to_end(settings, tmp_path):
    settings.database_url = f"sqlite:///{tmp_path / 'integration.db'}"
    db = Database(settings.database_path)
    db.subscribe(1234)
    dms: list = []

    async def send(uid, payload):
        dms.append((uid, payload))

    router = build_router()
    http = make_http(router)
    scanner = Scanner(settings, db, build_sources(), http,
                      notify=Notifier(db, settings, send, pause=0).deliver_pending, now=lambda: NOW)

    # ---------------- first scan
    r1 = await scanner.run("startup")
    stored = {(j.company, j.title) for j in db.latest_jobs(limit=100)}
    assert stored == EXPECTED_NEW, stored ^ EXPECTED_NEW
    assert r1.new_jobs == len(EXPECTED_NEW) == 11
    # irrelevant jobs excluded
    for bad in ["Content Design Intern (Summer 2027)", "Marketing Intern - Summer 2027", "Mission Operations Specialist",
                "Staff Software Engineer", "Treasury & Payment Solutions Strategy Intern", "Package Layout Automation Intern"]:
        assert all(bad != t for _, t in stored)
    # US-only intern position (Foster City, CA) excluded
    assert db.count_jobs() == 11
    # duplicates across sources suppressed: Simplify Lyft == Greenhouse Lyft, Markdown RBC == Workday RBC
    assert r1.duplicates >= 2
    lyft = next(j for j in db.latest_jobs(limit=100) if j.title.startswith("Software Engineer Intern - Backend"))
    assert lyft.source == "greenhouse:lyft" and "github_listings:simplify-summer2027" in lyft.also_seen_on
    rbc = next(j for j in db.latest_jobs(limit=100) if j.company == "Royal Bank of Canada")
    assert rbc.source.startswith("workday:") and "markdown_table:cti-2027" in rbc.also_seen_on
    # one failing source did not block the others
    assert r1.sources_failed == 1 and "greenhouse:brokenboard" in r1.errors and r1.sources_ok == 7
    # correct number of notifications
    embeds = [p["embed"] for _, p in dms if "embed" in p]
    assert r1.notifications_sent == len(embeds) == 11
    assert len({e.url for e in embeds}) == 11
    # Québec/Ontario Summer 2027 SWE ranks at the top
    top = db.latest_jobs(limit=100)
    best = max(top, key=lambda j: j.score)
    assert best.province in ("ON", "QC") and best.priority == "High"

    # ---------------- second scan: nothing new, no duplicate notifications
    r2 = await scanner.run("scheduled")
    assert r2.new_jobs == 0 and r2.notifications_sent == 0
    assert len([p for _, p in dms if "embed" in p]) == 11
    # known jobs are not re-fetched in detail
    detail_calls_before = router.count(r"/jobs/\d+$")
    await scanner.run("scheduled")
    assert router.count(r"/jobs/\d+$") == detail_calls_before

    # ---------------- restart: new DB connection + new scanner on the same file
    await http.client.aclose()
    db.close()
    db2 = Database(settings.database_path)
    http2 = make_http(build_router())
    scanner2 = Scanner(settings, db2, build_sources(), http2,
                       notify=Notifier(db2, settings, send, pause=0).deliver_pending, now=lambda: NOW)
    r3 = await scanner2.run("startup")
    assert r3.new_jobs == 0 and r3.notifications_sent == 0
    assert db2.count_jobs() == 11 and db2.count_notified() == 11
    await http2.client.aclose()
    db2.close()
