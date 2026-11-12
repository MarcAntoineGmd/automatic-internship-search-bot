"""Wiring + Discord-independent command logic (so it can be unit-tested)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .config import Settings, load_sources_config
from .db import APPLICATION_STATUSES, Database
from .http import HttpClient, build_async_client
from .notify import Notifier, SendFn
from .scanner import Scanner
from .sources import build_sources

log = logging.getLogger(__name__)

HELP_TEXT = """**Canadian SWE Internship Radar**
`/subscribe` — register for DMs (first user becomes the owner if DISCORD_USER_ID is unset)
`/unsubscribe` — stop DMs
`/notify_here` — post new internships in the current server channel instead of DMs
`/notify_dm` — go back to DMs
`/status` — subscription, counts, last/next scan, source errors
`/scan` — run a scan now
`/latest [count] [page]` — most recently discovered matching jobs, page by page (anyone)
`/export [html|csv]` — download every internship found since the start (anyone)
`/test_notification` — send a test message (to the channel or your DMs)
`/applied <job_id> [status]` — track your application (applied, interviewing, offer, rejected, ignored, new)
`/help` — this message"""


@dataclass
class CommandResult:
    ok: bool
    message: str


class RadarApp:
    """Owns the long-lived components. ``send`` delivers a DM payload to a user id."""

    def __init__(self, settings: Settings, send: SendFn | None = None, http: HttpClient | None = None,
                 db: Database | None = None, sources=None) -> None:
        self.settings = settings
        self.db = db or Database(settings.database_path)
        if sources is None:
            cfg = load_sources_config(settings.sources_file)
            sources, self.config_errors = build_sources(cfg)
        else:
            self.config_errors = []
        self.sources = sources
        self.http = http or HttpClient(
            build_async_client(settings.http_timeout_seconds, settings.user_agent),
            max_retries=settings.http_max_retries,
        )
        self.notifier = Notifier(self.db, settings, send) if send else None
        self.scanner = Scanner(settings, self.db, self.sources, self.http,
                               notify=self.notifier.deliver_pending if self.notifier else None)
        self.scheduler = None  # set by the Discord layer

    async def aclose(self) -> None:
        await self.http.client.aclose()
        self.db.close()

    # ------------------------------------------------------------------ auth
    def owner_id(self) -> str | None:
        if self.settings.discord_user_id:
            return str(self.settings.discord_user_id)
        return self.db.get_meta("owner_id")

    def is_owner(self, user_id: int | str) -> bool:
        owner = self.owner_id()
        return owner is not None and str(user_id) == owner

    # ------------------------------------------------------------------ commands
    def subscribe(self, user_id: int | str) -> CommandResult:
        owner = self.owner_id()
        if owner is not None and str(user_id) != owner:
            return CommandResult(False, "This is a private bot; only its owner can subscribe.")
        if owner is None:
            self.db.set_meta("owner_id", str(user_id))
        activated = self.db.subscribe(user_id)
        pending = self.db.count_pending(self.settings.notify_min_score)
        if not activated:
            return CommandResult(True, "You're already subscribed.")
        extra = f" {pending} stored match(es) are pending and will be sent with the next scan." if pending else ""
        channel = self.notifier.channel_id() if self.notifier else None
        where = f"post new matching internships in <#{channel}>" if channel else "DM you new matching internships"
        return CommandResult(True, f"✅ Subscribed. I'll {where}." + extra)

    def unsubscribe(self, user_id: int | str) -> CommandResult:
        if not self.is_owner(user_id):
            return CommandResult(False, "Only the owner can manage this bot.")
        if self.db.unsubscribe(user_id):
            return CommandResult(True, "🔕 Unsubscribed. Scans keep running; use /subscribe to resume DMs.")
        return CommandResult(True, "You were not subscribed.")

    def set_notify_channel(self, user_id: int | str, channel_id: int | str | None) -> CommandResult:
        """channel_id=None switches back to DMs."""
        if not self.is_owner(user_id):
            return CommandResult(False, "Only the owner can manage this bot.")
        self.db.set_meta("notify_channel_id", str(channel_id) if channel_id else "")
        if channel_id:
            return CommandResult(True, f"📢 New internships will now be posted in <#{channel_id}>.")
        return CommandResult(True, "📬 New internships will now be sent to your DMs (run /subscribe if needed).")

    def notify_target_label(self) -> str:
        channel = self.notifier.channel_id() if self.notifier else None
        return f"<#{channel}>" if channel else "Your DMs"

    def mark_applied(self, user_id: int | str, job_id: int, status: str = "applied") -> CommandResult:
        if not self.is_owner(user_id):
            return CommandResult(False, "Only the owner can manage this bot.")
        status = (status or "applied").lower()
        if status not in APPLICATION_STATUSES:
            return CommandResult(False, f"Unknown status. Use one of: {', '.join(APPLICATION_STATUSES)}")
        job = self.db.get_job(job_id)
        if not job:
            return CommandResult(False, f"No job with ID {job_id}.")
        self.db.set_application_status(job_id, status)
        return CommandResult(True, f"Marked `{job_id}` **{job.company} — {job.title}** as **{status}**.")

    def status(self, user_id: int | str, next_scan_at: datetime | None = None) -> dict:
        last_ok = self.db.last_successful_scan()
        last = self.db.last_scan()
        active = {src.id for src in self.sources}
        # Only report sources that are still configured; disabled/removed ones keep old rows in the DB.
        failing = [s for s in self.db.failing_sources() if s["source"] in active]
        return {
            "subscribed": self.db.is_subscribed(user_id),
        "notify_target": self.notify_target_label(),
            "jobs": self.db.count_jobs(),
            "notified": self.db.count_notified(),
            "pending": self.db.count_pending(self.settings.notify_min_score),
            "active_sources": len(self.sources),
            "last_successful_scan": last_ok["finished_at"] if last_ok else None,
            "last_scan": last,
            "next_scan": next_scan_at.isoformat(timespec="seconds") if next_scan_at else None,
            "scan_running": self.scanner.running,
            "failing_sources": [(s["source"], s["error"]) for s in failing],
            "config_errors": list(self.config_errors),
            "applications": self.db.application_counts(),
        }
