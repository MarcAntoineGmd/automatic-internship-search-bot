"""Community internship trackers on GitHub that publish a Simplify-format listings.json.

Verified formats (Nov 2026):
  * SimplifyJobs/Summer2027-Internships  .github/scripts/listings.json
  * vanshb03/Summer2027-Internships      .github/scripts/listings.json
  * SimplifyJobs/New-Grad-Positions      .github/scripts/listings.json (new-grad, optional)

Files are fetched from raw.githubusercontent.com with ETag caching so unchanged files cost
a 304 response instead of a full download.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from ..http import HttpClient, SourceFetchError
from ..location import parse_location
from ..models import Job
from ..textutil import parse_datetime
from .base import FetchContext, Source

log = logging.getLogger(__name__)


def _parse_json(resp: httpx.Response):
    return json.loads(resp.content)


class GitHubListingsSource(Source):
    kind = "github_listings"
    priority = 40

    def __init__(self, url: str | None = None, internships_only: bool = True, resolve_redirects: bool = False,
                 **kw) -> None:
        if not url:
            raise ValueError("github_listings source requires 'url'")
        super().__init__(name=kw.pop("name", None) or url, **kw)
        self.url = url
        self.internships_only = internships_only
        self.resolve_redirects = resolve_redirects
        self._resolved: dict[str, str] = {}
        m = re.search(r"(?:Summer|Fall|Winter|Spring)?(20\d\d)-Internships", url)
        self.default_year = kw.get("default_year") or (m.group(1) if m else None)

    @staticmethod
    def _is_candidate(raw: dict) -> bool:
        if not raw.get("active", True) or not raw.get("is_visible", True):
            return False
        locs = raw.get("locations") or []
        info = parse_location(locs)
        return info.country == "Canada" or (info.country == "Unknown" and (info.remote or not locs))

    def parse_job(self, raw: dict, url: str | None = None) -> Job:
        terms = [t for t in (raw.get("terms") or []) if t and t != "N/A"]
        if not terms and raw.get("season") and self.default_year:
            # vanshb03 format: season without a year; the repo name carries the year (Summer2027-Internships).
            terms = [f"{raw['season']} {self.default_year}"]
        return Job(
            source=self.id,
            source_job_id=str(raw.get("id")),
            company=(raw.get("company_name") or "").strip(),
            title=(raw.get("title") or "").strip(),
            url=url or raw.get("url") or "",
            location=" | ".join(raw.get("locations") or []),
            terms_hint=terms,
            date_posted=parse_datetime(raw.get("date_posted")),
            category_hint=raw.get("category"),
            assume_internship=self.internships_only,
            assume_country=self.assume_country,
        )

    async def _resolve(self, http: HttpClient, url: str) -> str:
        if url in self._resolved:
            return self._resolved[url]
        try:
            resp = await http.client.get(url, follow_redirects=False, timeout=10)
            target = resp.headers.get("Location") if resp.status_code in (301, 302, 303, 307, 308) else None
        except httpx.HTTPError:
            target = None
        self._resolved[url] = target or url
        return self._resolved[url]

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        data, _ = await http.get_cached(self.url, _parse_json)
        if not isinstance(data, list):
            raise SourceFetchError(f"Unexpected listings format at {self.url}")
        jobs: list[Job] = []
        unresolved: list[dict] = []
        for raw in data:
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
                continue
            if not self._is_candidate(raw):
                continue
            uid = f"{self.id}:{raw['id']}"
            if self.resolve_redirects and not ctx.is_known(uid):
                unresolved.append(raw)
            else:
                jobs.append(self.parse_job(raw))
        if unresolved:
            sem = asyncio.Semaphore(5)

            async def one(raw: dict) -> Job:
                async with sem:
                    return self.parse_job(raw, await self._resolve(http, raw.get("url") or ""))

            jobs.extend(await asyncio.gather(*(one(r) for r in unresolved[: max(ctx.max_details, 60)])))
            jobs.extend(self.parse_job(r) for r in unresolved[max(ctx.max_details, 60):])
        return jobs
