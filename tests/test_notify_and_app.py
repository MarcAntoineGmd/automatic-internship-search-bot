"""Discord embed formatting and subscription/command logic (no real Discord calls)."""
from __future__ import annotations

import discord

from bot.app import RadarApp
from bot.classify import FilterConfig, classify
from bot.notify import Notifier, build_job_embed, build_summary_embed
from tests.conftest import NOW, make_job


def stored_job(db, **kw):
    job = make_job(**kw)
    cls = classify(job, FilterConfig(), now=NOW)
    return db.get_job(db.insert_job(job, cls, now=NOW))


# ------------------------------------------------------------------ formatting
def test_job_embed_contains_required_fields(db):
    row = stored_job(db, company="Wealthsimple", title="Software Developer Intern (Summer 2027)",
                     url="https://jobs.ashbyhq.com/wealthsimple/abc", location="Montréal, QC, Canada",
                     workplace="hybrid", description="Wealthsimple is a fintech.\nAs an intern you will build Python services "
                     "for millions of Canadians using Kotlin and React.")
    embed = build_job_embed(row)
    assert isinstance(embed, discord.Embed)
    d = embed.to_dict()
    fields = {f["name"]: f["value"] for f in d["fields"]}
    assert d["title"] == "🔵 QC · ☀️ Été 2027 | Software Developer Intern (Summer 2027)"
    assert d["color"] == 0x1F6FEB  # Québec = blue
    assert d["url"] == "https://jobs.ashbyhq.com/wealthsimple/abc"
    assert d["author"]["name"] == "Wealthsimple"
    assert fields["Location"] == "Montréal, QC, Canada"
    assert fields["Province"] == "Québec"
    assert fields["Term"] == "☀️ Été 2027"
    assert fields["Arrangement"] == "Hybrid"
    assert fields["Posted"] == "Not provided by source"  # never claims it was just posted
    assert fields["Discovered"] == "2026-11-11"
    assert fields["Job ID"] == f"`{row.id}`"
    assert "High" in fields["Priority"] and "/100" in fields["Priority"]
    assert "](https://jobs.ashbyhq.com/wealthsimple/abc)" in fields["Apply"]
    assert d["description"].startswith("As an intern")
    assert f"/applied {row.id}" in d["footer"]["text"]
    assert len(embed) <= 6000


def test_embed_respects_discord_limits(db):
    row = stored_job(db, title="Software Engineer Intern " + "X" * 400, description="word " * 5000,
                     location="Toronto, ON | " * 100)
    embed = build_job_embed(row)
    d = embed.to_dict()
    assert len(d["title"]) <= 256 and len(d.get("description", "")) <= 4096
    assert all(len(f["value"]) <= 1024 for f in d["fields"])
    assert len(embed) <= 6000


def test_embed_unknown_values(db):
    row = stored_job(db, location="Remote")
    fields = {f["name"]: f["value"] for f in build_job_embed(row).to_dict()["fields"]}
    assert fields["Term"] == "❔ Session non précisée" and fields["Province"] == "Unknown"


def test_summary_embed(db):
    rows = [stored_job(db, source_job_id=str(i), url=f"https://e.example/j/{i}", title=f"Software Engineer Intern {i}")
            for i in range(12)]
    d = build_summary_embed("Latest", rows, limit=10).to_dict()
    assert d["description"].count("\n") == 9
    assert any(f["name"] == "More" for f in d["fields"])


async def test_test_notification_and_daily_summary(settings, db):
    sent = []

    async def send(uid, payload):
        sent.append((uid, payload))

    n = Notifier(db, settings, send, pause=0)
    await n.send_test("42")
    assert sent[0][0] == "42" and "Test notification" in sent[0][1]["embed"].title
    db.subscribe(42)
    stored_job(db)
    assert await n.send_daily_summary(now=NOW)
    assert "Daily summary" in sent[-1][1]["embed"].title


# ------------------------------------------------------------------ subscription / commands
def app_for(settings, db):
    async def send(uid, payload):
        pass

    return RadarApp(settings, send=send, db=db, sources=[], http=None)


def test_first_subscriber_becomes_owner(settings, db):
    app = app_for(settings, db)
    assert app.owner_id() is None
    r = app.subscribe(111)
    assert r.ok and db.is_subscribed(111) and app.owner_id() == "111"
    assert app.subscribe(111).message.startswith("You're already")
    other = app.subscribe(222)
    assert not other.ok and not db.is_subscribed(222)


def test_configured_owner_only(settings, db):
    settings.discord_user_id = 555
    app = app_for(settings, db)
    assert not app.subscribe(111).ok
    assert app.subscribe(555).ok and app.is_owner(555) and not app.is_owner(111)


def test_unsubscribe_and_resubscribe(settings, db):
    app = app_for(settings, db)
    app.subscribe(111)
    assert app.unsubscribe(111).ok and not db.is_subscribed(111)
    assert "not subscribed" in app.unsubscribe(111).message
    assert not app.unsubscribe(999).ok  # non-owner
    assert app.subscribe(111).ok and db.is_subscribed(111)
    assert db.active_subscribers() == ["111"]


def test_applied_command(settings, db):
    app = app_for(settings, db)
    app.subscribe(111)
    row = stored_job(db)
    assert app.mark_applied(111, row.id).ok
    assert db.get_job(row.id).application_status == "applied"
    assert app.mark_applied(111, row.id, "interviewing").ok
    assert not app.mark_applied(111, 9999).ok
    assert not app.mark_applied(111, row.id, "bogus").ok
    assert not app.mark_applied(222, row.id).ok  # not the owner


def test_status_payload(settings, db):
    app = app_for(settings, db)
    app.subscribe(111)
    stored_job(db)
    st = app.status(111, next_scan_at=NOW)
    assert st["subscribed"] and st["jobs"] == 1 and st["notified"] == 0 and st["pending"] == 1
    assert st["active_sources"] == 0 and st["last_successful_scan"] is None
    assert st["next_scan"].startswith("2026-11-11")


# ------------------------------------------------------------------ channel mode
async def test_channel_mode_posts_to_channel_without_subscription(settings, db):
    settings.notify_channel_id = 777
    sent = []

    async def send(target, payload):
        sent.append(target)

    n = Notifier(db, settings, send, pause=0)
    stored_job(db)
    assert n.targets() == ["channel:777"]
    assert await n.deliver_pending() == 1
    assert sent == ["channel:777"]
    assert await n.deliver_pending() == 0  # still only once
    assert await n.send_test("42") == "channel:777"


async def test_notify_here_and_back_to_dms(settings, db):
    sent = []

    async def send(target, payload):
        sent.append(target)

    app = RadarApp(settings, send=send, db=db, sources=[], http=None)
    app.subscribe(111)
    assert app.notifier.targets() == ["111"]
    assert not app.set_notify_channel(222, 555).ok  # not the owner
    res = app.set_notify_channel(111, 555)
    assert res.ok and "<#555>" in res.message
    assert app.notifier.targets() == ["channel:555"]
    assert app.status(111)["notify_target"] == "<#555>"
    stored_job(db)
    assert await app.notifier.deliver_pending() == 1 and sent == ["channel:555"]
    assert app.set_notify_channel(111, None).ok
    assert app.notifier.targets() == ["111"] and app.status(111)["notify_target"] == "Your DMs"


def test_notify_command_overrides_env_channel(settings, db):
    settings.notify_channel_id = 777
    app = app_for(settings, db)
    app.subscribe(111)
    app.set_notify_channel(111, None)  # /notify_dm wins over NOTIFY_CHANNEL_ID
    assert app.notifier.targets() == ["111"]


def test_channel_id_from_env():
    from bot.config import load_settings

    assert load_settings(env={"NOTIFY_CHANNEL_ID": "123456"}).notify_channel_id == 123456
    assert load_settings(env={}).notify_channel_id is None


def test_status_hides_errors_from_disabled_sources(settings, db):
    from datetime import datetime, timezone

    from bot.sources.greenhouse import GreenhouseSource

    now = datetime.now(timezone.utc)
    db.record_source_check(1, "greenhouse:visiersolutionsinc", False, now, 10, error="HTTP 404")
    db.record_source_check(1, "greenhouse:lyft", False, now, 10, error="HTTP 500")

    async def send(target, payload):
        pass

    app = RadarApp(settings, send=send, db=db, sources=[GreenhouseSource(board="lyft")], http=None)
    app.subscribe(1)
    assert app.status(1)["failing_sources"] == [("greenhouse:lyft", "HTTP 500")]
