"""The Discord layer wires up without contacting Discord (no token, no network)."""
from __future__ import annotations

from bot.__main__ import main


async def test_bot_registers_all_commands(settings, monkeypatch, tmp_path):
    from bot import discord_app

    settings.sources_file = "sources.toml"
    bot = discord_app.RadarBot(settings)
    try:
        names = {c.name for c in bot.tree.get_commands()}
        assert names == {"subscribe", "unsubscribe", "status", "scan", "latest", "test_notification", "applied", "help",
                         "notify_here", "notify_dm"}
        assert bot.scheduler.interval.total_seconds() == settings.scan_interval_minutes * 60
        assert bot.daily is None  # daily summary disabled by default
        assert len(bot.app.sources) >= 50
    finally:
        await bot.app.aclose()


def test_cli_without_token_explains_setup(monkeypatch, capsys):
    import bot.__main__ as entry

    real = entry.load_settings
    monkeypatch.setattr(entry, "load_settings", lambda: real(env={}))
    assert main([]) == 2
    assert "DISCORD_BOT_TOKEN" in capsys.readouterr().err
