"""Lever public Postings API: https://github.com/lever/postings-api"""
from __future__ import annotations

from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import parse_datetime, strip_html
from .base import FetchContext, Source

API = "https://api.lever.co/v0/postings/{company}?mode=json"
COUNTRY_CODES = {"CA": "Canada", "US": "United States"}


class LeverSource(Source):
    kind = "lever"
    priority = 10

    def __init__(self, company_id: str | None = None, **kw) -> None:
        super().__init__(name=company_id or kw.pop("name", None), **kw)

    def parse_job(self, raw: dict) -> Job:
        cats = raw.get("categories") or {}
        locs = [cats.get("location")] + list(cats.get("allLocations") or [])
        locs = [l for l in dict.fromkeys(l for l in locs if l)]
        country = raw.get("country")
        countries = [COUNTRY_CODES.get(country, country)] if country else []
        workplace = (raw.get("workplaceType") or "").lower()
        workplace = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(workplace)
        desc_parts = [raw.get("descriptionPlain") or strip_html(raw.get("description"))]
        for lst in raw.get("lists") or []:
            desc_parts.append(f"{lst.get('text', '')}\n{strip_html(lst.get('content'))}")
        desc_parts.append(raw.get("additionalPlain") or strip_html(raw.get("additional")))
        job_id = str(raw["id"])
        return Job(
            source=self.id,
            source_job_id=job_id,
            company=self.company,
            title=(raw.get("text") or "").strip(),
            url=raw.get("hostedUrl") or raw.get("applyUrl") or f"https://jobs.lever.co/{self.name}/{job_id}",
            description="\n\n".join(p for p in desc_parts if p).strip(),
            location=" | ".join(locs),
            countries=[c for c in countries if c],
            workplace=workplace,
            employment_type=cats.get("commitment"),
            date_posted=parse_datetime(raw.get("createdAt")),
            assume_country=self.assume_country,
            extra_keys=[f"lever:{job_id.lower()}"],
        )

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        data = await http.get_json(API.format(company=self.name))
        if not isinstance(data, list):
            msg = data.get("message") if isinstance(data, dict) else None
            raise SourceFetchError(f"Unexpected Lever payload for {self.name!r}: {msg or type(data).__name__}")
        return [self.parse_job(r) for r in data if r.get("id") and r.get("text")]
