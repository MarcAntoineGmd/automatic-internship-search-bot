"""Live, read-only smoke tests against real public endpoints.

Skipped by default. Run with:  pytest -m live -o addopts=""
Each test is skipped (not failed) if the host is unreachable from your network.
"""
from __future__ import annotations

import pytest

from bot.http import HttpClient, SourceFetchError, build_async_client
from bot.sources.ashby import AshbySource
from bot.sources.base import FetchContext
from bot.sources.github_listings import GitHubListingsSource
from bot.sources.greenhouse import GreenhouseSource
from bot.sources.lever import LeverSource
from bot.sources.markdown_table import MarkdownTableSource
from bot.sources.smartrecruiters import SmartRecruitersSource
from bot.sources.workday import WorkdaySource

pytestmark = pytest.mark.live

CASES = [
    GitHubListingsSource(name="simplify", url="https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"),
    MarkdownTableSource(name="cti", url="https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2027/main/README.md"),
    GreenhouseSource(board="hootsuite", company="Hootsuite"),
    LeverSource(company_id="kepler", company="Kepler"),
    AshbySource(org="cohere", company="Cohere"),
    WorkdaySource(company="RBC", host="rbc.wd3.myworkdayjobs.com", site="RBCEARLYTALENT1", search=[""], max_pages=2),
    SmartRecruitersSource(company_id="Ubisoft2", company="Ubisoft", max_pages=1),
]


@pytest.mark.parametrize("source", CASES, ids=[s.id for s in CASES])
async def test_live_source(source):
    http = HttpClient(build_async_client(20, "CanadianInternshipRadar/1.0 (smoke test)"), max_retries=1)
    try:
        jobs = await source.fetch(http, FetchContext(max_details=3))
    except SourceFetchError as exc:
        if any(code in str(exc) for code in ("ConnectError", "ProxyError", "403", "ConnectTimeout")):
            pytest.skip(f"{source.id} unreachable from this network: {exc}")
        raise
    finally:
        await http.client.aclose()
    assert isinstance(jobs, list)
    for j in jobs[:20]:
        assert j.title and j.url and j.company
