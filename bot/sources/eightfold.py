"""Eightfold "PCSX" career sites (public JSON behind e.g. jobs.autodesk.com, apply.careers.microsoft.com).

Verified 2026-11-12: GET https://<host>/api/pcsx/search?domain=<domain>&query=intern&location=Canada&start=0
returns {"data": {"count": N, "positions": [{id, name, locations, postedTs, atsJobId, positionUrl, ...}]}},
and /api/pcsx/position_details?position_id=<id>&domain=<domain> returns the description.
"""
from __future__ import annotations

import asyncio
import logging

from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html
from .base import FetchContext, Source, looks_like_student_role

log = logging.getLogger(__name__)
WORKPLACE = {"onsite": "onsite", "on-site": "onsite", "remote": "remote", "hybrid": "hybrid"}


class EightfoldSource(Source):
    kind = "eightfold"
    priority = 15

    def __init__(self, host: str | None = None, domain: str | None = None, queries: list[str] | None = None,
                 location: str = "Canada", ats_key_prefix: str | None = None, max_pages: int = 5, **kw) -> None:
        if not host or not domain:
            raise ValueError("eightfold source requires 'host' and 'domain'")
        super().__init__(name=kw.pop("name", None) or domain, **kw)
        self.host, self.domain, self.location = host, domain, location
        self.queries = queries or ["intern"]
        # Companies that moved off Workday keep the old requisition id (e.g. Autodesk 26WD102151):
        # "workday:autodesk" lets tracker links to the old Workday page match the live posting here.
        self.ats_key_prefix = ats_key_prefix
        self.max_pages = max_pages

    @property
    def base(self) -> str:
        return f"https://{self.host}/api/pcsx"

    def parse_job(self, p: dict, detail: dict | None = None) -> Job:
        detail = detail or {}
        locations = detail.get("locations") or p.get("locations") or []
        ats_id = str(detail.get("atsJobId") or p.get("atsJobId") or p.get("displayJobId") or "").strip()
        path = detail.get("positionUrl") or p.get("positionUrl") or f"/careers/job/{p['id']}"
        url = detail.get("publicUrl") or f"https://{self.host}{path}"
        wp = str(detail.get("workLocationOption") or p.get("workLocationOption") or "").lower()
        return Job(
            source=self.id,
            source_job_id=str(p["id"]),
            company=self.company,
            title=(p.get("name") or "").strip(),
            url=url,
            description=strip_html(detail.get("jobDescription")),
            location=" | ".join(locations),
            workplace=WORKPLACE.get(wp),
            date_posted=parse_datetime(p.get("postedTs") or detail.get("postedTs")),
            assume_country=self.assume_country,
            extra_keys=[f"{self.ats_key_prefix}:{ats_id.upper()}"] if self.ats_key_prefix and ats_id else [],
            details_fetched=bool(detail),
        )

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        positions: dict[str, dict] = {}
        for query in self.queries:
            start = 0
            for _ in range(self.max_pages):
                params = {"domain": self.domain, "query": query, "location": self.location, "start": start}
                data = await http.get_json(f"{self.base}/search", params=params)
                body = data.get("data") if isinstance(data, dict) else None
                if not isinstance(body, dict) or "positions" not in body:
                    raise SourceFetchError(f"Unexpected Eightfold payload from {self.host}")
                page = body.get("positions") or []
                for p in page:
                    if p.get("id") and p.get("name"):
                        positions.setdefault(str(p["id"]), p)
                start += len(page)
                if not page or start >= (body.get("count") or 0):
                    break
        jobs: list[Job] = []
        need: list[dict] = []
        for p in positions.values():
            if not looks_like_student_role(p["name"]):
                continue
            (jobs.append(self.parse_job(p)) if ctx.is_known(f"{self.id}:{p['id']}") else need.append(p))

        async def detail(p: dict) -> Job:
            try:
                d = await http.get_json(f"{self.base}/position_details",
                                        params={"position_id": p["id"], "domain": self.domain, "hl": "en"})
                return self.parse_job(p, (d or {}).get("data") or {})
            except SourceFetchError as exc:
                log.debug("Eightfold detail failed for %s: %s", p.get("id"), exc)
                return self.parse_job(p)

        jobs.extend(await asyncio.gather(*(detail(p) for p in need[: ctx.max_details])))
        jobs.extend(self.parse_job(p) for p in need[ctx.max_details:])
        return jobs
