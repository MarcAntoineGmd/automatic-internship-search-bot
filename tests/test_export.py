"""Full-list export (/export) and /latest paging."""
from __future__ import annotations

import csv
import io

import pytest

from bot.classify import FilterConfig, classify
from bot.export import COLUMNS, to_csv, to_html
from tests.conftest import NOW, make_job


def store(db, **kw):
    job = make_job(**kw)
    return db.insert_job(job, classify(job, FilterConfig(), now=NOW), now=NOW)


def test_csv_contains_every_job_best_first(db):
    store(db, source_job_id="a", title="Data Engineer Intern", url="https://e.example/j/a", location="Vancouver, BC")
    best = store(db, source_job_id="b", company="Desjardins", title="Développeur logiciel - Stagiaire (Été 2027)",
                 url="https://e.example/j/b", location="Montréal, QC")
    data = to_csv(db.all_jobs())
    assert data.startswith(b"\xef\xbb\xbf")  # BOM for Excel accents
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == COLUMNS and len(rows) == 3
    assert rows[1][0] == str(best) and rows[1][4].startswith("Développeur") and "Québec" in rows[1][7]
    assert rows[1][5] == "Logiciel (général)" and rows[2][5] == "Données"
    assert rows[1][-1] == "https://e.example/j/b" and rows[1][13] == "new"


def test_html_is_escaped_and_clickable(db):
    store(db, title="Software Engineer Intern <script>alert(1)</script>", url="https://e.example/j/1?a=1&b=2")
    page = to_html(db.all_jobs()).decode()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "href='https://e.example/j/1?a=1&amp;b=2'" in page
    assert "1 stored matches" in page


def test_latest_paging(db):
    ids = [store(db, source_job_id=str(i), title=f"Software Engineer Intern {i}", url=f"https://e.example/j/{i}")
           for i in range(30)]
    page1 = db.latest_jobs(limit=25, offset=0)
    page2 = db.latest_jobs(limit=25, offset=25)
    assert len(page1) == 25 and len(page2) == 5
    assert {j.id for j in page1} | {j.id for j in page2} == set(ids)
    assert len(db.all_jobs()) == 30


async def test_scan_rewrites_growing_html_list(settings, db, tmp_path):
    from bot.scanner import Scanner
    from tests.test_scanner import FakeSource, make_http_dummy

    settings.export_html_path = str(tmp_path / "internships.html")
    src = FakeSource("a", [make_job(source="fake:a", source_job_id="1", url="https://e.example/j/1")])
    scanner = Scanner(settings, db, [src], make_http_dummy(), now=lambda: NOW)
    await scanner.run()
    first = (tmp_path / "internships.html").read_text(encoding="utf-8")
    assert "1 stored matches" in first and "NEW" in first
    src.jobs.append(make_job(source="fake:a", source_job_id="2", title="Backend Developer Co-op",
                             url="https://e.example/j/2"))
    await scanner.run()
    second = (tmp_path / "internships.html").read_text(encoding="utf-8")
    assert "2 stored matches" in second  # the list only grows
    assert not (tmp_path / "internships.html.tmp").exists()


async def test_export_can_be_disabled(settings, db, tmp_path, monkeypatch):
    from bot.config import load_settings
    from bot.scanner import Scanner
    from tests.test_scanner import FakeSource, make_http_dummy

    assert load_settings(env={"EXPORT_HTML_PATH": ""}).export_html_path is None
    assert load_settings(env={}).export_html_path == "internships.html"
    monkeypatch.chdir(tmp_path)
    settings.export_html_path = None
    await Scanner(settings, db, [FakeSource("a", [make_job()])], make_http_dummy(), now=lambda: NOW).run()
    assert not (tmp_path / "internships.html").exists()


def test_shared_export_hides_my_application_status(db):
    job_id = store(db, title="Software Engineer Intern", url="https://e.example/j/9")
    db.set_application_status(job_id, "interviewing")
    mine = to_csv(db.all_jobs()).decode("utf-8-sig")
    shared = to_csv(db.all_jobs(), include_status=False).decode("utf-8-sig")
    assert "My status" in mine and "interviewing" in mine
    assert "My status" not in shared and "interviewing" not in shared
    rows = list(csv.reader(io.StringIO(shared)))
    assert len(rows[0]) == len(COLUMNS) - 1 and rows[1][-1] == "https://e.example/j/9"
    page = to_html(db.all_jobs(), include_status=False).decode()
    assert "interviewing" not in page and "My status" not in page


def test_html_flags_postings_no_source_lists_anymore(db):
    from datetime import timedelta
    old = make_job(source_job_id="old", title="Software Engineer Intern Old", url="https://e.example/j/old")
    db.insert_job(old, classify(old, FilterConfig(), now=NOW), now=NOW - timedelta(hours=13))
    store(db, source_job_id="live", title="Software Engineer Intern Live", url="https://e.example/j/live")
    # the source was checked successfully after it last listed "old" -> it dropped it
    db.record_source_check(None, "test:board", True, NOW - timedelta(minutes=5), 10)
    stale = db.stale_job_ids(NOW)
    page = to_html(db.all_jobs(), now=NOW, stale_ids=stale).decode()
    old_row = next(r for r in page.split("<tr") if "j/old" in r)
    live_row = next(r for r in page.split("<tr") if "j/live" in r)
    assert "data-status='stale'" in old_row and "Peut-être fermé" in old_row
    assert "data-status='open'" in live_row and "Peut-être fermé" not in live_row
    assert "Ouverts (1)" in page and "Peut-être fermés (1)" in page


def test_failing_source_never_makes_postings_look_closed(db):
    """Workday rate-limited us for a day: its postings must not be flagged as closed."""
    from datetime import timedelta
    job = make_job(source="workday:rbc/x", source_job_id="r1", url="https://e.example/j/r1")
    db.insert_job(job, classify(job, FilterConfig(), now=NOW), now=NOW - timedelta(hours=30))
    db.record_source_check(None, "workday:rbc/x", True, NOW - timedelta(hours=31), 10)   # last success: before
    db.record_source_check(None, "workday:rbc/x", False, NOW - timedelta(minutes=5), 10, error="HTTP 429")
    assert db.stale_job_ids(NOW) == set()
    # ...but a source removed from sources.toml can't vouch for it anymore
    assert db.stale_job_ids(NOW, active_sources={"other:src"}) == {db.find_duplicate(job, use_fingerprint=False)}


def test_html_sector_column_and_multi_select_filters(db):
    store(db, source_job_id="e", title="Embedded Software Intern (Winter 2027)", url="https://e.example/j/e")
    store(db, source_job_id="w", title="Full Stack Developer Intern (Summer 2027)", url="https://e.example/j/w")
    store(db, source_job_id="m", title="Machine Learning Engineer Intern (Fall 2027)", url="https://e.example/j/m")
    page = to_html(db.all_jobs(), now=NOW).decode()
    assert "data-sector='embedded'" in page and "data-sector='fullstack web'" in page and "data-sector='ai'" in page
    assert "🔌 Embarqué / Firmware (1)" in page and "🌐 Web (1)" in page and "🤖 IA / ML (1)" in page
    assert "Été 2027 (1)" in page and "Hiver 2027 (1)" in page and "Automne 2027 (1)" in page
    # one Set per group: several values in a group are OR-ed, groups are AND-ed, state kept in the URL
    assert "sel[g].has(v))sel[g].delete(v);else sel[g].add(v)" in page
    assert "some(x=>sel[g].has(x))" in page and "history.replaceState" in page


async def test_multi_select_filters_in_a_real_browser(db, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    store(db, source_job_id="e", title="Embedded Software Intern (Winter 2027)", url="https://e.example/j/e")
    store(db, source_job_id="w", title="Full Stack Developer Intern (Summer 2027)", url="https://e.example/j/w")
    store(db, source_job_id="m", title="Machine Learning Engineer Intern (Fall 2027)", url="https://e.example/j/m")
    f = tmp_path / "list.html"
    f.write_bytes(to_html(db.all_jobs(), now=NOW))
    try:
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f.as_uri())
            shown = "() => [...document.querySelectorAll('#t tbody tr')].filter(r => r.style.display !== 'none').length"
            assert await page.evaluate(shown) == 3
            await page.click(".chip[data-group=term][data-value=Winter-2027]")
            await page.click(".chip[data-group=term][data-value=Summer-2027]")
            assert await page.evaluate(shown) == 2          # Winter OR Summer
            await page.click(".chip[data-group=sector][data-value=embedded]")
            assert await page.evaluate(shown) == 1          # ... AND embedded
            await page.reload()                             # selection survives a refresh (URL hash)
            assert await page.evaluate(shown) == 1
            await page.click(".chip[data-group=term][data-value='']")
            await page.click(".chip[data-group=sector][data-value='']")
            assert await page.evaluate(shown) == 3
            await browser.close()
    except Exception as exc:  # no browser installed on this machine
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip("Chromium not installed")
        raise
