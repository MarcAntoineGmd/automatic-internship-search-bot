"""Québec vs Ontario and winter/summer/fall are visible in posts, /latest and the HTML list."""
from __future__ import annotations

import pytest

from bot.classify import FilterConfig, classify
from bot.notify import build_job_embed, build_summary_embed
from bot.tags import province_color, season_tag, tag_line
from tests.conftest import NOW, make_job


@pytest.mark.parametrize("provs,country,term,wp,expected", [
    (["QC"], "Canada", "Summer 2027", None, "🔵 QC · ☀️ Été 2027"),
    (["ON"], "Canada", "Winter 2027", None, "🔴 ON · ❄️ Hiver 2027"),
    (["ON", "QC"], "Canada", "Fall 2027", None, "🔴 ON · 🔵 QC · 🍂 Automne 2027"),
    (["BC"], "Canada", "2027", None, "🟢 BC · 📅 2027"),
    (["NS"], "Canada", None, None, "⚪ NS · ❔ Session non précisée"),
    ([], "Canada", "Summer 2027", "remote", "🌐 Canada (télétravail) · ☀️ Été 2027"),
    ([], "Unknown", None, None, "❔ Lieu non précisé · ❔ Session non précisée"),
])
def test_tag_line(provs, country, term, wp, expected):
    assert tag_line(provs, country, term, wp) == expected


def test_colors_differ_by_province():
    assert province_color(["QC"], "Canada") != province_color(["ON"], "Canada")
    assert season_tag("Spring 2027") == "🌱 Printemps 2027"


def store(db, **kw):
    job = make_job(**kw)
    return db.insert_job(job, classify(job, FilterConfig(), now=NOW), now=NOW)


def test_latest_filters_by_province_and_session(db):
    store(db, source_job_id="qc-s", title="Software Developer Intern (Summer 2027)", location="Montréal, QC",
          url="https://e.example/1")
    store(db, source_job_id="qc-w", title="Software Developer Intern (Winter 2027)", location="Québec City, QC",
          url="https://e.example/2")
    store(db, source_job_id="on-s", title="Backend Developer Intern (Summer 2027)", location="Toronto, ON",
          url="https://e.example/3")
    store(db, source_job_id="bc-f", title="Data Engineer Intern (Fall 2027)", location="Vancouver, BC",
          url="https://e.example/4")
    titles = lambda **kw: sorted(j.title for j in db.latest_jobs(limit=25, **kw))
    assert titles(province="QC") == ["Software Developer Intern (Summer 2027)", "Software Developer Intern (Winter 2027)"]
    assert titles(province="ON") == ["Backend Developer Intern (Summer 2027)"]
    assert titles(province="OTHER") == ["Data Engineer Intern (Fall 2027)"]
    assert titles(season="Summer") == ["Backend Developer Intern (Summer 2027)", "Software Developer Intern (Summer 2027)"]
    assert titles(province="QC", season="Winter") == ["Software Developer Intern (Winter 2027)"]
    assert db.count_filtered("QC", None) == 2 and db.count_filtered(None, "Fall") == 1 and db.count_filtered() == 4


def test_summary_lines_show_tags(db):
    store(db, title="Software Developer Intern (Summer 2027)", location="Montréal, QC")
    d = build_summary_embed("Latest", db.latest_jobs()).to_dict()
    assert "🔵 QC · ☀️ Été 2027" in d["description"]
    e = build_job_embed(db.latest_jobs()[0]).to_dict()
    assert e["title"].startswith("🔵 QC · ☀️ Été 2027 | ")


def test_html_has_badges_and_filter_buttons(db):
    from bot.export import to_html

    store(db, source_job_id="a", title="Software Developer Intern (Summer 2027)", location="Montréal, QC",
          url="https://e.example/a")
    store(db, source_job_id="b", title="Backend Developer Intern (Winter 2027)", location="Toronto, ON",
          url="https://e.example/b")
    page = to_html(db.all_jobs()).decode()
    assert "data-prov='QC'" in page and "data-season='Summer'" in page
    assert "data-prov='ON'" in page and "data-season='Winter'" in page
    assert "🔵 Québec (1)" in page and "🔴 Ontario (1)" in page and "☀️ Été (1)" in page and "❄️ Hiver (1)" in page
    assert "class='prov p-QC'" in page and "class='season s-Summer'" in page
