"""Configuration loaded from environment variables (.env) and sources.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .classify import FilterConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _csv(value: str | None, default: list[str]) -> list[str]:
    if value is None or value.strip() == "":
        return list(default)
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "y")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        raise ValueError(f"Expected an integer, got {value!r}") from None


def database_path_from_url(url: str) -> str:
    """sqlite:///./jobs.db -> ./jobs.db ; sqlite:////data/jobs.db -> /data/jobs.db"""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):] or "jobs.db"
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):] or "jobs.db"
    if "://" in url:
        raise ValueError(f"Only sqlite DATABASE_URL values are supported, got {url!r}")
    return url


@dataclass
class Settings:
    discord_bot_token: str | None = None
    discord_guild_id: int | None = None
    discord_user_id: int | None = None
    notify_channel_id: int | None = None  # post in this channel instead of DMs
    scan_interval_minutes: int = 30
    database_url: str = "sqlite:///./jobs.db"
    log_level: str = "INFO"
    sources_file: str = "sources.toml"
    filters: FilterConfig = field(default_factory=FilterConfig)
    notify_min_score: int = 55
    max_dms_per_scan: int = 25
    baseline_notify_limit: int = 10
    pending_max_age_days: int = 3
    daily_summary_enabled: bool = False
    daily_summary_hour: int = 9
    timezone: str = "America/Toronto"
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    source_concurrency: int = 6
    max_detail_requests_per_source: int = 40
    user_agent: str = "CanadianInternshipRadar/1.0 (personal job-alert bot)"
    export_html_path: str | None = "internships.html"  # rewritten after every scan; None/empty = off

    @property
    def database_path(self) -> str:
        return database_path_from_url(self.database_url)


def load_settings(env: dict[str, str] | None = None, load_dotenv_file: bool = True) -> Settings:
    if env is None:
        if load_dotenv_file:
            try:
                from dotenv import load_dotenv

                load_dotenv(PROJECT_ROOT / ".env")
                load_dotenv()  # also current working directory
            except ImportError:  # pragma: no cover
                pass
        env = dict(os.environ)
    g = env.get
    filters = FilterConfig(
        target_provinces=[p.upper() for p in _csv(g("TARGET_PROVINCES"), ["QC", "ON"])],
        target_cities=_csv(g("TARGET_CITIES"), ["Montreal", "Quebec City", "Toronto", "Waterloo", "Ottawa"]),
        target_terms=_csv(g("TARGET_TERMS"), ["Summer 2027", "Fall 2027", "Winter 2027"]),
        included_categories=_csv(g("INCLUDED_CATEGORIES"), FilterConfig().included_categories),
        excluded_keywords=_csv(g("EXCLUDED_KEYWORDS"), FilterConfig().excluded_keywords),
        include_new_grad=_bool(g("INCLUDE_NEW_GRAD"), False),
        allow_unpaid=_bool(g("ALLOW_UNPAID"), False),
        include_unknown_location=_bool(g("INCLUDE_UNKNOWN_LOCATION"), True),
        min_score=_int(g("MIN_RELEVANCE_SCORE"), 40),
    )
    channel = g("NOTIFY_CHANNEL_ID")
    guild = g("DISCORD_GUILD_ID")
    user = g("DISCORD_USER_ID")
    s = Settings(
        discord_bot_token=(g("DISCORD_BOT_TOKEN") or None),
        discord_guild_id=int(guild) if guild and guild.strip().isdigit() else None,
        discord_user_id=int(user) if user and user.strip().isdigit() else None,
        notify_channel_id=int(channel) if channel and channel.strip().isdigit() else None,
        scan_interval_minutes=max(5, _int(g("SCAN_INTERVAL_MINUTES"), 30)),
        database_url=g("DATABASE_URL") or "sqlite:///./jobs.db",
        log_level=(g("LOG_LEVEL") or "INFO").upper(),
        sources_file=g("SOURCES_FILE") or "sources.toml",
        filters=filters,
        notify_min_score=_int(g("NOTIFY_MIN_SCORE"), max(55, filters.min_score)),
        max_dms_per_scan=_int(g("MAX_DMS_PER_SCAN"), 25),
        baseline_notify_limit=_int(g("BASELINE_NOTIFY_LIMIT"), 10),
        pending_max_age_days=_int(g("PENDING_MAX_AGE_DAYS"), 3),
        daily_summary_enabled=_bool(g("DAILY_SUMMARY_ENABLED"), False),
        daily_summary_hour=_int(g("DAILY_SUMMARY_HOUR"), 9),
        timezone=g("TIMEZONE") or "America/Toronto",
        http_timeout_seconds=float(g("HTTP_TIMEOUT_SECONDS") or 20),
        http_max_retries=_int(g("HTTP_MAX_RETRIES"), 3),
        source_concurrency=max(1, _int(g("SOURCE_CONCURRENCY"), 6)),
        max_detail_requests_per_source=_int(g("MAX_DETAIL_REQUESTS_PER_SOURCE"), 40),
        export_html_path=(g("EXPORT_HTML_PATH") if g("EXPORT_HTML_PATH") is not None else "internships.html") or None,
    )
    return s


def resolve_sources_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        candidate = PROJECT_ROOT / p
        if candidate.exists():
            return candidate
    return p


def load_sources_config(path: str | Path) -> dict:
    p = resolve_sources_path(str(path))
    with open(p, "rb") as fh:
        return tomllib.load(fh)
