"""Check "possibly closed" postings directly on the company's own job board.

When no source lists a posting anymore, the bot looks it up on the official board its link points to
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters — tracker short links are resolved first):

* still there            -> keep it open and use the company's link;
* gone, but the same role was re-posted on that board (same title, compatible place and term)
                         -> switch to the new posting's link;
* gone                   -> marked "Fermé (vérifié)";
* anything else (unsupported site, rate limit, network error) -> left as "Peut-être fermé".

Verified on 2026-11-17: Workday answers HTTP 403 (errorCode S22) for a closed posting and 200 with
jobPostingInfo.canApply=true for an open one; searching a Workday site for a requisition id finds it
only while it is open. Greenhouse and Lever answer 404 for closed postings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import httpx

from .classify import detect_terms_in_text
from .db import Database, JobRow
from .dedupe import _UUID, normalize_title, provinces_compatible, terms_compatible
from .http import HttpClient, SourceFetchError
from .location import parse_location

log = logging.getLogger(__name__)

SHORTENERS = {"go.vansh.nyc"}
RECHECK_AFTER = timedelta(hours=72)  # closed / unsupported results are not re-checked before this
# "unknown" for these reasons is final for RECHECK_AFTER; any other unknown (rate limit, server error, board
# unavailable) is stored as "retry" and tried again on the next scan.
FINAL_UNKNOWN = ("site not supported", "no link")


@dataclass
class Ref:
    """A posting on a company job board, read from its URL."""
    ats: str  # greenhouse | lever | ashby | workday | smartrecruiters
    board: str  # greenhouse board, lever company, ashby org, workday "host/tenant/site", smartrecruiters company
    job_id: str  # greenhouse id, lever/ashby uuid, workday externalPath, smartrecruiters id
    url: str


@dataclass
class CheckResult:
    status: str  # open | moved | closed | unknown
    url: str | None = None
    detail: str = ""


def parse_ref(url: str | None) -> Ref | None:
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.netloc or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    query = {k.lower(): v for k, v in parse_qsl(parts.query)}
    if host.endswith("greenhouse.io"):
        if "jobs" in segs:
            i = segs.index("jobs")
            if i >= 1 and i + 1 < len(segs) and segs[i + 1].isdigit():
                return Ref("greenhouse", segs[i - 1], segs[i + 1], url)
        if query.get("for") and query.get("token", "").isdigit():
            return Ref("greenhouse", query["for"], query["token"], url)
    if host == "jobs.lever.co" and len(segs) >= 2 and re.fullmatch(_UUID, segs[1]):
        return Ref("lever", segs[0], segs[1].lower(), url)
    if host == "jobs.ashbyhq.com" and len(segs) >= 2 and re.fullmatch(_UUID, segs[1]):
        return Ref("ashby", segs[0], segs[1].lower(), url)
    if host.endswith(".myworkdayjobs.com") and "job" in segs:
        i = segs.index("job")
        site_parts = [s for s in segs[:i] if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if site_parts and i + 1 < len(segs):
            tenant = host.split(".")[0]
            return Ref("workday", f"{host}/{tenant}/{site_parts[-1]}", "/" + "/".join(segs[i:]), url)
    if host == "jobs.smartrecruiters.com" and len(segs) >= 2:
        m = re.match(r"(\d{9,})", segs[1])
        if m:
            return Ref("smartrecruiters", segs[0], m.group(1), url)
    return None


def _tokens(title: str) -> set[str]:
    return set(normalize_title(title).split()) - {"intern", "coop", "student"}


def same_role(job: JobRow, title: str, location: str | None = None) -> bool:
    """Is a posting with this title (on the same company board) the same role as `job`?"""
    a, b = _tokens(job.title), _tokens(title)
    if not a or not b:
        return False
    if a != b and len(a & b) / len(a | b) < 0.75:
        return False
    other_terms = detect_terms_in_text(title)
    if other_terms and job.term and not terms_compatible(job.term, other_terms[0]):
        return False
    if location:
        provs = parse_location(location).provinces
        if provs and job.provinces and not provinces_compatible(job.provinces, provs):
            return False
    return True


class LinkChecker:
    def __init__(self, db: Database, http: HttpClient, limit: int = 30) -> None:
        self.db = db
        self.http = http
        self.limit = limit

    # ------------------------------------------------------------------ helpers
    async def _get(self, url: str, **kw) -> httpx.Response | None:
        try:
            return await self.http.request("GET", url, **kw)
        except SourceFetchError as exc:
            log.debug("link check: %s", exc)
            return None

    async def _post(self, url: str, payload: dict) -> httpx.Response | None:
        try:
            return await self.http.request("POST", url, json=payload)
        except SourceFetchError as exc:
            log.debug("link check: %s", exc)
            return None

    @staticmethod
    def _json(resp: httpx.Response | None):
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def resolve(self, url: str) -> str:
        """Follow tracker short links (go.vansh.nyc/…) to the company's own URL."""
        for _ in range(3):
            if (urlsplit(url).netloc or "").lower() not in SHORTENERS:
                return url
            try:
                resp = await self.http.client.get(url, follow_redirects=False, timeout=10)
            except httpx.HTTPError:
                return url
            target = resp.headers.get("Location") if resp.status_code in (301, 302, 303, 307, 308) else None
            if not target:
                return url
            url = str(httpx.URL(url).join(target))
        return url

    # ------------------------------------------------------------------ per-ATS checks
    async def _greenhouse(self, job: JobRow, ref: Ref) -> CheckResult:
        api = f"https://boards-api.greenhouse.io/v1/boards/{ref.board}/jobs"
        resp = await self._get(f"{api}/{ref.job_id}")
        if resp is not None and resp.status_code == 200:
            return CheckResult("open", (self._json(resp) or {}).get("absolute_url") or ref.url)
        if resp is None or resp.status_code != 404:
            return CheckResult("unknown", detail=f"greenhouse {getattr(resp, 'status_code', 'error')}")
        data = self._json(await self._get(api)) or {}
        if "jobs" not in data:
            return CheckResult("unknown", detail="greenhouse board unavailable")
        for p in data["jobs"]:
            if same_role(job, p.get("title", ""), (p.get("location") or {}).get("name")):
                return CheckResult("moved", p.get("absolute_url"))
        return CheckResult("closed", detail="greenhouse 404")

    async def _lever(self, job: JobRow, ref: Ref) -> CheckResult:
        api = f"https://api.lever.co/v0/postings/{ref.board}"
        resp = await self._get(f"{api}/{ref.job_id}")
        if resp is not None and resp.status_code == 200:
            return CheckResult("open", (self._json(resp) or {}).get("hostedUrl") or ref.url)
        if resp is None or resp.status_code != 404:
            return CheckResult("unknown", detail=f"lever {getattr(resp, 'status_code', 'error')}")
        data = self._json(await self._get(api, params={"mode": "json"}))
        if not isinstance(data, list):
            return CheckResult("unknown", detail="lever board unavailable")
        for p in data:
            if same_role(job, p.get("text", ""), (p.get("categories") or {}).get("location")):
                return CheckResult("moved", p.get("hostedUrl"))
        return CheckResult("closed", detail="lever 404")

    async def _ashby(self, job: JobRow, ref: Ref) -> CheckResult:
        data = self._json(await self._get(f"https://api.ashbyhq.com/posting-api/job-board/{ref.board}"))
        if not isinstance(data, dict) or "jobs" not in data:
            return CheckResult("unknown", detail="ashby board unavailable")
        for p in data["jobs"]:
            if str(p.get("id", "")).lower() == ref.job_id:
                return CheckResult("open", p.get("jobUrl") or ref.url)
        for p in data["jobs"]:
            if same_role(job, p.get("title", ""), p.get("location")):
                return CheckResult("moved", p.get("jobUrl"))
        return CheckResult("closed", detail="not on the ashby board")

    async def _workday(self, job: JobRow, ref: Ref) -> CheckResult:
        host, tenant, site = ref.board.split("/")
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        resp = await self._get(base + ref.job_id)
        if resp is not None and resp.status_code == 200:
            info = (self._json(resp) or {}).get("jobPostingInfo") or {}
            if info and info.get("canApply") is not False:
                return CheckResult("open", info.get("externalUrl") or ref.url)
        elif resp is None or resp.status_code not in (403, 404, 410):
            return CheckResult("unknown", detail=f"workday {getattr(resp, 'status_code', 'error')}")
        data = self._json(await self._post(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0,
                                                            "searchText": job.title}))
        if not isinstance(data, dict) or "jobPostings" not in data:
            return CheckResult("unknown", detail="workday search unavailable")
        for p in data["jobPostings"]:
            if p.get("externalPath") and same_role(job, p.get("title", ""), p.get("locationsText")):
                return CheckResult("moved", f"https://{host}/{site}{p['externalPath']}")
        return CheckResult("closed", detail=f"workday {resp.status_code if resp is not None else ''}".strip())

    async def _smartrecruiters(self, job: JobRow, ref: Ref) -> CheckResult:
        api = f"https://api.smartrecruiters.com/v1/companies/{ref.board}/postings"
        resp = await self._get(f"{api}/{ref.job_id}")
        if resp is not None and resp.status_code == 200:
            return CheckResult("open", f"https://jobs.smartrecruiters.com/{ref.board}/{ref.job_id}")
        if resp is None or resp.status_code != 404:
            return CheckResult("unknown", detail=f"smartrecruiters {getattr(resp, 'status_code', 'error')}")
        data = self._json(await self._get(api, params={"q": job.title, "limit": 50})) or {}
        for p in data.get("content") or []:
            loc = p.get("location") or {}
            if same_role(job, p.get("name", ""), ", ".join(str(v) for v in (loc.get("city"), loc.get("region")) if v)):
                return CheckResult("moved", f"https://jobs.smartrecruiters.com/{ref.board}/{p.get('id')}")
        return CheckResult("closed", detail="smartrecruiters 404")

    async def _generic(self, url: str) -> CheckResult:
        """Unsupported site (iCIMS, Oracle, custom pages): only a clear 404/410 counts as closed."""
        resp = await self._get(url, follow_redirects=True)
        if resp is not None and resp.status_code in (404, 410):
            return CheckResult("closed", detail=f"page {resp.status_code}")
        if resp is None or resp.status_code == 429 or resp.status_code >= 500:
            return CheckResult("unknown", detail="site unreachable")
        return CheckResult("unknown", detail=FINAL_UNKNOWN[0])

    async def check(self, job: JobRow) -> CheckResult:
        urls = [l["url"] for l in self.db.links_for(job.id)] + [job.url]
        seen: set[str] = set()
        best: CheckResult | None = None
        for raw in urls:
            if not raw or raw in seen:
                continue
            seen.add(raw)
            url = await self.resolve(raw)
            if url in seen and url != raw:
                continue
            seen.add(url)
            ref = parse_ref(url)
            result = await self._generic(url) if ref is None else await getattr(self, f"_{ref.ats}")(job, ref)
            if result.status in ("open", "moved"):
                return result
            if result.status == "closed" or best is None:  # a clear "closed" beats "unknown"
                best = result
        return best or CheckResult("unknown", detail=FINAL_UNKNOWN[1])

    # ------------------------------------------------------------------ scan step
    async def run(self, now: datetime, active_sources: set[str] | None = None) -> dict[str, int]:
        stats = {"checked": 0, "open": 0, "moved": 0, "closed": 0, "unknown": 0}
        if self.limit <= 0:
            return stats
        stale = self.db.stale_job_ids(now, active_sources=active_sources)
        recent = self.db.recent_link_checks(now - RECHECK_AFTER)
        todo = [j for j in (self.db.get_job(i) for i in stale - recent) if j]
        todo.sort(key=lambda j: -j.score)
        for job in todo[: self.limit]:
            try:
                result = await self.check(job)
            except Exception:  # one odd page must not stop the scan
                log.exception("Link check failed for job %s", job.id)
                result = CheckResult("unknown", detail="error")
            stats["checked"] += 1
            stats[result.status] += 1
            stored = "retry" if result.status == "unknown" and result.detail not in FINAL_UNKNOWN else result.status
            self.db.record_link_check(job.id, stored, result.url, result.detail, now)
            if result.status in ("open", "moved") and result.url:
                ref = parse_ref(result.url)
                self.db.confirm_open(job.id, f"verify:{ref.ats if ref else 'site'}", result.url, now)
        if stats["checked"]:
            log.info("Link check: %(checked)d possibly-closed postings checked on company sites — %(open)d still "
                     "open, %(moved)d re-posted (link updated), %(closed)d closed, %(unknown)d unknown", stats)
        return stats

