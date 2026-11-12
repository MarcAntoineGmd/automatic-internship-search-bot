"""Normalized job record shared by every source adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Classification:
    """Result of deterministic filtering/scoring for one job."""

    included: bool
    score: int
    priority: str  # "High" | "Medium" | "Low"
    category: str | None
    reasons: list[str] = field(default_factory=list)
    exclusion_reason: str | None = None
    # Derived/normalized fields
    country: str = "Unknown"  # "Canada" | "Foreign" | "Unknown"
    province: str | None = None
    provinces: list[str] = field(default_factory=list)
    city: str | None = None
    workplace: str | None = None  # "remote" | "hybrid" | "onsite" | None
    remote_scope: str | None = None  # "canada" | "north_america" | "global" | "us" | None
    internship_type: str | None = None  # "internship" | "co-op" | "student" | "new_grad"
    term: str | None = None
    terms: list[str] = field(default_factory=list)
    eligibility: str | None = None


@dataclass
class Job:
    """A job as returned by a source adapter, before persistence.

    Unknown fields stay ``None``/empty; adapters must never invent values.
    """

    source: str  # source id, e.g. "greenhouse:lyft"
    source_job_id: str
    company: str
    title: str
    url: str
    description: str = ""
    location: str = ""  # human-readable raw location text
    countries: list[str] = field(default_factory=list)  # structured country hints from the source
    workplace: str | None = None  # remote/hybrid/onsite if the source says so
    employment_type: str | None = None
    terms_hint: list[str] = field(default_factory=list)  # terms provided by the source (aggregators)
    date_posted: datetime | None = None
    category_hint: str | None = None
    assume_internship: bool = False  # source only lists internships (e.g. internship trackers)
    assume_country: str | None = None  # e.g. "Canada" for a Canada-only board
    extra_keys: list[str] = field(default_factory=list)  # adapter-provided canonical dedupe keys
    details_fetched: bool = True  # False when an adapter skipped the detail call (already known job)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_job_id}"
