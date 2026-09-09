"""Resume version store: save_resume_version, get_resume_version, list_resume_versions.

The conversation-side Claude drafts and tailors resume text. This module
PERSISTS and RETRIEVES that raw text — it never parses, structures, scores,
or judges resume content, and it never calls back to the model. This is the
project's third persistence shape: unlike jobs_store.py's upsert-by-key,
resume versions must COEXIST, so this store is append-only with parent_id
lineage.

SQLite-backed (design D1/D2/D3/D6/D8). Append-only is now enforced by the
database, not merely by application discipline: `resume_versions` has
BEFORE UPDATE/DELETE triggers (see tools/_db.py's schema, and test_db.py's
direct-SQL proof that INSERT OR REPLACE is also blocked via
recursive_triggers=ON). `job_id` is a real FOREIGN KEY against jobs.id,
replacing the prior job_url string convention that nothing server-side ever
validated — an unknown job_id is rejected with error="job_not_found", never
a raw sqlite3.IntegrityError and never a generic "invalid_input" (design D8:
SQL owns the referential-integrity invariant, the tool boundary translates
its failure into an actionable message).

`legacy_job_url` is a migration-only column (see tools/_db.py's
_migrate_legacy_stores): never written by save_resume_version, it holds a
pre-0.3.0 job_url string that matched no job at migration time, so that
orphan is excluded from the general-resume selection (SC-46) rather than
being silently promoted to "general" by falling through to job_id=NULL.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, ValidationError

from tools._db import connect

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ResumeVersion(BaseModel):
    """A single saved resume version. Content is raw text, stored verbatim."""

    model_config = ConfigDict(extra="forbid")

    id: str  # uuid4().hex, server-generated
    label: str
    content: str  # raw text — sole source of truth, no format/length validation
    # No default: parent_id=None carries the special meaning "this is the
    # base/root" version, so every construction site must decide explicitly.
    parent_id: str | None
    job_id: str | None = None  # FK -> jobs.id; replaces the prior job_url string
    legacy_job_url: str | None = None  # migration-only, never written here
    created_at: str  # ISO-8601 UTC, server-stamped


class SaveResumeVersionResult(BaseModel):
    """Return value for save_resume_version."""

    success: bool
    id: str | None = None
    label: str | None = None
    parent_id: str | None = None
    job_id: str | None = None
    error: str | None = (
        # "invalid_parent" | "parent_not_found" | "job_not_found"
        # | "invalid_input" | "write_error" | "corrupt"
        None
    )
    message: str | None = None


class GetResumeVersionResult(BaseModel):
    """Return value for get_resume_version."""

    success: bool
    version: ResumeVersion | None = None
    error: str | None = None  # "not_found" | "corrupt"
    message: str | None = None


class ResumeVersionSummary(BaseModel):
    """A resume version WITHOUT its content, for listing."""

    id: str
    label: str
    parent_id: str | None
    job_id: str | None
    created_at: str


class ListResumeVersionsResult(BaseModel):
    """Return value for list_resume_versions."""

    success: bool
    versions: list[ResumeVersionSummary] = []
    count: int = 0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_version(row: sqlite3.Row) -> ResumeVersion:
    return ResumeVersion.model_validate(dict(row))


def _general_resume(
    job_id: str | None = None, conn: sqlite3.Connection | None = None
) -> ResumeVersion | None:
    """Select the GENERAL (non-job-tailored) resume for analyze_job (design D6).

    "General" means job_id IS NULL AND legacy_job_url IS NULL — the second
    condition is what keeps a migration-orphaned resume (job_url matched no
    saved job at migration time) from being silently promoted to the
    scoring baseline for every future job (SC-46). Among general candidates,
    the most recently created one wins: a user who refreshes their general
    resume saves it as a new version, and returning the root instead would
    silently ignore every update.

    Fallback, reachable only if no version has job_id IS NULL AND
    legacy_job_url IS NULL — every version, including the first, was saved
    against some job: return the most recently created root, excluding one
    tailored for `job_id` specifically (never hand back a resume already
    written for the very job being analyzed — that would score it against
    itself). The fallback ALSO requires legacy_job_url IS NULL: a lone
    migration orphan (job_id IS NULL, legacy_job_url set) is a root too, and
    without this clause it would be the fallback's answer — silently
    becoming the general/scoring-baseline resume the migration's own stderr
    message says it was excluded from.

    Args:
        job_id: The job id about to be analyzed, so the fallback branch can
                refuse a resume tailored to it. None disables that exclusion.
        conn:   An already-open connection to read on. None opens one for
                the duration of this call.

    Returns:
        The selected ResumeVersion, or None when nothing usable exists.
    """
    with nullcontext(conn) if conn is not None else connect() as c:
        row = c.execute(
            "SELECT * FROM resume_versions WHERE job_id IS NULL AND "
            "legacy_job_url IS NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return _row_to_version(row)

        # One query for both branches: with job_id NULL the first disjunct
        # is true for every row, which is exactly the unfiltered fallback.
        root_row = c.execute(
            "SELECT * FROM resume_versions WHERE parent_id IS NULL AND "
            "legacy_job_url IS NULL AND (? IS NULL OR job_id IS NULL OR job_id != ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (job_id, job_id),
        ).fetchone()
        return _row_to_version(root_row) if root_row is not None else None


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def save_resume_version(
    content: str,
    label: str,
    parent_id: str | None = None,
    job_id: str | None = None,
) -> SaveResumeVersionResult:
    """Append a new resume version to the store. Never mutates or deletes.

    The store enforces a single-root tree: the first saved version's
    parent_id MUST be None; every subsequent version's parent_id MUST
    reference an existing version id. `job_id`, when given, MUST reference
    an existing job — call save_job_analysis first and pass the id it
    returns (the FK is real now, D1/D8). Content is stored raw and verbatim.
    This tool NEVER raises.

    Args:
        content:   Raw resume text, stored verbatim.
        label:     Caller-supplied human-readable label.
        parent_id: id of the version this one derives from. None only on the
                   very first save (establishes the base).
        job_id:    Optional job id this version was tailored for.

    Returns:
        SaveResumeVersionResult with success=True, id, label, parent_id,
        job_id on success; success=False with error/message on failure
        ("invalid_parent", "parent_not_found", "job_not_found",
        "invalid_input", "corrupt", "write_error").
    """
    try:
        with connect(write=True) as conn:
            # Emptiness test, never a real count: COUNT(*) walks the whole
            # covering index to answer a question the first row settles.
            store_is_empty = (
                conn.execute("SELECT 1 FROM resume_versions LIMIT 1").fetchone() is None
            )

            if store_is_empty:
                if parent_id is not None:
                    return SaveResumeVersionResult(
                        success=False,
                        error="invalid_parent",
                        message=(
                            "The store is empty; the first saved version must "
                            "have parent_id=None."
                        ),
                    )
            else:
                if parent_id is None:
                    return SaveResumeVersionResult(
                        success=False,
                        error="invalid_parent",
                        message=(
                            "A base version already exists; parent_id is "
                            "required for subsequent versions."
                        ),
                    )
                parent_row = conn.execute(
                    "SELECT id FROM resume_versions WHERE id = ?", (parent_id,)
                ).fetchone()
                if parent_row is None:
                    return SaveResumeVersionResult(
                        success=False,
                        error="parent_not_found",
                        message=f"No resume version exists with id {parent_id!r}.",
                    )

            if job_id is not None:
                job_row = conn.execute(
                    "SELECT id FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job_row is None:
                    return SaveResumeVersionResult(
                        success=False,
                        error="job_not_found",
                        message=(
                            f"No job exists with id {job_id!r}. Call "
                            f"save_job_analysis first and pass the id it "
                            f"returns as job_id."
                        ),
                    )

            try:
                record = ResumeVersion(
                    id=uuid.uuid4().hex,
                    label=label,
                    content=content,
                    parent_id=parent_id,
                    job_id=job_id,
                    legacy_job_url=None,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            except ValidationError as exc:
                return SaveResumeVersionResult(
                    success=False, error="invalid_input", message=str(exc)
                )

            try:
                conn.execute(
                    "INSERT INTO resume_versions (id, label, content, parent_id, "
                    "job_id, legacy_job_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.label,
                        record.content,
                        record.parent_id,
                        record.job_id,
                        record.legacy_job_url,
                        record.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                return SaveResumeVersionResult(
                    success=False, error="write_error", message=str(exc)
                )

            return SaveResumeVersionResult(
                success=True,
                id=record.id,
                label=record.label,
                parent_id=record.parent_id,
                job_id=record.job_id,
            )
    except ValueError as exc:
        return SaveResumeVersionResult(success=False, error="corrupt", message=str(exc))
    except sqlite3.Error as exc:
        # The IntegrityError clause inside the block catches the expected
        # write failures; this catches the rest (a full disk, an append-only
        # trigger firing from an unexpected path) so none of them escape as
        # a raw traceback past "This tool NEVER raises".
        return SaveResumeVersionResult(
            success=False, error="write_error", message=str(exc)
        )


def get_resume_version(id: str) -> GetResumeVersionResult:
    """Retrieve a single resume version by id, or the most recent via "latest".

    This tool NEVER raises.

    Args:
        id: Exact version id, or the literal string "latest" for the most
            recently created version in the store.

    Returns:
        GetResumeVersionResult with success=True and version on success;
        success=False with error="not_found" or error="corrupt".
    """
    try:
        with connect() as conn:
            if id == "latest":
                row = conn.execute(
                    "SELECT * FROM resume_versions ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return GetResumeVersionResult(
                        success=False,
                        error="not_found",
                        message="No resume versions have been saved yet.",
                    )
                return GetResumeVersionResult(
                    success=True, version=_row_to_version(row)
                )

            row = conn.execute(
                "SELECT * FROM resume_versions WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return GetResumeVersionResult(
                    success=False,
                    error="not_found",
                    message=f"No resume version exists with id {id!r}.",
                )
            return GetResumeVersionResult(success=True, version=_row_to_version(row))
    except (ValueError, sqlite3.Error) as exc:
        return GetResumeVersionResult(success=False, error="corrupt", message=str(exc))


def list_resume_versions(
    job_id: str | None = None,
    limit: int | None = None,
) -> ListResumeVersionsResult:
    """Return stored resume versions, newest-first, with optional filtering.

    Pipeline order: FILTER -> SORT -> LIMIT. This tool NEVER raises.

    Args:
        job_id: When provided, only versions linked to this exact job id are
                returned.
        limit:  Maximum number of records to return (after sort).

    Returns:
        ListResumeVersionsResult with success=True and filtered/sorted
        SUMMARIES (no resume text); success=False with error_message.
    """
    if limit is not None and limit <= 0:
        return ListResumeVersionsResult(
            success=False, error_message="limit must be a positive integer"
        )

    # Summary columns only, never SELECT * — the same finding get_job's
    # linked-version query already fixes. This builds ResumeVersionSummary,
    # which has no `content` field, so SELECT * would read every listed
    # version's full resume text off disk purely to discard it.
    sql = "SELECT id, label, parent_id, job_id, created_at FROM resume_versions"
    params: list[object] = []
    if job_id is not None:
        sql += " WHERE job_id = ?"
        params.append(job_id)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    try:
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except (ValueError, sqlite3.Error) as exc:
        return ListResumeVersionsResult(success=False, error_message=str(exc))

    summaries = [
        ResumeVersionSummary(
            id=r["id"],
            label=r["label"],
            parent_id=r["parent_id"],
            job_id=r["job_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return ListResumeVersionsResult(
        success=True, versions=summaries, count=len(summaries)
    )
