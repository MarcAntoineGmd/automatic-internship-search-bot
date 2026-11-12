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
