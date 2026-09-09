"""Work authorization: set_work_authorization + the live country-comparison
warning wired into analyze_job (design D7, spec R12-R14, tasks 3b.1a-n).

This is NOT a property of the job — it is a COMPARISON between the job's
country and the countries the user has declared they may legally work in. A
job-intrinsic "this role needs a visa" flag has no idea who is asking (the
prior cycle's `visa_verdict`, dropped entirely). The comparison is computed
LIVE on every `analyze_job` call and NEVER persisted: a stored verdict goes
stale the moment the user's authorization changes (they get a visa, one
expires, they move), and a job saved months ago would keep claiming a
country match that no longer holds.

Storage keyed on canonical country (tools/_country.py's pure string
transform — no I/O, ported logic, deliberately not folded into this module
or into tools/_db.py, per #334 D1's "a shared module must not become a
grab-bag" rule). Both the raw and canonical forms are stored, and the raw
form is what gets echoed back and shown in a mismatch warning — a wrong
canonicalization is invisible to the user, a wrong echoed country name is
obvious (D7).

Never-set vs. declared-with-zero-countries (SC-27) are distinct, persisted
states. The `work_authorizations` table alone cannot express this by row
count (both states have zero "real" rows) — see `_MARKER_CANONICAL`.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from tools._country import _canonical_country
from tools._db import connect

# Reserved key marking "set_work_authorization has been called at least
# once", independent of how many real countries were declared (possibly
# zero). Composed only of underscores and uppercase letters that
# _canonical_country would produce verbatim from literal input — colliding
# requires a user to declare a country whose text is EXACTLY this string,
# an accepted, effectively-impossible edge case (documented, not silently
# assumed away).
_MARKER_CANONICAL = "__WORK_AUTH_DECLARED_MARKER__"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DeclaredCountry(BaseModel):
    """One declared work-authorized country, both forms (D7)."""

    model_config = ConfigDict(extra="forbid")

    canonical: str
    raw: str


class SetWorkAuthorizationResult(BaseModel):
    """Return value for set_work_authorization."""

    success: bool
    countries_raw: list[str] = []
    countries_canonical: list[str] = []
    error: str | None = None  # "invalid_input" | "corrupt" | "write_error"
    message: str | None = None


class WorkAuthorizationCheck(BaseModel):
    """Live comparison result embedded in AnalyzeJobResult.work_authorization.

    Three outcomes, deliberately distinguishable by `status` alone (D7):
    "authorized" (job's country is among the declared ones — no warning),
    "warned" (it is not — `warning` names both the job's country and the
    declared list, as the user wrote them), "undetermined" (the job's
    country could not be interpreted at all — never silently treated as
    either of the other two). The warning is always advisory: it never
    blocks the envelope.
    """

    model_config = ConfigDict(extra="forbid")

    status: str  # "authorized" | "warned" | "undetermined"
    warning: str | None = None


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def set_work_authorization(countries: list[str]) -> SetWorkAuthorizationResult:
    """Declare the FULL set of countries the user may legally work in.

    REPLACES the previous declaration in its entirety (SC-26) — this is a
    statement about the whole set ("I can work in X and Y"), not an
    additive append. An empty list is a valid, distinct declaration
    (SC-27): "declared, zero countries" differs from "never declared" —
    `analyze_job` treats the two differently (no_work_authorization only
    for the latter).

    Countries are canonicalized at write time (tools/_country.py); both the
    raw text and the canonical form are stored. Two raw spellings that
    canonicalize to the same country (e.g. "USA" and "United States" in one
    call) collapse to a single declared row — the first raw spelling
    encountered wins the echo.

    This tool NEVER raises.

    Args:
        countries: Free-text country names, as the user states them. An
                   empty list explicitly declares zero authorized countries.

    Entries that canonicalize to nothing ("", "   ", "...") are not countries
    and are dropped. If that leaves nothing, the call is REJECTED rather than
    silently storing a declaration that means nothing — see the loop below.

    Returns:
        SetWorkAuthorizationResult with success=True and the stored raw/
        canonical forms on success; success=False with error="invalid_input"
        when no given name could be understood, "corrupt" on a broken
        database, or "write_error" when the write itself fails.
    """
    declared_at = datetime.now(timezone.utc).isoformat()

    seen: dict[str, str] = {}
    dropped: list[str] = []
    for raw in countries:
        canonical = _canonical_country(raw)
        # An entry that canonicalizes to nothing — "", "   ", "..." — is not a
        # country. Storing it would let a user who declared nothing meaningful
        # satisfy analyze_job's precondition as though they had, and every
        # later warning would render the empty raw text: "...not among your
        # declared countries (   )". The job side already refuses to guess at
        # an uninterpretable country ("undetermined"); the declaration side
        # gets the same treatment instead of accepting garbage silently.
        if not canonical:
            dropped.append(raw)
            continue
        if canonical not in seen:
            seen[canonical] = raw

    # Reject rather than silently narrow: a caller who passed only unusable
    # names has declared nothing, and telling them so is the difference
    # between fixing a typo now and trusting a warning that never fires.
    if dropped and not seen:
        return SetWorkAuthorizationResult(
            success=False,
            error="invalid_input",
            message=(
                f"None of the given countries could be understood: "
                f"{dropped!r}. Pass country names as plain text, e.g. "
                f'["United States", "Germany"]. To declare that you may work '
                f"nowhere, pass an empty list."
            ),
        )

    try:
        with connect(write=True) as conn:
            conn.execute("DELETE FROM work_authorizations")
            conn.execute(
                "INSERT INTO work_authorizations "
                "(country_canonical, country_raw, declared_at) VALUES (?, ?, ?)",
                (_MARKER_CANONICAL, "", declared_at),
            )
            for canonical, raw in seen.items():
                conn.execute(
                    "INSERT INTO work_authorizations "
                    "(country_canonical, country_raw, declared_at) VALUES (?, ?, ?)",
                    (canonical, raw, declared_at),
                )
    except ValueError as exc:
        return SetWorkAuthorizationResult(
            success=False, error="corrupt", message=str(exc)
        )
    except sqlite3.Error as exc:
        # connect() translates errors it raises while OPENING the database,
        # but not ones raised by statements inside the yielded block — so this
        # module's own DELETE/INSERT can still throw. Without this clause a
        # UNIQUE collision or a full disk escapes as a raw traceback, breaking
        # the "This tool NEVER raises" contract in the docstring above.
        # jobs_store.py and resumes.py catch the same class from their inserts.
        return SetWorkAuthorizationResult(
            success=False, error="write_error", message=str(exc)
        )

    return SetWorkAuthorizationResult(
        success=True,
        countries_raw=list(seen.values()),
        countries_canonical=list(seen.keys()),
    )


# ---------------------------------------------------------------------------
# Internal helpers — used by analyze.py's Step 2 precondition
# ---------------------------------------------------------------------------


def _declared_authorizations(
    conn: sqlite3.Connection | None = None,
) -> list[DeclaredCountry] | None:
    """Return the declared countries, or None if never declared (SC-24).

    Distinguishes "set_work_authorization was never called" from "called
    with countries=[]" (SC-27) via `_MARKER_CANONICAL`, inserted by every
    set_work_authorization call regardless of list length: zero rows at all
    means never called; a marker-only row means called with zero countries.

    Raises:
        ValueError: on a corrupt or unreadable database (SC-32) — propagates
        so analyze_job's precondition can report "corrupt" instead of
        "no_work_authorization", which would send the caller to
        set_work_authorization only to have IT fail identically on the same
        broken database.
    """
    with nullcontext(conn) if conn is not None else connect() as c:
        rows = c.execute(
            "SELECT country_canonical, country_raw FROM work_authorizations"
        ).fetchall()
    if not rows:
        return None
    return [
        DeclaredCountry(canonical=r["country_canonical"], raw=r["country_raw"])
        for r in rows
        if r["country_canonical"] != _MARKER_CANONICAL
    ]


def _check_work_authorization(
    job_country: str, declared: list[DeclaredCountry]
) -> WorkAuthorizationCheck:
    """Live comparison — computed fresh on every call, never persisted (D7).

    Checked BEFORE the declared-set membership test: a job country that
    canonicalizes to an empty string (nothing left after stripping
    whitespace and punctuation — e.g. "" or "...") is uninterpretable
    regardless of what is or is not declared, and must never be silently
    treated as a match (a false "authorized") or a mismatch (a false
    "warned").

    Args:
        job_country: The job's free-text country, as Claude extracted it.
        declared:    The user's currently declared countries (possibly an
                     empty list — SC-27's "declared, zero countries" state).

    Returns:
        A WorkAuthorizationCheck with status "authorized" (no warning),
        "warned" (warning names both the job's country and the declared
        list, as the user wrote them), or "undetermined" (no warning, the
        job's country could not be interpreted).
    """
    canonical_job_country = _canonical_country(job_country)
    if not canonical_job_country:
        return WorkAuthorizationCheck(status="undetermined")

    declared_canonicals = {d.canonical for d in declared}
    if canonical_job_country in declared_canonicals:
        return WorkAuthorizationCheck(status="authorized")

    declared_raw = ", ".join(d.raw for d in declared) if declared else "(none declared)"
    return WorkAuthorizationCheck(
        status="warned",
        warning=(
            f"This job's country ({job_country!r}) is not among your "
            f"declared work-authorized countries ({declared_raw})."
        ),
    )
