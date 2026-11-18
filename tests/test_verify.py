"""Looking up "possibly closed" postings on the company's own job board (bot/verify.py)."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from bot.classify import FilterConfig, classify
from bot.verify import LinkChecker, parse_ref, same_role
from tests.conftest import NOW, make_http, make_job

WD = "https://mckesson.wd3.myworkdayjobs.com/External_Careers/job/CAN-QC-Montreal/Developer-Intern_JR0154903"
WD_API = r"mckesson\.wd3\.myworkdayjobs\.com/wday/cxs/mckesson/External_Careers"


def stale_job(db, **kw):
    """Store a job seen 2 days ago whose source was checked since and no longer lists it."""
    base = dict(source="github_listings:vansh", source_job_id="v1", company="McKesson",
                title="Developer Intern (Winter 2027)", url=WD, location="Montréal, QC", assume_internship=True)
    base.update(kw)
    job = make_job(**base)
    job_id = db.insert_job(job, classify(job, FilterConfig(), now=NOW), now=NOW - timedelta(days=2))
    db.record_source_check(None, base["source"], True, NOW - timedelta(minutes=5), 10)
    assert job_id in db.stale_job_ids(NOW)
    return job_id


# ------------------------------------------------------------------ URL parsing / matching
def test_parse_ref_for_each_board():
    assert parse_ref("https://job-boards.greenhouse.io/hootsuite/jobs/8359242").board == "hootsuite"
    assert parse_ref("https://boards.greenhouse.io/embed/job_app?for=geotab&token=123").job_id == "123"
    lever = parse_ref("https://jobs.lever.co/gomaterials/be3fb0e8-54a1-43de-b998-99dd071eed19/")
    assert (lever.ats, lever.board) == ("lever", "gomaterials")
    assert parse_ref("https://jobs.ashbyhq.com/1password/b9ff2259-517c-4ec2-b92f-646be8bf12b6").ats == "ashby"
    wd = parse_ref("https://rbc.wd3.myworkdayjobs.com/fr-CA/RBCEARLYTALENT1/job/CALGARY/Data-Engineer_R-0000191201")
    assert wd.board == "rbc.wd3.myworkdayjobs.com/rbc/RBCEARLYTALENT1"  # locale segment skipped
    assert wd.job_id == "/job/CALGARY/Data-Engineer_R-0000191201"
    assert parse_ref("https://jobs.smartrecruiters.com/Ubisoft2/744000012345678-intern").job_id == "744000012345678"
    assert parse_ref("https://careers.amd.com/jobs/91367?icims=1") is None


def test_same_role_needs_matching_title_place_and_term(db):
    job_id = stale_job(db)
    job = db.get_job(job_id)
    assert same_role(job, "Developer Intern - Winter 2027", "CAN, QC, Montreal")
    assert same_role(job, "Stagiaire Developer (Winter 2027)")          # intern/stagiaire words ignored
    assert not same_role(job, "Developer Intern (Summer 2027)")         # other term
    assert not same_role(job, "Developer Intern", "Toronto, ON")        # other province
    assert not same_role(job, "Senior Data Analyst")


# ------------------------------------------------------------------ Workday
async def test_workday_posting_still_open_keeps_job_open(db, router):
    job_id = stale_job(db)
    router.add("GET", WD_API + "/job/", {"jobPostingInfo": {"title": "Developer Intern", "canApply": True,
                                                             "externalUrl": WD}})
    stats = await LinkChecker(db, make_http(router)).run(NOW)
    assert stats["open"] == 1
    assert job_id not in db.stale_job_ids(NOW)          # no longer "possibly closed"
    assert db.get_job(job_id).url == WD
    # ... and stays open until the check expires (24 h), then it is looked up again
    assert job_id not in db.stale_job_ids(NOW + timedelta(hours=20))
    assert job_id in db.stale_job_ids(NOW + timedelta(hours=37))


async def test_workday_closed_posting_is_marked_closed(db, router):
    job_id = stale_job(db)
    router.add("GET", WD_API + "/job/", httpx.Response(403, json={"errorCode": "S22", "message": "permission denied"}))
    router.add("POST", WD_API + "/jobs$", {"total": 1, "jobPostings": [
        {"title": "Senior Manager, Pharmaceutical Distribution", "externalPath": "/job/x/Senior_JR1", "locationsText": "CAN"}]})
    stats = await LinkChecker(db, make_http(router)).run(NOW)
    assert stats["closed"] == 1
    assert db.verified_closed_ids() == {job_id}
    # not looked up again for 3 days
    await LinkChecker(db, make_http(router)).run(NOW + timedelta(hours=1))
    assert router.count(r"/job/CAN") == 1


async def test_workday_reposted_role_gets_the_new_link(db, router):
    job_id = stale_job(db)
    new_path = "/job/CAN-QC-Montreal/Developer-Intern--Winter-2027-_JR0160000"
    router.add("GET", WD_API + "/job/CAN-QC-Montreal/Developer-Intern_JR", httpx.Response(403, json={"errorCode": "S22"}))
    router.add("POST", WD_API + "/jobs$", {"total": 2, "jobPostings": [
        {"title": "Developer Intern (Summer 2027)", "externalPath": "/job/x/Dev_JR1", "locationsText": "CAN, QC"},
        {"title": "Developer Intern - Winter 2027", "externalPath": new_path, "locationsText": "CAN, QC, Montreal"}]})
    stats = await LinkChecker(db, make_http(router)).run(NOW)
    assert stats["moved"] == 1
    assert db.get_job(job_id).url == "https://mckesson.wd3.myworkdayjobs.com/External_Careers" + new_path
    assert job_id not in db.stale_job_ids(NOW)


async def test_rate_limit_or_unsupported_site_leaves_it_possibly_closed(db, router):
    a = stale_job(db)
    b = stale_job(db, source_job_id="v2", title="Firmware Intern", url="https://careers.amd.com/jobs/91367?icims=1")
    router.add("GET", WD_API, httpx.Response(429))
    router.add("GET", r"careers\.amd\.com", httpx.Response(200, text="<html>job page</html>"))
    stats = await LinkChecker(db, make_http(router, max_retries=0)).run(NOW)
    assert (stats["unknown"], stats["retry"]) == (1, 1) and db.verified_closed_ids() == set()
    assert {a, b} <= db.stale_job_ids(NOW)


async def test_unsupported_site_404_counts_as_closed(db, router):
    job_id = stale_job(db, url="https://careers.amd.com/jobs/91367?icims=1")
    router.add("GET", r"careers\.amd\.com", httpx.Response(404))
    await LinkChecker(db, make_http(router)).run(NOW)
    assert db.verified_closed_ids() == {job_id}


# ------------------------------------------------------------------ short links, Greenhouse, Lever, Ashby
async def test_tracker_short_link_is_resolved_then_checked(db, router):
    rbc = "https://rbc.wd3.myworkdayjobs.com/fr-CA/RBCEARLYTALENT1/job/CALGARY/Data-Engineer_R-0000191201"
    job_id = stale_job(db, company="Royal Bank of Canada", title="Data Engineer Intern", url="https://go.vansh.nyc/Qa",
                       location="Calgary, AB")
    router.add("GET", r"go\.vansh\.nyc/Qa", httpx.Response(302, headers={"Location": rbc}))
    router.add("GET", r"/wday/cxs/rbc/RBCEARLYTALENT1/job/CALGARY", {"jobPostingInfo": {"canApply": True}})
    stats = await LinkChecker(db, make_http(router)).run(NOW)
    assert stats["open"] == 1 and db.get_job(job_id).url == rbc  # short link replaced by RBC's own page


async def test_greenhouse_open_and_closed(db, router):
    a = stale_job(db, source_job_id="g1", company="Hootsuite", title="Co-op, DevOps",
                  url="https://job-boards.greenhouse.io/hootsuite/jobs/111")
    b = stale_job(db, source_job_id="g2", company="Hootsuite", title="Co-op, Software Development",
                  url="https://job-boards.greenhouse.io/hootsuite/jobs/222")
    router.add("GET", r"boards/hootsuite/jobs/111", {"id": 111, "absolute_url": "https://hootsuite.com/careers?gh_jid=111"})
    router.add("GET", r"boards/hootsuite/jobs/222", httpx.Response(404))
    router.add("GET", r"boards/hootsuite/jobs$", {"jobs": [{"id": 9, "title": "Account Executive",
                                                            "location": {"name": "Toronto"}}]})
    await LinkChecker(db, make_http(router)).run(NOW)
    assert a not in db.stale_job_ids(NOW) and db.get_job(a).url == "https://hootsuite.com/careers?gh_jid=111"
    assert db.verified_closed_ids() == {b}


async def test_lever_and_ashby(db, router):
    lever_url = "https://jobs.lever.co/gomaterials/be3fb0e8-54a1-43de-b998-99dd071eed19"
    ashby_id = "b9ff2259-517c-4ec2-b92f-646be8bf12b6"
    a = stale_job(db, source_job_id="l1", company="GoMaterials", title="Data Quality Intern", url=lever_url)
    b = stale_job(db, source_job_id="a1", company="1Password", title="Developer, Enterprise Tenants",
                  url=f"https://jobs.ashbyhq.com/1password/{ashby_id}")
    router.add("GET", r"api\.lever\.co/v0/postings/gomaterials/be3fb0e8", {"hostedUrl": lever_url})
    router.add("GET", r"posting-api/job-board/1password", {"jobs": [{"id": "other", "title": "Recruiter"}]})
    stats = await LinkChecker(db, make_http(router)).run(NOW)
    assert (stats["open"], stats["closed"]) == (1, 1)
    assert a not in db.stale_job_ids(NOW) and db.verified_closed_ids() == {b}


async def test_limit_per_scan_and_off_switch(db, router):
    for i in range(3):
        stale_job(db, source_job_id=f"x{i}", title=f"Developer Intern {i}", url=f"https://careers.amd.com/jobs/{i}")
    router.add("GET", r"careers\.amd\.com", httpx.Response(404))
    assert (await LinkChecker(db, make_http(router), limit=2).run(NOW))["checked"] == 2
    assert (await LinkChecker(db, make_http(router), limit=2).run(NOW))["checked"] == 1   # the remaining one
    assert (await LinkChecker(db, make_http(router), limit=0).run(NOW))["checked"] == 0


async def test_html_shows_verified_closed_separately(db, router):
    from bot.export import to_html

    closed = stale_job(db, source_job_id="c", title="Closed Intern", url="https://careers.amd.com/jobs/1")
    maybe = stale_job(db, source_job_id="m", title="Maybe Intern", url="https://careers.amd.com/jobs/2")
    router.add("GET", r"careers\.amd\.com/jobs/1", httpx.Response(404))
    router.add("GET", r"careers\.amd\.com/jobs/2", httpx.Response(200, text="ok"))
    await LinkChecker(db, make_http(router)).run(NOW)
    stale = db.stale_job_ids(NOW)
    page = to_html(db.all_jobs(), now=NOW, stale_ids=stale, closed_ids=stale & db.verified_closed_ids()).decode()
    closed_row = next(r for r in page.split("<tr") if "jobs/1'" in r)
    maybe_row = next(r for r in page.split("<tr") if "jobs/2'" in r)
    assert "data-status='closed'" in closed_row and "❌ Fermé" in closed_row
    assert "data-status='stale'" in maybe_row and "Peut-être fermé" in maybe_row
    assert "❌ Fermés (vérifiés) (1)" in page
    assert closed != maybe


async def test_scan_runs_the_link_check(settings, db, router):
    from bot.scanner import Scanner

    job_id = stale_job(db, url="https://careers.amd.com/jobs/1")
    router.add("GET", r"careers\.amd\.com", httpx.Response(404))
    http = make_http(router)
    scanner = Scanner(settings, db, [], http, now=lambda: NOW, link_checker=LinkChecker(db, http))
    report = await scanner.run()
    assert report.link_check["closed"] == 1 and "links checked 1" in report.summary()
    assert db.verified_closed_ids() == {job_id}


@pytest.mark.parametrize("status", [500, 503])
async def test_server_errors_are_unknown_not_closed(db, router, status):
    stale_job(db)
    router.add("GET", WD_API, httpx.Response(status))
    stats = await LinkChecker(db, make_http(router, max_retries=0)).run(NOW)
    assert stats["retry"] == 1 and db.verified_closed_ids() == set()


async def test_rate_limited_lookups_are_retried_next_scan_but_unsupported_sites_wait(db, router):
    wd = stale_job(db)
    amd = stale_job(db, source_job_id="v2", title="Firmware Intern", url="https://careers.amd.com/jobs/91367")
    router.add("GET", WD_API, httpx.Response(429))
    router.add("GET", r"careers\.amd\.com", httpx.Response(200, text="<html>job page</html>"))
    await LinkChecker(db, make_http(router, max_retries=0)).run(NOW)
    assert db.link_check(wd)["status"] == "retry" and db.link_check(amd)["status"] == "unknown"
    router.calls.clear()
    await LinkChecker(db, make_http(router, max_retries=0)).run(NOW + timedelta(minutes=30))
    assert router.count(r"careers\.amd\.com") == 0  # unsupported: waits 3 days
    assert router.count(WD_API) == 1                 # rate-limited: asked again
    # Workday is now in its 15-min cooldown in that client; a fresh client (next scan) asks again
    assert db.link_check(wd)["status"] == "retry"
