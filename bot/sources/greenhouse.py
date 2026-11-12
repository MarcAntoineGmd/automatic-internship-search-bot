"""Greenhouse public Job Board API: https://developers.greenhouse.io/job-board.html"""
from __future__ import annotations

import asyncio
import logging

from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html
from .base import FetchContext, Source, looks_like_student_role

log = logging.getLogger(__name__)
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


class GreenhouseSource(Source):
    kind = "greenhouse"
    priority = 10

    def __init__(self, board: str | None = None, **kw) -> None:
        super().__init__(name=board or kw.pop("name", None), **kw)

    def parse_job(self, raw: dict, description: str = "", details_fetched: bool = True) -> Job:
        offices = raw.get("offices") or []
        loc = (raw.get("location") or {}).get("name") or ""
        office_locs = [o.get("location") or o.get("name") for o in offices if (o.get("location") or o.get("name"))]
        location = loc
        extra = [o for o in office_locs if o and o not in loc]
        if extra:
            location = " | ".join([loc] + extra) if loc else " | ".join(extra)
        employment = None
        for md in raw.get("metadata") or []:
            name = (md.get("name") or "").lower()
            if any(k in name for k in ("employment type", "job type", "time type")) and md.get("value"):
                val = md["value"]
                employment = ", ".join(val) if isinstance(val, list) else str(val)
        job_id = str(raw["id"])
        return Job(
            source=self.id,
            source_job_id=job_id,
            company=raw.get("company_name") or self.company,
            title=(raw.get("title") or "").strip(),
            url=raw.get("absolute_url") or f"https://job-boards.greenhouse.io/{self.name}/jobs/{job_id}",
            description=description or strip_html(raw.get("content")),
            location=location,
            employment_type=employment,
            date_posted=parse_datetime(raw.get("first_published")),  # updated_at is NOT a posting date
            assume_country=self.assume_country,
            extra_keys=[f"greenhouse:{job_id}"],
            details_fetched=details_fetched,
        )

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        data = await http.get_json(API.format(board=self.name))
        if not isinstance(data, dict) or "jobs" not in data:
            raise SourceFetchError(f"Unexpected Greenhouse payload for board {self.name!r}")
        jobs: list[Job] = []
        to_detail: list[dict] = []
        for raw in data["jobs"]:
            if not raw.get("id") or not raw.get("title"):
                continue
            if not looks_like_student_role(raw["title"]):
                continue  # full-time/experienced roles never qualify; skip early
            uid = f"{self.id}:{raw['id']}"
            if raw.get("content") or ctx.is_known(uid):
                jobs.append(self.parse_job(raw, details_fetched=bool(raw.get("content"))))
            else:
                to_detail.append(raw)

        async def detail(raw: dict) -> Job:
            try:
                full = await http.get_json(f"{API.format(board=self.name)}/{raw['id']}")
                return self.parse_job({**raw, **full})
            except SourceFetchError as exc:
                log.debug("Greenhouse detail failed for %s: %s", raw["id"], exc)
                return self.parse_job(raw, details_fetched=False)

        limited = to_detail[: ctx.max_details]
        jobs.extend(await asyncio.gather(*(detail(r) for r in limited)))
        jobs.extend(self.parse_job(r, details_fetched=False) for r in to_detail[ctx.max_details:])
        return jobs
