"""asyncio-based scheduling: startup scan, periodic scans, optional daily summary."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .textutil import utcnow

log = logging.getLogger(__name__)


class ScanScheduler:
    """Runs ``job`` immediately, then every ``interval_minutes``. Never overlaps itself
    (each run is awaited) and the Scanner's lock also rejects overlapping manual scans."""

    def __init__(self, job: Callable[[str], Awaitable[object]], interval_minutes: float,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.job = job
        self.interval = timedelta(minutes=interval_minutes)
        self._sleep = sleep
        self._now = now
        self.next_run_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.runs = 0
        self._task: asyncio.Task | None = None

    async def run_forever(self, max_runs: int | None = None) -> None:
        trigger = "startup"
        while max_runs is None or self.runs < max_runs:
            started = self._now()
            self.last_run_at = started
            self.next_run_at = started + self.interval
            try:
                await self.job(trigger)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled scan failed")
            self.runs += 1
            trigger = "scheduled"
            if max_runs is not None and self.runs >= max_runs:
                break
            wait = (self.next_run_at - self._now()).total_seconds()
            await self._sleep(max(1.0, wait))

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="scan-scheduler")
        return self._task

    def stop(self) -> None:
        if self._task:
            self._task.cancel()


def seconds_until_hour(now: datetime, hour: int, tz: str) -> float:
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


class DailySummaryScheduler:
    def __init__(self, job: Callable[[], Awaitable[object]], hour: int, tz: str,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.job, self.hour, self.tz = job, hour, tz
        self._sleep, self._now = sleep, now
        self._task: asyncio.Task | None = None

    async def run_forever(self, max_runs: int | None = None) -> None:
        runs = 0
        while max_runs is None or runs < max_runs:
            await self._sleep(seconds_until_hour(self._now(), self.hour, self.tz))
            try:
                await self.job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Daily summary failed")
            runs += 1

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="daily-summary")
        return self._task

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
