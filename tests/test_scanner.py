"""Scanner: notification de-duplication, API failures/timeouts, source isolation, overlap prevention."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.http import HttpClient, SourceFetchError
from bot.notify import Notifier
from bot.scanner import Scanner
from bot.sources.base import FetchContext, Source
from bot.sources.greenhouse import GreenhouseSource
from tests.conftest import NOW, load_fixture, make_http, make_job


class FakeSource(Source):
    kind = "fake"

    def __init__(self, name, jobs=None, error=None, delay=0.0, priority=10):
        super().__init__(name=name)
        self.jobs, self.error, self.delay, self.calls = jobs or [], error, delay, 0
        self.priority = priority

    async def fetch(self, http, ctx: FetchContext):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [j for j in self.jobs]


class DMs:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, user_id, payload):
        if self.fail:
            raise RuntimeError("Cannot send messages to this user")
        self.sent.append((user_id, payload))

    @property
    def embeds(self):
        return [p["embed"] for _, p in self.sent if "embed" in p]


def build(settings, db, sources, dms=None, http=None):
    notifier = Notifier(db, settings, dms or DMs(), pause=0)
    scanner = Scanner(settings, db, sources, http or make_http_dummy(), notify=notifier.deliver_pending, now=lambda: NOW)
    return scanner, notifier


def make_http_dummy():
    return HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def jobs_ab():
    return [
        make_job(source="fake:a", source_job_id="1", title="Software Engineer Intern (Summer 2027)", url="https://x.example/j/1"),
        make_job(source="fake:a", source_job_id="2", title="Backend Developer Co-op", url="https://x.example/j/2",
                 location="Montreal, QC"),
    ]


async def test_notifications_sent_once_across_scans(settings, db):
    db.subscribe(1)
    dms = DMs()
    src = FakeSource("a", jobs_ab())
    scanner, _ = build(settings, db, [src], dms)
    r1 = await scanner.run()
    assert r1.new_jobs == 2 and r1.notifications_sent == 2 and len(dms.embeds) == 2
    r2 = await scanner.run()
    assert r2.new_jobs == 0 and r2.notifications_sent == 0 and len(dms.embeds) == 2
    assert db.count_notified() == 2


async def test_no_dm_when_nothing_new(settings, db):
    db.subscribe(1)
    dms = DMs()
    scanner, _ = build(settings, db, [FakeSource("a", [])], dms)
    report = await scanner.run()
    assert report.new_jobs == 0 and dms.sent == []


async def test_failed_dm_keeps_job_pending_and_retries(settings, db):
    db.subscribe(1)
    failing = DMs(fail=True)
    scanner, notifier = build(settings, db, [FakeSource("a", jobs_ab())], failing)
    report = await scanner.run()
    assert report.notifications_sent == 0
    assert db.count_pending(settings.notify_min_score) == 2
    ok = DMs()
    notifier.send = ok
    assert await notifier.deliver_pending() == 2
    assert await notifier.deliver_pending() == 0  # never twice


async def test_jobs_wait_for_subscription(settings, db):
    dms = DMs()
    scanner, notifier = build(settings, db, [FakeSource("a", jobs_ab())], dms)
    await scanner.run()
    assert dms.sent == [] and db.count_pending(settings.notify_min_score) == 2
    db.subscribe(7)
    assert await notifier.deliver_pending() == 2


async def test_dm_cap_per_scan(settings, db):
    settings.max_dms_per_scan = 1
    db.subscribe(1)
    dms = DMs()
    scanner, _ = build(settings, db, [FakeSource("a", jobs_ab())], dms)
    r1 = await scanner.run()
    assert r1.notifications_sent == 1
    r2 = await scanner.run()  # remainder goes out next scan, still exactly once
    assert r2.notifications_sent == 1 and len(dms.embeds) == 2


async def test_initial_import_baseline(settings, db):
    settings.baseline_notify_limit = 1
    db.subscribe(1)
    dms = DMs()
    scanner, _ = build(settings, db, [FakeSource("a", jobs_ab())], dms)
    report = await scanner.run()
    assert len(report.baseline_job_ids) == 1
    assert len(dms.embeds) == 1  # only the best job is DM'd on first import
    assert any("Initial import" in (p.get("content") or "") for _, p in dms.sent)
    # Source now has a successful check: later new jobs are not baselined
    scanner.sources[0].jobs.append(make_job(source="fake:a", source_job_id="3", title="Data Engineer Intern",
                                            url="https://x.example/j/3"))
    r2 = await scanner.run()
    assert r2.baseline_job_ids == [] and r2.notifications_sent == 1


async def test_source_failure_is_isolated(settings, db):
    good = FakeSource("good", jobs_ab())
    bad = FakeSource("bad", error=SourceFetchError("HTTP 503 from https://bad"))
    crash = FakeSource("crash", error=KeyError("unexpected field"))
    scanner, _ = build(settings, db, [bad, good, crash])
    report = await scanner.run()
    assert report.sources_ok == 1 and report.sources_failed == 2
    assert report.new_jobs == 2
    assert set(report.errors) == {"fake:bad", "fake:crash"}
    failing = {s["source"]: s["error"] for s in db.failing_sources()}
    assert "503" in failing["fake:bad"] and "KeyError" in failing["fake:crash"]
    assert db.last_successful_scan() is not None


async def test_all_sources_failing_is_not_reported_as_success(settings, db):
    scanner, _ = build(settings, db, [FakeSource("bad", error=SourceFetchError("boom"))])
    report = await scanner.run()
    assert report.sources_ok == 0 and report.sources_failed == 1
    assert db.last_successful_scan() is None


async def test_http_retries_then_succeeds(router):
    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return load_fixture("lever_postings.json")

    router.add("GET", r"postings/kepler", flaky)
    from bot.sources.lever import LeverSource

    jobs = await LeverSource(company_id="kepler").fetch(make_http(router, max_retries=3), FetchContext())
    assert attempts["n"] == 3 and len(jobs) == 3


async def test_http_timeout_raises_source_error(router):
    router.add("GET", r"boards/slow/jobs", httpx.ReadTimeout("timed out"))
    with pytest.raises(SourceFetchError, match="ReadTimeout"):
        await GreenhouseSource(board="slow").fetch(make_http(router, max_retries=2), FetchContext())
    assert router.count(r"boards/slow") == 3  # 1 try + 2 retries


async def test_http_429_respects_retry_after(router):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"jobs": []})])
    router.add("GET", r"boards/rl/jobs", lambda r: next(responses))
    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(router.handler)), max_retries=2, sleep=fake_sleep)
    assert await GreenhouseSource(board="rl").fetch(http, FetchContext()) == []
    assert delays == [7.0]


async def test_connection_error_in_one_real_adapter_does_not_block_others(settings, db, router):
    router.add("GET", r"boards/down/jobs", httpx.ConnectError("connection refused"))
    router.add("GET", r"boards/hootsuite/jobs$", load_fixture("greenhouse_jobs.json"))
    router.add("GET", r"boards/hootsuite/jobs/\d+", lambda r: {"content": "&lt;p&gt;Python React&lt;/p&gt;"})
    scanner, _ = build(settings, db, [GreenhouseSource(board="down"), GreenhouseSource(board="hootsuite", company="Hootsuite")],
                       http=make_http(router))
    report = await scanner.run()
    assert report.errors.keys() == {"greenhouse:down"}
    assert report.new_jobs >= 2


async def test_overlapping_scans_are_prevented(settings, db):
    slow = FakeSource("slow", jobs_ab(), delay=0.2)
    scanner, _ = build(settings, db, [slow])
    first, second = await asyncio.gather(scanner.run("scheduled"), scanner.run("manual"))
    assert not first.skipped and second.skipped
    assert slow.calls == 1
    third = await scanner.run("manual")  # lock released afterwards
    assert not third.skipped


async def test_rejected_jobs_are_cached_and_not_reclassified(settings, db):
    sales = make_job(source="fake:a", source_job_id="s", title="Sales Intern", url="https://x.example/s")
    scanner, _ = build(settings, db, [FakeSource("a", [sales])])
    r1 = await scanner.run()
    assert r1.rejected == 1 and db.is_known_uid("fake:a:s")
    r2 = await scanner.run()
    assert r2.rejected == 1 and r2.new_jobs == 0 and db.count_jobs() == 0
