"""discord.py client: slash commands, DM delivery, scheduler start-up."""
from __future__ import annotations

import io
import logging
from datetime import datetime

import discord
from discord import app_commands

from .app import HELP_TEXT, RadarApp
from .config import Settings
from .export import to_csv, to_html
from .notify import CHANNEL_PREFIX, build_summary_embed
from .scheduler import DailySummaryScheduler, ScanScheduler

log = logging.getLogger(__name__)

STATUS_CHOICES = [app_commands.Choice(name=s, value=s)
                  for s in ("applied", "interviewing", "offer", "rejected", "ignored", "new")]


def _ts(value: str | datetime | None) -> str:
    if not value:
        return "never"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return f"<t:{int(value.timestamp())}:R>"


class RadarBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.app = RadarApp(settings, send=self.send_dm)
        self.scheduler = ScanScheduler(self.app.scanner.run, settings.scan_interval_minutes)
        self.daily = (DailySummaryScheduler(self.app.notifier.send_daily_summary, settings.daily_summary_hour,
                                            settings.timezone) if settings.daily_summary_enabled else None)
        self._started = False
        self._register_commands()

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), self.settings.discord_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands (can take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id if self.user else "?")
        if not self._started:
            self._started = True
            self.scheduler.start()
            if self.daily:
                self.daily.start()

    async def close(self) -> None:
        self.scheduler.stop()
        if self.daily:
            self.daily.stop()
        try:
            await self.app.aclose()
        finally:
            await super().close()

    async def send_dm(self, target: str, payload: dict) -> None:
        """Deliver to a user's DMs, or to a server channel when target is 'channel:<id>'."""
        if target.startswith(CHANNEL_PREFIX):
            cid = int(target[len(CHANNEL_PREFIX):])
            dest = self.get_channel(cid) or await self.fetch_channel(cid)
        else:
            uid = int(target)
            dest = self.get_user(uid) or await self.fetch_user(uid)
        await dest.send(content=payload.get("content"), embed=payload.get("embed"))

    # ------------------------------------------------------------------ commands
    def _register_commands(self) -> None:
        tree, app = self.tree, self.app

        async def deny_if_not_owner(interaction: discord.Interaction) -> bool:
            if not app.is_owner(interaction.user.id):
                await interaction.response.send_message(
                    "Only the registered owner can use this command (run /subscribe first).", ephemeral=True)
                return True
            return False

        @tree.command(name="subscribe", description="Receive DMs for new Canadian SWE internships")
        async def subscribe(interaction: discord.Interaction):
            res = app.subscribe(interaction.user.id)
            await interaction.response.send_message(res.message, ephemeral=True)
            if res.ok and app.notifier and not app.scanner.running:
                try:
                    await app.notifier.deliver_pending()
                except Exception:
                    log.exception("Delivery after subscribe failed")

        @tree.command(name="unsubscribe", description="Stop internship DMs")
        async def unsubscribe(interaction: discord.Interaction):
            res = app.unsubscribe(interaction.user.id)
            await interaction.response.send_message(res.message, ephemeral=True)

        @tree.command(name="status", description="Bot status, counts and source health")
        async def status(interaction: discord.Interaction):
            if await deny_if_not_owner(interaction):
                return
            st = app.status(interaction.user.id, self.scheduler.next_run_at)
            embed = discord.Embed(title="Internship Radar status", color=0x3498DB)
            embed.add_field(name="Subscribed", value="Yes" if st["subscribed"] else "No")
            embed.add_field(name="Notifications go to", value=st["notify_target"])
            embed.add_field(name="Jobs stored", value=str(st["jobs"]))
            embed.add_field(name="DMs sent", value=str(st["notified"]))
            embed.add_field(name="Pending DMs", value=str(st["pending"]))
            embed.add_field(name="Active sources", value=str(st["active_sources"]))
            embed.add_field(name="Scan running", value="Yes" if st["scan_running"] else "No")
            embed.add_field(name="Last successful scan", value=_ts(st["last_successful_scan"]))
            embed.add_field(name="Next scheduled scan", value=_ts(st["next_scan"]))
            apps = st["applications"]
            embed.add_field(name="Applications", value=", ".join(f"{k}: {v}" for k, v in apps.items() if k != "new") or "none")
            if st["failing_sources"]:
                lines = [f"• `{s}` — {(e or '')[:90]}" for s, e in st["failing_sources"][:12]]
                more = len(st["failing_sources"]) - 12
                if more > 0:
                    lines.append(f"…and {more} more")
                embed.add_field(name=f"Source errors ({len(st['failing_sources'])})", value="\n".join(lines)[:1024],
                                inline=False)
            else:
                embed.add_field(name="Source errors", value="None in the latest checks", inline=False)
            if st["config_errors"]:
                embed.add_field(name="Config warnings", value="\n".join(st["config_errors"])[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @tree.command(name="notify_here", description="Post new internships in this channel instead of DMs")
        async def notify_here(interaction: discord.Interaction):
            if await deny_if_not_owner(interaction):
                return
            channel = interaction.channel
            if interaction.guild is None or channel is None:
                await interaction.response.send_message("Run this inside a server text channel.", ephemeral=True)
                return
            perms = channel.permissions_for(interaction.guild.me)
            missing = [name for name, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                             ("Embed Links", perms.embed_links)) if not ok]
            if missing:
                await interaction.response.send_message(
                    f"I can't post here yet — missing permission(s): {', '.join(missing)}.", ephemeral=True)
                return
            res = app.set_notify_channel(interaction.user.id, channel.id)
            await interaction.response.send_message(res.message, ephemeral=True)

        @tree.command(name="notify_dm", description="Send new internships to your DMs instead of a channel")
        async def notify_dm(interaction: discord.Interaction):
            if await deny_if_not_owner(interaction):
                return
            res = app.set_notify_channel(interaction.user.id, None)
            await interaction.response.send_message(res.message, ephemeral=True)

        @tree.command(name="scan", description="Run a job scan now")
        async def scan(interaction: discord.Interaction):
            if await deny_if_not_owner(interaction):
                return
            if app.scanner.running:
                await interaction.response.send_message("A scan is already running.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            report = await app.scanner.run("manual")
            await interaction.followup.send(report.summary() + (
                "\nFailed: " + ", ".join(report.errors) if report.errors else ""), ephemeral=True)

        @tree.command(name="latest", description="Show the most recently discovered matching jobs")
        @app_commands.describe(count="Jobs per page (1-25)", page="Page number (1 = newest)",
                               province="Only Québec, only Ontario, or elsewhere",
                               session="Only winter, summer or fall internships")
        @app_commands.choices(
            province=[app_commands.Choice(name="🔵 Québec (QC)", value="QC"),
                      app_commands.Choice(name="🔴 Ontario (ON)", value="ON"),
                      app_commands.Choice(name="⚪ Ailleurs / autres", value="OTHER")],
            session=[app_commands.Choice(name="❄️ Hiver", value="Winter"),
                     app_commands.Choice(name="☀️ Été", value="Summer"),
                     app_commands.Choice(name="🍂 Automne", value="Fall")])
        async def latest(interaction: discord.Interaction, count: app_commands.Range[int, 1, 25] = 10,
                         page: app_commands.Range[int, 1, 1000] = 1,
                         province: app_commands.Choice[str] | None = None,
                         session: app_commands.Choice[str] | None = None):
            prov = province.value if province else None
            season = session.value if session else None
            total = app.db.count_filtered(prov, season)
            pages = max(1, -(-total // count))
            jobs = app.db.latest_jobs(limit=count, offset=(page - 1) * count, province=prov, season=season)
            label = " · ".join(c.name for c in (province, session) if c) or "all"
            embed = build_summary_embed(f"Latest jobs ({label}) — page {page}/{pages}, {total} found", jobs,
                                        limit=count,
                                        footer="🔵 QC · 🔴 ON · ❄️ Hiver · ☀️ Été · 🍂 Automne · Full list: /export")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @tree.command(name="export", description="Download every stored match as a file")
        @app_commands.describe(format="html = clickable table for your browser, csv = Excel / Google Sheets")
        @app_commands.choices(format=[app_commands.Choice(name="html (browser)", value="html"),
                                      app_commands.Choice(name="csv (Excel)", value="csv")])
        async def export(interaction: discord.Interaction, format: app_commands.Choice[str] | None = None):
            # Anyone in the server can download the list; only the owner's copy includes application statuses.
            mine = app.is_owner(interaction.user.id)
            fmt = format.value if format else "html"
            jobs = app.db.all_jobs()
            tz = self.settings.timezone
            data = to_html(jobs, tz, include_status=mine) if fmt == "html" else to_csv(jobs, tz, include_status=mine)
            name = f"internships-{datetime.now().strftime('%Y-%m-%d')}.{fmt}"
            note = " Your status column updates with /applied." if mine else ""
            await interaction.response.send_message(
                f"{len(jobs)} internships found since the start, best first. Open the file in your browser.{note}",
                file=discord.File(io.BytesIO(data), filename=name), ephemeral=True)

        @tree.command(name="test_notification", description="Send a test notification (channel or DM)")
        async def test_notification(interaction: discord.Interaction):
            if await deny_if_not_owner(interaction):
                return
            try:
                target = await app.notifier.send_test(str(interaction.user.id))
                if target.startswith(CHANNEL_PREFIX):
                    msg = f"Test message posted in <#{target[len(CHANNEL_PREFIX):]}>."
                else:
                    msg = "Test DM sent — check your direct messages."
            except discord.Forbidden:
                if app.notifier.channel_id():
                    msg = ("Discord refused the post. Give the bot *View Channel*, *Send Messages* and "
                           "*Embed Links* in the notification channel.")
                else:
                    msg = ("Discord refused the DM. Enable *Allow direct messages from server members* for this "
                           "server (Server → Privacy Settings) and try again.")
            except Exception as exc:
                msg = f"Test DM failed: {type(exc).__name__}: {exc}"
            await interaction.response.send_message(msg, ephemeral=True)

        @tree.command(name="applied", description="Mark a job as applied (or another status)")
        @app_commands.describe(job_id="Job ID shown in the notification", status="Application status")
        @app_commands.choices(status=STATUS_CHOICES)
        async def applied(interaction: discord.Interaction, job_id: int,
                          status: app_commands.Choice[str] | None = None):
            res = app.mark_applied(interaction.user.id, job_id, status.value if status else "applied")
            await interaction.response.send_message(res.message, ephemeral=True)

        @tree.command(name="help", description="List commands")
        async def help_cmd(interaction: discord.Interaction):
            await interaction.response.send_message(HELP_TEXT, ephemeral=True)
