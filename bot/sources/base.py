"""Common interface for job sources.

To add a new ATS/aggregator: subclass ``Source``, set ``kind``, implement ``fetch`` and
register the class in ``bot/sources/__init__.py``. Each instance is configured from one
``[[<kind>]]`` table in sources.toml.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Callable

from ..http import HttpClient
from ..models import Job

INTERNISH_TITLE_RE = re.compile(
    r"\b(intern\w*|co-?op|coop|stagiaire|stage|student|apprentice\w*|trainee|placement|pey|"
    r"university|campus|new[\s-]?grad\w*|graduate|early career|entry[\s-]level|junior|"
    r"summer|fall|winter|spring|alternance|20(2[6-9]|3\d))\b",
    re.I,
)


def looks_like_student_role(title: str) -> bool:
    """Cheap pre-filter used before expensive per-job detail requests."""
    return bool(INTERNISH_TITLE_RE.search(title or ""))


@dataclass
class FetchContext:
    """Information the pipeline gives adapters so they can skip work for known jobs."""

    is_known: Callable[[str], bool] = lambda uid: False
    max_details: int = 40
    notes: list[str] = field(default_factory=list)


class Source(abc.ABC):
    kind: str = "base"
    priority: int = 50  # lower = processed first when de-duplicating (direct ATS before aggregators)

    def __init__(self, name: str, company: str | None = None, enabled: bool = True,
                 assume_country: str | None = None, **options) -> None:
        if not name:
            raise ValueError(f"{self.kind} source requires a name/board identifier")
        self.name = name
        self.company = company or name
        self.enabled = enabled
        self.assume_country = assume_country
        self.options = options

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"

    @abc.abstractmethod
    async def fetch(self, http: HttpClient, ctx: FetchContext) -> list[Job]:
        """Fetch, parse and normalize listings. Raise SourceFetchError on failure."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.id}>"
