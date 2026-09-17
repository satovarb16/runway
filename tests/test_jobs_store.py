"""Tests for tools/jobs_store.py — SQLite-backed job store (design D1, D10).

get_job and list_jobs's company filter are PR3a's, not tested here. Covers
save_job_analysis (SC-01..SC-10, SC-14 write-half), list_jobs (unchanged
contract, now SQL-backed), set_application_status (+id), and the PR#32
omitted-argument regression — flagged by design as "the single most likely
place for a silently-passing-but-wrong test in this release", so its fixture
carries non-default prior values throughout (task 2.4j).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_stored_job_has_no_visa_verdict_field():
    from tools.jobs_store import StoredJob

    assert "visa_verdict" not in StoredJob.model_fields


@pytest.mark.contract
def test_stored_job_has_id_custom_title_country():
    from tools.jobs_store import StoredJob

    job = StoredJob(
        id="J1",
        url="https://ex.com/1",
        custom_title=None,
        title="SWE",
        company="Acme",
        country="USA",
        status="not_applied",
        analyzed_at="2025-01-01T00:00:00+00:00",
    )
    assert job.id == "J1"
    assert job.country == "USA"


@pytest.mark.contract
def test_stored_job_rejects_unknown_fields():
    from pydantic import ValidationError

    from tools.jobs_store import StoredJob

    with pytest.raises(ValidationError):
        StoredJob(
            id="J1",
            url="https://ex.com/1",
            title="SWE",
            company="Acme",
            country="USA",
            analyzed_at="2025-01-01T00:00:00+00:00",
            visa_verdict="GREEN",
        )


# ---------------------------------------------------------------------------
# R1 / SC-01..SC-03: at least one handle required
# ---------------------------------------------------------------------------


def test_sc01_creation_with_url_only(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(
        url="https://example.com/job/1", title="SWE", company="Acme", country="USA"
    )

    assert result.success is True
    assert result.id is not None
    assert result.url == "https://example.com/job/1"
    assert result.custom_title is None


def test_sc02_creation_with_custom_title_only_carries_remember_message(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(
        custom_title="Acme referral role", title="SWE", company="Acme", country="USA"
    )

    assert result.success is True
    assert result.id is not None
    assert result.url is None
    assert result.custom_title == "Acme referral role"
    assert result.message is not None
    assert "custom" in result.message.lower() or "title" in result.message.lower()


def test_sc03_neither_url_nor_custom_title_rejected(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(title="SWE", company="Acme", country="USA")

    assert result.success is False
    assert result.error == "invalid_input"

    from tools.jobs_store import list_jobs

    assert list_jobs().count == 0


# ---------------------------------------------------------------------------
# R2 / SC-04..SC-05: identity survives a URL supplied later
# ---------------------------------------------------------------------------


def test_sc04_supplying_url_later_preserves_id(db_path):
    from tools.jobs_store import save_job_analysis
    from tools.resumes import save_resume_version

    created = save_job_analysis(
        custom_title="Acme referral role", title="SWE", company="Acme", country="USA"
    )
    job_id = created.id

    save_resume_version(
        content="tailored", label="Tailored", parent_id=None, job_id=job_id
    )

    updated = save_job_analysis(
        id=job_id,
        url="https://example.com/job/1",
        title="SWE",
        company="Acme",
        country="USA",
    )

    assert updated.success is True
    assert updated.id == job_id
    assert updated.url == "https://example.com/job/1"
    assert updated.custom_title == "Acme referral role"  # preserved, omitted this call


def test_sc05_unknown_id_on_explicit_update_rejected(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(
        id="ghost", url="https://ex.com/x", title="X", company="Y", country="Z"
    )

    assert result.success is False
    assert result.error == "not_found"

    from tools.jobs_store import list_jobs

    assert list_jobs().count == 0


# ---------------------------------------------------------------------------
# R3 / SC-06..SC-09: upsert semantics
# ---------------------------------------------------------------------------


def test_sc06_resave_same_url_updates_existing_id_unchanged(db_path):
    from tools.jobs_store import save_job_analysis

    first = save_job_analysis(
        url="https://example.com/job/1", title="SWE", company="Acme", country="USA"
    )
    second = save_job_analysis(
        url="https://example.com/job/1",
        title="SWE",
        company="Acme",
        country="USA",
        score=80,
    )

    assert second.success is True
    assert second.id == first.id
    assert second.updated is True

    from tools.jobs_store import list_jobs

    assert list_jobs().count == 1


def test_sc07_new_url_creates_new_record(db_path):
    from tools.jobs_store import save_job_analysis

    first = save_job_analysis(
        url="https://example.com/job/1", title="SWE", company="Acme", country="USA"
    )
    second = save_job_analysis(
        url="https://example.com/job/2", title="Other", company="Beta", country="USA"
    )

    assert second.success is True
    assert second.id != first.id

    from tools.jobs_store import list_jobs

    assert list_jobs().count == 2


def test_sc08_resave_same_custom_title_never_matches(db_path):
    from tools.jobs_store import save_job_analysis

    first = save_job_analysis(
        custom_title="Referral role", title="SWE", company="Acme", country="USA"
    )
    second = save_job_analysis(
        custom_title="Referral role", title="SWE", company="Acme", country="USA"
    )

    assert second.success is True
    assert second.id != first.id

    from tools.jobs_store import list_jobs

    assert list_jobs().count == 2


def test_sc09_setting_url_to_one_owned_by_another_job_rejected(db_path):
    from tools.jobs_store import save_job_analysis

    job_a = save_job_analysis(
        url="https://example.com/job/1", title="X", company="Y", country="Z"
    )
    job_b = save_job_analysis(custom_title="B", title="X", company="Y", country="Z")

    result = save_job_analysis(
        id=job_b.id,
        url="https://example.com/job/1",
        title="X",
        company="Y",
        country="Z",
    )

    assert result.success is False
    assert result.error == "duplicate_url"

    from tools.jobs_store import list_jobs

    jobs = {j.id: j for j in list_jobs().jobs}
    assert jobs[job_b.id].url is None  # unchanged
    assert jobs[job_a.id].url == "https://example.com/job/1"  # unchanged


# ---------------------------------------------------------------------------
# SC-10 — highest-risk silently-passing test: prior values are non-default
# ---------------------------------------------------------------------------


def test_sc10_omitted_optional_fields_preserve_prior_nondefault_values(db_path):
    """The fixture's prior notes/score/recommendation/status are all
    non-default, so this test fails under BOTH a correct partial UPDATE and
    (crucially) is the only shape that would also fail under an incorrect
    full overwrite with defaults — a fixture with all-None priors passes
    under both implementations and proves nothing (design §14)."""
    from tools.jobs_store import save_job_analysis, set_application_status

    created = save_job_analysis(
        url="https://example.com/job/1",
        title="SWE",
        company="Acme",
        country="USA",
        score=70,
        recommendation="CONSIDER",
        notes="keep me",
    )
    set_application_status(id=created.id, status="interviewing")

    result = save_job_analysis(
        id=created.id,
        url=None,
        title="SWE",
        company="Acme",
        country="USA",
        score=85,  # explicit — must overwrite
    )

    assert result.success is True

    from tools.jobs_store import list_jobs

    record = list_jobs().jobs[0]
    assert record.score == 85  # explicit arg wins
    assert record.recommendation == "CONSIDER"  # omitted — preserved
    assert record.notes == "keep me"  # omitted — preserved
    assert record.status == "interviewing"  # never touched by save_job_analysis


# ---------------------------------------------------------------------------
# SC-14 (write-half): jd_text optional, round-trips through job_descriptions
# ---------------------------------------------------------------------------


def test_sc14_jd_text_omitted_on_save_leaves_no_job_descriptions_row(db_path):
    from tools.jobs_store import save_job_analysis
    from tools._db import connect

    result = save_job_analysis(
        url="https://example.com/1", title="SWE", company="Acme", country="USA"
    )

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM job_descriptions WHERE job_id = ?", (result.id,)
        ).fetchone()
    assert row is None


def test_jd_text_provided_writes_job_descriptions_row(db_path):
    from tools.jobs_store import save_job_analysis
    from tools._db import connect

    result = save_job_analysis(
        url="https://example.com/1",
        title="SWE",
        company="Acme",
        country="USA",
        jd_text="Full posting...\nwith newlines",
    )

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT jd_text FROM job_descriptions WHERE job_id = ?", (result.id,)
        ).fetchone()
    assert row["jd_text"] == "Full posting...\nwith newlines"


def test_jd_text_omitted_on_update_leaves_existing_jd_text_unchanged(db_path):
    from tools.jobs_store import save_job_analysis
    from tools._db import connect

    created = save_job_analysis(
        url="https://example.com/1",
        title="SWE",
        company="Acme",
        country="USA",
        jd_text="original JD",
    )
    save_job_analysis(
        id=created.id, url=None, title="SWE", company="Acme", country="USA"
    )

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT jd_text FROM job_descriptions WHERE job_id = ?", (created.id,)
        ).fetchone()
    assert row["jd_text"] == "original JD"


# ---------------------------------------------------------------------------
# list_jobs — unchanged contract, now SQL-backed
# ---------------------------------------------------------------------------


def test_list_jobs_empty_store_returns_empty(db_path):
    from tools.jobs_store import list_jobs

    result = list_jobs()

    assert result.success is True
    assert result.jobs == []
    assert result.count == 0


def test_list_jobs_status_filter_and_min_score(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status, list_jobs

    a = save_job_analysis(
        url="https://a.com", title="A", company="A", country="US", score=90
    )
    set_application_status(id=a.id, status="applied")
    save_job_analysis(
        url="https://b.com", title="B", company="B", country="US", score=50
    )

    result = list_jobs(status="applied", min_score=60)

    assert result.success is True
    assert result.count == 1
    assert result.jobs[0].url == "https://a.com"


def test_list_jobs_status_list_and_invalid_member(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(url="https://a.com", title="A", company="A", country="US")

    result = list_jobs(status=["applied", "bogus"])

    assert result.success is False
    assert "bogus" in result.error_message


def test_list_jobs_sort_by_score_desc_none_last(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(
        url="https://a.com", title="A", company="A", country="US", score=70
    )
    save_job_analysis(
        url="https://b.com", title="B", company="B", country="US", score=90
    )
    save_job_analysis(url="https://c.com", title="C", company="C", country="US")

    result = list_jobs(sort_by="score")

    assert [j.url for j in result.jobs] == [
        "https://b.com",
        "https://a.com",
        "https://c.com",
    ]


def test_list_jobs_limit_and_default_sort_newest_first(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(url="https://a.com", title="A", company="A", country="US")
    save_job_analysis(url="https://b.com", title="B", company="B", country="US")

    result = list_jobs()

    assert result.jobs[0].url == "https://b.com"


def test_list_jobs_invalid_sort_by_returns_error(db_path):
    from tools.jobs_store import list_jobs

    result = list_jobs(sort_by="bogus_field")

    assert result.success is False
    assert "bogus_field" in result.error_message


def test_list_jobs_limit_zero_or_negative_returns_error(db_path):
    from tools.jobs_store import list_jobs

    assert list_jobs(limit=0).success is False
    assert list_jobs(limit=-1).success is False


def test_list_jobs_since_filter(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    # Stamp analyzed_at deterministically by monkeypatching is overkill here;
    # instead rely on two saves in sequence and filter with a cutoff before
    # both, then after both.
    save_job_analysis(url="https://a.com", title="A", company="A", country="US")
    result_before = list_jobs(since="2000-01-01T00:00:00Z")
    result_after = list_jobs(since="2999-01-01T00:00:00Z")

    assert result_before.count == 1
    assert result_after.count == 0


def test_list_jobs_since_filter_normalizes_offset_to_utc(db_path):
    """Finding 4 (PR2 apply-fix review): `since` must be UTC-normalized
    before the lexicographic string comparison against analyzed_at, which is
    always stored '+00:00'-stamped. A `since` cutoff expressed with a
    negative offset that is numerically AFTER a stored UTC timestamp must
    exclude that record — comparing the raw un-normalized strings would
    include it, because '09' < '10' even though 09:00-05:00 (14:00 UTC) is
    LATER than 10:00+00:00."""
    from tools._db import connect
    from tools.jobs_store import list_jobs

    with connect(db_path, write=True) as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, custom_title, title, company, country, "
            "status, score, recommendation, notes, analyzed_at) VALUES "
            "('J1', 'https://ex.com/1', NULL, 'T', 'C', NULL, 'not_applied', "
            "NULL, NULL, NULL, '2025-06-01T10:00:00+00:00')"
        )

    result = list_jobs(since="2025-06-01T09:00:00-05:00")

    assert result.success is True
    assert result.count == 0


def test_list_jobs_never_returns_jd_text_field(db_path):
    """SC-15 (jobs half): the summary shape has no jd_text field at all."""
    from tools.jobs_store import save_job_analysis, list_jobs, StoredJob

    save_job_analysis(
        url="https://a.com",
        title="A",
        company="A",
        country="US",
        jd_text="huge posting" * 500,
    )

    assert "jd_text" not in StoredJob.model_fields
    result = list_jobs()
    assert result.jobs[0].url == "https://a.com"


# ---------------------------------------------------------------------------
# D9 (task 3a.1h/3a.1i): list_jobs gains a `company` filter — substring,
# COLLATE NOCASE. SC-1 is the release's headline query ("did I apply to
# Acme?") and is unanswerable without this filter except by listing the
# whole store.
# ---------------------------------------------------------------------------


def test_list_jobs_company_filter_substring_match(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(
        url="https://a.com/1", title="A", company="Acme Corp", country="US"
    )
    save_job_analysis(
        url="https://b.com/1", title="B", company="Beta Inc", country="US"
    )

    result = list_jobs(company="Acme")

    assert result.success is True
    assert result.count == 1
    assert result.jobs[0].company == "Acme Corp"


def test_list_jobs_company_filter_is_case_insensitive(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(
        url="https://a.com/1", title="A", company="Acme Corp", country="US"
    )

    result = list_jobs(company="acme")

    assert result.success is True
    assert result.count == 1


def test_list_jobs_company_filter_no_match_returns_empty(db_path):
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(
        url="https://a.com/1", title="A", company="Acme Corp", country="US"
    )

    result = list_jobs(company="Nonexistent")

    assert result.success is True
    assert result.count == 0


def test_list_jobs_company_filter_combines_with_status_filter(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status, list_jobs

    j1 = save_job_analysis(
        url="https://a.com/1", title="A", company="Acme Corp", country="US"
    )
    save_job_analysis(
        url="https://a.com/2", title="B", company="Acme Corp", country="US"
    )
    set_application_status(id=j1.id, status="applied")

    result = list_jobs(company="Acme", status="applied")

    assert result.success is True
    assert result.count == 1
    assert result.jobs[0].id == j1.id


def test_list_jobs_company_filter_escapes_percent_wildcard(db_path):
    """Finding 3: '%' in the user's string must be a literal, not a SQL
    LIKE wildcard — otherwise company='%' would match every row while the
    caller believes they asked for a filtered result."""
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(
        url="https://a.com/1", title="A", company="Acme Corp", country="US"
    )

    result = list_jobs(company="%")

    assert result.success is True
    assert result.count == 0


def test_list_jobs_company_filter_escapes_underscore_wildcard(db_path):
    """Finding 3: '_' in the user's string must be a literal, not a SQL
    LIKE single-character wildcard — otherwise company='A_B' would match
    'AxB'."""
    from tools.jobs_store import save_job_analysis, list_jobs

    save_job_analysis(url="https://a.com/1", title="A", company="AxB", country="US")

    result = list_jobs(company="A_B")

    assert result.success is True
    assert result.count == 0


# ---------------------------------------------------------------------------
# set_application_status — now accepts id OR url
# ---------------------------------------------------------------------------


def test_set_application_status_by_id(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status, list_jobs

    created = save_job_analysis(
        url="https://a.com", title="A", company="A", country="US", notes="keep me"
    )

    result = set_application_status(id=created.id, status="applied")

    assert result.success is True
    record = list_jobs().jobs[0]
    assert record.status == "applied"
    assert record.notes == "keep me"


def test_set_application_status_by_url(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status

    save_job_analysis(url="https://a.com", title="A", company="A", country="US")

    result = set_application_status(url="https://a.com", status="applied")

    assert result.success is True
    assert result.status == "applied"


def test_set_application_status_invalid_status_rejected(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status

    created = save_job_analysis(
        url="https://a.com", title="A", company="A", country="US"
    )

    result = set_application_status(id=created.id, status="bogus")

    assert result.success is False
    assert result.error == "invalid_status"


def test_set_application_status_unknown_id_returns_not_found(db_path):
    from tools.jobs_store import set_application_status

    result = set_application_status(id="ghost", status="applied")

    assert result.success is False
    assert result.error == "not_found"


def test_set_application_status_free_transition_any_to_any(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status

    created = save_job_analysis(
        url="https://a.com", title="A", company="A", country="US"
    )
    set_application_status(id=created.id, status="rejected")

    result = set_application_status(id=created.id, status="interviewing")

    assert result.success is True
    assert result.status == "interviewing"


# ---------------------------------------------------------------------------
# Corruption / error-envelope discipline
# ---------------------------------------------------------------------------


def test_list_jobs_on_missing_database_is_normal_not_error(db_path):
    from tools.jobs_store import list_jobs

    result = list_jobs()

    assert result.success is True
    assert result.count == 0


def test_save_job_analysis_never_raises_on_bad_score_type(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(
        url="https://a.com",
        title="A",
        company="A",
        country="US",
        score="not-a-valid-int-string",  # type: ignore[arg-type]
    )

    assert result.success is False


# ---------------------------------------------------------------------------
# ApplicationStatus enum — unchanged 7 values
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_application_status_has_exactly_seven_values():
    from tools.jobs_store import ApplicationStatus

    values = {member.value for member in ApplicationStatus}
    assert values == {
        "not_applied",
        "applied",
        "interviewing",
        "offer",
        "rejected",
        "withdrawn",
        "ghosted",
    }


# ---------------------------------------------------------------------------
# D10 advisory: possible_duplicate_id on a URL-less insert matching an
# existing job's (company, title) — non-blocking, per design's "the user has
# the id to reconcile them" reasoning. Not gated by an explicit spec SC, but
# implemented per design D10 and tested here rather than left unverified.
# ---------------------------------------------------------------------------


def test_url_less_insert_matching_company_and_title_flags_possible_duplicate(db_path):
    from tools.jobs_store import save_job_analysis

    first = save_job_analysis(
        url="https://example.com/job/1", title="SWE", company="Acme", country="USA"
    )

    result = save_job_analysis(
        custom_title="Referral for the same role",
        title="SWE",
        company="Acme",
        country="USA",
    )

    assert result.success is True  # advisory only — never blocks
    assert result.possible_duplicate_id == first.id


def test_url_less_insert_with_no_match_has_no_possible_duplicate(db_path):
    from tools.jobs_store import save_job_analysis

    result = save_job_analysis(
        custom_title="Unique referral", title="SWE", company="Acme", country="USA"
    )

    assert result.success is True
    assert result.possible_duplicate_id is None


# ---------------------------------------------------------------------------
# 2.6f: extra="forbid" still bites — an undeclared table column raises at
# first read rather than being silently dropped.
# ---------------------------------------------------------------------------


def test_undeclared_column_on_jobs_raises_at_first_read(db_path):
    from tools._db import connect
    from tools.jobs_store import list_jobs

    with connect(db_path, write=True) as conn:
        conn.execute("ALTER TABLE jobs ADD COLUMN unexpected_future_column TEXT")
        conn.execute(
            "INSERT INTO jobs (id, url, custom_title, title, company, country, "
            "status, score, recommendation, notes, analyzed_at, "
            "unexpected_future_column) VALUES "
            "('J1', 'https://ex.com/1', NULL, 'SWE', 'Acme', 'USA', "
            "'not_applied', NULL, NULL, NULL, '2025-01-01T00:00:00+00:00', "
            "'surprise')"
        )

    result = list_jobs()

    assert result.success is False
    assert result.error_message is not None


# ---------------------------------------------------------------------------
# SC-22 (partial — full round-trip needs get_job, deferred to PR3a):
# list_jobs -> list_resume_versions(job_id=...) -> get_resume_version chain.
# ---------------------------------------------------------------------------


def test_sc22_partial_cross_conversation_recall_without_get_job(db_path):
    from tools.jobs_store import save_job_analysis, set_application_status, list_jobs
    from tools.resumes import (
        save_resume_version,
        list_resume_versions,
        get_resume_version,
    )

    job = save_job_analysis(
        url="https://example.com/datadog", title="SWE", company="Datadog", country="US"
    )
    set_application_status(id=job.id, status="interviewing")
    version = save_resume_version(
        content="Datadog-tailored resume text",
        label="Datadog — infra focus",
        parent_id=None,
        job_id=job.id,
    )

    found_jobs = list_jobs(status="interviewing")
    assert found_jobs.count == 1
    assert found_jobs.jobs[0].id == job.id

    linked_versions = list_resume_versions(job_id=job.id)
    assert linked_versions.count == 1
    assert linked_versions.versions[0].id == version.id
    assert linked_versions.versions[0].label == "Datadog — infra focus"

    fetched = get_resume_version(id=version.id)
    assert fetched.version.content == "Datadog-tailored resume text"


# ---------------------------------------------------------------------------
# get_job (D6, tasks 3a.1a-3a.1g/3a.1j/3a.1k) — REQUIRED new tool. Without
# it jd_text is write-only, reachable through no read path.
# ---------------------------------------------------------------------------


def test_sc11_jd_text_stored_and_retrievable_via_get_job(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        url="https://ex.com/1",
        title="SWE",
        company="Acme",
        country="USA",
        jd_text="Full pasted description...\nwith newlines",
    )

    result = get_job(id=saved.id, include_description=True)

    assert result.success is True
    assert result.job.id == saved.id
    assert result.description == "Full pasted description...\nwith newlines"
    assert result.has_description is True


def test_sc12_get_job_unknown_id_returns_not_found(db_path):
    from tools.jobs_store import get_job

    result = get_job(id="ghost")

    assert result.success is False
    assert result.error == "not_found"


def test_sc13_get_job_on_corrupt_store_returns_corrupt_not_not_found(db_path):
    """A naive implementation that catches broadly and reports "not_found"
    would wrongly suggest the id is simply wrong, when the whole store is
    broken."""
    from tools.jobs_store import get_job

    db_path.write_bytes(b"not a database")

    result = get_job(id="J1")

    assert result.success is False
    assert result.error == "corrupt"


def test_sc14_jd_text_omitted_on_save_leaves_get_job_description_none(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        url="https://ex.com/1", title="SWE", company="Acme", country="USA"
    )

    result = get_job(id=saved.id, include_description=True)

    assert result.success is True
    assert result.description is None
    assert result.has_description is False


def test_sc16_get_job_is_the_only_path_that_returns_jd_text(db_path):
    """SC-15/SC-16 pairing: list_jobs excludes it (proven elsewhere), get_job
    is the sole read path — and only when include_description=True."""
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        url="https://ex.com/1",
        title="SWE",
        company="Acme",
        country="USA",
        jd_text="a" * 5000,
    )

    default_call = get_job(id=saved.id)
    assert default_call.description is None  # include_description defaults False
    assert default_call.has_description is True  # but the affordance is visible

    explicit_call = get_job(id=saved.id, include_description=True)
    assert explicit_call.description == "a" * 5000


def test_get_job_by_url(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        url="https://ex.com/1", title="SWE", company="Acme", country="USA"
    )

    result = get_job(url="https://ex.com/1")

    assert result.success is True
    assert result.job.id == saved.id


def test_get_job_neither_id_nor_url_is_invalid_input(db_path):
    from tools.jobs_store import get_job

    result = get_job()

    assert result.success is False
    assert result.error == "invalid_input"


def test_get_job_both_id_and_url_is_invalid_input(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        url="https://ex.com/1", title="SWE", company="Acme", country="USA"
    )

    result = get_job(id=saved.id, url="https://ex.com/1")

    assert result.success is False
    assert result.error == "invalid_input"


def test_get_job_returns_linked_resume_version_summaries(db_path):
    """D6: the linked resume summaries ARE the headline query — 'did I apply
    to X?' -> 'yes, and with this resume.' Summaries, not content."""
    from tools.jobs_store import save_job_analysis, get_job
    from tools.resumes import save_resume_version

    job = save_job_analysis(
        url="https://ex.com/1", title="SWE", company="Acme", country="USA"
    )
    version = save_resume_version(
        content="tailored text",
        label="Tailored for Acme",
        parent_id=None,
        job_id=job.id,
    )

    result = get_job(id=job.id)

    assert result.success is True
    assert len(result.resume_versions) == 1
    assert result.resume_versions[0].id == version.id
    assert result.resume_versions[0].label == "Tailored for Acme"
    # summary, not the full text
    assert not hasattr(result.resume_versions[0], "content")


def test_sc22_full_cross_conversation_recall_via_get_job(db_path):
    """SC-22: completes the round-trip from the PR2 partial test — this time
    through get_job rather than list_resume_versions alone."""
    from tools.jobs_store import save_job_analysis, set_application_status, get_job
    from tools.resumes import save_resume_version, get_resume_version

    job = save_job_analysis(
        url="https://example.com/datadog", title="SWE", company="Datadog", country="US"
    )
    set_application_status(id=job.id, status="interviewing")
    version = save_resume_version(
        content="Datadog-tailored resume text",
        label="Datadog — infra focus",
        parent_id=None,
        job_id=job.id,
    )

    found = get_job(id=job.id)

    assert found.success is True
    assert found.job.status == "interviewing"
    assert len(found.resume_versions) == 1
    assert found.resume_versions[0].id == version.id
    assert found.resume_versions[0].label == "Datadog — infra focus"

    fetched = get_resume_version(id=version.id)
    assert fetched.version.content == "Datadog-tailored resume text"


@pytest.mark.contract
def test_get_job_result_has_no_visa_field_and_has_description_always_present():
    from tools.jobs_store import GetJobResult

    result = GetJobResult(success=True)
    assert "has_description" in GetJobResult.model_fields
    assert result.has_description is False  # always-present, defaults False
    assert "visa" not in GetJobResult.model_fields


# ---------------------------------------------------------------------------
# get_job's custom_title lookup (findings 1/2) — a job saved without a url
# is findable ONLY by custom_title; without this, that promise had no
# retrieval path.
# ---------------------------------------------------------------------------


def test_get_job_by_custom_title_single_match(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        custom_title="Acme referral", title="SWE", company="Acme", country="USA"
    )

    result = get_job(custom_title="Acme referral")

    assert result.success is True
    assert result.job.id == saved.id


def test_get_job_by_custom_title_no_match_is_not_found(db_path):
    from tools.jobs_store import get_job

    result = get_job(custom_title="Nonexistent")

    assert result.success is False
    assert result.error == "not_found"


def test_get_job_by_custom_title_ambiguous_when_multiple_jobs_share_it(db_path):
    """custom_title is NOT unique (R3/SC-08) — a lookup matching more than
    one job must say so explicitly, not silently return the first."""
    from tools.jobs_store import save_job_analysis, get_job

    j1 = save_job_analysis(
        custom_title="Acme referral", title="SWE 1", company="Acme", country="USA"
    )
    j2 = save_job_analysis(
        custom_title="Acme referral", title="SWE 2", company="Acme", country="USA"
    )

    result = get_job(custom_title="Acme referral")

    assert result.success is False
    assert result.error == "ambiguous"
    assert j1.id in result.message
    assert j2.id in result.message


def test_get_job_neither_id_url_nor_custom_title_is_invalid_input(db_path):
    from tools.jobs_store import get_job

    result = get_job()

    assert result.success is False
    assert result.error == "invalid_input"


def test_get_job_id_and_custom_title_together_is_invalid_input(db_path):
    from tools.jobs_store import save_job_analysis, get_job

    saved = save_job_analysis(
        custom_title="Acme referral", title="SWE", company="Acme", country="USA"
    )

    result = get_job(id=saved.id, custom_title="Acme referral")

    assert result.success is False
    assert result.error == "invalid_input"


# ---------------------------------------------------------------------------
# Finding 5: get_job's linked-resume-summaries query must not be `SELECT *`
# — that would pull every version's full `content` even on the default
# include_description=False path, to build a summary that discards it.
# ---------------------------------------------------------------------------


def test_get_job_resume_summaries_query_is_column_scoped_not_select_star(
    db_path, monkeypatch
):
    """sqlite3.Connection is a C-extension type and cannot be monkeypatched
    directly (its methods are read-only), so this spies via a thin proxy
    wrapped around the real connection `get_job` actually uses."""
    from contextlib import contextmanager

    import tools.jobs_store as jobs_store_mod
    from tools.jobs_store import save_job_analysis, get_job
    from tools.resumes import save_resume_version

    job = save_job_analysis(
        url="https://ex.com/1", title="SWE", company="Acme", country="USA"
    )
    save_resume_version(
        content="x" * 10_000, label="Tailored", parent_id=None, job_id=job.id
    )

    executed_sql: list[str] = []

    class _SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            executed_sql.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    original_connect = jobs_store_mod.connect

    @contextmanager
    def _spy_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as conn:
            yield _SpyConn(conn)

    monkeypatch.setattr(jobs_store_mod, "connect", _spy_connect)

    get_job(id=job.id)

    resume_query = next(
        sql for sql in executed_sql if "resume_versions" in sql and "job_id = ?" in sql
    )
    assert "SELECT *" not in resume_query


def test_list_jobs_returns_an_envelope_when_a_statement_fails_mid_read():
    """A raw sqlite3.Error from the caller-phase must not escape the tool.

    connect() deliberately translates only its own setup phase, so a
    database whose sqlite_master page is intact but whose `jobs` table has a
    corrupt page passes the schema check and then raises on the SELECT —
    not a ValueError, so `except ValueError` alone let it crash through
    "This tool NEVER raises".
    """
    import sqlite3
    from contextlib import contextmanager

    import pytest

    import tools.jobs_store as js

    @contextmanager
    def _corrupt_page(path=None, *, write=False):
        class _Conn:
            def execute(self, *args, **kwargs):
                raise sqlite3.DatabaseError("database disk image is malformed")

        yield _Conn()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(js, "connect", _corrupt_page)
        result = js.list_jobs()

    assert result.success is False
    assert "malformed" in result.error_message


# ---------------------------------------------------------------------------
# status_notes: the analysis survives the whole application lifecycle
#
# Regression guard for the data-loss bug found in the 2026-09-17 end-to-end
# run: `jobs.notes` served two incompatible purposes (score reasoning AND
# follow-up notes), so recording "applied on the 17th" silently destroyed the
# reasoning behind the score. The two are now separate columns, and
# set_application_status never mentions `notes` in its UPDATE at all.
# ---------------------------------------------------------------------------


_ANALYSIS = "Strong match. Python, agents, MCP. Gaps: Docker, AWS."


def _job_with_analysis():
    from tools.jobs_store import save_job_analysis

    return save_job_analysis(
        url="https://ex.com/ai-eng",
        title="AI Engineer",
        company="Acme",
        country="Peru",
        score=78,
        recommendation="APPLY",
        notes=_ANALYSIS,
    )


def test_status_note_does_not_touch_analysis_notes(db_path):
    """THE regression. A follow-up note must never overwrite the analysis."""
    from tools.jobs_store import get_job, set_application_status

    created = _job_with_analysis()
    set_application_status(id=created.id, status="applied", notes="sent the v2 resume")

    job = get_job(id=created.id).job
    assert job.notes == _ANALYSIS


def test_status_note_appends_to_status_notes_with_date_and_status(db_path):
    import re

    from tools.jobs_store import get_job, set_application_status

    created = _job_with_analysis()
    set_application_status(id=created.id, status="applied", notes="sent the v2 resume")

    job = get_job(id=created.id).job
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2}\] applied — sent the v2 resume", job.status_notes
    )


def test_two_status_changes_accumulate_in_status_notes(db_path):
    from tools.jobs_store import get_job, set_application_status

    created = _job_with_analysis()
    set_application_status(id=created.id, status="applied", notes="sent the v2 resume")
    set_application_status(id=created.id, status="rejected", notes="phone call")

    job = get_job(id=created.id).job
    lines = job.status_notes.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("applied — sent the v2 resume")
    assert lines[1].endswith("rejected — phone call")
    assert job.notes == _ANALYSIS


def test_status_change_without_notes_leaves_status_notes_untouched(db_path):
    from tools.jobs_store import get_job, set_application_status

    created = _job_with_analysis()
    set_application_status(id=created.id, status="applied", notes="sent the v2 resume")
    before = get_job(id=created.id).job.status_notes

    set_application_status(id=created.id, status="interviewing")

    job = get_job(id=created.id).job
    assert job.status_notes == before
    assert job.status == "interviewing"


def test_set_application_status_echoes_the_resulting_timeline(db_path):
    from tools.jobs_store import set_application_status

    created = _job_with_analysis()
    result = set_application_status(id=created.id, status="applied", notes="sent it")

    assert result.success is True
    assert result.status_notes.endswith("applied — sent it")


def test_list_jobs_exposes_status_notes(db_path):
    from tools.jobs_store import list_jobs, set_application_status

    created = _job_with_analysis()
    set_application_status(id=created.id, status="applied", notes="sent it")

    record = list_jobs().jobs[0]
    assert record.notes == _ANALYSIS
    assert record.status_notes.endswith("applied — sent it")


# ---------------------------------------------------------------------------
# delete_job: the only destructive, id-targeted tool in the surface
# ---------------------------------------------------------------------------


def test_delete_job_by_id_removes_the_record(db_path):
    from tools.jobs_store import delete_job, get_job, list_jobs

    created = _job_with_analysis()
    result = delete_job(id=created.id)

    assert result.success is True
    assert result.id == created.id
    assert result.title == "AI Engineer"
    assert result.company == "Acme"
    assert list_jobs().count == 0
    assert get_job(id=created.id).error == "not_found"


def test_delete_job_by_url_removes_the_record(db_path):
    from tools.jobs_store import delete_job, list_jobs

    _job_with_analysis()
    result = delete_job(url="https://ex.com/ai-eng")

    assert result.success is True
    assert list_jobs().count == 0


def test_delete_job_without_id_or_url_returns_invalid_input(db_path):
    from tools.jobs_store import delete_job, list_jobs

    _job_with_analysis()
    result = delete_job()

    assert result.success is False
    assert result.error == "invalid_input"
    assert list_jobs().count == 1


def test_delete_job_unknown_id_returns_not_found(db_path):
    from tools.jobs_store import delete_job

    result = delete_job(id="0" * 32)

    assert result.success is False
    assert result.error == "not_found"


def test_delete_job_removes_the_linked_job_description(db_path):
    from tools.jobs_store import delete_job, save_job_analysis

    created = save_job_analysis(
        url="https://ex.com/jd",
        title="AI Engineer",
        company="Acme",
        country="Peru",
        jd_text="full posting text",
    )
    result = delete_job(id=created.id)

    assert result.success is True
    assert result.deleted_description is True


def test_delete_job_reports_when_there_was_no_description(db_path):
    from tools.jobs_store import delete_job

    created = _job_with_analysis()
    result = delete_job(id=created.id)

    assert result.deleted_description is False


def test_delete_job_unlinks_resume_versions_and_preserves_them_verbatim(db_path):
    """The append-only promise holds: the CV survives, only the pointer dies."""
    from tools.jobs_store import delete_job
    from tools.resumes import get_resume_version, save_resume_version

    created = _job_with_analysis()
    base = save_resume_version(content="base text", label="Base")
    tailored = save_resume_version(
        content="tailored text",
        label="Tailored",
        parent_id=base.id,
        job_id=created.id,
    )

    result = delete_job(id=created.id)

    assert result.success is True
    assert result.unlinked_resume_versions == [tailored.id]

    survivor = get_resume_version(id=tailored.id).version
    assert survivor.content == "tailored text"
    assert survivor.label == "Tailored"
    assert survivor.parent_id == base.id
    assert survivor.job_id is None


def test_delete_job_receipt_names_every_unlinked_version(db_path):
    from tools.jobs_store import delete_job
    from tools.resumes import save_resume_version

    created = _job_with_analysis()
    base = save_resume_version(content="base", label="Base")
    v1 = save_resume_version(
        content="v1", label="V1", parent_id=base.id, job_id=created.id
    )
    v2 = save_resume_version(
        content="v2", label="V2", parent_id=v1.id, job_id=created.id
    )

    result = delete_job(id=created.id)

    assert set(result.unlinked_resume_versions) == {v1.id, v2.id}


def test_delete_job_does_not_accept_custom_title(db_path):
    """custom_title is NOT unique — deleting by an ambiguous key is how you
    delete the wrong record. id and url only, unlike get_job."""
    import inspect

    from tools.jobs_store import delete_job

    assert "custom_title" not in inspect.signature(delete_job).parameters
