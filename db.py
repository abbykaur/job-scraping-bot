"""SQLite storage layer for the jobs bot.

Design: producers (fetchers/scrapers) normalize jobs and upsert them here;
the Discord consumer queries per-feed subsets of unposted rows and marks them
posted. One row per job (deduped by job_id), storing objective flags so each
feed is just a query — "store facts, derive views".

Stdlib only (sqlite3). Safe to call init_db() on every startup.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("jobs.db")

# Row shape. Flags are computed once at ingest so the consumer can filter cheaply.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,          -- "simplify", later "greenhouse", ...
    company      TEXT,
    title        TEXT,
    url          TEXT,
    location     TEXT,                   -- human-readable, comma-joined
    category     TEXT,                   -- raw source category (kept for reference)
    is_security  INTEGER NOT NULL DEFAULT 0,
    is_aiml      INTEGER NOT NULL DEFAULT 0,
    is_bay       INTEGER NOT NULL DEFAULT 0,
    scope        TEXT NOT NULL DEFAULT 'none',  -- 'local' | 'remote' | 'none'
    active       INTEGER NOT NULL DEFAULT 1,
    posted       INTEGER NOT NULL DEFAULT 0,     -- 0 until sent to Discord
    date_posted  INTEGER DEFAULT 0,      -- source's timestamp, for ordering
    first_seen   TEXT                    -- ISO time we first ingested it
);
"""

# Index the columns the consumer filters/sorts on most.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_security ON jobs(posted, is_bay, is_security);",
    "CREATE INDEX IF NOT EXISTS idx_aiml ON jobs(posted, is_bay, is_aiml);",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    return conn


def init_db() -> None:
    """Create the table + indexes if they don't exist. Idempotent."""
    with _connect() as conn:
        conn.execute(SCHEMA)
        for stmt in INDEXES:
            conn.execute(stmt)


def upsert_job(conn: sqlite3.Connection, job: dict) -> None:
    """Insert a normalized job, or update it if we've seen this job_id before.

    `job` is the normalized dict with keys matching the columns:
    job_id, source, company, title, url, location, category,
    is_security, is_aiml, is_bay, scope, active, date_posted, first_seen.

    On re-seeing a job (same job_id) we REFRESH the mutable fields the source
    may have changed (active/scope/title/location/url and the derived flags),
    but PRESERVE `posted` and `first_seen` — `posted` must never reset or we'd
    re-post every cycle (this column replaces the old seen_jobs.json), and
    `first_seen` records the original sighting.
    """
    conn.execute(
        """
        INSERT INTO jobs
          (job_id, source, company, title, url, location, category,
           is_security, is_aiml, is_bay, scope, active, date_posted, first_seen)
        VALUES
          (:job_id, :source, :company, :title, :url, :location, :category,
           :is_security, :is_aiml, :is_bay, :scope, :active, :date_posted, :first_seen)
        ON CONFLICT(job_id) DO UPDATE SET
          company     = excluded.company,
          title       = excluded.title,
          url         = excluded.url,
          location    = excluded.location,
          category    = excluded.category,
          is_security = excluded.is_security,
          is_aiml     = excluded.is_aiml,
          is_bay      = excluded.is_bay,
          scope       = excluded.scope,
          active      = excluded.active,
          date_posted = excluded.date_posted
          -- posted and first_seen are intentionally NOT updated (preserved)
        """,
        job,
    )


def ingest(jobs: list[dict]) -> int:
    """Upsert a batch of normalized jobs in one transaction. Returns count."""
    with _connect() as conn:
        for job in jobs:
            upsert_job(conn, job)
    return len(jobs)


def prune_missing(source: str, current_ids: list[str]) -> int:
    """Hard-delete rows from `source` whose job_id is no longer in the feed.

    SimplifyJobs is a full snapshot each pull, so a stored job that's absent from
    the current fetch has genuinely been removed. Scoped to `source` so one feed's
    sweep never touches jobs produced by another source.

    Returns the number of rows deleted. `current_ids` empty is treated as a no-op
    guard (an empty/failed fetch must not wipe the table).
    """
    if not current_ids:
        return 0
    with _connect() as conn:
        # Chunk the NOT IN list to stay under SQLite's ~999 variable limit.
        keep = set(current_ids)
        stored = [r[0] for r in conn.execute(
            "SELECT job_id FROM jobs WHERE source = ?", (source,)
        )]
        to_delete = [jid for jid in stored if jid not in keep]
        conn.executemany("DELETE FROM jobs WHERE job_id = ?",
                         [(jid,) for jid in to_delete])
        return len(to_delete)


def fetch_unposted(where: str, params: tuple = (), limit: int = 10) -> list[sqlite3.Row]:
    """Return unposted, active jobs matching a feed's WHERE clause, oldest first.

    `where` is the feed-specific predicate, e.g. "is_security=1 AND is_bay=1".
    Kept as a parameter so each feed supplies its own filter (the "derive views"
    part of the design).
    """
    sql = (
        "SELECT * FROM jobs "
        f"WHERE posted=0 AND active=1 AND ({where}) "
        "ORDER BY date_posted ASC "
        "LIMIT ?"
    )
    with _connect() as conn:
        return conn.execute(sql, (*params, limit)).fetchall()


def fetch_recent(where: str, params: tuple = (), limit: int = 5) -> list[sqlite3.Row]:
    """Return the most-recent active jobs matching a feed's WHERE clause.

    Unlike fetch_unposted, this ignores the posted flag — it's for the /jobs
    command, which shows current listings on demand regardless of prior posting.
    """
    sql = (
        "SELECT * FROM jobs "
        f"WHERE active=1 AND ({where}) "
        "ORDER BY date_posted DESC "
        "LIMIT ?"
    )
    with _connect() as conn:
        return conn.execute(sql, (*params, limit)).fetchall()


def mark_posted(job_ids: list[str]) -> None:
    """Flag the given jobs as posted so they're never sent again."""
    if not job_ids:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE jobs SET posted=1 WHERE job_id=?",
            [(jid,) for jid in job_ids],
        )


def delete_job(job_id: str) -> None:
    """Hard-delete a single job (e.g. its URL was found dead at post time)."""
    with _connect() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
