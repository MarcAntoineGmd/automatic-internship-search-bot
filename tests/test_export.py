"""Full-list export (/export) and /latest paging."""
from __future__ import annotations

import csv
import io

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
    assert rows[1][0] == str(best) and rows[1][4].startswith("Développeur") and "Québec" in rows[1][6]
    assert rows[1][-1] == "https://e.example/j/b" and rows[1][12] == "new"


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
