"""Discord embed formatting and DM delivery logic (transport injected, so it is testable)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import discord

from .config import Settings
from .db import Database, JobRow
from .tags import province_color, season_tag, tag_line
from .textutil import truncate, utcnow

log = logging.getLogger(__name__)

PRIORITY_COLORS = {"High": 0x2ECC71, "Medium": 0xF1C40F, "Low": 0x95A5A6}
PRIORITY_EMOJI = {"High": "🔥", "Medium": "⭐", "Low": "•"}
WORKPLACE_LABEL = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
PROVINCE_NAMES = {
    "QC": "Québec", "ON": "Ontario", "BC": "British Columbia", "AB": "Alberta", "MB": "Manitoba",
    "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut",
}

# (target, {"content":..., "embed":...}); target is a user id, or "channel:<id>" for a server channel
SendFn = Callable[[str, dict], Awaitable[None]]
CHANNEL_PREFIX = "channel:"


def _fmt_date(dt: datetime | None, tz: str) -> str | None:
    if not dt:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz))
    except Exception:  # unknown tz / missing tzdata
        local = dt
    return local.strftime("%Y-%m-%d")


def build_job_embed(job: JobRow, tz: str = "America/Toronto") -> discord.Embed:
    tags = tag_line(job.provinces, job.country, job.term, job.workplace)
    title = truncate(f"{tags} | {job.title}", 250)
    embed = discord.Embed(
        title=title,
        url=job.url or None,
        description=truncate(_short_description(job), 350) or None,
        color=province_color(job.provinces, job.country),  # blue = QC, red = ON, green = BC, yellow = AB
    )
    embed.set_author(name=truncate(job.company, 200))
    location = job.location or "Unknown"
    embed.add_field(name="Location", value=truncate(location, 200), inline=True)
    prov = ", ".join(PROVINCE_NAMES.get(p, p) for p in job.provinces[:3]) or (
        "Unknown" if job.country != "Canada" else "Canada (province not stated)")
    embed.add_field(name="Province", value=prov, inline=True)
    embed.add_field(name="Term", value=season_tag(job.term), inline=True)
    embed.add_field(name="Arrangement", value=WORKPLACE_LABEL.get(job.workplace or "", "Not stated"), inline=True)
    kind = {"co-op": "Co-op", "internship": "Internship", "student": "Student position",
            "new_grad": "New grad"}.get(job.internship_type or "", "Internship (per source)")
    embed.add_field(name="Type", value=kind, inline=True)
    embed.add_field(name="Priority", value=f"{PRIORITY_EMOJI.get(job.priority, '')} {job.priority} ({job.score}/100)",
                    inline=True)
    posted = _fmt_date(job.date_posted, tz)
    embed.add_field(name="Posted", value=posted or "Not provided by source", inline=True)
    embed.add_field(name="Discovered", value=_fmt_date(job.date_discovered, tz) or "?", inline=True)
    embed.add_field(name="Job ID", value=f"`{job.id}`", inline=True)
    if job.eligibility:
        embed.add_field(name="Eligibility", value=truncate(job.eligibility, 300), inline=False)
    if job.url:
        embed.add_field(name="Apply", value=f"[Open application]({job.url})", inline=False)
    sources = [job.source] + job.also_seen_on
    embed.set_footer(text=truncate(f"Source: {', '.join(sources)} · /applied {job.id} once you apply", 300))
    return embed


def _short_description(job: JobRow) -> str:
    desc = (job.description or "").strip()
    if not desc:
        return ""
    # Skip generic company boilerplate at the top when possible
    paras = [p.strip() for p in desc.split("\n") if len(p.strip()) > 40]
    for p in paras:
        lower = p.lower()
        if any(w in lower for w in ("you will", "you'll", "as an intern", "as a co-op", "responsib", "what you",
                                    "the role", "this role", "vous allez", "votre rôle", "in this role")):
            return p
    return paras[0] if paras else desc


def build_summary_embed(title: str, jobs: list[JobRow], footer: str | None = None, limit: int = 10) -> discord.Embed:
    embed = discord.Embed(title=title, color=0x3498DB)
    if not jobs:
        embed.description = "No jobs to show."
    lines = []
    for j in jobs[:limit]:
        link = f"[{truncate(j.title, 80)}]({j.url})" if j.url else truncate(j.title, 80)
        tags = tag_line(j.provinces, j.country, j.term, j.workplace)
        lines.append(f"`{j.id}` {tags} · **{truncate(j.company, 40)}** — {link}")
    if lines:
        embed.description = truncate("\n".join(lines), 4000)
    if len(jobs) > limit:
        embed.add_field(name="More", value=f"+{len(jobs) - limit} more — use /latest", inline=False)
    if footer:
        embed.set_footer(text=truncate(footer, 300))
    return embed


class Notifier:
    def __init__(self, db: Database, settings: Settings, send: SendFn, pause: float = 0.6) -> None:
        self.db = db
        self.settings = settings
        self.send = send
        self.pause = pause

    def channel_id(self) -> str | None:
        """Notification channel: set with /notify_here (stored in DB), else NOTIFY_CHANNEL_ID, else None (DMs)."""
        stored = self.db.get_meta("notify_channel_id")
        if stored is not None:
            return stored or None  # "" = explicitly switched back to DMs with /notify_dm
        return str(self.settings.notify_channel_id) if self.settings.notify_channel_id else None

    def targets(self) -> list[str]:
        channel = self.channel_id()
        if channel:
            return [f"{CHANNEL_PREFIX}{channel}"]
        return self.db.active_subscribers()

    async def deliver_pending(self, report=None) -> int:
        subscribers = self.targets()
        if not subscribers:
            return 0
        jobs = self.db.pending_notifications(
            min_score=self.settings.notify_min_score,
            max_age_days=self.settings.pending_max_age_days,
            limit=self.settings.max_dms_per_scan,
        )
        sent = 0
        if report is not None and getattr(report, "baseline_job_ids", None):
            n = len(report.baseline_job_ids)
            try:
                for uid in subscribers:
                    await self.send(uid, {"content": (
                        f"📥 Initial import: {n} existing matching postings were stored without individual notifications "
                        f"(the top {self.settings.baseline_notify_limit} by score follow). Use /latest to browse them.")})
            except Exception as exc:
                log.warning("Could not send baseline summary: %s", exc)
        for job in jobs:
            embed = build_job_embed(job, self.settings.timezone)
            delivered = True
            for uid in subscribers:
                try:
                    await self.send(uid, {"embed": embed})
                except Exception as exc:
                    delivered = False
                    log.warning("Notification to %s failed for job %s: %s", uid, job.id, exc)
            if not delivered:
                break  # keep it pending; retry on the next scan
            self.db.mark_notified(job.id, "sent")
            sent += 1
            if self.pause:
                await asyncio.sleep(self.pause)
        return sent

    async def send_daily_summary(self, now: datetime | None = None) -> bool:
        from datetime import timedelta

        subscribers = self.targets()
        if not subscribers:
            return False
        now = now or utcnow()
        jobs = self.db.jobs_since(now - timedelta(days=1))
        embed = build_summary_embed(f"Daily summary — {len(jobs)} new matching posting(s) in 24h", jobs,
                                    footer=f"Total stored: {self.db.count_jobs()} · DMs sent: {self.db.count_notified()}")
        for uid in subscribers:
            await self.send(uid, {"embed": embed})
        return True

    async def send_test(self, user_id: str) -> str:
        """Send a test message where notifications go. Returns the target used."""
        channel = self.channel_id()
        target = f"{CHANNEL_PREFIX}{channel}" if channel else str(user_id)
        embed = discord.Embed(
            title="✅ Test notification",
            description=("Delivery works. New Canadian SWE internships will be posted "
                         f"{'in this channel' if channel else 'here'}."),
            color=0x2ECC71,
        )
        await self.send(target, {"embed": embed})
        return target
