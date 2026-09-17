"""Job store persistence tools: save_job_analysis, list_jobs, set_application_status.

The conversation-side Claude analyzes a job posting and produces a score and
recommendation. These tools PERSIST and RETRIEVE that structured data — they
never call back to the model. This keeps the server free of MCP sampling and
matches the project's philosophy: tools shape data, Claude reasons.

SQLite-backed (design D1/D3/D8/D9/D10). `jobs.id` is a server-generated
surrogate key — `url` is a nullable, unique ATTRIBUTE, not the identity, so
supplying a URL later never orphans a resume version pointed at the job
(D1's deciding argument). SQL owns structural invariants (url uniqueness via
UNIQUE, referential integrity is resume_versions' concern); pydantic and this
module own value-domain invariants (the 7-value status enum, "at least one
handle" on create) — see design D8, no invariant is expressed in both places.

get_job and list_jobs's `company` filter are PR3a's, not this module's.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, ValidationError

from tools._db import connect, _normalize_timestamp
from tools.resumes import ResumeVersionSummary

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ApplicationStatus(str, Enum):
    """The 7 allowed application status values (binding, spec-defined)."""

    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    GHOSTED = "ghosted"


class StoredJob(BaseModel):
    """A single analyzed job record persisted to the jobs table.

    No `jd_text` field here by construction — it lives in the 1:1
    `job_descriptions` side table (D1), so `SELECT * FROM jobs` (and this
    model) structurally cannot leak it into a list result.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # uuid4().hex, server-generated — the identity, never url/custom_title
    url: str | None = None
    custom_title: str | None = None
    title: str
    company: str
    country: str | None = None  # Claude-extracted free text; NULL for migrated rows
    analyzed_at: str  # ISO-8601, server-stamped by save_job_analysis
    status: ApplicationStatus = ApplicationStatus.NOT_APPLIED
    score: int | None = None
    recommendation: str | None = None
    # Why the analysis and the application timeline are separate columns:
    # one field serving both meant recording "applied on the 17th" silently
    # destroyed the reasoning behind the score. `notes` is written only by
    # save_job_analysis; `status_notes` only by set_application_status, and
    # only ever appended to.
    notes: str | None = None
    status_notes: str | None = None


class SaveJobResult(BaseModel):
    """Return value for save_job_analysis."""

    success: bool
    id: str | None = None
    url: str | None = None
    custom_title: str | None = None
    updated: bool | None = None  # True=upserted existing record, False=new record
    possible_duplicate_id: str | None = None  # advisory only, never blocks (D10)
    error: str | None = (
        # "invalid_input" | "not_found" | "duplicate_url" | "corrupt" | "write_error"
        None
    )
    message: str | None = None


class ListJobsResult(BaseModel):
    """Return value for list_jobs."""

    success: bool
    jobs: list[StoredJob] = []
    count: int = 0
    error_message: str | None = None


class GetJobResult(BaseModel):
    """Return value for get_job (D6)."""

    success: bool
    job: StoredJob | None = None
    description: str | None = None
    has_description: bool = False  # ALWAYS present — the affordance that
    # makes the jd_text opt-in discoverable without dragging it into
    # context by default (include_description=False is the default).
    resume_versions: list[ResumeVersionSummary] = []
    # "not_found" | "invalid_input" | "corrupt" | "ambiguous" (custom_title
    # matched more than one job — see get_job's docstring)
    error: str | None = None
    message: str | None = None


class SetStatusResult(BaseModel):
    """Return value for set_application_status."""

    success: bool
    id: str | None = None
    url: str | None = None
    status: str | None = None
    previous_status: str | None = None
    # The timeline as it stands AFTER this call, so the caller can see what
    # was recorded without a second round trip.
    status_notes: str | None = None
    error: str | None = (
        None  # "not_found" | "corrupt" | "invalid_status" | "invalid_input" | "write_error"
    )
    message: str | None = None


class DeleteJobResult(BaseModel):
    """Return value for delete_job — a receipt for an irreversible act.

    Every field beyond `success` exists so the reply can name exactly what
    disappeared. This tool is the only destructive, id-targeted operation in
    the surface and there is no undo, so the receipt IS the safety mechanism.
    """

    success: bool
    id: str | None = None
    url: str | None = None
    title: str | None = None
    company: str | None = None
    # Versions whose job_id was severed. Their content is untouched — the
    # append-only tree survives its pointer.
    unlinked_resume_versions: list[str] = []
    deleted_description: bool = False
    error: str | None = (
        None  # "invalid_input" | "not_found" | "corrupt" | "write_error"
    )
    message: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_stored_job(row: sqlite3.Row) -> StoredJob:
    return StoredJob.model_validate(dict(row))


def _append_status_note(
    existing: str | None, status: str, note: str | None
) -> str | None:
    """Append one dated line to the application timeline.

    Returns `existing` unchanged when there is no note to add, so a bare
    status change (no `notes` argument) records nothing — the status column
    already carries that fact, and a line saying only the status would be
    noise.

    The date, not a full timestamp: this is a human-readable log of an
    application, and several events on one day are ordinary. `analyzed_at`
    remains the machine-precision field.
    """
    if note is None:
        return existing

    stamped = f"[{datetime.now(timezone.utc).date().isoformat()}] {status} — {note}"
    return stamped if not existing else f"{existing}\n{stamped}"


def _find_job_by_id(conn: sqlite3.Connection, id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()


def _find_job_by_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters so a caller's literal string is
    matched literally, only the surrounding `%%` wildcards added by the
    caller are wildcards.

    Without this, `list_jobs(company="A_B")` would match "AxB" (`_` matches
    any single character) and `list_jobs(company="%")` would match every
    row while still reporting a filtered result — both silent, both wrong.
    The escape character `\\` must itself be escaped first, or a caller's
    literal backslash would be misread as escaping the character after it.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_job_id_by_url(url: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Resolve a job url to its surrogate id via an exact match (url is UNIQUE).

    Used by analyze.py's Step 1 (Guard 1 resolution, PR2 task 2.5l): the
    incoming raw `url` is resolved to a `job_id` before calling the
    job_id-keyed `_general_resume`, so the anti-self-scoring guard survives
    the SQLite rewrite even though `analyze_job`'s signature is still
    PR1-shaped (raw url in, not the D5 extracted-fields contract).

    Args:
        url:  The raw job posting URL.
        conn: An already-open connection to read on. None opens one for the
              duration of this call.

    Returns:
        The matching job's id, or None if no job has this url — this is the
        ONLY case where None is returned; it is never a blanket bypass.

    Raises:
        ValueError: on a corrupt or unreadable database (propagates so the
                    caller can distinguish "corrupt" from "no match").
    """
    with nullcontext(conn) if conn is not None else connect() as c:
        row = c.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    return row["id"] if row is not None else None


def _find_job_ids_by_custom_title(
    custom_title: str, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Resolve a custom_title to every job id sharing it, exact match.

    `custom_title` is NOT unique — `save_job_analysis` deliberately never
    matches an existing record by `custom_title` (R3/SC-08), so re-saving
    under the same title creates a second row. Callers that need a single
    job (analyze.py's Guard 1 resolution, get_job's custom_title lookup)
    MUST look at the length of the returned list and decide explicitly what
    to do with 0, 1, or >1 matches — silently taking the first would hide a
    genuine ambiguity from the caller.

    Args:
        custom_title: The exact custom_title to look up.
        conn:         An already-open connection to read on. None opens one
                      for the duration of this call.

    Returns:
        A list of matching job ids — possibly empty, possibly more than one.

    Raises:
        ValueError: on a corrupt or unreadable database.
    """
    with nullcontext(conn) if conn is not None else connect() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE custom_title = ?", (custom_title,)
        ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def save_job_analysis(
    title: str,
    company: str,
    country: str,
    id: str | None = None,
    url: str | None = None,
    custom_title: str | None = None,
    jd_text: str | None = None,
    score: int | None = None,
    recommendation: str | None = None,
    notes: str | None = None,
) -> SaveJobResult:
    """Persist an analyzed job record. Upserts by `id` then by `url` (D10).

    Resolution order:
    1. `id` given -> that row is the target; unknown id -> error="not_found".
    2. No `id`, `url` given and matching an existing row -> that row is the
       target (upsert by URL, unchanged semantics from the prior JSON store,
       including "omitted optional argument preserves the previous value").
    3. Otherwise -> a brand new row. `title`/`company` are NEVER used to
       match an existing record (R3) — a user editing a title for clarity
       must not silently create a duplicate under an update path, and title
       text is never treated as an update key either way.

    `url` and `custom_title` follow the same "explicit argument overwrites,
    omission (None) preserves the previous value" rule as `score`/
    `recommendation`/`notes`/`jd_text` — this is what lets a URL be supplied
    later without disturbing the custom_title, and vice versa (SC-04). On a
    brand new row, at least one of `url`/`custom_title` must resolve to a
    non-None value, or the record has no way for a human to find it again
    (R1, SC-03).

    This tool NEVER raises — all failures are encoded in the return envelope.

    Args:
        title:           Job title (Claude-extracted). Always required, always
                          overwrites — no omit-preserve semantics.
        company:         Company name (Claude-extracted). Same as title.
        country:         Free-text country (Claude-extracted). Same as title.
        id:              Existing job id to update. None to create or
                          upsert-by-url.
        url:              Job posting URL. Nullable/unique attribute, not the
                          identity (D1). Omitted (None) preserves the
                          existing value on an update.
        custom_title:     User-supplied handle when url is absent. Same
                          omit-preserve rule as url.
        jd_text:          Full pasted job description, stored in the
                          `job_descriptions` side table. Omitted (None)
                          leaves any existing captured JD untouched.
        score:            0-100 match score (Claude-supplied). Omit-preserve.
        recommendation:   APPLY/CONSIDER/SKIP (Claude-supplied). Omit-preserve.
        notes:            Free-text notes. Omit-preserve.

    Returns:
        SaveJobResult with success=True, id, url, custom_title, updated flag
        on success; success=False with error/message on failure.
    """
    try:
        with connect(write=True) as conn:
            if id is not None:
                existing = _find_job_by_id(conn, id)
                if existing is None:
                    return SaveJobResult(
                        success=False,
                        error="not_found",
                        message=f"No job exists with id {id!r}.",
                    )
                target_id = id
                updated = True
            elif url is not None:
                existing = _find_job_by_url(conn, url)
                target_id = existing["id"] if existing else uuid.uuid4().hex
                updated = existing is not None
            else:
                existing = None
                target_id = uuid.uuid4().hex
                updated = False

            final_url = (
                url if url is not None else (existing["url"] if existing else None)
            )
            final_custom_title = (
                custom_title
                if custom_title is not None
                else (existing["custom_title"] if existing else None)
            )

            if not updated and final_url is None and final_custom_title is None:
                return SaveJobResult(
                    success=False,
                    error="invalid_input",
                    message=(
                        "At least one of 'url' or 'custom_title' is required "
                        "so this job can be found again later."
                    ),
                )

            final_score = (
                score
                if score is not None
                else (existing["score"] if existing else None)
            )
            final_recommendation = (
                recommendation
                if recommendation is not None
                else (existing["recommendation"] if existing else None)
            )
            final_notes = (
                notes
                if notes is not None
                else (existing["notes"] if existing else None)
            )
            final_status = (
                existing["status"] if existing else ApplicationStatus.NOT_APPLIED.value
            )
            analyzed_at = datetime.now(timezone.utc).isoformat()

            # Validate types via pydantic BEFORE touching SQL, so a bad
            # score type is reported as invalid_input, never write_error.
            try:
                StoredJob(
                    id=target_id,
                    url=final_url,
                    custom_title=final_custom_title,
                    title=title,
                    company=company,
                    country=country,
                    analyzed_at=analyzed_at,
                    status=final_status,
                    score=final_score,
                    recommendation=final_recommendation,
                    notes=final_notes,
                )
            except ValidationError as exc:
                return SaveJobResult(
                    success=False, error="invalid_input", message=str(exc)
                )

            try:
                if updated:
                    conn.execute(
                        "UPDATE jobs SET url=?, custom_title=?, title=?, company=?, "
                        "country=?, score=?, recommendation=?, notes=?, analyzed_at=? "
                        "WHERE id=?",
                        (
                            final_url,
                            final_custom_title,
                            title,
                            company,
                            country,
                            final_score,
                            final_recommendation,
                            final_notes,
                            analyzed_at,
                            target_id,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO jobs (id, url, custom_title, title, company, "
                        "country, status, score, recommendation, notes, analyzed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            target_id,
                            final_url,
                            final_custom_title,
                            title,
                            company,
                            country,
                            final_status,
                            final_score,
                            final_recommendation,
                            final_notes,
                            analyzed_at,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                if "url" in str(exc).lower():
                    return SaveJobResult(
                        success=False,
                        error="duplicate_url",
                        message=f"Another job already has url {final_url!r}.",
                    )
                raise

            if jd_text is not None:
                conn.execute(
                    "INSERT INTO job_descriptions (job_id, jd_text) VALUES (?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET jd_text = excluded.jd_text",
                    (target_id, jd_text),
                )

            # Both of these are the url-less-new-record case, so they share
            # one branch rather than recomputing the same condition twice.
            possible_duplicate_id = None
            message = None
            if not updated and final_url is None:
                dup = conn.execute(
                    "SELECT id FROM jobs WHERE company = ? AND title = ? AND id != ?",
                    (company, title, target_id),
                ).fetchone()
                if dup is not None:
                    possible_duplicate_id = dup["id"]

                message = (
                    f"Saved with custom_title={final_custom_title!r} and "
                    f"id={target_id!r} — no url was given. Keep the id: pass "
                    f"it back as `id` to set_application_status or a future "
                    f"save_job_analysis update to reach this record again."
                )

            return SaveJobResult(
                success=True,
                id=target_id,
                url=final_url,
                custom_title=final_custom_title,
                updated=updated,
                possible_duplicate_id=possible_duplicate_id,
                message=message,
            )
    except ValueError as exc:
        return SaveJobResult(success=False, error="corrupt", message=str(exc))
    except sqlite3.Error as exc:
        # connect() translates only its own setup phase; statements inside
        # the yielded block stay raw. That includes the non-url
        # IntegrityError re-raised above, and any UPDATE/INSERT that fails
        # for a reason the block does not name (a full disk, a FK
        # violation). Without this, "This tool NEVER raises" is false.
        return SaveJobResult(success=False, error="write_error", message=str(exc))


def list_jobs(
    since: str | None = None,
    status: str | list[str] | None = None,
    min_score: int | None = None,
    company: str | None = None,
    limit: int | None = None,
    sort_by: str = "analyzed_at",
) -> ListJobsResult:
    """Return stored job records with optional filtering, sorting, and limiting.

    Pipeline order: FILTER -> SORT -> LIMIT, expressed as WHERE/ORDER BY/
    LIMIT (D9). Arguments are validated in Python BEFORE any SQL is built, so
    an invalid status still produces the "use one of: ..." message rather
    than a raw SQL error. This tool NEVER raises.

    Args:
        since:     ISO-8601 string cutoff (inclusive).
        status:    One ApplicationStatus value, or a list to match any member.
        min_score: Minimum score threshold (inclusive). None scores excluded.
        company:   Substring match against company, case-insensitive
                    (lower() on both sides, ASCII-only — SQLite's lower()
                    does not fold non-ASCII). This is SC-1, the release's
                    headline query ("did I apply to Acme?") — D9.
        limit:     Maximum number of records to return (after sort).
        sort_by:   "analyzed_at" (default, newest first) or "score"
                   (descending, None scores last).

    Returns:
        ListJobsResult with success=True and filtered/sorted records on
        success; success=False with error_message on failure.
    """
    where_clauses: list[str] = []
    params: list[object] = []

    if company is not None:
        # lower() on BOTH sides, not COLLATE NOCASE: LIKE is a function in
        # SQLite and ignores collation entirely, so the COLLATE clause this
        # replaces was dead text — the case-insensitivity came only from the
        # default `case_sensitive_like = OFF`. Same ASCII-only semantics as
        # documented, now stated by the query instead of assumed from a
        # connection default a future PRAGMA could flip.
        where_clauses.append("lower(company) LIKE lower(?) ESCAPE '\\'")
        params.append(f"%{_escape_like(company)}%")

    if since is not None:
        try:
            # Normalize to UTC before comparing — analyzed_at is ALWAYS
            # stored UTC-normalized (server-stamped, and migration
            # normalizes legacy rows too), but SQL compares TEXT
            # lexicographically, not as instants. Comparing an un-normalized
            # offset (e.g. "-05:00") against a "+00:00"-stamped value would
            # silently compare the wrong characters (finding 4).
            since_normalized = _normalize_timestamp(since)
        except ValueError as exc:
            return ListJobsResult(
                success=False,
                error_message=f"Invalid 'since' timestamp: {exc}",
            )
        where_clauses.append("analyzed_at >= ?")
        params.append(since_normalized)

    if status is not None:
        if isinstance(status, str):
            requested = [status]
        elif isinstance(status, (list, tuple)):
            requested = list(status)
        else:
            return ListJobsResult(
                success=False,
                error_message=(
                    f"Invalid status: expected a string or a list of strings, "
                    f"got {type(status).__name__}"
                ),
            )
        if not requested:
            return ListJobsResult(
                success=False,
                error_message=(
                    "Invalid status: empty list. Omit the filter to return every job."
                ),
            )
        wanted = []
        for value in requested:
            try:
                wanted.append(ApplicationStatus(value).value)
            except ValueError:
                return ListJobsResult(
                    success=False,
                    error_message=(
                        f"Invalid status: {value!r} (use one of: "
                        f"{', '.join(m.value for m in ApplicationStatus)})"
                    ),
                )
        placeholders = ", ".join("?" for _ in wanted)
        where_clauses.append(f"status IN ({placeholders})")
        params.extend(wanted)

    if min_score is not None:
        where_clauses.append("score IS NOT NULL AND score >= ?")
        params.append(min_score)

    if sort_by == "analyzed_at":
        order_clause = "ORDER BY analyzed_at DESC"
    elif sort_by == "score":
        order_clause = "ORDER BY score DESC"
    else:
        return ListJobsResult(
            success=False,
            error_message=f"Invalid sort_by: {sort_by!r} (use 'analyzed_at' or 'score')",
        )

    if limit is not None and limit <= 0:
        return ListJobsResult(
            success=False,
            error_message="limit must be a positive integer",
        )

    sql = "SELECT * FROM jobs"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += f" {order_clause}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    try:
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            jobs = [_row_to_stored_job(r) for r in rows]
    except (ValueError, sqlite3.Error) as exc:
        # sqlite3.Error too: connect() leaves caller-phase statements
        # untranslated, so a table whose page is corrupt passes the schema
        # check and then fails on this SELECT as a raw sqlite3.DatabaseError.
        return ListJobsResult(success=False, error_message=str(exc))

    return ListJobsResult(success=True, jobs=jobs, count=len(jobs))


def set_application_status(
    status: str,
    id: str | None = None,
    url: str | None = None,
    notes: str | None = None,
) -> SetStatusResult:
    """Set the application status of a stored job record, by id or url.

    Transitions are deliberately UNVALIDATED — any of the 7 status values may
    transition to any other. This tool NEVER raises.

    `notes` APPENDS one dated line to `status_notes` and never touches
    `notes`, the analysis field. Those were a single column until the two
    purposes collided in practice: recording "applied on the 17th" wiped the
    reasoning behind the score, so the highest-scoring jobs in the store were
    exactly the ones whose analysis was gone. Splitting them makes that
    structurally impossible rather than a rule to remember.

    To edit the analysis itself, call save_job_analysis with an `id` — its
    omit-preserve semantics leave every field you do not pass alone.

    Args:
        status: One of: not_applied, applied, interviewing, offer, rejected,
                withdrawn, ghosted.
        id:     Job id (preferred — always resolvable, unlike url).
        url:    Alternate lookup key when id is not known.
        notes:  Optional follow-up note, appended to the timeline as
                "[YYYY-MM-DD] <status> — <note>". Omitted, the timeline is
                left untouched and only the status changes.

    Returns:
        SetStatusResult with success=True, id, url, status, previous_status
        and the resulting status_notes on success; success=False with
        error="invalid_status" (record unchanged), error="not_found", or
        error/message on a store failure.
    """
    try:
        status_member = ApplicationStatus(status)
    except ValueError:
        return SetStatusResult(
            success=False,
            error="invalid_status",
            message=(
                f"Invalid status: {status!r} (use one of: "
                f"{', '.join(m.value for m in ApplicationStatus)})"
            ),
        )

    try:
        with connect(write=True) as conn:
            if id is not None:
                row = _find_job_by_id(conn, id)
            elif url is not None:
                row = _find_job_by_url(conn, url)
            else:
                return SetStatusResult(
                    success=False,
                    error="invalid_input",
                    message="Provide either 'id' or 'url'.",
                )

            if row is None:
                return SetStatusResult(success=False, error="not_found")

            previous_status = row["status"]
            final_status_notes = _append_status_note(
                row["status_notes"], status_member.value, notes
            )

            # `notes` is deliberately absent from this UPDATE. The analysis
            # is not this tool's to touch — use save_job_analysis for that.
            conn.execute(
                "UPDATE jobs SET status=?, status_notes=? WHERE id=?",
                (status_member.value, final_status_notes, row["id"]),
            )

            return SetStatusResult(
                success=True,
                id=row["id"],
                url=row["url"],
                status=status_member.value,
                previous_status=previous_status,
                status_notes=final_status_notes,
                message=f"Status set to {status_member.value}.",
            )
    except ValueError as exc:
        return SetStatusResult(success=False, error="corrupt", message=str(exc))
    except sqlite3.Error as exc:
        return SetStatusResult(success=False, error="write_error", message=str(exc))


def delete_job(id: str | None = None, url: str | None = None) -> DeleteJobResult:
    """Permanently delete a stored job record, by id or url.

    For a record that should never have existed: a mistyped entry, a posting
    that turned out to be a duplicate, test data. This is NOT the same thing
    as the `withdrawn` status, which says "I pulled out of this process" —
    a real event worth keeping. Conflating the two poisons every later query,
    because a withdrawn job still counts as a job you looked at.

    Lookup is by `id` or `url` ONLY — deliberately not `custom_title`, which
    get_job does accept. custom_title is not unique (save_job_analysis never
    matches on it), and deleting by an ambiguous key is precisely how the
    wrong record gets destroyed. Ambiguity is survivable on a read; here it
    is not.

    What happens to everything pointing at the job:

    - `job_descriptions` row: deleted. The captured posting belongs to the
      job and has no meaning without it.
    - `resume_versions`: NOT deleted — `job_id` is set to NULL. The tree is
      append-only and stays that way: a tailored resume is a document you
      really sent, and it survives the posting it was aimed at. Only the
      pointer dies, which is why the append-only UPDATE trigger guards every
      column EXCEPT job_id.

    There is no confirmation flag and no undo. The returned receipt names the
    job and every resume version that was unlinked, so the caller can report
    exactly what disappeared — with no delete_job there is no way back, the
    receipt is the safety mechanism.

    This tool NEVER raises.

    Args:
        id:  Job id (preferred — always resolvable, unlike url).
        url: Alternate lookup key when id is not known.

    Returns:
        DeleteJobResult with success=True and the receipt on success;
        success=False with error="invalid_input" (neither key given),
        "not_found", "corrupt", or "write_error".
    """
    if id is None and url is None:
        return DeleteJobResult(
            success=False,
            error="invalid_input",
            message="Provide either 'id' or 'url'.",
        )

    try:
        with connect(write=True) as conn:
            row = (
                _find_job_by_id(conn, id)
                if id is not None
                else _find_job_by_url(conn, url)  # type: ignore[arg-type]
            )
            if row is None:
                return DeleteJobResult(success=False, error="not_found")

            job_id = row["id"]

            unlinked = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM resume_versions WHERE job_id = ?", (job_id,)
                )
            ]
            # Order matters: both child references must be cleared before the
            # parent row goes, because foreign_keys=ON makes the RESTRICT
            # clauses real — a DELETE with either still pointing here fails.
            if unlinked:
                conn.execute(
                    "UPDATE resume_versions SET job_id = NULL WHERE job_id = ?",
                    (job_id,),
                )
            deleted_description = (
                conn.execute(
                    "DELETE FROM job_descriptions WHERE job_id = ?", (job_id,)
                ).rowcount
                > 0
            )
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

            return DeleteJobResult(
                success=True,
                id=job_id,
                url=row["url"],
                title=row["title"],
                company=row["company"],
                unlinked_resume_versions=unlinked,
                deleted_description=deleted_description,
                message=(
                    f"Deleted {row['title']!r} at {row['company']!r}. "
                    f"{len(unlinked)} resume version(s) unlinked and kept."
                ),
            )
    except ValueError as exc:
        return DeleteJobResult(success=False, error="corrupt", message=str(exc))
    except sqlite3.Error as exc:
        return DeleteJobResult(success=False, error="write_error", message=str(exc))


def get_job(
    id: str | None = None,
    url: str | None = None,
    custom_title: str | None = None,
    include_description: bool = False,
) -> GetJobResult:
    """Retrieve a single job record by id, url, or custom_title (D6, the
    REQUIRED read path for jd_text — without this tool, jd_text is
    write-only).

    `custom_title` exists as a lookup key because it exists as a SAVE
    affordance: a job saved without a `url` is findable ONLY by the
    `custom_title` the user agreed to (analyze.py's `_NO_URL_NOTICE`
    promises exactly this). Without this parameter, that promise had no
    retrieval path at all — a caller could only dump every job via
    `list_jobs` and eyeball it.

    `custom_title` is NOT unique (`save_job_analysis` deliberately never
    matches an existing record by it — R3/SC-08), so a lookup CAN match more
    than one job. Silently returning the first match would hide that
    ambiguity from the caller and could resolve to the wrong job. Instead:
    0 matches -> `not_found`; exactly 1 -> that job; more than 1 ->
    `error="ambiguous"`, naming every matching id so the caller can retry
    with a specific `id`.

    `has_description` is ALWAYS present in the response, regardless of
    `include_description` — it is the affordance that makes the jd_text
    opt-in discoverable without dragging JD text into context by default.
    Also returns the linked resume version SUMMARIES (no content) — the
    headline query is "did I apply to X?" -> "yes, and with this resume."
    (SC-22). The full text still comes from get_resume_version.

    This tool NEVER raises.

    Args:
        id:                   Job id. Exactly one of id/url/custom_title
                               must be given.
        url:                  Alternate lookup key (url is UNIQUE).
        custom_title:         Alternate lookup key for a URL-less job. NOT
                               unique — see above for the multi-match rule.
        include_description:  When True and a description was captured,
                               populates `description` with the full pasted
                               JD text. Defaults to False so a routine status
                               check does not drag JD text into context.

    Returns:
        GetJobResult with success=True, job, description, has_description,
        resume_versions on success; success=False with
        error="not_found" | "invalid_input" | "corrupt" | "ambiguous" on
        failure.
    """
    handles_given = sum(1 for h in (id, url, custom_title) if h is not None)
    if handles_given != 1:
        return GetJobResult(
            success=False,
            error="invalid_input",
            message="Provide exactly one of 'id', 'url', or 'custom_title'.",
        )

    try:
        with connect() as conn:
            if id is not None:
                row = _find_job_by_id(conn, id)
                lookup_key, lookup_value = "id", id
            elif url is not None:
                row = _find_job_by_url(conn, url)
                lookup_key, lookup_value = "url", url
            else:
                # Full rows in ONE query, against the already-open `conn`.
                # The ambiguity check needs only the count, and the single
                # match needs the row this result set already carries —
                # selecting ids and then re-fetching the winner by id was a
                # second round trip for a row we had in hand.
                matches = conn.execute(
                    "SELECT * FROM jobs WHERE custom_title = ?", (custom_title,)
                ).fetchall()
                if len(matches) > 1:
                    matching_ids = [m["id"] for m in matches]
                    return GetJobResult(
                        success=False,
                        error="ambiguous",
                        message=(
                            f"{len(matching_ids)} jobs share custom_title "
                            f"{custom_title!r} (ids: {matching_ids}). Pass "
                            f"one of those as 'id' to disambiguate."
                        ),
                    )
                row = matches[0] if matches else None
                lookup_key, lookup_value = "custom_title", custom_title

            if row is None:
                return GetJobResult(
                    success=False,
                    error="not_found",
                    message=f"No job exists with {lookup_key} {lookup_value!r}.",
                )

            job = _row_to_stored_job(row)

            desc_row = conn.execute(
                "SELECT jd_text FROM job_descriptions WHERE job_id = ?", (job.id,)
            ).fetchone()
            has_description = desc_row is not None
            description = (
                desc_row["jd_text"]
                if (include_description and desc_row is not None)
                else None
            )

            # Explicit summary columns, not `SELECT *` (finding 5): this
            # runs on the DEFAULT include_description=False path too, so a
            # SELECT * here would pull every linked version's full `content`
            # (often the largest column in the schema) just to build
            # summaries that discard it — the same context-cost principle
            # the job_descriptions side table exists to enforce for jd_text.
            version_rows = conn.execute(
                "SELECT id, label, parent_id, job_id, created_at "
                "FROM resume_versions WHERE job_id = ? ORDER BY created_at DESC",
                (job.id,),
            ).fetchall()
    except (ValueError, sqlite3.Error) as exc:
        return GetJobResult(success=False, error="corrupt", message=str(exc))

    resume_versions = [
        ResumeVersionSummary(
            id=r["id"],
            label=r["label"],
            parent_id=r["parent_id"],
            job_id=r["job_id"],
            created_at=r["created_at"],
        )
        for r in version_rows
    ]

    return GetJobResult(
        success=True,
        job=job,
        description=description,
        has_description=has_description,
        resume_versions=resume_versions,
    )
