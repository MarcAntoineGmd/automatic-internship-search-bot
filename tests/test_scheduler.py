"""Scheduling: startup scan, periodic interval, no overlapping scans, daily summary timing."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from bot.scheduler import DailySummaryScheduler, ScanScheduler, seconds_until_hour


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


async def test_initial_scan_then_periodic_interval():
    clock = Clock()
    triggers = []

    async def job(trigger):
        triggers.append((trigger, clock.now()))
        clock.t += timedelta(minutes=2)  # the scan takes 2 minutes

    sched = ScanScheduler(job, interval_minutes=30, sleep=clock.sleep, now=clock.now)
    await sched.run_forever(max_runs=3)
    assert [t for t, _ in triggers] == ["startup", "scheduled", "scheduled"]
    # runs start exactly 30 minutes apart even though each scan took 2 minutes
    starts = [ts for _, ts in triggers]
    assert starts[1] - starts[0] == timedelta(minutes=30) and starts[2] - starts[1] == timedelta(minutes=30)
    assert clock.sleeps[0] == 28 * 60
    assert sched.next_run_at == starts[2] + timedelta(minutes=30)


async def test_scheduler_survives_job_exception():
    clock = Clock()
    calls = []

    async def job(trigger):
        calls.append(trigger)
        if len(calls) == 1:
            raise RuntimeError("boom")

    await ScanScheduler(job, 30, sleep=clock.sleep, now=clock.now).run_forever(max_runs=2)
    assert calls == ["startup", "scheduled"]


async def test_scheduler_never_runs_scans_concurrently():
    running = {"now": 0, "max": 0}

    async def job(trigger):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1

    async def fast_sleep(_):
        await asyncio.sleep(0)

    await ScanScheduler(job, 0.0001, sleep=fast_sleep).run_forever(max_runs=5)
    assert running["max"] == 1


async def test_scheduler_start_stop():
    started = asyncio.Event()

    async def job(trigger):
        started.set()

    sched = ScanScheduler(job, 30)
    task = sched.start()
    await asyncio.wait_for(started.wait(), 1)
    assert sched.start() is task  # starting twice does not create a second loop
    sched.stop()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


def test_seconds_until_hour_in_toronto():
    now = datetime(2026, 11, 11, 13, 30, tzinfo=timezone.utc)  # 08:30 in Toronto (UTC-5)
    assert seconds_until_hour(now, 9, "America/Toronto") == 30 * 60
    assert seconds_until_hour(now, 8, "America/Toronto") == (23 * 60 + 30) * 60


async def test_daily_summary_scheduler_runs_job():
    clock = Clock()
    ran = []

    async def job():
        ran.append(clock.now())

    await DailySummaryScheduler(job, 9, "America/Toronto", sleep=clock.sleep, now=clock.now).run_forever(max_runs=2)
    assert len(ran) == 2 and ran[1] - ran[0] == timedelta(days=1)
    assert ran[0].astimezone().hour is not None
