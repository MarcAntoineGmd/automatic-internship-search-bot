"""Duplicate detection and persistence across restarts."""
from __future__ import annotations

from bot.classify import FilterConfig, classify
from bot.db import Database
from tests.conftest import NOW, make_job


def store(db, job):
    cls = classify(job, FilterConfig(), now=NOW)
    assert cls.included, cls.exclusion_reason
    dup = db.find_duplicate(job, term=cls.term, provinces=cls.provinces)
    if dup is not None:
        db.touch(dup, job)
        return dup, False
    return db.insert_job(job, cls), True


def test_same_uid_is_duplicate(db):
    j = make_job()
    _, new1 = store(db, j)
    _, new2 = store(db, make_job())
    assert new1 and not new2
    assert db.count_jobs() == 1


def test_cross_source_duplicate_by_ats_url(db):
    ats = make_job(source="greenhouse:lyft", source_job_id="8796054002", company="Lyft",
                   title="Software Engineer Intern - Backend (Summer 2027)",
                   url="https://job-boards.greenhouse.io/lyft/jobs/8796054002", extra_keys=["greenhouse:8796054002"])
    agg = make_job(source="github_listings:simplify", source_job_id="s1", company="Lyft",
                   title="Software Engineer Intern, Backend",
                   url="https://app.careerpuck.com/job-board/lyft/job/8796054002?gh_jid=8796054002&utm_source=Simplify",
                   assume_internship=True)
    id1, new1 = store(db, ats)
    id2, new2 = store(db, agg)
    assert new1 and not new2 and id1 == id2
    row = db.get_job(id1)
    assert row.also_seen_on == ["github_listings:simplify"]


def test_cross_source_duplicate_by_fingerprint(db):
    a = make_job(source="ashby:x", source_job_id="a", company="RBC", title="Developer Intern (Summer 2027)",
                 url="https://careers.rbc.example/a/1")
    b = make_job(source="markdown_table:y", source_job_id="b", company="Royal Bank of Canada",
                 title="Developer Intern - Summer 2027", url="https://other.example/x/2", location="Toronto, ON")
    _, n1 = store(db, a)
    _, n2 = store(db, b)
    assert n1 and not n2


def test_different_term_is_not_a_duplicate(db):
    a = make_job(source_job_id="w", title="Software Developer Co-op (Winter 2027)", url="https://e.example/j/1")
    b = make_job(source_job_id="s", title="Software Developer Co-op (Summer 2027)", url="https://e.example/j/2")
    _, n1 = store(db, a)
    _, n2 = store(db, b)
    assert n1 and n2


def test_different_province_is_not_a_duplicate(db):
    a = make_job(source_job_id="1", title="Software Developer Intern", url="https://e.example/j/1", location="Montreal, QC")
    b = make_job(source_job_id="2", title="Software Developer Intern", url="https://e.example/j/2", location="Vancouver, BC")
    assert store(db, a)[1] and store(db, b)[1]


def test_generic_careers_url_does_not_merge_different_roles(db):
    a = make_job(source_job_id="1", title="Software Developer Intern", url="https://acme.example/careers")
    b = make_job(source_job_id="2", title="Data Engineer Intern", url="https://acme.example/careers")
    assert store(db, a)[1] and store(db, b)[1]


def test_reappearing_job_is_not_new(db):
    j = make_job()
    job_id, _ = store(db, j)
    db.mark_notified(job_id, "sent")
    # job disappears from the source for a while, then reappears
    _, new = store(db, make_job())
    assert not new
    assert db.get_job(job_id).notify_status == "sent"


def test_persistence_across_restarts(tmp_path):
    path = tmp_path / "persist.db"
    db1 = Database(path)
    job_id, _ = store(db1, make_job())
    db1.mark_notified(job_id, "sent")
    db1.subscribe(999)
    db1.set_meta("owner_id", "999")
    db1.add_rejected("test:board:zzz", "not an internship")
    db1.close()

    db2 = Database(path)  # "restart"
    assert db2.count_jobs() == 1 and db2.count_notified() == 1
    assert db2.is_known_uid("test:board:1") and db2.is_known_uid("test:board:zzz")
    assert db2.is_subscribed(999) and db2.get_meta("owner_id") == "999"
    assert db2.pending_notifications(0, 30, 10, now=NOW) == []
    _, new = store(db2, make_job())
    assert not new
    db2.close()


def test_rejected_cache_cleared_when_filters_change(db):
    db.sync_filter_signature("a")
    db.add_rejected("x:1", "nope")
    assert db.is_rejected("x:1")
    assert db.sync_filter_signature("a") is False and db.is_rejected("x:1")
    assert db.sync_filter_signature("b") is True and not db.is_rejected("x:1")


def test_application_status(db):
    job_id, _ = store(db, make_job())
    assert db.set_application_status(job_id, "applied")
    assert db.get_job(job_id).application_status == "applied"
    assert db.application_counts() == {"applied": 1}
    assert not db.set_application_status(9999, "applied")


# ------------------------------------------------------------------ live links
def _autodesk_jobs():
    tracker = make_job(source="github_listings:vansh", source_job_id="v1", company="Autodesk",
                       title="Software Developer Intern", location="Toronto, ON",
                       url="https://autodesk.wd1.myworkdayjobs.com/Ext/job/Toronto-ON-CAN/Software-Developer-Intern_26WD102151-1",
                       assume_internship=True)
    company = make_job(source="eightfold:autodesk", source_job_id="1443154491171", company="Autodesk",
                       title="Software Developer Intern", location="Toronto, ON, Canada",
                       url="https://jobs.autodesk.com/careers/job/1443154491171",
                       extra_keys=["workday:autodesk:26WD102151"])
    return tracker, company


def _store_at(db, job, now):
    cls = classify(job, FilterConfig(), now=NOW)
    dup = db.find_duplicate(job, term=cls.term, provinces=cls.provinces)
    if dup is not None:
        db.touch(dup, job, now=now)
        return dup, False
    return db.insert_job(job, cls, now=now), True


def test_company_site_link_replaces_dropped_tracker_link(db):
    """Tracker posted the old Workday link (now dead); Autodesk's own site still lists the job."""
    from datetime import timedelta
    tracker, company = _autodesk_jobs()
    job_id, new1 = _store_at(db, tracker, NOW - timedelta(hours=6))
    assert db.get_job(job_id).url.startswith("https://autodesk.wd1.myworkdayjobs.com/")
    dup_id, new2 = _store_at(db, company, NOW)
    assert new1 and not new2 and dup_id == job_id  # same posting: no second notification
    assert db.get_job(job_id).url == "https://jobs.autodesk.com/careers/job/1443154491171"
    assert {l["source"] for l in db.links_for(job_id)} == {"github_listings:vansh", "eightfold:autodesk"}


def test_company_link_preferred_when_both_list_it(db):
    from datetime import timedelta
    tracker, company = _autodesk_jobs()
    job_id, _ = _store_at(db, company, NOW)
    _store_at(db, tracker, NOW + timedelta(minutes=1))  # tracker seen later in the same scan
    assert db.get_job(job_id).url == "https://jobs.autodesk.com/careers/job/1443154491171"


def test_tracker_link_used_when_company_site_stopped_listing(db):
    from datetime import timedelta
    tracker, company = _autodesk_jobs()
    job_id, _ = _store_at(db, company, NOW)
    _store_at(db, tracker, NOW + timedelta(hours=5))
    assert db.get_job(job_id).url.startswith("https://autodesk.wd1.myworkdayjobs.com/")


def test_migration_backfills_links_and_new_url_keys(settings):
    from bot.db import Database
    tracker, company = _autodesk_jobs()
    d = Database(settings.database_path)
    cls = classify(tracker, FilterConfig(), now=NOW)
    job_id = d.insert_job(tracker, cls, now=NOW)
    # simulate a database created by the previous version
    d._exec("DELETE FROM job_links")
    d._exec("DELETE FROM job_keys WHERE key LIKE 'workday:%'")
    d.set_meta("schema_version", None)
    d.close()
    d = Database(settings.database_path)
    try:
        assert d.find_duplicate(company, use_fingerprint=False) == job_id  # matched on workday:autodesk:26WD102151
        assert [l["source"] for l in d.links_for(job_id)] == ["github_listings:vansh"]
        assert d.get_meta("schema_version") == "2"
    finally:
        d.close()
