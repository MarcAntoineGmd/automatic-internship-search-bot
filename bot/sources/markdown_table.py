"""Markdown-table internship trackers (e.g. negarprh/Canadian-Tech-Internships-2027 README).

Expected columns: Company | Role | Location | Apply | Date Posted. A company cell of "↳"
repeats the previous company. Rows whose Apply cell has no link (e.g. "Closed🔒") are skipped.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import httpx

from ..dedupe import canonical_url_key
from ..http import HttpClient, SourceFetchError
from ..models import Job
from ..textutil import strip_html
from .base import FetchContext, Source

LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)|href=\"(https?://[^\"]+)\"")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>", re.I)
MD_TEXT_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")


def _cell_text(cell: str) -> str:
    cell = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", cell)  # images/badges
    cell = MD_TEXT_LINK_RE.sub(r"\1", cell)
    cell = re.sub(r"<br\s*/?>|</br>", " | ", cell, flags=re.I)
    cell = strip_html(cell).replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", cell).strip()


def _parse_date(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_markdown_table(markdown: str) -> list[dict]:
    rows: list[dict] = []
    header: list[str] | None = None
    last_company = ""
    for line in markdown.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if header and rows and s == "":
                continue
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "company" in lowered and any("role" in c or "position" in c or "title" in c for c in lowered):
            header = lowered
            continue
        if header is None or set(s.replace("|", "").strip()) <= set("-: "):
            continue
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        row = dict(zip(header, cells))
        company = _cell_text(row.get("company", ""))
        if company in ("↳", "", "^") :
            company = last_company
        else:
            last_company = company
        apply_cell = row.get("apply") or row.get("application") or row.get("link") or ""
        role_cell = row.get("role") or row.get("position") or row.get("title") or ""
        apply_links = IMG_RE.sub("", apply_cell)  # drop badge images: [![Apply](badge.svg)](real-url)
        m = LINK_RE.search(apply_links) or LINK_RE.search(IMG_RE.sub("", role_cell))
        url = (m.group(1) or m.group(2)) if m else None
        rows.append({
            "company": company,
            "title": _cell_text(role_cell),
            "location": _cell_text(row.get("location", "")),
            "url": url,
            "closed": url is None or "🔒" in apply_cell,
            "date": _parse_date(_cell_text(row.get("date posted") or row.get("date") or "")),
        })
    return rows


class MarkdownTableSource(Source):
    kind = "markdown_table"
    priority = 45

    def __init__(self, url: str | None = None, internships_only: bool = True, **kw) -> None:
        if not url:
            raise ValueError("markdown_table source requires 'url'")
        super().__init__(name=kw.pop("name", None) or url, **kw)
        self.url = url
        self.internships_only = internships_only

    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        text, _ = await http.get_cached(self.url, lambda r: r.text)
        rows = parse_markdown_table(text)
        if not rows:
            raise SourceFetchError(f"No table rows found at {self.url}")
        jobs = []
        for r in rows:
            if r["closed"] or not r["title"]:
                continue
            key = canonical_url_key(r["url"]) or ""
            ident = hashlib.sha1(f"{r['company']}|{r['title']}|{key}".encode()).hexdigest()[:16]
            jobs.append(Job(
                source=self.id,
                source_job_id=ident,
                company=r["company"],
                title=r["title"],
                url=r["url"],
                location=r["location"],
                date_posted=r["date"],
                assume_internship=self.internships_only,
                assume_country=self.assume_country,
            ))
        return jobs
