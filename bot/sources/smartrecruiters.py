"""SmartRecruiters public Posting API: https://developers.smartrecruiters.com/docs/posting-api"""
from __future__ import annotations

import asyncio
import logging

from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html
from .base import FetchContext, Source, looks_like_student_role

log = logging.getLogger(__name__)
API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"


class SmartRecruitersSource(Source):
    kind = "smartrecruiters"
    priority = 20

    def __init__(self, company_id: str | None = None, country: str | None = "ca", max_pages: int = 5, **kw) -> None:
        super().__init__(name=company_id or kw.pop("name", None), **kw)
        self.country = country
        self.max_pages = max_pages

    def parse_job(self, raw: dict, detail: dict | None = None) -> Job:
        loc = raw.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            p for p in (loc.get("city"), (loc.get("region") or "").upper() or None, loc.get("country", "").upper()) if p)
        countries = ["Canada"] if (loc.get("country") or "").lower() == "ca" else (
            [loc["country"].upper()] if loc.get("country") else [])
        workplace = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
        description = ""
        if detail:
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description = "\n\n".join(strip_html((sections.get(k) or {}).get("text"))
                                      for k in ("jobDescription", "qualifications", "additionalInformation"))
        emp = (raw.get("typeOfEmployment") or {}).get("label")
        level = (raw.get("experienceLevel") or {}).get("id")
        if level == "internship":
            emp = f"{emp or ''} (internship)".strip()
        job_id = str(raw["id"])
        return Job(
            source=self.id,
            source_job_id=job_id,
            company=(raw.get("company") or {}).get("name") or self.company,
            title=(raw.get("name") or "").strip(),
            url=f"https://jobs.smartrecruiters.com/{self.name}/{job_id}",
            description=description.strip(),
            location=location,
            countries=countries,
            workplace=workplace,
            employment_type=emp,
            date_posted=parse_datetime(raw.get("releasedDate")),
            assume_country=self.assume_country,
            extra_keys=[f"smartrecruiters:{job_id}"],
            details_fetched=detail is not None,
        )

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        raws: list[dict] = []
        for page in range(self.max_pages):
            params = {"limit": 100, "offset": page * 100}
            if self.country:
                params["country"] = self.country
            data = await http.get_json(API.format(company=self.name), params=params)
            if not isinstance(data, dict) or "content" not in data:
                raise SourceFetchError(f"Unexpected SmartRecruiters payload for {self.name!r}")
            raws.extend(data["content"] or [])
            if len(data["content"] or []) < 100 or len(raws) >= (data.get("totalFound") or 0):
                break
        candidates = [r for r in raws if r.get("id") and looks_like_student_role(r.get("name", "")
                      + " " + ((r.get("experienceLevel") or {}).get("id") or ""))]
        jobs: list[Job] = []
        need: list[dict] = []
        for r in candidates:
            (jobs.append(self.parse_job(r)) if ctx.is_known(f"{self.id}:{r['id']}") else need.append(r))

        async def detail(r: dict) -> Job:
            try:
                return self.parse_job(r, await http.get_json(f"{API.format(company=self.name)}/{r['id']}"))
            except SourceFetchError as exc:
                log.debug("SmartRecruiters detail failed for %s: %s", r["id"], exc)
                return self.parse_job(r)

        jobs.extend(await asyncio.gather(*(detail(r) for r in need[: ctx.max_details])))
        jobs.extend(self.parse_job(r) for r in need[ctx.max_details:])
        return jobs
