"""Ashby public Job Posting API: https://developers.ashbyhq.com/docs/public-job-posting-api"""
from __future__ import annotations

from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html
from .base import FetchContext, Source

API = "https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false"


class AshbySource(Source):
    kind = "ashby"
    priority = 10

    def __init__(self, org: str | None = None, **kw) -> None:
        super().__init__(name=org or kw.pop("name", None), **kw)

    @staticmethod
    def _addr_text(addr: dict | None) -> tuple[str, str | None]:
        pa = (addr or {}).get("postalAddress") or {}
        parts = [pa.get("addressLocality"), (pa.get("addressRegion") or "").strip() or None, pa.get("addressCountry")]
        return ", ".join(p for p in parts if p), pa.get("addressCountry")

    def parse_job(self, raw: dict) -> Job:
        locs: list[str] = []
        countries: list[str] = []
        primary_text, primary_country = self._addr_text(raw.get("address"))
        locs.append(primary_text or raw.get("location") or "")
        if primary_country:
            countries.append(primary_country)
        for sec in raw.get("secondaryLocations") or []:
            text, country = self._addr_text(sec.get("address"))
            locs.append(text or sec.get("location") or "")
            if country:
                countries.append(country)
        if raw.get("location") and raw["location"] not in " ".join(locs):
            locs.insert(0, raw["location"])
        wp = (raw.get("workplaceType") or "").lower().replace("-", "")
        workplace = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp)
        if not workplace and raw.get("isRemote"):
            workplace = "remote"
        job_id = str(raw["id"])
        return Job(
            source=self.id,
            source_job_id=job_id,
            company=self.company,
            title=(raw.get("title") or "").strip(),
            url=raw.get("jobUrl") or raw.get("applyUrl") or f"https://jobs.ashbyhq.com/{self.name}/{job_id}",
            description=raw.get("descriptionPlain") or strip_html(raw.get("descriptionHtml")),
            location=" | ".join(l for l in dict.fromkeys(locs) if l),
            countries=list(dict.fromkeys(countries)),
            workplace=workplace,
            employment_type=raw.get("employmentType"),
            date_posted=parse_datetime(raw.get("publishedAt")),
            assume_country=self.assume_country,
            extra_keys=[f"ashby:{job_id.lower()}"],
        )

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        data = await http.get_json(API.format(org=self.name))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceFetchError(f"Unexpected Ashby payload for {self.name!r}")
        return [self.parse_job(r) for r in data["jobs"]
                if r.get("id") and r.get("title") and r.get("isListed", True)]
