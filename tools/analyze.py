"""Orchestrator tool: analyze_job.

FINAL 0.3.0 contract (sqlite-memory-and-pasted-jd, design D5). Claude reads
the job description pasted into the conversation, extracts `title`,
`company`, `country` (plus optional `url`/`custom_title`), and passes those
— the raw JD text is NEVER a parameter of this tool. The text is already in
Claude's context; accepting it here would make it travel a second time as
call payload, for text the server cannot use (every parser that provided a
floor of truth is deleted). The text reaches the server exactly once, at
save_job_analysis, and only for jobs the user decides to keep.

Two consequences accepted at the design-decision level (obs #368): there is
NO server-side validation of Claude's extraction — a wrong company or
country is not detected here; and a job analyzed but never saved leaves no
trace of its text. The envelope's `extracted` echo is the only mitigation:
it does not detect a bad extraction, it makes one VISIBLE to the only party
who can catch it.

`analyze_job` is `def`, not `async def`: it has zero awaits, and its only
I/O (SQLite reads) is blocking — an async tool runs on FastMCP's event loop
and would block it on every store read; sync tools run on the worker pool,
where every other store tool already runs.

Guard 1 (anti-self-scoring, SC-21) resolution, carried forward from PR2's
tasks 2.5l/2.5n and now adapted to `url` being OPTIONAL (task 3a.2j): when
`url` is given, it is resolved to a `job_id` via an exact lookup against the
`jobs` table before calling the job_id-keyed `_general_resume`. When `url`
is absent but `custom_title` is given, the SAME resolution happens against
`custom_title` instead — a job saved without a `url` (the referral case
`_NO_URL_NOTICE` exists for) is still resolvable, so the guard stays armed
on that path too; a naive "no url -> job_id=None always" reading of an
earlier revision of this docstring was WRONG for exactly this case: it
would silently hand back a job's own tailored resume when that job is
re-analyzed by custom_title alone, the precise self-scoring inflation this
guard exists to prevent. `job_id` is None only when neither `url` nor
`custom_title` is given, or the one given matches no saved job — never a
blanket bypass. `custom_title` is NOT unique (see jobs_store.py's
`_find_job_ids_by_custom_title`); when it resolves to more than one job,
this tool refuses to guess and returns `error="ambiguous_custom_title"`
rather than silently picking one (which could arm the guard for the wrong
job, or fail to arm it for the right one).

Guard 2 (no_resume vs corrupt) is preserved by wrapping every store read
this tool makes — the url/custom_title -> job_id resolution, _general_resume
and _declared_authorizations — in a single try/except block: a corrupt or
unreadable database raises from any of those calls and is reported as
"corrupt"; "no_resume" is reported only when the database is readable but
nothing usable was found. Those three reads share ONE connection (each
connect() pays a mkdir, four PRAGMAs, a schema check and a user_version
read, and this tool used to open three). Sharing the connection does NOT
mean reading everything up front: the no_resume check still sits between
the resume read and the work-auth read, so a database with a readable
resume table and an unreadable work_authorizations table still answers
"no_resume" rather than "corrupt" — the ordering paragraph below is
load-bearing. sqlite3.Error is caught alongside ValueError because
connect() translates only its own setup phase; a statement failing
mid-read stays raw.

Work authorization (design D7, task 3b.1m) is Step 2, inserted between the
resume precondition (Step 1, unchanged) and envelope-build (now Step 3):
`_declared_authorizations()` returns None only when set_work_authorization
has NEVER been called (never a default — defaulting to any country, e.g.
the US, would mis-warn everyone outside it), producing
error="no_work_authorization". A database corrupt enough to break THIS
read is reported as "corrupt", never "no_work_authorization" — the same
Guard-2-style distinction Step 1 already makes for the resume precondition,
extended to this one. The comparison itself
(`tools.work_auth._check_work_authorization`) is LIVE: computed fresh on
every call against the CURRENT declared set, never persisted — a stored
verdict would go stale the moment the user's authorization changes, exactly
the mistake the deleted `visa_verdict` made.

Precondition ordering, decided deliberately (not incidentally): resume
FIRST, work-authorization SECOND. A first-time user with neither saved
loses only the resume message on this call and gets the work-authorization
message on the very next one — one extra round trip, not "many". Reversing
the order was rejected because `test_sc38_empty_store_no_resume_error`
(PR3a, pinned) already asserts "no_resume" wins when both preconditions are
unset — an already-shipped guard this chain's "never weaken a safety test"
rule forbids relitigating by changing behavior out from under it.

Adds zero new external dependencies — the resume lookup is delegated to
tools/resumes.py and tools/jobs_store.py; the work-authorization lookup and
comparison to tools/work_auth.py.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from tools._db import connect
from tools.jobs_store import _find_job_id_by_url, _find_job_ids_by_custom_title
from tools.resumes import _general_resume, ResumeVersion
from tools.work_auth import (
    _check_work_authorization,
    _declared_authorizations,
    WorkAuthorizationCheck,
)


# ---------------------------------------------------------------------------
# Scoring rubric (applied by the conversation-side Claude, not the server)
# ---------------------------------------------------------------------------

_RECOMMENDATION_RULES: list[str] = [
    "SKIP if the match score is below 40.",
    "APPLY if the match score is 70 or higher.",
    "CONSIDER in every other case.",
]

_SCORING_INSTRUCTIONS: str = (
    "The job posting is NOT part of this envelope — the server no longer "
    "fetches it. Score against the posting text present in the conversation; "
    "if you do not have it, ask the user to paste it rather than working from "
    "the URL alone. "
    "Using that posting and the candidate's general resume text, produce: "
    "(1) a technical-fit match score from 0 to 100 based on skills, "
    "experience, and education; (2) matched_skills and missing_skills lists; "
    "(3) a short, factual summary of the fit; and (4) a recommendation of "
    "APPLY, CONSIDER, or SKIP by applying the recommendation_rules in order. "
    "Be factual and objective — do not inflate the score. If you recommend "
    "APPLY or CONSIDER, tailor the resume text for this job and save it with "
    "save_resume_version (parent_id=this resume's id, job_id=the id returned "
    "by save_job_analysis). save_job_analysis must be called FIRST so the "
    "job row exists — save_resume_version's job_id is a foreign key and "
    "rejects an id that does not yet exist."
)

_NO_URL_NOTICE: str = (
    "No url was given for this job. Agree a memorable custom_title with the "
    "user now and tell them it is how they will find this record again "
    "later — pass it to save_job_analysis as custom_title."
)
# Shown only when NEITHER url NOR custom_title is present — a caller who
# already supplied custom_title has already agreed a handle; repeating the
# notice would send them through a redundant round-trip over one already in
# hand (finding 4).


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ScoringGuide(BaseModel):
    """Instructions + rubric for the conversation-side Claude to score the match."""

    instructions: str
    recommendation_rules: list[str]


class ExtractedFields(BaseModel):
    """Verbatim echo of analyze_job's Claude-extracted input (design D5).

    Every parser that provided a floor of truth on these fields is deleted;
    this echo is the ONLY visibility the user gets into a bad extraction. No
    normalization or reformatting is applied here — that would defeat the
    purpose of showing exactly what was received.
    """

    title: str
    company: str
    country: str
    url: str | None = None
    custom_title: str | None = None


class AnalyzeJobResult(BaseModel):
    """Decision-ready envelope of FACTS. Claude derives the score and verdict.

    On success, extracted/resume/scoring_guide are populated. The server
    does not compute a match score or recommendation — those are left to
    Claude, which reasons over this envelope and the scoring_guide.

    No `job` or `visa` field — both the deleted job-fetch and visa-check
    steps are gone from this orchestrator entirely.
    """

    extracted: ExtractedFields | None = None
    resume: ResumeVersion | None = None
    scoring_guide: ScoringGuide | None = None
    work_authorization: WorkAuthorizationCheck | None = None
    notice: str | None = None  # e.g. "no url given, agree a custom_title"
    # top-level error code: no_resume | corrupt | ambiguous_custom_title
    # | no_work_authorization
    error: str | None = None
    message: str | None = None  # human-readable explanation for top-level errors


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _scoring_guide() -> ScoringGuide:
    """Build the scoring guide handed to Claude."""
    return ScoringGuide(
        instructions=_SCORING_INSTRUCTIONS,
        recommendation_rules=list(_RECOMMENDATION_RULES),
    )


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def analyze_job(
    title: str,
    company: str,
    country: str,
    url: str | None = None,
    custom_title: str | None = None,
) -> AnalyzeJobResult:
    """Gather the general resume + scoring guide so Claude can score the match.

    1. Verifies a general resume exists (returns error envelope if not).
    2. Verifies work authorization has been declared at least once (returns
       error="no_work_authorization" if not — see design D7/module docstring
       for the precondition ordering decision).
    3. Returns the general resume, a scoring guide, an `extracted` echo of
       the caller's input, and a live work-authorization comparison.

    The match score and APPLY/CONSIDER/SKIP recommendation are NOT computed by
    this tool — after calling it, score the candidate's resume against the job
    (already pasted into this conversation) and apply the scoring_guide's
    recommendation_rules in your reply.

    The resume injected here is the GENERAL resume (design D6), not the most
    recently tailored one: scoring a job against a resume already tailored
    FOR that job would inflate the match score by scoring the resume against
    itself.

    This tool NEVER raises for any documented failure mode — all such
    failures are encoded in the return envelope. An unexpected keyword
    argument (e.g. a JD text payload) is a caller programming error and
    raises normally, exactly as calling any Python function with an unknown
    keyword does.

    Args:
        title:        Job title, Claude-extracted from the pasted posting.
        company:      Company name, Claude-extracted.
        country:      Free-text country, Claude-extracted. Compared against
                      the user's declared work authorization; a mismatch
                      populates work_authorization with an advisory warning.
        url:          The job posting URL, if any. Resolved to a job_id
                      (exact match against the jobs table) so the
                      general-resume selection can exclude a resume already
                      tailored to this job. None when the user has no URL to
                      give (e.g. a referral).
        custom_title: The user's handle for a URL-less job. Echoed back, AND
                      — when `url` is absent — resolved to a job_id the same
                      way `url` is, so Guard 1 (anti-self-scoring) stays
                      armed for jobs saved without a url. If more than one
                      saved job shares this custom_title, this tool refuses
                      to guess and returns error="ambiguous_custom_title".

    Returns:
        AnalyzeJobResult with extracted/resume/scoring_guide/
        work_authorization populated on success, or error/message fields
        populated on failure ("no_resume" | "corrupt" |
        "ambiguous_custom_title" | "no_work_authorization").
    """
    # --- Steps 1 and 2: every store read this call makes, on ONE connection ---
    # These are three independent reads (job lookup, general resume, declared
    # authorizations) that used to open three connections — each paying a
    # mkdir, four PRAGMAs, a schema check and a user_version read.
    #
    # The no_resume check stays BETWEEN the resume read and the work-auth
    # read, where it has always been, rather than moving below the block
    # with the other precondition. Reading both up front and checking after
    # would be tidier and would be wrong: on a database whose
    # work_authorizations table is unreadable but whose resume table is
    # fine, the eager read raises and this returns "corrupt" where it used
    # to return "no_resume". Laziness here is load-bearing, not incidental —
    # see the ordering paragraph in the module docstring.
    try:
        with connect() as conn:
            if url is not None:
                job_id = _find_job_id_by_url(url, conn)
            elif custom_title is not None:
                matching_ids = _find_job_ids_by_custom_title(custom_title, conn)
                if len(matching_ids) > 1:
                    return AnalyzeJobResult(
                        error="ambiguous_custom_title",
                        message=(
                            f"{len(matching_ids)} saved jobs share custom_title "
                            f"{custom_title!r} (ids: {matching_ids}). Re-analyze "
                            f"with 'url' instead, or use get_job with a specific "
                            f"'id' to confirm which job this is before "
                            f"proceeding."
                        ),
                    )
                job_id = matching_ids[0] if matching_ids else None
            else:
                job_id = None
            resume = _general_resume(job_id=job_id, conn=conn)
            if resume is None:
                return AnalyzeJobResult(
                    error="no_resume",
                    message="No resume found. Run save_resume_version first.",
                )
            declared = _declared_authorizations(conn)
    except (ValueError, sqlite3.Error) as exc:
        # NOT no_resume / no_work_authorization: the database exists and is
        # unreadable or malformed. Telling the user to run
        # save_resume_version or set_work_authorization here sends them to a
        # tool that will fail the same way, on the same file. sqlite3.Error
        # is caught alongside ValueError because connect() only translates
        # its own setup phase — a statement failing mid-read stays raw, and
        # this tool never raises.
        return AnalyzeJobResult(error="corrupt", message=str(exc))

    # --- Work-authorization precondition (design D7, task 3b.1m) ---
    if declared is None:
        return AnalyzeJobResult(
            error="no_work_authorization",
            message=(
                "No work authorization declared yet. Call "
                "set_work_authorization with the countries you may legally "
                "work in before analyzing a job."
            ),
        )

    # --- Step 3: Hand facts + rubric to Claude for scoring ---
    return AnalyzeJobResult(
        extracted=ExtractedFields(
            title=title,
            company=company,
            country=country,
            url=url,
            custom_title=custom_title,
        ),
        resume=resume,
        scoring_guide=_scoring_guide(),
        work_authorization=_check_work_authorization(country, declared),
        notice=_NO_URL_NOTICE if (url is None and custom_title is None) else None,
    )
