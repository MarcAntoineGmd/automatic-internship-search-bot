"""US internships: collected only when a US channel is set (or INCLUDE_US=true), posted in their own channel."""
from __future__ import annotations

from datetime import timedelta

from bot.classify import FilterConfig, classify
from bot.location import parse_location
from bot.notify import Notifier
from bot.scanner import Scanner
from bot.sources.github_listings import GitHubListingsSource
from tests.conftest import NOW, make_job
from tests.test_scanner import FakeSource, make_http_dummy

US_CH, CA_CH = "999", "555"


def us_job(i="u1", title="Software Engineer Intern (Summer 2027)", location="San Francisco, CA", **kw):
    return make_job(source="fake:us", source_job_id=i, company=kw.pop("company", "Stripe"), title=title,
                    url=f"https://e.example/us/{i}", location=location, **kw)


def ca_job(i="c1", title="Software Engineer Intern (Summer 2027)", location="Toronto, ON", **kw):
    return make_job(source="fake:ca", source_job_id=i, company=kw.pop("company", "Stripe"), title=title,
                    url=f"https://e.example/ca/{i}", location=location, **kw)


class Sent:
    def __init__(self):
        self.items = []

    async def __call__(self, target, payload):
        self.items.append((target, payload))

    def to(self, channel):
        return [p for t, p in self.items if t == f"channel:{channel}" and "embed" in p]

    def texts(self, channel):
        return [p.get("content", "") for t, p in self.items if t == f"channel:{channel}"]


def build(settings, db, sources, clock=lambda: NOW):
    sent = Sent()
    notifier = Notifier(db, settings, sent, pause=0)
    return Scanner(settings, db, sources, make_http_dummy(), notify=notifier.deliver_pending, now=clock), sent


# ------------------------------------------------------------------ location / classification
def test_us_locations_are_recognised():
    assert parse_location("San Francisco, CA").us
    assert parse_location("Remote - US").us
    assert parse_location("New York, NY | London, UK").us
    assert parse_location("Anywhere", countries=["United States"]).us
    assert not parse_location("Toronto, ON, CA").us           # trailing CA = Canada
    assert not parse_location("Toronto, ON | Seattle, WA").us  # has a Canadian location -> Canada
    assert not parse_location("London, UK").us


def test_us_postings_rejected_unless_enabled():
    job = us_job()
    off = classify(job, FilterConfig(), now=NOW)
    assert not off.included and off.exclusion_reason == "outside Canada"
    on = classify(job, FilterConfig(include_us=True), now=NOW)
    assert on.included and on.country == "United States" and on.region == "US" and on.score >= 55
    assert classify(ca_job(), FilterConfig(include_us=True), now=NOW).region == "CA"
    # other countries stay out
    assert not classify(us_job(location="London, UK"), FilterConfig(include_us=True), now=NOW).included


def test_us_work_authorization_is_shown_not_excluded_for_the_us_channel():
    job = us_job(description="Candidates must be authorized to work in the United States without sponsorship.")
    cls = classify(job, FilterConfig(include_us=True), now=NOW)
    assert cls.included and "Requires US work authorization" in cls.eligibility


def test_tracker_prefilter_keeps_us_only_when_enabled():
    raw = {"id": "1", "title": "SWE Intern", "locations": ["Austin, TX"], "active": True, "is_visible": True}
    assert not GitHubListingsSource._is_candidate(raw)
    assert GitHubListingsSource._is_candidate(raw, include_us=True)


# ------------------------------------------------------------------ routing
async def test_no_us_collection_or_posts_without_a_us_channel(settings, db):
    db.set_meta("notify_channel_id", CA_CH)
    scanner, sent = build(settings, db, [FakeSource("ca", [ca_job()]), FakeSource("us", [us_job()])])
    report = await scanner.run()
    assert report.new_jobs == 1 and len(sent.to(CA_CH)) == 1 and not sent.to(US_CH)
    assert db.latest_jobs(province="US") == []


async def test_us_channel_gets_us_postings_and_canada_channel_the_rest(settings, db):
    settings.baseline_notify_limit = 2
    db.set_meta("notify_channel_id", CA_CH)
    clock = [NOW]
    ca = FakeSource("ca", [ca_job()])
    us = FakeSource("us", [us_job(f"u{i}", title=f"Software Engineer Intern {i} (Summer 2027)") for i in range(5)])
    scanner, sent = build(settings, db, [ca, us], clock=lambda: clock[0])
    await scanner.run()                       # Canada only for now
    assert len(sent.to(CA_CH)) == 1

    db.set_meta("notify_channel_id_us", US_CH)  # /notify_here region:United States
    clock[0] = NOW + timedelta(minutes=30)
    report = await scanner.run()
    # US initial import: the 5 existing US postings -> best 2 posted, 3 stored silently with one summary line
    assert len(sent.to(US_CH)) == 2 and len(report.baseline_job_ids) == 3
    assert any("Initial import: 3" in t and "United States" in t for t in sent.texts(US_CH))
    assert len(sent.to(CA_CH)) == 1           # nothing new for Canada, no US post there

    us.jobs.append(us_job("u9", title="Backend Engineer Intern (Summer 2027)", location="Seattle, WA"))
    ca.jobs.append(ca_job("c9", title="Developer Intern (Summer 2027)", location="Montréal, QC"))
    clock[0] = NOW + timedelta(minutes=60)
    await scanner.run()
    assert len(sent.to(US_CH)) == 3 and "Backend Engineer Intern" in sent.to(US_CH)[-1]["embed"].title
    assert len(sent.to(CA_CH)) == 2 and "Developer Intern" in sent.to(CA_CH)[-1]["embed"].title
    us_embed = sent.to(US_CH)[-1]["embed"]
    assert "🇺🇸 US" in us_embed.title and us_embed.colour.value == 0x8250DF


async def test_turning_us_on_rechecks_postings_rejected_as_outside_canada(settings, db):
    job = us_job()
    db.sync_filter_signature(settings.filters.signature())  # as after earlier scans
    db.add_rejected(job.uid, "outside Canada", now=NOW)
    db.add_rejected("other:1", "not an internship/co-op", now=NOW)
    db.set_meta("notify_channel_id_us", US_CH)
    scanner, _ = build(settings, db, [FakeSource("us", [job])])
    report = await scanner.run()
    assert report.new_jobs == 1 and db.is_rejected("other:1") and not db.is_rejected(job.uid)


def test_us_and_canadian_posting_with_same_title_are_not_merged(db):
    a, b = ca_job(), us_job()
    cls_a = classify(a, FilterConfig(include_us=True), now=NOW)
    db.insert_job(a, cls_a, now=NOW)
    cls_b = classify(b, FilterConfig(include_us=True), now=NOW)
    assert db.find_duplicate(b, term=cls_b.term, provinces=cls_b.provinces, country=cls_b.country) is None


async def test_us_backlog_does_not_starve_the_canada_channel(settings, db):
    settings.max_dms_per_scan = 2
    db.set_meta("notify_channel_id", CA_CH)
    cfg = FilterConfig(include_us=True)
    for i in range(5):
        j = us_job(f"u{i}", title=f"Software Engineer Intern {i}")
        db.insert_job(j, classify(j, cfg, now=NOW), now=NOW)
    c = ca_job()
    db.insert_job(c, classify(c, cfg, now=NOW), now=NOW)
    sent = Sent()
    assert await Notifier(db, settings, sent, pause=0).deliver_pending() == 1  # no US channel: US waits
    assert len(sent.to(CA_CH)) == 1


def test_setting_the_us_channel_and_status(settings, db):
    from bot.app import RadarApp

    settings.discord_user_id = 111
    app = RadarApp(settings, send=Sent(), db=db, sources=[], http=make_http_dummy())
    cfg = FilterConfig(include_us=True)
    old = us_job("old")
    db.insert_job(old, classify(old, cfg, now=NOW), now=NOW)
    assert not app.set_notify_channel(222, 999, "US").ok             # owner only
    res = app.set_notify_channel(111, 999, "US")
    assert res.ok and "<#999>" in res.message and "next scan" in res.message
    assert db.pending_notifications(0, 30, 10, now=NOW, region="US") == []  # earlier US postings not dumped
    st = app.status(111)
    assert st["notify_target_us"] == "<#999>" and st["notify_target"] == "Your DMs"
    assert app.set_notify_channel(111, None, "US").ok and not app.scanner.us_enabled()
    assert app.status(111)["notify_target_us"].startswith("Off")


def test_html_and_latest_filter_by_country(db):
    from bot.export import to_html

    cfg = FilterConfig(include_us=True)
    for j in (us_job(), ca_job()):
        db.insert_job(j, classify(j, cfg, now=NOW), now=NOW)
    page = to_html(db.all_jobs(), now=NOW).decode()
    assert "data-country='US'" in page and "data-country='CA'" in page
    assert "🇺🇸 États-Unis (1)" in page and "🇨🇦 Canada (1)" in page and "class='prov p-US'" in page
    assert [j.country for j in db.latest_jobs(province="US")] == ["United States"]
    assert all(j.country != "United States" for j in db.latest_jobs(province="OTHER"))
