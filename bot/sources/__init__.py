"""Source registry. Add new adapters to SOURCE_TYPES."""
from __future__ import annotations

import logging

from .ashby import AshbySource
from .base import FetchContext, Source
from .github_listings import GitHubListingsSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .markdown_table import MarkdownTableSource
from .smartrecruiters import SmartRecruitersSource
from .workday import WorkdaySource

log = logging.getLogger(__name__)

SOURCE_TYPES: dict[str, type[Source]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workday": WorkdaySource,
    "smartrecruiters": SmartRecruitersSource,
    "github_listings": GitHubListingsSource,
    "markdown_table": MarkdownTableSource,
}


def build_sources(config: dict, include_disabled: bool = False) -> tuple[list[Source], list[str]]:
    """Build source instances from a parsed sources.toml. Returns (sources, config_errors)."""
    sources: list[Source] = []
    errors: list[str] = []
    seen: set[str] = set()
    for kind, cls in SOURCE_TYPES.items():
        entries = config.get(kind) or []
        if isinstance(entries, dict):
            entries = [entries]
        for i, entry in enumerate(entries):
            try:
                src = cls(**dict(entry))
            except (TypeError, ValueError) as exc:
                errors.append(f"[[{kind}]] #{i + 1}: {exc}")
                continue
            if src.id in seen:
                errors.append(f"duplicate source {src.id}")
                continue
            seen.add(src.id)
            if src.enabled or include_disabled:
                sources.append(src)
    unknown = [k for k in config if k not in SOURCE_TYPES and k != "settings"]
    for k in unknown:
        errors.append(f"unknown source type [[{k}]]")
    for e in errors:
        log.warning("sources config: %s", e)
    return sources, errors


__all__ = ["Source", "FetchContext", "SOURCE_TYPES", "build_sources"]
