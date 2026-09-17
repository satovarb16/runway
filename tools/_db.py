"""SQLite connection, schema, and JSON->SQL migration (design D1-D4).

Structural replacement for tools/_storage.py, which is deleted in full in
this same change. Where _storage.py serialized a whole-file read-modify-write
via a filesystem lock, this module serializes at the database with an
explicit ``BEGIN IMMEDIATE`` transaction and lets SQLite's own file locking
(plus ``busy_timeout``) do the waiting — across processes, not just threads.

Connection-per-call, never cached at module level: sqlite3 connections are
thread-bound (``check_same_thread=True``), and FastMCP dispatches sync tools
on a worker pool, so a cached connection would raise the first time a second
worker thread touched it. This also preserves the ``_X_PATH`` monkeypatch
convention (here ``_DB_PATH``) the whole test suite depends on: it must be
resolved at call time, never bound as a default argument.

The four PRAGMAs re-issued on every connection are the whole point of this
module — none of them persist except journal_mode, and each is a silent,
one-line way to ship the appearance of a guarantee with none of its
enforcement:

- ``foreign_keys = ON``: OFF by default in SQLite. Without it, every
  ``REFERENCES`` clause in the schema below is decorative.
- ``recursive_triggers = ON``: without it, ``INSERT OR REPLACE`` bypasses a
  ``BEFORE DELETE`` trigger, defeating the append-only guarantee on
  ``resume_versions``.
- ``busy_timeout = 10000``: the direct replacement for _storage.py's
  ``_LOCK_TIMEOUT = 10.0``. Same number on purpose.
- ``BEGIN IMMEDIATE`` (never ``DEFERRED``): a deferred transaction that reads
  first and writes later must *upgrade* to a write lock, and busy_timeout
  does not retry a lock upgrade — it would resurface the exact contention
  failure this design replaces, as a spurious "database is locked".

Migration runs once, inside ``_ensure_schema``, keyed on schema presence
(not file presence) so a partial failure is retryable rather than requiring
manual cleanup. Backups of both legacy JSON files are written before either
is read, and a backup failure aborts the migration rather than proceeding
best-effort — this is irreplaceable, hand-authored data, unlike the
in-memory coercion _backup_once historically guarded.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from pydantic import BaseModel, ConfigDict, ValidationError

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DB_PATH: Path = Path.home() / ".config" / "runway-mcp" / "runway.db"
_SCHEMA_VERSION: int = 5
_BUSY_TIMEOUT_MS: int = 10_000

_JOBS_JSON_NAME = "jobs.json"
_RESUMES_JSON_NAME = "resumes.json"

# Tracks which db paths have already been checked for stale legacy JSON, so
# the check (2 stat calls) and its stderr warning run at most once per
# process per path — "one-time", as the docstring below already claims,
# rather than once per connection/tool call.
_stale_json_checked: set[Path] = set()


class SchemaTooNewError(ValueError):
    """The database was written by a newer runway-mcp than this one understands.

    A ValueError subclass so every existing ``except ValueError`` at the tool
    boundary still turns it into an error envelope, but callers that need to
    distinguish it from ordinary corruption (SC-48) can catch it by name. The
    file is not corrupt and must not be rewritten or deleted.
    """


def _translate_sqlite_error(exc: sqlite3.Error) -> ValueError:
    """Translate a raw ``sqlite3.Error`` into the project's ValueError boundary.

    Every tool docstring says "This tool NEVER raises" and every caller
    already catches ``ValueError`` — but ``sqlite3.DatabaseError`` /
    ``OperationalError`` / ``IntegrityError`` are not ``ValueError``
    subclasses, so an unhandled one crashes straight through the boundary.
    This function is the single place that closes that gap for failures
    happening during connection setup (PRAGMAs, schema check/migration, and
    acquiring the write lock) — see ``connect()``.

    ``SchemaTooNewError`` is deliberately NOT produced here: it is raised
    directly (as a plain Python exception, never a ``sqlite3.Error``) so a
    database written by a newer runway-mcp stays distinguishable from actual
    corruption and is never described as something to delete.

    A lock/busy timeout is a healthy file that is merely contended — its
    message must not use corruption language, which would push a user to
    delete a perfectly good database.
    """
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, sqlite3.OperationalError) and (
        "locked" in lowered or "busy" in lowered
    ):
        return ValueError(
            f"The database is temporarily locked by another runway-mcp "
            f"process or thread and did not become available in time: "
            f"{message}. This is not corruption — retry the request."
        )
    return ValueError(f"The database file is corrupt or could not be read: {message}")


# ---------------------------------------------------------------------------
# Schema DDL — one statement per list entry (sqlite3.execute takes exactly one)
# ---------------------------------------------------------------------------

# Every resume read path orders by created_at DESC — get_resume_version
# ("latest"), list_resume_versions, and both of _general_resume's fallback
# queries. Without this index all four report SCAN + USE TEMP B-TREE FOR
# ORDER BY, sorting the whole table (full resume text in every row) to
# return one. `jobs` has had the equivalent index since 0.3.0.
_RESUME_CREATED_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_resume_versions_created_at "
    "ON resume_versions(created_at DESC)"
)

# The UPDATE trigger names the columns it guards rather than banning UPDATE
# outright. delete_job has to sever resume_versions.job_id when the job it
# points at is removed, and severing a pointer to a row that no longer exists
# is not rewriting history — the version's identity and content stay
# absolutely immutable. `UPDATE OF` fires when a listed column appears in the
# SET clause at all, even assigned its current value, so a mixed statement
# like `SET job_id=NULL, content='x'` still aborts.
_RESUME_NO_UPDATE_TRIGGER = (
    "CREATE TRIGGER resume_versions_no_update "
    "BEFORE UPDATE OF id, label, content, parent_id, legacy_job_url, created_at "
    "ON resume_versions "
    "BEGIN SELECT RAISE(ABORT, 'resume_versions is append-only: versions are "
    "never modified (only job_id may be unlinked, by delete_job)'); END"
)


def _add_status_notes_column(conn: sqlite3.Connection) -> None:
    """Add jobs.status_notes, skipping a database that already has it.

    SQLite has no ADD COLUMN IF NOT EXISTS, and a bare ALTER is the one kind
    of upgrade statement that cannot be re-run. The guard is what keeps the
    idempotency promise below true for this entry.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info('jobs')")}
    if "status_notes" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN status_notes TEXT")


# schema version -> steps that carry a database from version-1 to it. A step
# is either a SQL string or a callable taking the open connection, for the
# cases SQL alone cannot express idempotently.
# Only databases that predate a version run its entry; a fresh database gets
# the finished schema from _SCHEMA_STATEMENTS and is stamped current.
_UpgradeStep = str | Callable[[sqlite3.Connection], None]

_UPGRADES: dict[int, tuple[_UpgradeStep, ...]] = {
    4: (_RESUME_CREATED_AT_INDEX,),
    5: (
        _add_status_notes_column,
        "DROP TRIGGER IF EXISTS resume_versions_no_update",
        _RESUME_NO_UPDATE_TRIGGER,
    ),
}

_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE jobs (
        id             TEXT PRIMARY KEY,
        url            TEXT UNIQUE,
        custom_title   TEXT,
        title          TEXT NOT NULL,
        company        TEXT NOT NULL,
        country        TEXT,
        status         TEXT NOT NULL DEFAULT 'not_applied',
        score          INTEGER,
        recommendation TEXT,
        notes          TEXT,
        status_notes   TEXT,
        analyzed_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE job_descriptions (
        job_id  TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
        jd_text TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE resume_versions (
        id              TEXT PRIMARY KEY,
        label           TEXT NOT NULL,
        content         TEXT NOT NULL,
        parent_id       TEXT REFERENCES resume_versions(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
        job_id          TEXT REFERENCES jobs(id)           ON DELETE RESTRICT ON UPDATE RESTRICT,
        legacy_job_url  TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE work_authorizations (
        country_canonical TEXT PRIMARY KEY,
        country_raw       TEXT NOT NULL,
        declared_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_jobs_status_score ON jobs(status, score DESC)",
    "CREATE INDEX idx_jobs_analyzed_at ON jobs(analyzed_at DESC)",
    "CREATE INDEX idx_resume_versions_job_id ON resume_versions(job_id)",
    _RESUME_CREATED_AT_INDEX,
    _RESUME_NO_UPDATE_TRIGGER,
    """
    CREATE TRIGGER resume_versions_no_delete
    BEFORE DELETE ON resume_versions
    BEGIN SELECT RAISE(ABORT, 'resume_versions is append-only: versions are never deleted'); END
    """,
]


# ---------------------------------------------------------------------------
# Legacy JSON shapes — validated during migration only, never used elsewhere.
# extra="ignore" (not "forbid"): a legacy jobs.json legitimately carries
# visa_verdict, which this migration intentionally drops (D4) rather than
# rejecting the whole file over a field we already decided not to keep.
# ---------------------------------------------------------------------------


class _LegacyJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    title: str
    company: str
    status: str = "not_applied"
    score: int | None = None
    recommendation: str | None = None
    notes: str | None = None
    analyzed_at: str


class _LegacyResumeVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    content: str
    parent_id: str | None = None
    job_url: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Timestamp normalization (moved from jobs_store._parse_iso)
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 datetime string, tolerating both 'Z' and '+00:00'."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_timestamp(value: str) -> str:
    """Normalize a stored timestamp to UTC ISO-8601 with a '+00:00' suffix.

    SQL ORDER BY on TEXT is lexicographic, and '+00:00' sorts differently
    from 'Z' for the same instant ('+' = 0x2B < 'Z' = 0x5A). The server
    always stamps '+00:00' going forward; migration is the one free moment
    to fix any pre-existing mixed-suffix or non-UTC-offset records.
    """
    dt = _parse_iso(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the per-connection PRAGMAs. None of these persist except WAL.

    ``busy_timeout`` MUST be issued FIRST. It configures how long SQLite
    retries an internal lock before giving up — including the lock the very
    next PRAGMA (``journal_mode = WAL``) needs to acquire on a file another
    connection is mid-write on. Issued in any other order, a contended
    conversion to WAL fails instantly with "database is locked" instead of
    waiting, because busy_timeout wasn't in effect yet when that first lock
    was requested.
    """
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _backup_once(path: Path) -> None:
    """Write a one-time ``.bak`` copy of a legacy JSON store.

    Unlike jobs_store.py's historical ``_backup_once`` (best-effort, swallows
    OSError), failure here PROPAGATES and aborts the migration (D4). This is
    a deliberate divergence: that precedent guarded an in-memory coercion
    that only rewrote the file on the *next* write, so the original stayed
    recoverable even if the backup failed. Here we create a new store the
    user starts writing to immediately, and the JSON becomes the only copy
    of hand-authored tailored resumes — best-effort is the wrong failure
    mode for irreplaceable data.

    Skips silently if the backup already exists (forward-only, idempotent).
    """
    backup_path = path.with_name(path.name + ".bak")
    if backup_path.exists():
        return
    shutil.copyfile(path, backup_path)


def _load_legacy_payload(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Legacy store {path.name} is unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Legacy store {path.name} is corrupt: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"Legacy store {path.name} is corrupt: expected a JSON object at "
            f"the top level, got {type(raw).__name__}"
        )
    return raw


def _topologically_order_versions(
    versions: list[_LegacyResumeVersion],
) -> list[_LegacyResumeVersion]:
    """Order legacy resume versions so every parent is inserted before its child.

    ``resume_versions.parent_id`` is an immediately-enforced FK (``foreign_keys
    = ON``, no ``DEFERRABLE`` clause on the column), so the array order in a
    hand-edited or externally-generated ``resumes.json`` cannot be trusted: a
    child listed before its parent raises ``sqlite3.IntegrityError`` on the
    very first self-consistent record, aborting the transaction. Because
    ``_ensure_schema`` is keyed on schema presence (not file presence), that
    rollback leaves "no jobs table" behind — which is indistinguishable from
    "never migrated", so the identical failure repeats on every subsequent
    call with no way out.

    Raises:
        ValueError: a ``parent_id`` that matches no version in this file, or
                    a lineage cycle — both are malformed source data, and
                    this MUST be detected before a single row is inserted
                    (mirrors the "validate everything before writing" rule
                    the rest of this migration already follows).
    """
    by_id: dict[str, _LegacyResumeVersion] = {}
    duplicate_ids: set[str] = set()
    for v in versions:
        if v.id in by_id:
            duplicate_ids.add(v.id)
        by_id[v.id] = v
    if duplicate_ids:
        raise ValueError(
            f"Legacy resumes store is corrupt: duplicate version id(s): "
            f"{sorted(duplicate_ids)}"
        )

    ordered: list[_LegacyResumeVersion] = []
    resolved: set[str] = set()
    remaining = list(versions)
    while remaining:
        ready = [v for v in remaining if v.parent_id is None or v.parent_id in resolved]
        if not ready:
            stuck = remaining[0]
            if stuck.parent_id not in by_id:
                raise ValueError(
                    f"Legacy resumes store is corrupt: version {stuck.id!r} "
                    f"has parent_id {stuck.parent_id!r}, which does not "
                    f"exist in this file."
                )
            raise ValueError(
                "Legacy resumes store is corrupt: resume version lineage "
                "contains a cycle."
            )
        for v in ready:
            ordered.append(v)
            resolved.add(v.id)
        remaining = [v for v in remaining if v.id not in resolved]
    return ordered


def _migrate_legacy_stores(conn: sqlite3.Connection, db_path: Path) -> None:
    """Import jobs.json/resumes.json into a freshly created schema.

    Called only when the schema does not yet exist (SC-40, SC-42). Both
    files are backed up (if present) BEFORE either is read (SC-41). The
    entire import — schema creation, both imports, user_version stamp — runs
    inside the single write transaction the caller already opened, so a
    failure anywhere rolls back everything (SC-44/SC-45): the database is
    left with no tables, which is exactly the "uninitialized" state that
    triggers a clean retry next time.
    """
    jobs_json = db_path.parent / _JOBS_JSON_NAME
    resumes_json = db_path.parent / _RESUMES_JSON_NAME

    jobs_exists = jobs_json.exists()
    resumes_exists = resumes_json.exists()

    # Backup BEFORE any read (D4) — order matters, not merely "both happen".
    if jobs_exists:
        _backup_once(jobs_json)
    if resumes_exists:
        _backup_once(resumes_json)

    jobs_payload = _load_legacy_payload(jobs_json) if jobs_exists else None
    resumes_payload = _load_legacy_payload(resumes_json) if resumes_exists else None

    # Validate every record BEFORE writing anything, so a malformed record
    # anywhere aborts before a single row is inserted (SC-44).
    legacy_jobs: list[_LegacyJob] = []
    if jobs_payload is not None:
        raw_jobs = jobs_payload.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise ValueError(
                f"Legacy jobs store is corrupt: expected 'jobs' to be a list, "
                f"got {type(raw_jobs).__name__}"
            )
        try:
            legacy_jobs = [_LegacyJob.model_validate(j) for j in raw_jobs]
        except ValidationError as exc:
            raise ValueError(f"Legacy jobs store is corrupt: {exc}") from exc
        seen_urls: set[str] = set()
        duplicate_urls: set[str] = set()
        for job in legacy_jobs:
            if job.url in seen_urls:
                duplicate_urls.add(job.url)
            seen_urls.add(job.url)
        if duplicate_urls:
            raise ValueError(
                f"Legacy jobs store is corrupt: duplicate url(s): "
                f"{sorted(duplicate_urls)}"
            )

    legacy_versions: list[_LegacyResumeVersion] = []
    if resumes_payload is not None:
        raw_versions = resumes_payload.get("versions", [])
        if not isinstance(raw_versions, list):
            raise ValueError(
                f"Legacy resumes store is corrupt: expected 'versions' to be "
                f"a list, got {type(raw_versions).__name__}"
            )
        try:
            legacy_versions = [
                _LegacyResumeVersion.model_validate(v) for v in raw_versions
            ]
        except ValidationError as exc:
            raise ValueError(f"Legacy resumes store is corrupt: {exc}") from exc
        # Topologically order BEFORE writing anything (SC-44's "validate
        # everything first" rule extended to lineage, not just per-record
        # shape) — array order in the source file is untrusted (task 2 fix).
        legacy_versions = _topologically_order_versions(legacy_versions)

    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)

    url_to_job_id: dict[str, str] = {}
    for job in legacy_jobs:
        job_id = uuid.uuid4().hex
        url_to_job_id[job.url] = job_id
        conn.execute(
            "INSERT INTO jobs (id, url, custom_title, title, company, country, "
            "status, score, recommendation, notes, analyzed_at) VALUES "
            "(?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                job_id,
                job.url,
                job.title,
                job.company,
                job.status,
                job.score,
                job.recommendation,
                job.notes,
                _normalize_timestamp(job.analyzed_at),
            ),
        )

    unresolved_count = 0
    for version in legacy_versions:
        job_id = url_to_job_id.get(version.job_url) if version.job_url else None
        # Unresolvable job_url (matches no saved job) is preserved in
        # legacy_job_url, NEVER dropped to job_id=NULL — that would silently
        # reclassify a job-tailored resume as general (the sharpest hazard in
        # this migration; see _general_resume's WHERE clause in resumes.py).
        legacy_job_url = None
        if version.job_url and job_id is None:
            legacy_job_url = version.job_url
            unresolved_count += 1
        conn.execute(
            "INSERT INTO resume_versions (id, label, content, parent_id, "
            "job_id, legacy_job_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version.id,
                version.label,
                version.content,
                version.parent_id,
                job_id,
                legacy_job_url,
                _normalize_timestamp(version.created_at),
            ),
        )

    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # A brand-new install (no legacy files at all) is not a migration —
    # printing "migrated 0 job(s) and 0 resume version(s)..." there would be
    # noise implying an event that never happened.
    if legacy_jobs or legacy_versions:
        print(
            f"runway-mcp: migrated {len(legacy_jobs)} job(s) and "
            f"{len(legacy_versions)} resume version(s) into {db_path}. "
            f"{unresolved_count} resume version(s) had a job_url matching no "
            f"saved job and were preserved as legacy_job_url (excluded from "
            f"the general-resume selection).",
            file=sys.stderr,
        )


def _warn_stale_json_ignored(db_path: Path) -> None:
    """One-time stderr warning: schema exists, legacy JSON present, no .bak.

    That combination means WE never migrated these files (a live database
    already existed when they appeared) — never merge, but silence here is
    the "empty vs broken" mistake: the user would see nothing happen and
    conclude their data vanished (D4).
    """
    if db_path in _stale_json_checked:
        return
    _stale_json_checked.add(db_path)

    jobs_json = db_path.parent / _JOBS_JSON_NAME
    resumes_json = db_path.parent / _RESUMES_JSON_NAME
    stale = [
        p
        for p in (jobs_json, resumes_json)
        if p.exists() and not p.with_name(p.name + ".bak").exists()
    ]
    if stale:
        names = ", ".join(str(p) for p in stale)
        print(
            f"runway-mcp: found legacy JSON file(s) beside an existing "
            f"database and is ignoring them (already migrated or never "
            f"related to this database): {names}",
            file=sys.stderr,
        )


def _validate_schema_version(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > _SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"Database was written by a newer version of runway-mcp "
            f"(user_version {version} > {_SCHEMA_VERSION}). The file is "
            f"fine: upgrade runway-mcp rather than letting it rewrite "
            f"the file."
        )


def _apply_pending_upgrades(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to _SCHEMA_VERSION, once.

    _ensure_schema is keyed on TABLE PRESENCE, so a database created by an
    earlier release takes the fast path forever and would never see a
    statement added to _SCHEMA_STATEMENTS afterwards. This is the one place
    that closes that gap.

    Registered upgrades are additive and idempotent, because the stamp is
    what makes them run once and the stamp is best-effort by nature: a
    version with no entry here is assumed to need nothing, which is true for
    1-3 (no schema shipped between them differs from 4 except this index).

    Most steps get that idempotency from SQL itself (IF NOT EXISTS, or a
    DROP paired with a CREATE). ADD COLUMN cannot — SQLite has no
    ``IF NOT EXISTS`` for it — so a step may also be a callable that
    inspects the database first; see ``_add_status_notes_column``. This
    matters beyond theory: a test that rewinds only the stamp on an
    already-current database would otherwise brick it on reopen.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock: two processes can both pass the
        # unlocked check above on the same first run, exactly like
        # _ensure_schema's doubled table-presence check.
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < _SCHEMA_VERSION:
            for target in range(current + 1, _SCHEMA_VERSION + 1):
                for step in _UPGRADES.get(target, ()):
                    if callable(step):
                        step(conn)
                    else:
                        conn.execute(step)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _ensure_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    """Create the schema (migrating legacy JSON if present) or validate it.

    Keyed on schema presence, not file presence (D4): the next call after a
    rolled-back partial failure sees "no jobs table" and retries cleanly
    rather than needing manual cleanup.

    The presence check below is deliberately performed TWICE: once outside
    any lock (cheap, the common case after the first run), and again right
    after acquiring the write lock. Two threads or two processes racing on a
    genuinely first run can both see "no jobs table" on the first
    (unlocked) check before either has started its transaction. Without the
    second check, the loser would blindly re-run ``CREATE TABLE`` after the
    winner already committed it and crash on "table jobs already exists" —
    which, being a raw sqlite3.Error, would escape the "this tool NEVER
    raises" boundary. Re-checking under ``BEGIN IMMEDIATE`` costs nothing
    (same connection already holds the lock) and turns the race into the
    loser simply recognizing the winner already did the work.
    """
    if _table_exists(conn, "jobs"):
        _validate_schema_version(conn)
        _apply_pending_upgrades(conn)
        _warn_stale_json_ignored(db_path)
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(conn, "jobs"):
            # Lost the race: another connection created the schema while we
            # were waiting for the write lock. Nothing to do — commit the
            # (empty) transaction and fall through to the same validation
            # the fast path above performs.
            conn.execute("COMMIT")
            _validate_schema_version(conn)
            _apply_pending_upgrades(conn)
            _warn_stale_json_ignored(db_path)
            return
        _migrate_legacy_stores(conn, db_path)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@contextmanager
def connect(
    path: Path | None = None, *, write: bool = False
) -> Iterator[sqlite3.Connection]:
    """Open a connection, ensure the schema exists, and optionally start a
    write transaction.

    Args:
        path:  Database file path. If None, uses the module-level _DB_PATH
               (resolved at call time so tests can monkeypatch it).
        write: When True, wraps the yielded block in ``BEGIN IMMEDIATE`` ...
               ``COMMIT`` (rolled back on any exception) — the direct
               replacement for tools._storage.store_lock's whole-cycle lock,
               now enforced by the database rather than a filesystem lock
               file (D3).

    Yields:
        A configured sqlite3.Connection (row_factory=sqlite3.Row).

    Raises:
        ValueError: on a corrupt or unreadable legacy JSON store during
                    migration, a malformed legacy record (SC-44), or ANY
                    other sqlite3.Error raised while opening the connection,
                    checking/creating the schema, or acquiring the write
                    lock (finding 1) — e.g. a corrupt database file or a
                    lock that outlasted busy_timeout.
        SchemaTooNewError: if the database was written by a newer schema
                    version than this build understands (SC-48).
        sqlite3.IntegrityError: from a caller's own statement inside the
                    ``with`` block (e.g. an append-only trigger firing, or a
                    foreign key violation) — callers translate these into
                    error envelopes at the tool boundary. NOT translated by
                    this function, since that phase is the caller's own SQL,
                    not this module's connection-setup machinery.
    """
    resolved = path if path is not None else _DB_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), isolation_level=None, check_same_thread=True)
    try:
        # Setup phase: PRAGMAs, schema check/migration, acquiring the write
        # lock. A raw sqlite3.Error here (a corrupt file failing its first
        # PRAGMA, a lock that outlasted busy_timeout, a migration integrity
        # failure) is translated to ValueError so it reaches the tool
        # boundary as an error envelope instead of an unhandled crash
        # (finding 1). SchemaTooNewError is a plain ValueError raised
        # directly by _ensure_schema, not a sqlite3.Error, so it is
        # unaffected and stays distinguishable from ordinary corruption.
        try:
            _configure_connection(conn)
            _ensure_schema(conn, resolved)
            if write:
                conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc
        # Caller-code phase (the yielded ``with`` block): deliberately left
        # UNTRANSLATED. Tool modules catch specific sqlite3 exceptions here
        # themselves (e.g. IntegrityError for a duplicate url) before they
        # would reach this frame, and tests exercise raw SQL against this
        # connection directly, asserting on the real sqlite3 exception types
        # (e.g. an append-only trigger firing). Only the connection-setup
        # machinery above is this module's own responsibility to translate.
        try:
            yield conn
        except Exception:
            if write:
                conn.execute("ROLLBACK")
            raise
        else:
            if write:
                conn.execute("COMMIT")
    finally:
        conn.close()
