"""End-to-end lifecycle tests: the whole job hunt, through the public tools.

Every other test file checks one tool against one behaviour. This one walks a
job from "pasted a posting" to "deleted the record", calling the tools in the
order a real session calls them, because the two bugs this file was written
for were both invisible at the unit level:

- Recording a status wiped the analysis. Each tool was individually correct;
  the damage only appeared when one ran after the other.
- Deleting a job had no path at all, and the first attempt at one would have
  tripped an ON DELETE RESTRICT that no single-tool test ever reaches.

These are marked `integration` and hit real SQLite through the same `db_path`
fixture as everything else — no mocks, because what is being tested is
precisely the interaction the mocks would paper over.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration


ANALYSIS = (
    "Score 78. Matched: Python, agent design with production evidence, REST "
    "APIs, PostgreSQL. Missing: Docker, explicit AWS, pipeline observability. "
    "The wall is the 3-5 years of experience; the domain itself is a fit."
)


def _full_cycle_up_to_applied(db_path):
    """Analyze -> save -> tailor -> apply. Returns (job, base, tailored)."""
    from tools.jobs_store import save_job_analysis, set_application_status
    from tools.resumes import save_resume_version
    from tools.work_auth import set_work_authorization

    set_work_authorization(countries=["Peru", "Mexico"])

    job = save_job_analysis(
        url="https://boards.example.com/ai-engineer",
        title="AI Engineer — Agentic Systems",
        company="Yape",
        country="Peru",
        score=78,
        recommendation="APPLY",
        notes=ANALYSIS,
        jd_text="Python, LLM APIs, agent frameworks, REST, SQL. 0-2 years.",
    )
    base = save_resume_version(content="general resume text", label="Base")
    tailored = save_resume_version(
        content="tailored resume text",
        label="Tailored — Yape",
        parent_id=base.id,
        job_id=job.id,
    )
    set_application_status(
        id=job.id, status="applied", notes=f"sent version {tailored.id[:8]}"
    )
    return job, base, tailored


def test_the_analysis_survives_the_entire_application_lifecycle(db_path):
    """THE regression this release exists for.

    A job is analyzed, applied to, interviewed for and rejected. At the end,
    the reasoning behind the score must read exactly as it was written — and
    the follow-up notes must all still be there too. Losing either one is the
    bug; the point is that you never have to choose.
    """
    from tools.jobs_store import get_job, set_application_status

    job, _, _ = _full_cycle_up_to_applied(db_path)

    set_application_status(id=job.id, status="interviewing", notes="screen, 30 min")
    set_application_status(id=job.id, status="rejected", notes="call, no reason given")

    stored = get_job(id=job.id).job

    assert stored.notes == ANALYSIS
    assert stored.status == "rejected"

    timeline = stored.status_notes.splitlines()
    assert len(timeline) == 3
    assert "applied — sent version" in timeline[0]
    assert timeline[1].endswith("interviewing — screen, 30 min")
    assert timeline[2].endswith("rejected — call, no reason given")
    for line in timeline:
        assert re.match(r"^\[\d{4}-\d{2}-\d{2}\] ", line)


def test_a_bare_status_change_records_nothing_in_the_timeline(db_path):
    """Advancing state without a note must not litter the log."""
    from tools.jobs_store import get_job, set_application_status

    job, _, _ = _full_cycle_up_to_applied(db_path)
    before = get_job(id=job.id).job.status_notes

    set_application_status(id=job.id, status="offer")
    stored = get_job(id=job.id).job

    assert stored.status_notes == before
    assert stored.status == "offer"
    assert stored.notes == ANALYSIS


def test_deleting_a_job_keeps_every_resume_it_was_tailored_for(db_path):
    """The append-only promise outlives the posting.

    A resume version records a document actually sent to a company. The job
    row is a record of a posting. Deleting the second must never destroy the
    first — only the pointer between them.
    """
    from tools.jobs_store import delete_job, get_job, list_jobs
    from tools.resumes import get_resume_version, list_resume_versions

    job, base, tailored = _full_cycle_up_to_applied(db_path)
    content_before = get_resume_version(id=tailored.id).version.content

    receipt = delete_job(id=job.id)

    assert receipt.success is True
    assert receipt.title == "AI Engineer — Agentic Systems"
    assert receipt.company == "Yape"
    assert receipt.deleted_description is True
    assert receipt.unlinked_resume_versions == [tailored.id]

    # The job and its posting are gone.
    assert get_job(id=job.id).error == "not_found"
    assert list_jobs().count == 0

    # Both resume versions are not.
    survivor = get_resume_version(id=tailored.id).version
    assert survivor.content == content_before
    assert survivor.label == "Tailored — Yape"
    assert survivor.parent_id == base.id
    assert survivor.job_id is None
    assert list_resume_versions().count == 2


def test_deleting_a_job_leaves_the_rest_of_the_store_alone(db_path):
    """Neighbouring jobs and their links are untouched by a delete."""
    from tools.jobs_store import delete_job, list_jobs, save_job_analysis
    from tools.resumes import list_resume_versions, save_resume_version

    job, base, tailored = _full_cycle_up_to_applied(db_path)

    keeper = save_job_analysis(
        url="https://boards.example.com/backend",
        title="Backend Engineer",
        company="Kavak",
        country="Mexico",
        score=64,
        recommendation="CONSIDER",
        notes="different analysis entirely",
    )
    keeper_resume = save_resume_version(
        content="kavak resume",
        label="Tailored — Kavak",
        parent_id=tailored.id,
        job_id=keeper.id,
    )

    delete_job(id=job.id)

    remaining = list_jobs().jobs
    assert [j.id for j in remaining] == [keeper.id]
    assert remaining[0].notes == "different analysis entirely"

    # The surviving job keeps its link; only the deleted job's was severed.
    still_linked = list_resume_versions(job_id=keeper.id).versions
    assert [v.id for v in still_linked] == [keeper_resume.id]


def test_a_deleted_job_frees_its_url_for_reuse(db_path):
    """`url` is UNIQUE. If delete left the row behind, re-saving would fail —
    which is how you find out a delete did not really delete."""
    from tools.jobs_store import delete_job, save_job_analysis

    job, _, _ = _full_cycle_up_to_applied(db_path)
    delete_job(id=job.id)

    again = save_job_analysis(
        url="https://boards.example.com/ai-engineer",
        title="AI Engineer — Agentic Systems",
        company="Yape",
        country="Peru",
    )

    assert again.success is True
    assert again.updated is False  # a genuinely new row, not an upsert
    assert again.id != job.id


def test_work_authorization_warning_tracks_the_live_declaration(db_path):
    """The check is evaluated per call, never stored as an aging verdict."""
    from tools.analyze import analyze_job
    from tools.resumes import save_resume_version
    from tools.work_auth import set_work_authorization

    save_resume_version(content="general resume text", label="Base")
    set_work_authorization(countries=["Peru", "Mexico"])

    assert (
        analyze_job(
            title="AI Engineer", company="Yape", country="Peru"
        ).work_authorization.status
        == "authorized"
    )

    warned = analyze_job(title="MLE", company="Stripe", country="United States")
    assert warned.work_authorization.status == "warned"
    assert "United States" in warned.work_authorization.warning

    # Declaring a new set REPLACES it — the US job now passes, Peru does not.
    set_work_authorization(countries=["United States"])

    assert (
        analyze_job(
            title="MLE", company="Stripe", country="United States"
        ).work_authorization.status
        == "authorized"
    )
    assert (
        analyze_job(
            title="AI Engineer", company="Yape", country="Peru"
        ).work_authorization.status
        == "warned"
    )
