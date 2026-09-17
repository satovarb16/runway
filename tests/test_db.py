"""Tests for tools/_db.py — connection, PRAGMAs, schema DDL (design D1-D3).

These tests exist specifically to prove the four PRAGMAs the design calls
out as "fail silently if you get them wrong": foreign_keys, recursive_triggers,
busy_timeout, and BEGIN IMMEDIATE (vs DEFERRED). Each has its own test rather
than being asserted as a group, because each is an independent one-line
omission that silently removes a guarantee.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# 2.1a: fresh connection has the four PRAGMAs set correctly
# ---------------------------------------------------------------------------


def test_fresh_connection_has_foreign_keys_on(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fresh_connection_has_recursive_triggers_on(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


def test_fresh_connection_has_busy_timeout_10000(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_fresh_connection_has_wal_journal_mode(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# 2.1b: schema DDL creates the expected tables/indices
# ---------------------------------------------------------------------------


def test_schema_creates_all_four_tables(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}
    assert {
        "jobs",
        "job_descriptions",
        "resume_versions",
        "work_authorizations",
    } <= names


def test_schema_sets_current_user_version(db_path):
    from tools._db import _SCHEMA_VERSION, connect

    with connect(db_path):
        pass
    # separate connection to prove it persisted
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2.1c: append-only triggers — direct UPDATE/DELETE raise; INSERT OR REPLACE too
# ---------------------------------------------------------------------------


def _insert_base_job_and_resume(conn):
    conn.execute(
        "INSERT INTO jobs (id, url, custom_title, title, company, country, status, "
        "score, recommendation, notes, analyzed_at) VALUES "
        "('J1', 'https://ex.com/1', NULL, 'SWE', 'Acme', 'USA', 'not_applied', "
        "NULL, NULL, NULL, '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO resume_versions (id, label, content, parent_id, job_id, "
        "legacy_job_url, created_at) VALUES "
        "('V1', 'Base', 'text', NULL, NULL, NULL, '2025-01-01T00:00:00+00:00')"
    )


def test_direct_update_on_resume_versions_raises(db_path):
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_base_job_and_resume(conn)

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE resume_versions SET content='hacked' WHERE id='V1'")


def test_direct_delete_on_resume_versions_raises(db_path):
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_base_job_and_resume(conn)

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM resume_versions WHERE id='V1'")


def test_insert_or_replace_on_resume_versions_also_raises(db_path):
    """Proves recursive_triggers=ON closed the bypass: without it, INSERT OR
    REPLACE deletes the conflicting row without firing the DELETE trigger."""
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_base_job_and_resume(conn)

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO resume_versions (id, label, content, "
                "parent_id, job_id, legacy_job_url, created_at) VALUES "
                "('V1', 'Hacked', 'hacked', NULL, NULL, NULL, "
                "'2025-01-01T00:00:00+00:00')"
            )


# ---------------------------------------------------------------------------
# 2.1e: two overlapping writers serialize under BEGIN IMMEDIATE + busy_timeout
# ---------------------------------------------------------------------------


def test_overlapping_writers_serialize_under_begin_immediate(db_path):
    from tools._db import connect

    # Prime the schema/file before spawning threads to avoid a migration race.
    with connect(db_path):
        pass

    started = threading.Event()
    finished_at: dict[str, float] = {}

    def writer_a():
        with connect(db_path, write=True) as conn:
            started.set()
            time.sleep(0.4)
            conn.execute(
                "INSERT INTO jobs (id, url, custom_title, title, company, "
                "country, status, score, recommendation, notes, analyzed_at) "
                "VALUES ('A', 'https://ex.com/a', NULL, 'T', 'C', NULL, "
                "'not_applied', NULL, NULL, NULL, '2025-01-01T00:00:00+00:00')"
            )
        finished_at["a"] = time.monotonic()

    def writer_b():
        started.wait(timeout=5)
        begin = time.monotonic()
        with connect(db_path, write=True) as conn:
            conn.execute(
                "INSERT INTO jobs (id, url, custom_title, title, company, "
                "country, status, score, recommendation, notes, analyzed_at) "
                "VALUES ('B', 'https://ex.com/b', NULL, 'T', 'C', NULL, "
                "'not_applied', NULL, NULL, NULL, '2025-01-01T00:00:00+00:00')"
            )
        finished_at["b_wait"] = time.monotonic() - begin

    ta = threading.Thread(target=writer_a)
    tb = threading.Thread(target=writer_b)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    # writer_b must have been blocked waiting for writer_a's lock (serialized),
    # not failed instantly or run concurrently.
    assert finished_at["b_wait"] >= 0.3

    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# 2.1g/h: jd_text is structurally absent from `jobs` — lives in job_descriptions
# ---------------------------------------------------------------------------


def test_jobs_table_has_no_jd_text_column(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "jd_text" not in cols


def test_job_descriptions_table_has_jd_text_column(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(job_descriptions)").fetchall()
        }
    assert "jd_text" in cols


# ---------------------------------------------------------------------------
# Foreign key enforcement — the whole point of PRAGMA foreign_keys=ON
# ---------------------------------------------------------------------------


def test_foreign_key_actually_rejects_a_bad_job_id(db_path):
    """Proves the FK does something, not merely that it's declared.

    Without PRAGMA foreign_keys=ON this INSERT would succeed silently.
    """
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO resume_versions (id, label, content, parent_id, "
                "job_id, legacy_job_url, created_at) VALUES "
                "('V1', 'Base', 'text', NULL, 'no-such-job', NULL, "
                "'2025-01-01T00:00:00+00:00')"
            )


# ---------------------------------------------------------------------------
# Finding 1 (PR2 apply-fix review): sqlite3 errors during connection setup
# (corrupt file, migration failure) must translate to ValueError at the
# _db boundary, never escape as a raw sqlite3.Error.
# ---------------------------------------------------------------------------


def test_corrupt_db_file_raises_valueerror_not_raw_sqlite_error(db_path):
    """A file that isn't a valid SQLite database at all (e.g. truncated,
    overwritten, or never a database) must surface as ValueError — a raw
    sqlite3.DatabaseError would crash straight through every tool's
    'this tool NEVER raises' boundary, since DatabaseError is not a
    ValueError subclass."""
    from tools._db import connect

    db_path.write_bytes(b"not a database")

    with pytest.raises(ValueError):
        with connect(db_path):
            pass


def test_list_jobs_with_corrupt_db_returns_error_envelope_not_crash(db_path):
    """End-to-end: the tool boundary itself must not crash on a corrupt file."""
    from tools.jobs_store import list_jobs

    db_path.write_bytes(b"not a database")

    result = list_jobs()

    assert result.success is False
    assert result.error_message is not None


# ---------------------------------------------------------------------------
# Finding 3 (PR2 apply-fix review): TOCTOU in _ensure_schema — the table
# presence check happens outside BEGIN IMMEDIATE, so two racing first
# connections can both see "no jobs table" and the loser crashes trying to
# re-run CREATE TABLE.
# ---------------------------------------------------------------------------


def test_concurrent_first_connections_do_not_race_table_creation(db_path, monkeypatch):
    """Two threads racing tools._db.connect() on a genuinely fresh (no jobs
    table) file must not crash: the loser must recognize the winner already
    created the schema, not blindly re-run CREATE TABLE.

    WAL mode is primed up front, deliberately WITHOUT creating the schema
    (via a direct _configure_connection call, not a full connect()). This
    isolates the race under test to _ensure_schema's table-presence check
    (finding 3). Racing the WAL-mode conversion PRAGMA itself is a SEPARATE,
    unrelated SQLite limitation — verified directly against this
    environment's SQLite build (3.45.3): PRAGMA journal_mode=WAL does not
    retry via busy_timeout at all when contended by another connection's
    BEGIN IMMEDIATE, regardless of PRAGMA order, so conflating the two
    races would make this test flake for a reason finding 3 does not
    claim to fix.

    _table_exists is monkeypatched to make the race DETERMINISTIC: both
    threads are forced to rendezvous at their first (unlocked, pre-
    BEGIN-IMMEDIATE) presence check before either proceeds, guaranteeing
    both observe "no jobs table" — rather than relying on OS thread
    scheduling luck, which was observed to reproduce the crash only ~50% of
    runs.
    """
    import tools._db as db_mod
    from tools._db import _configure_connection, connect

    priming_conn = sqlite3.connect(str(db_path), isolation_level=None)
    _configure_connection(priming_conn)
    priming_conn.close()

    real_table_exists = db_mod._table_exists
    rendezvous = threading.Barrier(2, timeout=5)
    call_counts: dict[int, int] = {}
    call_counts_lock = threading.Lock()

    def synchronized_table_exists(conn, name):
        tid = threading.get_ident()
        with call_counts_lock:
            call_counts[tid] = call_counts.get(tid, 0) + 1
            first_call = call_counts[tid] == 1
        if first_call:
            rendezvous.wait()
        return real_table_exists(conn, name)

    monkeypatch.setattr(db_mod, "_table_exists", synchronized_table_exists)

    errors: list[BaseException] = []

    def worker():
        try:
            with connect(db_path):
                pass
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Restore the real _table_exists before the final verification call
    # below — otherwise it becomes a third rendezvous party with no partner
    # and hangs until the barrier's own timeout.
    monkeypatch.setattr(db_mod, "_table_exists", real_table_exists)

    assert errors == []

    with connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Finding 6 (PR2 apply-fix review): busy_timeout must be the FIRST PRAGMA
# issued, or the very next PRAGMA (journal_mode=WAL) fails instantly against
# a locked, still-default-journal-mode file instead of waiting.
# ---------------------------------------------------------------------------


def test_busy_timeout_is_the_first_pragma_configure_connection_issues(db_path):
    """Finding 6: busy_timeout must be the very first statement
    _configure_connection executes on a fresh connection.

    This asserts the CALL ORDER directly (a whitebox/structural check)
    rather than proving a timing difference. A timing-based proof was
    attempted and abandoned: verified directly against this environment's
    SQLite build (3.45.3) that PRAGMA journal_mode=WAL does not honor
    busy_timeout AT ALL when contended by another connection's BEGIN
    IMMEDIATE on a not-yet-WAL file — it fails in <1ms regardless of
    whether busy_timeout was set moments before it, via PRAGMA, or even via
    sqlite3.connect(timeout=...) at connection-open time. Reordering is
    still correct per SQLite's own documented guidance (busy_timeout should
    be set immediately after opening a connection) and is the only fix that
    matters for every OTHER statement this connection issues afterward
    (foreign_keys/recursive_triggers never need a lock; but a later
    ``BEGIN IMMEDIATE`` genuinely does, and depends on busy_timeout already
    being set), so the order is still enforced and tested here structurally.
    """
    executed: list[str] = []

    class _RecordingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            executed.append(sql.strip())
            return super().execute(sql, *args, **kwargs)

    from tools._db import _configure_connection

    conn = sqlite3.connect(
        str(db_path), isolation_level=None, factory=_RecordingConnection
    )
    try:
        _configure_connection(conn)
    finally:
        conn.close()

    assert executed, "expected _configure_connection to issue statements"
    assert "busy_timeout" in executed[0].lower(), (
        f"expected busy_timeout to be the FIRST statement, got: {executed[0]!r} "
        f"(full order: {executed})"
    )


# ---------------------------------------------------------------------------
# Pending upgrades: a database created before a schema statement shipped
# ---------------------------------------------------------------------------


def test_pending_upgrade_adds_missing_index_to_an_older_database(db_path):
    """An existing install must GET a statement added to the schema later.

    _ensure_schema takes the table-presence fast path forever once `jobs`
    exists, so without _apply_pending_upgrades a database created by an
    earlier release would never see the index — the exact gap that made
    every resume read fall back to SCAN + TEMP B-TREE.
    """
    import sqlite3

    from tools._db import _SCHEMA_VERSION, connect

    with connect(db_path):
        pass

    # Rewind to a pre-index database: version 3, the last one shipped before
    # the index became part of the schema. Pinned deliberately rather than
    # written as _SCHEMA_VERSION - 1, which silently stopped exercising this
    # upgrade the moment a version landed on top of it.
    raw = sqlite3.connect(str(db_path))
    raw.execute("DROP INDEX idx_resume_versions_created_at")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    with connect(db_path) as conn:
        names = {
            r["name"] for r in conn.execute("PRAGMA index_list('resume_versions')")
        }
        assert "idx_resume_versions_created_at" in names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION


def test_latest_resume_query_uses_the_created_at_index(db_path):
    """The index exists to take SCAN + TEMP B-TREE off every resume read.

    Asserting on the query plan, not on timing: this is what regresses
    silently if the index is dropped or the ORDER BY direction drifts.
    """
    from tools._db import connect

    with connect(db_path) as conn:
        plan = " ".join(
            r["detail"]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM resume_versions "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )

    assert "idx_resume_versions_created_at" in plan
    assert "TEMP B-TREE" not in plan.upper()


# ---------------------------------------------------------------------------
# 2.1d: the append-only UPDATE trigger protects CONTENT, not the job pointer
#
# delete_job has to sever resume_versions.job_id when the job it points at is
# removed. Severing a pointer to a row that no longer exists is not rewriting
# history, so the trigger names the columns it guards instead of blanket-
# banning UPDATE. Everything that carries the version's identity or content
# is still absolutely immutable.
# ---------------------------------------------------------------------------


def _insert_linked_resume(conn):
    conn.execute(
        "INSERT INTO jobs (id, url, custom_title, title, company, country, status, "
        "score, recommendation, notes, analyzed_at) VALUES "
        "('J9', 'https://ex.com/9', NULL, 'SWE', 'Acme', 'USA', 'not_applied', "
        "NULL, NULL, NULL, '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO resume_versions (id, label, content, parent_id, job_id, "
        "legacy_job_url, created_at) VALUES "
        "('V9', 'Tailored', 'text', NULL, 'J9', NULL, "
        "'2025-01-01T00:00:00+00:00')"
    )


def test_job_id_may_be_unlinked_on_resume_versions(db_path):
    """The one UPDATE delete_job needs. Content is untouched."""
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_linked_resume(conn)

    with connect(db_path, write=True) as conn:
        conn.execute("UPDATE resume_versions SET job_id=NULL WHERE id='V9'")

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT job_id, content, label FROM resume_versions WHERE id='V9'"
        ).fetchone()
        assert row["job_id"] is None
        assert row["content"] == "text"
        assert row["label"] == "Tailored"


def test_update_touching_content_alongside_job_id_still_raises(db_path):
    """The mixed case: naming a guarded column anywhere in SET aborts."""
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_linked_resume(conn)

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE resume_versions SET job_id=NULL, content='hacked' WHERE id='V9'"
            )


@pytest.mark.parametrize(
    "assignment",
    [
        "label='hacked'",
        "parent_id='V1'",
        "created_at='2030-01-01T00:00:00+00:00'",
        "id='V99'",
        "legacy_job_url='https://hacked'",
    ],
)
def test_every_other_column_on_resume_versions_is_still_immutable(db_path, assignment):
    from tools._db import connect

    with connect(db_path, write=True) as conn:
        _insert_linked_resume(conn)

    with connect(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE resume_versions SET {assignment} WHERE id='V9'")


def test_pending_upgrade_adds_status_notes_and_relaxes_the_trigger(db_path):
    """A v4 database must gain the column AND the reworded trigger.

    Rewinds a current database to the shape the previous release shipped,
    then reopens it the way any real install would.
    """
    import sqlite3 as sq

    from tools._db import _SCHEMA_VERSION, connect

    with connect(db_path):
        pass

    raw = sq.connect(str(db_path))
    raw.execute("ALTER TABLE jobs DROP COLUMN status_notes")
    raw.execute("DROP TRIGGER resume_versions_no_update")
    raw.execute(
        "CREATE TRIGGER resume_versions_no_update BEFORE UPDATE ON resume_versions "
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
    )
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    with connect(db_path, write=True) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info('jobs')")}
        assert "status_notes" in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        _insert_linked_resume(conn)
        conn.execute("UPDATE resume_versions SET job_id=NULL WHERE id='V9'")


def test_pending_upgrade_to_v5_is_safe_to_reapply(db_path):
    """The ADD COLUMN is guarded, so a stamp that never landed cannot brick
    the database on the next open — the promise _UPGRADES' docstring makes."""
    import sqlite3 as sq

    from tools._db import _SCHEMA_VERSION, connect

    with connect(db_path):
        pass

    # Column and trigger already current; only the stamp is rewound.
    raw = sq.connect(str(db_path))
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    with connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
