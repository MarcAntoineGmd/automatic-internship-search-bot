"""SQLite persistence (stdlib sqlite3; no ORM needed for a single-user bot)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dedupe import canonical_url_key, fingerprint_base, provinces_compatible, terms_compatible
from .models import Classification, Job
from .textutil import parse_datetime, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_job_id TEXT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    location TEXT,
    city TEXT,
    province TEXT,
    provinces TEXT,
    country TEXT,
    workplace TEXT,
    employment_type TEXT,
    internship_type TEXT,
    term TEXT,
    url TEXT,
    url_key TEXT,
    fp_base TEXT,
    date_posted TEXT,
    date_discovered TEXT NOT NULL,
    last_seen TEXT,
    eligibility TEXT,
    score INTEGER,
    priority TEXT,
    category TEXT,
    reasons TEXT,
    also_seen_on TEXT,
    notify_status TEXT,            -- NULL = pending, 'sent', 'baseline', 'skipped'
    notified_at TEXT,
    application_status TEXT NOT NULL DEFAULT 'new',
    application_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fp_base);
CREATE INDEX IF NOT EXISTS idx_jobs_url_key ON jobs(url_key);
CREATE INDEX IF NOT EXISTS idx_jobs_notify ON jobs(notify_status, score);
CREATE TABLE IF NOT EXISTS job_links (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (job_id, source)
);
CREATE TABLE IF NOT EXISTS job_keys (
    key TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS rejected (
    uid TEXT PRIMARY KEY,
    reason TEXT,
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_ok INTEGER DEFAULT 0,
    sources_failed INTEGER DEFAULT 0,
    jobs_fetched INTEGER DEFAULT 0,
    jobs_qualified INTEGER DEFAULT 0,
    new_jobs INTEGER DEFAULT 0,
    notifications_sent INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS source_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER,
    ok INTEGER NOT NULL,
    jobs_fetched INTEGER DEFAULT 0,
    new_jobs INTEGER DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_source ON source_checks(source, id);
CREATE TABLE IF NOT EXISTS subscribers (
    user_id TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    subscribed_at TEXT,
    unsubscribed_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

AGGREGATOR_PREFIXES = ("github_listings:", "markdown_table:")
LINK_FRESH_WINDOW = timedelta(hours=1)
SCHEMA_VERSION = "2"

APPLICATION_STATUSES = ("new", "applied", "interviewing", "offer", "rejected", "ignored")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


@dataclass
class JobRow:
    id: int
    uid: str
    source: str
    company: str
    title: str
    description: str | None
    location: str | None
    city: str | None
    province: str | None
    provinces: list[str]
    country: str | None
    workplace: str | None
    employment_type: str | None
    internship_type: str | None
    term: str | None
    url: str | None
    date_posted: datetime | None
    date_discovered: datetime
    last_seen: datetime | None
    eligibility: str | None
    score: int
    priority: str
    category: str | None
    reasons: list[str]
    also_seen_on: list[str]
    notify_status: str | None
    application_status: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "JobRow":
        return cls(
            id=r["id"], uid=r["uid"], source=r["source"], company=r["company"], title=r["title"],
            description=r["description"], location=r["location"], city=r["city"], province=r["province"],
            provinces=[p for p in (r["provinces"] or "").split(",") if p], country=r["country"],
            workplace=r["workplace"], employment_type=r["employment_type"],
            internship_type=r["internship_type"], term=r["term"], url=r["url"],
            date_posted=parse_datetime(r["date_posted"]), date_discovered=parse_datetime(r["date_discovered"]),
            last_seen=parse_datetime(r["last_seen"]),
            eligibility=r["eligibility"], score=r["score"] or 0, priority=r["priority"] or "Low",
            category=r["category"], reasons=json.loads(r["reasons"] or "[]"),
            also_seen_on=[s for s in (r["also_seen_on"] or "").split(",") if s],
            notify_status=r["notify_status"], application_status=r["application_status"],
        )


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe; avoids an fsync per write
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """v2: per-source links + re-derived URL keys (Workday req ids like 26WD102151 were missed before)."""
        if self.get_meta("schema_version") == SCHEMA_VERSION:
            return
        with self.transaction():
            rows = self.conn.execute("SELECT id, source, url, date_discovered, last_seen FROM jobs").fetchall()
            for r in rows:
                if r["url"]:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO job_links(job_id, source, url, first_seen, last_seen) VALUES(?,?,?,?,?)",
                        (r["id"], r["source"], r["url"], r["date_discovered"], r["last_seen"] or r["date_discovered"]))
                    key = canonical_url_key(r["url"])
                    if key and not self._is_generic_url_key(key):
                        self.conn.execute("INSERT OR IGNORE INTO job_keys(key, job_id) VALUES(?, ?)", (key, r["id"]))
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def transaction(self):
        """Group many writes into one commit (much faster, esp. on Windows)."""
        with self._lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _one(self, sql: str, params=()):
        return self._exec(sql, params).fetchone()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        self._exec("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    def sync_filter_signature(self, signature: str) -> bool:
        """Clear the rejected-job cache when filter settings change. Returns True if cleared."""
        if self.get_meta("filter_signature") != signature:
            self._exec("DELETE FROM rejected")
            self.set_meta("filter_signature", signature)
            return True
        return False

    # ------------------------------------------------------------------ dedupe
    @staticmethod
    def keys_for(job: Job) -> list[str]:
        keys = [f"uid:{job.uid}"]
        url_key = canonical_url_key(job.url)
        if url_key and not Database._is_generic_url_key(url_key):
            keys.append(url_key)
        keys.extend(job.extra_keys)
        return list(dict.fromkeys(k for k in keys if k))

    @staticmethod
    def _is_generic_url_key(key: str) -> bool:
        """A bare careers page (e.g. 'url:acme.com/careers') must not merge different roles."""
        if not key.startswith("url:"):
            return False
        rest = key[4:]
        host, _, path = rest.partition("/")
        if "?" in rest:
            return False
        segments = [s for s in path.split("/") if s]
        return len(segments) < 2 and not any(ch.isdigit() for ch in path)

    def is_known_uid(self, uid: str) -> bool:
        if self._one("SELECT 1 FROM job_keys WHERE key=?", (f"uid:{uid}",)):
            return True
        return self._one("SELECT 1 FROM rejected WHERE uid=?", (uid,)) is not None

    def is_rejected(self, uid: str) -> bool:
        return self._one("SELECT 1 FROM rejected WHERE uid=?", (uid,)) is not None

    def find_duplicate(self, job: Job, term: str | None = None, provinces: list[str] | None = None,
                       use_fingerprint: bool = True) -> int | None:
        keys = self.keys_for(job)
        q = ",".join("?" * len(keys))
        row = self._one(f"SELECT job_id FROM job_keys WHERE key IN ({q}) LIMIT 1", keys)
        if row:
            return row["job_id"]
        if not use_fingerprint:
            return None
        fp = fingerprint_base(job.company, job.title)
        if fp.endswith("|") or fp.startswith("|"):
            return None
        for r in self._exec("SELECT id, term, provinces FROM jobs WHERE fp_base=?", (fp,)).fetchall():
            other_provs = [p for p in (r["provinces"] or "").split(",") if p]
            if terms_compatible(term, r["term"]) and provinces_compatible(provinces or [], other_provs):
                return r["id"]
        return None

    def link_keys(self, job_id: int, job: Job) -> None:
        with self._lock:
            for key in self.keys_for(job):
                self.conn.execute("INSERT OR IGNORE INTO job_keys(key, job_id) VALUES(?, ?)", (key, job_id))

    def touch(self, job_id: int, job: Job, now: datetime | None = None) -> None:
        now = now or utcnow()
        row = self._one("SELECT source, also_seen_on FROM jobs WHERE id=?", (job_id,))
        if not row:
            return
        seen = [s for s in (row["also_seen_on"] or "").split(",") if s]
        if job.source != row["source"] and job.source not in seen:
            seen.append(job.source)
        self._exec("UPDATE jobs SET last_seen=?, also_seen_on=? WHERE id=?", (_iso(now), ",".join(seen), job_id))
        self.link_keys(job_id, job)
        self._record_link(job_id, job, now)
        self._refresh_best_url(job_id)

    def _record_link(self, job_id: int, job: Job, now: datetime) -> None:
        if not job.url:
            return
        self._exec("""INSERT INTO job_links(job_id, source, url, first_seen, last_seen) VALUES(?,?,?,?,?)
                      ON CONFLICT(job_id, source) DO UPDATE SET url=excluded.url, last_seen=excluded.last_seen""",
                   (job_id, job.source, job.url, _iso(now), _iso(now)))

    def _refresh_best_url(self, job_id: int) -> None:
        """Show the link that a source still lists; prefer the company's own site over trackers.

        When a tracker drops a posting (or its link dies) but the company's career site still
        lists it, the stored URL switches to the company's link automatically."""
        with self._lock:
            links = self.conn.execute("SELECT source, url, last_seen FROM job_links WHERE job_id=?", (job_id,)).fetchall()
        if not links:
            return
        newest = max(parse_datetime(l["last_seen"]) or datetime.min.replace(tzinfo=timezone.utc) for l in links)
        fresh = [l for l in links
                 if (parse_datetime(l["last_seen"]) or newest) >= newest - LINK_FRESH_WINDOW]
        best = sorted(fresh, key=lambda l: (l["source"].startswith(AGGREGATOR_PREFIXES),
                                            -(parse_datetime(l["last_seen"]) or newest).timestamp()))[0]
        self._exec("UPDATE jobs SET url=? WHERE id=? AND COALESCE(url,'') != ?", (best["url"], job_id, best["url"]))

    def links_for(self, job_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT source, url, first_seen, last_seen FROM job_links WHERE job_id=? ORDER BY last_seen DESC",
                (job_id,)).fetchall()]

    def insert_job(self, job: Job, cls: Classification, now: datetime | None = None,
                   notify_status: str | None = None) -> int:
        now = now or utcnow()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO jobs(uid, source, source_job_id, company, title, description, location, city,
                   province, provinces, country, workplace, employment_type, internship_type, term, url, url_key,
                   fp_base, date_posted, date_discovered, last_seen, eligibility, score, priority, category,
                   reasons, notify_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.uid, job.source, job.source_job_id, job.company, job.title, (job.description or "")[:8000],
                 job.location, cls.city, cls.province, ",".join(cls.provinces), cls.country, cls.workplace,
                 job.employment_type, cls.internship_type, cls.term, job.url, canonical_url_key(job.url),
                 fingerprint_base(job.company, job.title), _iso(job.date_posted), _iso(now), _iso(now),
                 cls.eligibility, cls.score, cls.priority, cls.category, json.dumps(cls.reasons), notify_status),
            )
            job_id = cur.lastrowid
            self.link_keys(job_id, job)
            self._record_link(job_id, job, now)
            return job_id

    def add_rejected(self, uid: str, reason: str | None, now: datetime | None = None) -> None:
        now_s = _iso(now or utcnow())
        self._exec("""INSERT INTO rejected(uid, reason, first_seen, last_seen) VALUES(?,?,?,?)
                      ON CONFLICT(uid) DO UPDATE SET last_seen=excluded.last_seen, reason=excluded.reason""",
                   (uid, reason, now_s, now_s))

    # ------------------------------------------------------------------ notifications
    def pending_notifications(self, min_score: int, max_age_days: int, limit: int,
                              now: datetime | None = None) -> list[JobRow]:
        cutoff = _iso((now or utcnow()) - timedelta(days=max_age_days))
        rows = self._exec(
            """SELECT * FROM jobs WHERE notify_status IS NULL AND score >= ? AND date_discovered >= ?
               ORDER BY score DESC, id ASC LIMIT ?""", (min_score, cutoff, limit)).fetchall()
        return [JobRow.from_row(r) for r in rows]

    def mark_notified(self, job_id: int, status: str = "sent", now: datetime | None = None) -> None:
        self._exec("UPDATE jobs SET notify_status=?, notified_at=? WHERE id=? AND notify_status IS NULL",
                   (status, _iso(now or utcnow()), job_id))

    def count_jobs(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM jobs")["n"]

    def count_notified(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM jobs WHERE notify_status='sent'")["n"]

    def count_pending(self, min_score: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM jobs WHERE notify_status IS NULL AND score >= ?",
                         (min_score,))["n"]

    # ------------------------------------------------------------------ queries
    def get_job(self, job_id: int) -> JobRow | None:
        r = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return JobRow.from_row(r) if r else None

    @staticmethod
    def _filters(province: str | None, season: str | None) -> tuple[str, list]:
        """province: 'QC', 'ON', ... or 'OTHER' (neither QC nor ON); season: 'Winter' | 'Spring' | 'Summer' | 'Fall'."""
        sql, params = "", []
        if province == "OTHER":
            sql += " AND COALESCE(provinces,'') NOT LIKE '%QC%' AND COALESCE(provinces,'') NOT LIKE '%ON%'"
        elif province:
            sql += " AND (',' || COALESCE(provinces,'') || ',') LIKE ?"
            params.append(f"%,{province},%")
        if season:
            sql += " AND term LIKE ?"
            params.append(f"{season} %")
        return sql, params

    def latest_jobs(self, limit: int = 10, min_score: int = 0, offset: int = 0,
                    province: str | None = None, season: str | None = None) -> list[JobRow]:
        extra, params = self._filters(province, season)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE score >= ?" + extra +
                " ORDER BY date_discovered DESC, score DESC, id DESC LIMIT ? OFFSET ?",
                [min_score, *params, limit, offset]).fetchall()
        return [JobRow.from_row(r) for r in rows]

    def count_filtered(self, province: str | None = None, season: str | None = None) -> int:
        extra, params = self._filters(province, season)
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE 1=1" + extra, params).fetchone()[0]

    def all_jobs(self) -> list[JobRow]:
        """Every stored match, best first (used by /export)."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY score DESC, date_discovered DESC, id DESC").fetchall()
        return [JobRow.from_row(r) for r in rows]

    def jobs_since(self, since: datetime) -> list[JobRow]:
        rows = self._exec("SELECT * FROM jobs WHERE date_discovered >= ? ORDER BY score DESC",
                          (_iso(since),)).fetchall()
        return [JobRow.from_row(r) for r in rows]

    def set_application_status(self, job_id: int, status: str, now: datetime | None = None) -> bool:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"status must be one of {', '.join(APPLICATION_STATUSES)}")
        cur = self._exec("UPDATE jobs SET application_status=?, application_updated_at=? WHERE id=?",
                         (status, _iso(now or utcnow()), job_id))
        return cur.rowcount > 0

    def application_counts(self) -> dict[str, int]:
        rows = self._exec("SELECT application_status AS s, COUNT(*) AS n FROM jobs GROUP BY application_status")
        return {r["s"]: r["n"] for r in rows.fetchall()}

    # ------------------------------------------------------------------ scans
    def start_scan(self, trigger: str, now: datetime | None = None) -> int:
        cur = self._exec("INSERT INTO scans(trigger, started_at) VALUES(?, ?)", (trigger, _iso(now or utcnow())))
        return cur.lastrowid

    def finish_scan(self, scan_id: int, *, sources_ok: int, sources_failed: int, jobs_fetched: int,
                    jobs_qualified: int, new_jobs: int, notifications_sent: int = 0, error: str | None = None,
                    now: datetime | None = None) -> None:
        self._exec("""UPDATE scans SET finished_at=?, sources_ok=?, sources_failed=?, jobs_fetched=?,
                      jobs_qualified=?, new_jobs=?, notifications_sent=?, error=? WHERE id=?""",
                   (_iso(now or utcnow()), sources_ok, sources_failed, jobs_fetched, jobs_qualified, new_jobs,
                    notifications_sent, error, scan_id))

    def add_notifications_to_scan(self, scan_id: int, sent: int) -> None:
        self._exec("UPDATE scans SET notifications_sent = notifications_sent + ? WHERE id=?", (sent, scan_id))

    def record_source_check(self, scan_id: int | None, source: str, ok: bool, started_at: datetime,
                            duration_ms: int, jobs_fetched: int = 0, new_jobs: int = 0,
                            error: str | None = None) -> None:
        self._exec("""INSERT INTO source_checks(scan_id, source, started_at, duration_ms, ok, jobs_fetched,
                      new_jobs, error) VALUES(?,?,?,?,?,?,?,?)""",
                   (scan_id, source, _iso(started_at), duration_ms, int(ok), jobs_fetched, new_jobs,
                    (error or "")[:500] or None))

    def has_successful_check(self, source: str) -> bool:
        return self._one("SELECT 1 FROM source_checks WHERE source=? AND ok=1 LIMIT 1", (source,)) is not None

    def last_scan(self) -> dict | None:
        r = self._one("SELECT * FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        return dict(r) if r else None

    def last_successful_scan(self) -> dict | None:
        r = self._one("SELECT * FROM scans WHERE finished_at IS NOT NULL AND sources_ok > 0 ORDER BY id DESC LIMIT 1")
        return dict(r) if r else None

    def latest_source_status(self) -> list[dict]:
        rows = self._exec(
            """SELECT c.* FROM source_checks c JOIN (SELECT source, MAX(id) AS mid FROM source_checks GROUP BY source) m
               ON c.id = m.mid ORDER BY c.source""").fetchall()
        return [dict(r) for r in rows]

    def failing_sources(self) -> list[dict]:
        return [s for s in self.latest_source_status() if not s["ok"]]

    # ------------------------------------------------------------------ subscribers
    def subscribe(self, user_id: int | str, now: datetime | None = None) -> bool:
        """Returns True if this call activated a subscription (False if already active)."""
        uid = str(user_id)
        r = self._one("SELECT active FROM subscribers WHERE user_id=?", (uid,))
        if r and r["active"]:
            return False
        self._exec("""INSERT INTO subscribers(user_id, active, subscribed_at) VALUES(?, 1, ?)
                      ON CONFLICT(user_id) DO UPDATE SET active=1, subscribed_at=excluded.subscribed_at,
                      unsubscribed_at=NULL""", (uid, _iso(now or utcnow())))
        return True

    def unsubscribe(self, user_id: int | str, now: datetime | None = None) -> bool:
        cur = self._exec("UPDATE subscribers SET active=0, unsubscribed_at=? WHERE user_id=? AND active=1",
                         (_iso(now or utcnow()), str(user_id)))
        return cur.rowcount > 0

    def is_subscribed(self, user_id: int | str) -> bool:
        r = self._one("SELECT active FROM subscribers WHERE user_id=?", (str(user_id),))
        return bool(r and r["active"])

    def active_subscribers(self) -> list[str]:
        return [r["user_id"] for r in self._exec("SELECT user_id FROM subscribers WHERE active=1").fetchall()]
