"""The bot must keep answering Discord commands while a scan runs (Discord gives 3 seconds)."""
from __future__ import annotations

import asyncio
import time

from bot.notify import Notifier
from bot.scanner import Scanner
from tests.conftest import NOW, make_job
from tests.test_scanner import FakeSource, make_http_dummy


async def test_event_loop_stays_responsive_during_large_scan(settings, db):
    jobs = [make_job(source="fake:big", source_job_id=str(i), company=f"Company {i % 300}",
                     title=f"Software Developer Intern {i}", url=f"https://x.example/j/{i}")
            for i in range(4000)]

    async def send(target, payload):
        pass

    scanner = Scanner(settings, db, [FakeSource("big", jobs)], make_http_dummy(),
                      notify=Notifier(db, settings, send, pause=0).deliver_pending, now=lambda: NOW)
    max_gap = 0.0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal max_gap
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            max_gap = max(max_gap, now - last)
            last = now

    hb = asyncio.create_task(heartbeat())
    for _ in range(2):  # first scan inserts, second scan only sees known jobs
        await scanner.run("test")
    stop.set()
    await hb
    assert db.count_jobs() == 4000
    assert max_gap < 0.5, f"event loop blocked for {max_gap:.2f}s during a scan"
