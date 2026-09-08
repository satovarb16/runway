"""Migration tests (design D4, spec R19-R23, SC-40..SC-48).

Tested end-to-end with realistic JSON fixtures written to disk, not mocks —
this is the part that touches real user data. The riskiest single test in
this release is test_orphan_job_url_migrates_to_legacy_job_url_not_general,
which proves a resume version whose job_url matches no saved job does NOT
get silently promoted to the "general" resume (the scoring baseline for
every future job) by the migration itself.
"""

from __future__ import annotations

import json

import pytest


def _write_jobs_json(tmp_path, jobs: list[dict]) -> None:
    (tmp_path / "jobs.json").write_text(
        json.dumps({"schema_version": 2, "jobs": jobs}), encoding="utf-8"
    )


def _write_resumes_json(tmp_path, versions: list[dict]) -> None:
    (tmp_path / "resumes.json").write_text(
        json.dumps({"schema_version": 1, "versions": versions}), encoding="utf-8"
    )


def _job(**overrides) -> dict:
    base = {
        "url": "https://example.com/job/1",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "visa_verdict": "GREEN",
        "analyzed_at": "2025-06-01T00:00:00+00:00",
        "status": "not_applied",
        "score": None,
        "recommendation": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def _version(**overrides) -> dict:
    base = {
        "id": "v1",
        "label": "Base",
        "content": "resume text",
        "parent_id": None,
        "job_url": None,
        "created_at": "2025-06-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# SC-40: first read after upgrade migrates both legacy files
# ---------------------------------------------------------------------------


def test_first_read_migrates_both_legacy_files_and_writes_backups(db_path):
    from tools._db import connect

    _write_jobs_json(db_path.parent, [_job()])
    _write_resumes_json(db_path.parent, [_version()])

    with connect(db_path) as conn:
        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        versions = conn.execute("SELECT * FROM resume_versions").fetchall()

    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://example.com/job/1"
    assert len(versions) == 1
    assert (db_path.parent / "jobs.json.bak").exists()
    assert (db_path.parent / "resumes.json.bak").exists()


# ---------------------------------------------------------------------------
# SC-41: backup written before any legacy read; backup failure aborts
# ---------------------------------------------------------------------------


def test_backup_failure_aborts_migration_leaves_no_partial_database(
    db_path, monkeypatch
):
    from tools import _db as db_mod

    _write_jobs_json(db_path.parent, [_job()])

    def _boom(path):
        raise OSError("destination unwritable")

    monkeypatch.setattr(db_mod, "_backup_once", _boom)

    with pytest.raises(OSError):
        with db_mod.connect(db_path):
            pass

    # Original JSON untouched, and no tables were created.
    assert json.loads((db_path.parent / "jobs.json").read_text())["jobs"]
    import sqlite3

    raw_conn = sqlite3.connect(str(db_path))
    tables = raw_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    raw_conn.close()
    assert tables == []


# ---------------------------------------------------------------------------
# SC-42: fresh install, no legacy files, no database — normal, not an error
# ---------------------------------------------------------------------------


def test_fresh_install_no_legacy_files_no_error(db_path):
    from tools._db import connect

    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    assert count == 0
    assert not (db_path.parent / "jobs.json.bak").exists()
    assert not (db_path.parent / "resumes.json.bak").exists()


# ---------------------------------------------------------------------------
# SC-43: an already-migrated store ignores legacy JSON entirely
# ---------------------------------------------------------------------------


def test_already_migrated_store_ignores_legacy_json_even_if_still_present(db_path):
    from tools._db import connect

    _write_jobs_json(db_path.parent, [_job(url="https://a.com")])
    with connect(db_path):
        pass  # first read migrates

    # Now change the JSON — a real re-migration would pick this up and
    # duplicate/alter data, which must NOT happen.
    _write_jobs_json(
        db_path.parent, [_job(url="https://a.com"), _job(url="https://b.com")]
    )

    with connect(db_path) as conn:
        jobs = conn.execute("SELECT url FROM jobs").fetchall()

    assert {j["url"] for j in jobs} == {"https://a.com"}


# ---------------------------------------------------------------------------
# SC-44 / SC-45: partial failure is all-or-nothing; retry succeeds after fix
# ---------------------------------------------------------------------------


def test_malformed_record_aborts_whole_migration(db_path):
    from tools._db import connect

    bad_job = _job(score="not-a-number-and-not-coercible")
    _write_jobs_json(db_path.parent, [_job(url="https://good.com"), bad_job])

    with pytest.raises(ValueError):
        with connect(db_path):
            pass

    import sqlite3

    raw_conn = sqlite3.connect(str(db_path))
    tables = raw_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    raw_conn.close()
    assert tables == []
    # .bak was already written (backup happens before validation/read).
    assert (db_path.parent / "jobs.json.bak").exists()


def test_retry_after_fixing_source_data_succeeds_from_untouched_json(db_path):
    from tools._db import connect

    bad_job = _job(url="https://bad.com", score="not-a-number-and-not-coercible")
    _write_jobs_json(db_path.parent, [_job(url="https://good.com"), bad_job])

    with pytest.raises(ValueError):
        with connect(db_path):
            pass

    # Fix the source data (simulating a user/operator correction) and retry.
    _write_jobs_json(
        db_path.parent,
        [_job(url="https://good.com"), _job(url="https://bad.com", score=50)],
    )

    with connect(db_path) as conn:
        jobs = conn.execute("SELECT url, score FROM jobs ORDER BY url").fetchall()

    assert len(jobs) == 2
    assert {j["url"] for j in jobs} == {"https://good.com", "https://bad.com"}


# ---------------------------------------------------------------------------
# SC-46 — THE RISKIEST TEST IN THIS RELEASE
# ---------------------------------------------------------------------------


def test_orphan_job_url_migrates_to_legacy_job_url_not_general(db_path):
    """A resume version whose job_url matches no job's url must migrate with
    legacy_job_url set, NOT job_id=NULL — and the orphan must NOT become the
    general-resume selection's answer.

    The fixture's orphan is deliberately the MOST RECENT version among all
    migrated versions. If it were not, this test would pass under a BROKEN
    implementation too (job_id=NULL for the orphan): the general-resume
    selection picks the newest job_id IS NULL row, and an older non-orphan
    "general" row would still win, hiding the bug. Making the orphan the
    newest forces the selection to actually distinguish
    "job_id IS NULL AND legacy_job_url IS NULL" from merely "job_id IS NULL".
    """
    from tools._db import connect

    _write_jobs_json(db_path.parent, [_job(url="https://saved.com")])
    _write_resumes_json(
        db_path.parent,
        [
            _version(
                id="general-old",
                label="General (old)",
                job_url=None,
                created_at="2025-01-01T00:00:00+00:00",
            ),
            _version(
                id="orphan-newest",
                label="Tailored for a job never saved",
                parent_id="general-old",
                job_url="https://gone.com/never-saved",
                created_at="2025-12-01T00:00:00+00:00",  # MOST RECENT
            ),
        ],
    )

    with connect(db_path) as conn:
        rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM resume_versions").fetchall()
        }

        assert rows["orphan-newest"]["job_id"] is None
        assert rows["orphan-newest"]["legacy_job_url"] == "https://gone.com/never-saved"
        assert rows["general-old"]["job_id"] is None
        assert rows["general-old"]["legacy_job_url"] is None

        # The SQLite-native general-resume selection (branch 1 of D4/D6's
        # rule) must exclude the orphan despite it being the newest
        # job_id IS NULL row.
        general = conn.execute(
            "SELECT id FROM resume_versions WHERE job_id IS NULL AND "
            "legacy_job_url IS NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert general["id"] == "general-old"


# ---------------------------------------------------------------------------
# Finding 2 (PR2 apply-fix review): a legacy resumes.json where a child
# version appears BEFORE its parent in the array must still migrate
# correctly. resume_versions.parent_id is an immediately-enforced FK, so a
# naive array-order insert hits IntegrityError on the very first row, rolls
# back, and — because _ensure_schema is keyed on schema presence, not file
# presence — every SUBSEQUENT call repeats the identical crash forever.
# ---------------------------------------------------------------------------


def test_migration_reorders_child_before_parent_in_source_json(db_path):
    from tools._db import connect

    _write_jobs_json(db_path.parent, [_job(url="https://saved.com")])
    _write_resumes_json(
        db_path.parent,
        [
            _version(
                id="child",
                parent_id="parent",  # references a row listed BELOW it
                created_at="2025-02-01T00:00:00+00:00",
            ),
            _version(
                id="parent",
                parent_id=None,
                created_at="2025-01-01T00:00:00+00:00",
            ),
        ],
    )

    # Must succeed on the very first call — no exception at all.
    with connect(db_path) as conn:
        rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM resume_versions").fetchall()
        }

    assert rows["parent"]["parent_id"] is None
    assert rows["child"]["parent_id"] == "parent"

    # And a second call must not repeat any failure — proves there is no
    # "every subsequent call crashes identically" state left behind.
    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM resume_versions").fetchone()[0]
    assert count == 2


def test_migration_rejects_unresolvable_parent_id_before_writing_anything(db_path):
    """A parent_id that matches no version anywhere in the file is malformed
    data, not a mis-ordering — must raise before any row is written, and
    must never silently drop or invent a value for it."""
    from tools._db import connect

    _write_resumes_json(
        db_path.parent,
        [_version(id="orphaned-child", parent_id="does-not-exist")],
    )

    with pytest.raises(ValueError):
        with connect(db_path):
            pass

    import sqlite3

    raw_conn = sqlite3.connect(str(db_path))
    tables = raw_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    raw_conn.close()
    assert tables == []


# ---------------------------------------------------------------------------
# SC-47: a resume version whose job_url DOES match is correctly linked
# ---------------------------------------------------------------------------


def test_resolvable_job_url_links_to_migrated_job_id(db_path):
    from tools._db import connect

    _write_jobs_json(db_path.parent, [_job(url="https://example.com/job/1")])
    _write_resumes_json(
        db_path.parent,
        [_version(id="v1", job_url="https://example.com/job/1")],
    )

    with connect(db_path) as conn:
        job_id = conn.execute("SELECT id FROM jobs").fetchone()["id"]
        version = conn.execute(
            "SELECT job_id, legacy_job_url FROM resume_versions WHERE id='v1'"
        ).fetchone()

    assert version["job_id"] == job_id
    assert version["legacy_job_url"] is None


# ---------------------------------------------------------------------------
# SC-48: a too-new database refuses uniformly, is not rewritten
# ---------------------------------------------------------------------------


def test_too_new_schema_refuses_and_does_not_rewrite_file(db_path):
    import sqlite3

    from tools._db import _SCHEMA_VERSION, SchemaTooNewError, connect

    with connect(db_path):
        pass
    raw = sqlite3.connect(str(db_path))
    raw.execute(f"PRAGMA user_version = {_SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()

    before = db_path.read_bytes()

    with pytest.raises(SchemaTooNewError):
        with connect(db_path):
            pass

    assert db_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Additional coverage: timestamp normalization, profile.json untouched,
# stale JSON beside a live db warns rather than merging
# ---------------------------------------------------------------------------


def test_migration_normalizes_z_suffix_timestamps_to_plus_00_00(db_path):
    from tools._db import connect

    _write_jobs_json(
        db_path.parent, [_job(url="https://z.com", analyzed_at="2025-06-01T12:00:00Z")]
    )

    with connect(db_path) as conn:
        analyzed_at = conn.execute(
            "SELECT analyzed_at FROM jobs WHERE url='https://z.com'"
        ).fetchone()[0]

    assert analyzed_at.endswith("+00:00")


def test_profile_json_untouched_by_migration(db_path):
    from tools._db import connect

    profile_path = db_path.parent / "profile.json"
    profile_path.write_text('{"name": "hands off"}', encoding="utf-8")
    _write_jobs_json(db_path.parent, [_job()])

    with connect(db_path):
        pass

    assert profile_path.read_text(encoding="utf-8") == '{"name": "hands off"}'
    assert not (db_path.parent / "profile.json.bak").exists()


def test_stale_json_beside_live_db_warns_and_is_ignored_not_merged(db_path, capsys):
    from tools._db import connect

    # DB already exists (fresh, no migration).
    with connect(db_path):
        pass

    # Now JSON files show up that were never part of this database's history
    # (no .bak beside them — this db never migrated them).
    _write_jobs_json(db_path.parent, [_job(url="https://late-arrival.com")])

    with connect(db_path) as conn:
        jobs = conn.execute("SELECT url FROM jobs").fetchall()

    assert jobs == []  # never merged
    captured = capsys.readouterr()
    assert "jobs.json" in captured.err
