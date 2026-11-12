"""One scan = fetch every source (isolated), classify, de-duplicate, persist, notify."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from .classify import classify
from .config import Settings
from .db import Database
from .http import HttpClient, SourceFetchError
from .models import Job
from .sources.base import FetchContext, Source
from .textutil import utcnow

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    source: Source
    ok: bool
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    started_at: datetime | None = None
    new_jobs: int = 0


@dataclass
class ScanReport:
    skipped: bool = False
    scan_id: int | None = None
    trigger: str = "scheduled"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sources_ok: int = 0
    sources_failed: int = 0
    jobs_fetched: int = 0
    qualified: int = 0
    new_job_ids: list[int] = field(default_factory=list)
    baseline_job_ids: list[int] = field(default_factory=list)
    duplicates: int = 0
    rejected: int = 0
    notifications_sent: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def new_jobs(self) -> int:
        return len(self.new_job_ids)

    def summary(self) -> str:
        if self.skipped:
            return "Scan skipped: another scan is already running."
        text = (f"Sources OK {self.sources_ok}, failed {self.sources_failed} | fetched {self.jobs_fetched} | "
                f"qualifying new {self.new_jobs} (initial import {len(self.baseline_job_ids)}) | "
                f"duplicates {self.duplicates} | DMs sent {self.notifications_sent}")
        return text


NotifyFn = Callable[[ScanReport], Awaitable[int]]


class Scanner:
    def __init__(self, settings: Settings, db: Database, sources: list[Source], http: HttpClient,
                 notify: NotifyFn | None = None, now: Callable[[], datetime] = utcnow) -> None:
        self.settings = settings
        self.db = db
        self.sources = sources
        self.http = http
        self.notify = notify
        self.now = now
        self._lock = asyncio.Lock()
        self.last_report: ScanReport | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, trigger: str = "scheduled") -> ScanReport:
        if self._lock.locked():
            log.info("Scan (%s) skipped: a scan is already running", trigger)
            return ScanReport(skipped=True, trigger=trigger)
        async with self._lock:
            report = await self._run(trigger)
            self.last_report = report
            return report

    async def _fetch_one(self, source: Source, ctx: FetchContext, sem: asyncio.Semaphore) -> SourceResult:
        started = self.now()
        t0 = time.perf_counter()
        async with sem:
            try:
                jobs = await source.fetch(self.http, ctx)
                return SourceResult(source, True, jobs=jobs, started_at=started,
                                    duration_ms=int((time.perf_counter() - t0) * 1000))
            except SourceFetchError as exc:
                err = str(exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # parsing bugs etc. must never break other sources
                log.exception("Source %s crashed", source.id)
                err = f"{type(exc).__name__}: {exc}"
        log.warning("Source %s failed: %s", source.id, err)
        return SourceResult(source, False, error=err, started_at=started,
                            duration_ms=int((time.perf_counter() - t0) * 1000))

    async def _run(self, trigger: str) -> ScanReport:
        s = self.settings
        report = ScanReport(trigger=trigger, started_at=self.now())
        self.db.sync_filter_signature(s.filters.signature())
        report.scan_id = self.db.start_scan(trigger, now=report.started_at)
        baseline_sources = {src.id for src in self.sources if not self.db.has_successful_check(src.id)}
        ctx = FetchContext(is_known=self.db.is_known_uid, max_details=s.max_detail_requests_per_source)
        sem = asyncio.Semaphore(s.source_concurrency)
        results = await asyncio.gather(*(self._fetch_one(src, ctx, sem) for src in self.sources))

        baseline_candidates: list[tuple[int, int]] = []  # (score, job_id)
        now = self.now()
        pending: list[tuple[SourceResult, Job]] = []
        for res in sorted(results, key=lambda r: (r.source.priority, r.source.id)):
            if not res.ok:
                report.sources_failed += 1
                report.errors[res.source.id] = res.error or "unknown error"
                continue
            report.sources_ok += 1
            report.jobs_fetched += len(res.jobs)
            pending.extend((res, job) for job in res.jobs)
        # Work in ~50 ms batches (one DB transaction each) and yield to the event loop in between,
        # so Discord commands still get answered within their 3-second limit during a scan.
        i = 0
        while i < len(pending):
            deadline = time.perf_counter() + 0.02
            with self.db.transaction():
                while i < len(pending) and time.perf_counter() < deadline:
                    res, job = pending[i]
                    i += 1
                    try:
                        outcome = self._process(job, now, res.source.id in baseline_sources,
                                                baseline_candidates, report)
                    except Exception:  # one malformed job must not kill the scan
                        log.exception("Failed to process job %s", job.uid)
                        continue
                    if outcome == "new":
                        res.new_jobs += 1
            await asyncio.sleep(0)
        for res in results:
            self.db.record_source_check(report.scan_id, res.source.id, res.ok, res.started_at or now,
                                        res.duration_ms, jobs_fetched=len(res.jobs), new_jobs=res.new_jobs,
                                        error=res.error)

        # Initial import of a source: keep only the best few as notifications, store the rest silently.
        if baseline_candidates:
            baseline_candidates.sort(reverse=True)
            with self.db.transaction():
                for _, job_id in baseline_candidates[s.baseline_notify_limit:]:
                    self.db.mark_notified(job_id, status="baseline", now=now)
                    report.baseline_job_ids.append(job_id)

        report.finished_at = self.now()
        self.db.finish_scan(report.scan_id, sources_ok=report.sources_ok, sources_failed=report.sources_failed,
                            jobs_fetched=report.jobs_fetched, jobs_qualified=report.qualified,
                            new_jobs=report.new_jobs, now=report.finished_at,
                            error="; ".join(f"{k}: {v}" for k, v in list(report.errors.items())[:5]) or None)
        if self.notify:
            try:
                report.notifications_sent = await self.notify(report)
                self.db.add_notifications_to_scan(report.scan_id, report.notifications_sent)
            except Exception:
                log.exception("Notification delivery failed")
        log.info("Scan %s (%s): %s", report.scan_id, trigger, report.summary())
        return report

    def _process(self, job: Job, now: datetime, from_baseline_source: bool,
                 baseline_candidates: list[tuple[int, int]], report: ScanReport) -> str:
        if not job.title or not job.url:
            return "invalid"
        if self.db.is_rejected(job.uid):
            report.rejected += 1
            return "rejected"
        # Cheap exact-key duplicate check first (no classification needed).
        dup = self.db.find_duplicate(job, use_fingerprint=False)
        if dup is not None:
            self.db.touch(dup, job, now=now)
            report.duplicates += 1
            return "duplicate"
        cls = classify(job, self.settings.filters, now=now)
        if not cls.included:
            reason = (cls.exclusion_reason or "excluded").split("(")[0].strip()
            report.rejection_reasons[reason] = report.rejection_reasons.get(reason, 0) + 1
            report.rejected += 1
            if job.details_fetched or job.description:
                self.db.add_rejected(job.uid, cls.exclusion_reason, now=now)
            return "rejected"
        dup = self.db.find_duplicate(job, term=cls.term, provinces=cls.provinces)
        if dup is not None:
            self.db.touch(dup, job, now=now)
            report.duplicates += 1
            return "duplicate"
        status = None if cls.score >= self.settings.notify_min_score else "skipped"
        job_id = self.db.insert_job(job, cls, now=now, notify_status=status)
        report.qualified += 1
        report.new_job_ids.append(job_id)
        if from_baseline_source and status is None:
            baseline_candidates.append((cls.score, job_id))
        return "new"
