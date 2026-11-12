"""Workday career sites (public, unauthenticated JSON used by *.myworkdayjobs.com pages).

Many Canadian banks and large employers (RBC, TD, BMO, CIBC, Desjardins, Intact, Autodesk,
NVIDIA, Thomson Reuters, Ciena, ...) post on Workday. We use the same read-only JSON endpoint
the public career page uses, with small page sizes and a cap on detail requests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta

from ..dedupe import canonical_url_key
from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html, utcnow
from .base import FetchContext, Source, looks_like_student_role

log = logging.getLogger(__name__)
PAGE_SIZE = 20


def parse_posted_on(text: str | None):
    """'Posted Today' / 'Posted 3 Days Ago' -> approximate datetime; '30+ Days Ago' -> None."""
    if not text:
        return None
    t = text.lower()
    now = utcnow()
    if "today" in t:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in t:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "+" in t:
        return None
    m = re.search(r"(\d+)\s+days?", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0)
    return None


class WorkdaySource(Source):
    kind = "workday"
    priority = 20

    def __init__(self, host: str | None = None, tenant: str | None = None, site: str | None = None,
                 search: list[str] | None = None, max_pages: int = 10, **kw) -> None:
        if not host or not site:
            raise ValueError("workday source requires 'host' and 'site'")
        tenant = tenant or host.split(".")[0]
        name = kw.pop("name", None) or f"{tenant}/{site}"
        super().__init__(name=name, **kw)
        self.host = host
        self.tenant = tenant
        self.site = site
        self.search_terms = search if search is not None else ["intern", "co-op", "student"]
        self.max_pages = max_pages

    @property
    def base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    def job_url(self, external_path: str) -> str:
        return f"https://{self.host}/{self.site}{external_path}"

    def _uid_suffix(self, posting: dict) -> str:
        url = self.job_url(posting.get("externalPath", ""))
        key = canonical_url_key(url)
        if key and key.startswith("workday:"):
            return key.split(":", 2)[2]
        bullets = posting.get("bulletFields") or []
        return str(bullets[0] if bullets else posting.get("externalPath"))

    def parse_job(self, posting: dict, info: dict | None = None) -> Job:
        info = info or {}
        url = info.get("externalUrl") or self.job_url(posting.get("externalPath", ""))
        locs = [info.get("location") or posting.get("locationsText") or ""]
        for extra in info.get("additionalLocations") or []:
            locs.append(extra if isinstance(extra, str) else (extra.get("descriptor") or ""))
        countries = []
        country = (info.get("jobRequisitionLocation") or {}).get("country") or info.get("country") or {}
        if country.get("alpha2Code") == "CA":
            countries.append("Canada")
        elif country.get("alpha2Code"):
            countries.append(country["alpha2Code"])
        posted = parse_datetime(info.get("startDate")) or parse_posted_on(info.get("postedOn") or posting.get("postedOn"))
        remote = (info.get("remoteType") or "").lower()
        workplace = "remote" if "remote" in remote else ("hybrid" if "hybrid" in remote else None)
        key = canonical_url_key(url)
        return Job(
            source=self.id,
            source_job_id=self._uid_suffix(posting),
            company=self.company,
            title=(info.get("title") or posting.get("title") or "").strip(),
            url=url,
            description=strip_html(info.get("jobDescription")),
            location=" | ".join(l for l in locs if l),
            countries=countries,
            workplace=workplace,
            employment_type=info.get("timeType") or posting.get("timeType"),
            date_posted=posted,
            assume_country=self.assume_country,
            extra_keys=[key] if key else [],
            details_fetched=bool(info),
        )

    async def _list(self, http: HttpClient, search: str) -> list[dict]:
        out: list[dict] = []
        for page in range(self.max_pages):
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE, "searchText": search}
            data = await http.post_json(f"{self.base}/jobs", payload)
            if not isinstance(data, dict) or "jobPostings" not in data:
                raise SourceFetchError(f"Unexpected Workday payload for {self.name}")
            postings = data.get("jobPostings") or []
            out.extend(postings)
            total = data.get("total") or 0
            if len(postings) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= total:
                break
        return out

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        postings: dict[str, dict] = {}
        for term in self.search_terms or [""]:
            for p in await self._list(http, term):
                if p.get("externalPath") and p.get("title"):
                    postings.setdefault(p["externalPath"], p)
        candidates = [p for p in postings.values() if looks_like_student_role(p["title"])]
        jobs: list[Job] = []
        need_detail: list[dict] = []
        for p in candidates:
            uid = f"{self.id}:{self._uid_suffix(p)}"
            if ctx.is_known(uid):
                jobs.append(self.parse_job(p))
            else:
                need_detail.append(p)

        async def detail(p: dict) -> Job:
            try:
                data = await http.get_json(f"{self.base}{p['externalPath']}")
                return self.parse_job(p, data.get("jobPostingInfo") or {})
            except SourceFetchError as exc:
                log.debug("Workday detail failed for %s: %s", p.get("externalPath"), exc)
                return self.parse_job(p)

        jobs.extend(await asyncio.gather(*(detail(p) for p in need_detail[: ctx.max_details])))
        jobs.extend(self.parse_job(p) for p in need_detail[ctx.max_details:])
        return jobs
