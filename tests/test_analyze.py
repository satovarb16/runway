"""Tests for tools/analyze.py — analyze_job orchestrator.

FINAL 0.3.0 contract (sqlite-memory-and-pasted-jd, design D5, tasks 3a.2a-q):
analyze_job receives Claude-extracted fields (title, company, country,
optional url/custom_title) and NEVER the raw JD text — the text is already
in Claude's context, so passing it as a tool argument would pay for it
twice. The envelope echoes the extracted fields back as `extracted`: that
echo is the ONLY visibility the user gets into a bad extraction now that
every parser providing a floor of truth is gone.

analyze_job is `def`, not `async def` — it has zero awaits, and its only
I/O (SQLite) is blocking, so an async tool would block FastMCP's event
loop. `work_authorization` behavior (the precondition and the live
warning) is PR3b's — out of scope here (see tools/work_auth.py, not yet
created).
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_base_resume(label="Base", content="Jane Doe — Python engineer"):
    from tools.resumes import save_resume_version

    return save_resume_version(content=content, label=label, parent_id=None)


def _declare_work_auth(countries=("USA",)):
    from tools.work_auth import set_work_authorization

    return set_work_authorization(countries=list(countries))


# ---------------------------------------------------------------------------
# 3a.2a: analyze_job is `def`, not `async def`
# ---------------------------------------------------------------------------


class TestSyncNotAsync:
    def test_analyze_job_is_not_a_coroutine_function(self):
        """3a.2a (must-not-lose #2): analyze_job has zero awaits and its
        only I/O (sqlite) is blocking — an async def would block FastMCP's
        event loop on every store read."""
        from tools.analyze import analyze_job

        assert not inspect.iscoroutinefunction(analyze_job)

    def test_calling_analyze_job_returns_a_result_directly_no_await_needed(
        self, db_path
    ):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")
        assert result.error is None

    def test_3a2k_no_other_registered_tool_is_async_def(self):
        """3a.2k: confirms it is safe to drop pytest-asyncio/asyncio_mode —
        analyze_job was the last async tool."""
        import server

        for tool_name, tool in server.mcp._tool_manager._tools.items():
            fn = tool.fn
            assert not inspect.iscoroutinefunction(fn), (
                f"{tool_name} is still async def"
            )


# ---------------------------------------------------------------------------
# Pydantic models — final contract shape
# ---------------------------------------------------------------------------


class TestPydanticModels:
    def test_scoring_guide_fields(self):
        from tools.analyze import ScoringGuide

        guide = ScoringGuide(instructions="do it", recommendation_rules=["a", "b"])
        assert guide.instructions == "do it"
        assert guide.recommendation_rules == ["a", "b"]

    def test_analyze_job_result_all_optional(self):
        from tools.analyze import AnalyzeJobResult

        result = AnalyzeJobResult()
        assert result.resume is None
        assert result.scoring_guide is None
        assert result.extracted is None
        assert result.error is None
        assert result.message is None

    def test_analyze_job_result_has_no_job_field(self):
        from tools.analyze import AnalyzeJobResult

        assert "job" not in AnalyzeJobResult.model_fields

    def test_analyze_job_result_has_no_visa_field(self):
        """SC-37: no `visa` field or anything resembling the deleted
        verdict shape."""
        from tools.analyze import AnalyzeJobResult

        assert "visa" not in AnalyzeJobResult.model_fields

    @pytest.mark.contract
    def test_3a2n_analyze_job_result_has_extracted_field(self):
        """3a.2n (contract): AnalyzeJobResult.extracted exists.
        `.work_authorization` behavior is PR3b's — not asserted here."""
        from tools.analyze import AnalyzeJobResult

        assert "extracted" in AnalyzeJobResult.model_fields

    @pytest.mark.contract
    def test_3b1n_analyze_job_result_has_work_authorization_field(self):
        """3b.1n (completes 3a.2n): the field itself did not exist on
        AnalyzeJobResult until PR3b."""
        from tools.analyze import AnalyzeJobResult

        assert "work_authorization" in AnalyzeJobResult.model_fields

    def test_analyze_job_result_no_match_or_recommendation_field(self):
        from tools.analyze import AnalyzeJobResult

        result = AnalyzeJobResult()
        assert not hasattr(result, "match"), "Server must NOT compute a match"
        assert not hasattr(result, "recommendation"), (
            "Server must NOT compute a recommendation"
        )


# ---------------------------------------------------------------------------
# T-02: Scoring guide — unchanged content, still mentions the save ordering
# (3a.2p/3a.2q, must-not-lose #5): save_job_analysis must be called BEFORE
# save_resume_version now that job_id is a real foreign key.
# ---------------------------------------------------------------------------


class TestScoringGuide:
    def test_scoring_guide_has_recommendation_rules(self):
        from tools.analyze import _scoring_guide

        guide = _scoring_guide()
        assert len(guide.recommendation_rules) >= 1

    def test_scoring_guide_instructions_mention_score(self):
        from tools.analyze import _scoring_guide

        guide = _scoring_guide()
        assert "score" in guide.instructions.lower()

    def test_scoring_guide_no_longer_references_visa(self):
        from tools.analyze import _scoring_guide

        guide = _scoring_guide()
        combined = " ".join(guide.recommendation_rules).lower()
        assert "visa" not in combined
        assert "visa" not in guide.instructions.lower()

    def test_sc18_scoring_guide_documents_save_job_analysis_ordering(self):
        """3a.2p/q: the ordering (save_job_analysis before save_resume_version)
        must be documented, or Claude will do it in the old order and hit
        save_resume_version's job_not_found error."""
        from tools.analyze import _scoring_guide

        guide = _scoring_guide()
        assert "save_job_analysis" in guide.instructions
        assert "save_resume_version" in guide.instructions


# ---------------------------------------------------------------------------
# R15/SC-33/SC-34: analyze_job receives extracted fields; it does NOT accept
# jd_text at all — not merely ignores it.
# ---------------------------------------------------------------------------


class TestNoJdTextParameter:
    def test_sc33_signature_has_no_jd_text_parameter(self):
        from tools.analyze import analyze_job

        sig = inspect.signature(analyze_job)
        assert "jd_text" not in sig.parameters

    def test_sc33_call_with_title_company_country_succeeds(self, db_path):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error is None

    def test_analyze_job_declares_no_jd_text_parameter(self, db_path):
        """The signature has no jd_text, so a direct Python call rejects it.

        This pins the SIGNATURE, and nothing more. SC-34 originally claimed
        analyze_job "rejects" an unexpected jd_text, and this test was read as
        proof of that. It is not: over the MCP wire the argument never reaches
        the function at all — FastMCP validates a call against the tool's
        advertised JSON schema and drops unrecognised fields before dispatch,
        so there is nothing left to reject. Verified against a live stdio
        server; see test_wire_contract.py, which pins what the transport
        actually does guarantee.

        SC-34 was withdrawn from the spec for that reason: a scenario that
        cannot hold where callers actually live is worse than no scenario,
        because it reads as a promise.
        """
        _save_base_resume()
        from tools.analyze import analyze_job

        with pytest.raises(TypeError):
            analyze_job(title="SWE", company="Acme", country="USA", jd_text="some text")


# ---------------------------------------------------------------------------
# R16/SC-35, SC-36, SC-37: output contract
# ---------------------------------------------------------------------------


class TestOutputContract:
    def test_sc35_extracted_echoes_input_fields_verbatim(self, db_path):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(
            title="Sr. Backend Engineer", company="Acme Corp", country="Germany"
        )

        assert result.extracted.title == "Sr. Backend Engineer"
        assert result.extracted.company == "Acme Corp"
        assert result.extracted.country == "Germany"

    def test_sc36_no_serverside_validation_implausible_country_accepted(self, db_path):
        """This documents the accepted risk from D2 as observable behavior,
        not merely narrative — no plausibility check on country/title/company."""
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="Nowhereland")

        assert result.error is None
        assert result.extracted.country == "Nowhereland"

    def test_sc37_envelope_has_resume_scoring_guide_extracted_and_nothing_else(
        self, db_path
    ):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")
        dumped = result.model_dump()

        assert "job" not in dumped
        assert "visa" not in dumped
        assert result.resume is not None
        assert result.scoring_guide is not None
        assert result.extracted is not None

    def test_extracted_url_and_custom_title_default_to_none_when_omitted(self, db_path):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.extracted.url is None
        assert result.extracted.custom_title is None

    def test_extracted_echoes_url_and_custom_title_when_given(self, db_path):
        from tools.jobs_store import save_job_analysis

        save_job_analysis(
            url="https://example.com/job/123", title="X", company="Y", country="Z"
        )
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(
            title="SWE",
            company="Acme",
            country="USA",
            url="https://example.com/job/123",
            custom_title="My referral",
        )

        assert result.extracted.url == "https://example.com/job/123"
        assert result.extracted.custom_title == "My referral"


# ---------------------------------------------------------------------------
# R18/SC-39: analyze_job persists nothing.
# ---------------------------------------------------------------------------


class TestNoPersistence:
    def test_sc39_analyzing_without_saving_leaves_no_trace(self, db_path):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job
        from tools.jobs_store import list_jobs

        result = analyze_job(title="SWE", company="Acme", country="USA")
        assert result.error is None

        listed = list_jobs()
        assert listed.count == 0


# ---------------------------------------------------------------------------
# `notice` — when url is None, the user needs to be told the custom_title
# is the only handle to find this record later (D5's surviving obligation).
# ---------------------------------------------------------------------------


class TestNotice:
    def test_notice_present_when_url_is_none(self, db_path):
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error is None
        assert result.notice is not None

    def test_notice_absent_when_url_is_given(self, db_path):
        from tools.jobs_store import save_job_analysis

        save_job_analysis(
            url="https://example.com/job/123", title="X", company="Y", country="Z"
        )
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(
            title="SWE",
            company="Acme",
            country="USA",
            url="https://example.com/job/123",
        )

        assert result.error is None
        assert result.notice is None

    def test_notice_absent_when_custom_title_is_given_even_without_url(self, db_path):
        """Finding 4: a caller who already agreed a custom_title with the
        user has already satisfied the notice's purpose — repeating it
        would send them through a redundant round-trip."""
        _save_base_resume()
        _declare_work_auth()
        from tools.analyze import analyze_job

        result = analyze_job(
            title="SWE", company="Acme", country="USA", custom_title="Acme referral"
        )

        assert result.error is None
        assert result.notice is None


# ---------------------------------------------------------------------------
# T-04: Resume precondition — Guard 2 (no_resume vs corrupt). Observable
# behavior UNCHANGED from PR2's intermediate call site; now exercised
# through the final D5 contract.
# ---------------------------------------------------------------------------


class TestResumePrecondition:
    def test_sc38_empty_store_no_resume_error(self, db_path):
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error == "no_resume"
        assert result.message is not None
        assert result.resume is None
        assert result.scoring_guide is None

    def test_corrupt_store_is_not_reported_as_no_resume(self, db_path, monkeypatch):
        """Guard 2: a broken database must not be reported as "you have no
        resume yet". That advice sends the user to save_resume_version,
        which fails on the same database for the same reason."""
        import tools.analyze as analyze_mod

        def _boom(job_id=None, conn=None):
            raise ValueError("Resume store is corrupt: bad data")

        monkeypatch.setattr(analyze_mod, "_general_resume", _boom)

        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error == "corrupt"
        assert "corrupt" in result.message

    def test_no_resume_still_wins_when_the_work_auth_read_would_fail(
        self, db_path, monkeypatch
    ):
        """The no_resume check runs BEFORE the work-authorization read, not
        after both reads.

        Sharing one connection across the three reads is a performance
        change and must not become a behaviour change. Reading work auth
        eagerly alongside the resume — tidier, and what the first draft of
        the shared-connection refactor did — makes a database with a fine
        resume table and an unreadable work_authorizations table answer
        "corrupt" where it has always answered "no_resume".
        """
        import tools.analyze as analyze_mod

        def _boom(conn=None):
            raise ValueError("work_authorizations is corrupt: bad data")

        monkeypatch.setattr(analyze_mod, "_declared_authorizations", _boom)

        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error == "no_resume", (
            "the work-auth read ran before the no_resume check and its "
            "failure won the race"
        )

    def test_corrupt_job_lookup_also_reported_as_corrupt_not_no_resume(
        self, db_path, monkeypatch
    ):
        """Guard 2: the url->job_id resolution call is inside the same
        try/except as _general_resume — a corrupt database surfacing there
        must ALSO be reported as corrupt, not no_resume."""
        from tools.jobs_store import save_job_analysis

        save_job_analysis(
            url="https://example.com/job/123", title="X", company="Y", country="Z"
        )

        import tools.analyze as analyze_mod

        def _boom(url, conn=None):
            raise ValueError("Jobs store is corrupt: bad data")

        monkeypatch.setattr(analyze_mod, "_find_job_id_by_url", _boom)

        from tools.analyze import analyze_job

        result = analyze_job(
            title="SWE",
            company="Acme",
            country="USA",
            url="https://example.com/job/123",
        )

        assert result.error == "corrupt"
        assert "corrupt" in result.message

    def test_sc13_envelope_carries_general_resume_not_tailored_child(self, db_path):
        """With a base + a job-tailored child, the envelope carries the
        GENERAL resume, never the tailored child."""
        from tools.jobs_store import save_job_analysis
        from tools.resumes import save_resume_version

        job = save_job_analysis(
            url="https://example.com/job/123", title="X", company="Y", country="Z"
        )
        base = _save_base_resume()
        tailored_child = save_resume_version(
            content="tailored",
            label="Tailored for Acme",
            parent_id=base.id,
            job_id=job.id,
        )
        _declare_work_auth()

        from tools.analyze import analyze_job

        result = analyze_job(
            title="X",
            company="Y",
            country="Z",
            url="https://example.com/job/123",
        )

        assert result.error is None
        assert result.resume is not None
        assert result.resume.id == base.id
        assert result.resume.id != tailored_child.id


# ---------------------------------------------------------------------------
# R12-R14/SC-24, SC-25, SC-28, SC-29, SC-31, SC-32 — the work-authorization
# precondition wired into analyze_job's step order (task 3b.1m). Design D5/
# D7: resume precondition first (unchanged), work-authorization second,
# envelope-build third.
# ---------------------------------------------------------------------------


class TestWorkAuthorizationPrecondition:
    def test_sc24_no_work_authorization_declared_returns_error(self, db_path):
        _save_base_resume()
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="Germany")

        assert result.error == "no_work_authorization"
        assert result.message is not None
        assert "set_work_authorization" in result.message
        assert result.resume is None
        assert result.scoring_guide is None
        assert result.work_authorization is None

    def test_resume_precondition_wins_when_both_missing(self, db_path):
        """Explicit ordering decision (task brief): a first-time user with
        NEITHER a resume NOR a declared work authorization must get
        "no_resume", not "no_work_authorization" — resume precondition is
        checked first, unchanged from PR3a/design D5. Already implied by
        test_sc38_empty_store_no_resume_error (which never declares work
        authorization either); this test names the ordering decision
        explicitly so it cannot be silently reversed."""
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="Germany")

        assert result.error == "no_resume"

    def test_sc25_analyze_job_succeeds_once_work_authorization_is_set(self, db_path):
        _save_base_resume()
        _declare_work_auth(["USA"])
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error is None
        assert result.work_authorization is not None
        assert result.work_authorization.status == "authorized"

    def test_sc28_declared_country_produces_no_warning_in_envelope(self, db_path):
        _save_base_resume()
        _declare_work_auth(["USA"])
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error is None
        assert result.work_authorization.warning is None

    def test_sc29_undeclared_country_produces_warning_but_still_succeeds(self, db_path):
        """SC-29: the warning is additive information, not a blocking
        error — the call still succeeds."""
        _save_base_resume()
        _declare_work_auth(["USA"])
        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="Germany")

        assert result.error is None
        assert result.resume is not None
        assert result.scoring_guide is not None
        assert result.work_authorization.status == "warned"
        assert result.work_authorization.warning is not None

    def test_sc31_warning_recomputes_live_never_cached(self, db_path):
        """The comparison must recompute fresh against the CURRENT setting
        on every call — nothing about it is a stored property of the job."""
        _save_base_resume()
        _declare_work_auth(["Germany"])
        from tools.analyze import analyze_job

        first = analyze_job(title="SWE", company="Acme", country="Germany")
        assert first.work_authorization.status == "authorized"

        _declare_work_auth(["USA"])
        second = analyze_job(title="SWE", company="Acme", country="Germany")

        assert second.work_authorization.status == "warned"

    def test_sc32_corrupt_work_auth_store_reported_as_corrupt_not_unset(
        self, db_path, monkeypatch
    ):
        """Guard-2-style distinction extended to the new precondition: a
        broken work_authorizations read must not be reported as
        no_work_authorization, which would send the user to
        set_work_authorization only to have it fail identically."""
        _save_base_resume()
        import tools.analyze as analyze_mod

        def _boom(conn=None):
            raise ValueError("work_authorizations store is corrupt")

        monkeypatch.setattr(analyze_mod, "_declared_authorizations", _boom)

        from tools.analyze import analyze_job

        result = analyze_job(title="SWE", company="Acme", country="USA")

        assert result.error == "corrupt"
        assert result.error != "no_work_authorization"


# ---------------------------------------------------------------------------
# Guard 1, chain-wide constraint, FINAL FORM (task 3a.2m) — the anti-self-
# scoring guard must stay green through analyze_job's final D5 call site.
# Same guard as 2.5m/2.5n, now exercised with url as an OPTIONAL parameter.
# ---------------------------------------------------------------------------


class TestGuard1AntiSelfScoring:
    def test_3a2m_general_resume_fallback_refuses_a_resume_tailored_to_this_job(
        self, db_path
    ):
        """The store contains NOTHING untailored — the only version is
        tailored for the exact job being analyzed — so the correct outcome
        is no_resume, never handing back the tailored version."""
        from tools.jobs_store import save_job_analysis
        from tools.resumes import save_resume_version

        job = save_job_analysis(
            url="https://acme.com/job/1", title="X", company="Y", country="Z"
        )
        save_resume_version(
            content="tailored for acme",
            label="Tailored",
            parent_id=None,
            job_id=job.id,
        )

        from tools.analyze import analyze_job

        result = analyze_job(
            title="X", company="Y", country="Z", url="https://acme.com/job/1"
        )

        assert result.error == "no_resume"
        assert result.resume is None

    def test_3a2m_unsaved_url_resolves_job_id_none_general_still_excludes_tailored(
        self, db_path
    ):
        """Negative case: a url that matches NO saved job resolves to
        job_id=None — proving the None-only-when-no-match branch, not a
        blanket bypass."""
        from tools.jobs_store import save_job_analysis
        from tools.resumes import save_resume_version

        other_job = save_job_analysis(
            url="https://other.com/job/9", title="X", company="Y", country="Z"
        )
        base = _save_base_resume()
        save_resume_version(
            content="tailored for other job",
            label="Tailored for other",
            parent_id=base.id,
            job_id=other_job.id,
        )
        _declare_work_auth()

        from tools.analyze import analyze_job

        # This url matches no saved job at all.
        result = analyze_job(
            title="A", company="B", country="C", url="https://never-saved.com/job/x"
        )

        assert result.error is None
        assert result.resume.id == base.id

    def test_guard1_holds_when_url_is_omitted_entirely(self, db_path):
        """3a.2m applies equally when url is None (no DB lookup at all) —
        job_id must be None, and the general resume must still be returned,
        excluding any job-tailored resume."""
        from tools.jobs_store import save_job_analysis
        from tools.resumes import save_resume_version

        other_job = save_job_analysis(
            url="https://other.com/job/9", title="X", company="Y", country="Z"
        )
        base = _save_base_resume()
        save_resume_version(
            content="tailored for other job",
            label="Tailored for other",
            parent_id=base.id,
            job_id=other_job.id,
        )
        _declare_work_auth()

        from tools.analyze import analyze_job

        result = analyze_job(title="A", company="B", country="C")

        assert result.error is None
        assert result.resume.id == base.id

    def test_guard1_holds_when_url_absent_but_custom_title_given(self, db_path):
        """Findings 1(b)/2: a job saved WITHOUT a url (only custom_title) is
        the exact case _NO_URL_NOTICE exists for. Concrete failure this
        guards against: only a root resume tailored for referral job R
        exists; re-analyzing R by custom_title alone (still no url) must
        NOT hand back that self-tailored resume — it must resolve
        custom_title -> job_id just like url does, so the exclusion still
        fires."""
        from tools.jobs_store import save_job_analysis
        from tools.resumes import save_resume_version

        job = save_job_analysis(
            custom_title="Acme referral", title="X", company="Y", country="Z"
        )
        save_resume_version(
            content="tailored for acme referral",
            label="Tailored",
            parent_id=None,
            job_id=job.id,
        )

        from tools.analyze import analyze_job

        result = analyze_job(
            title="X", company="Y", country="Z", custom_title="Acme referral"
        )

        assert result.error == "no_resume"
        assert result.resume is None

    def test_ambiguous_custom_title_refuses_to_guess(self, db_path):
        """custom_title is NOT unique — when it resolves to more than one
        job, silently picking one would risk arming the guard for the wrong
        job (or not arming it at all). This must be an explicit, distinct
        error, not a silent fallthrough to job_id=None."""
        from tools.jobs_store import save_job_analysis

        save_job_analysis(
            custom_title="Acme referral", title="X1", company="Y", country="Z"
        )
        save_job_analysis(
            custom_title="Acme referral", title="X2", company="Y", country="Z"
        )
        _save_base_resume()

        from tools.analyze import analyze_job

        result = analyze_job(
            title="X2", company="Y", country="Z", custom_title="Acme referral"
        )

        assert result.error == "ambiguous_custom_title"
        assert result.resume is None


# ---------------------------------------------------------------------------
# T-05: fetch_failed is unreachable — the path no longer exists
# ---------------------------------------------------------------------------


class TestFetchFailedRemoved:
    def test_fetch_failed_not_a_documented_error(self):
        from tools.analyze import AnalyzeJobResult

        docstring = AnalyzeJobResult.__doc__ or ""
        assert "fetch_failed" not in docstring


# ---------------------------------------------------------------------------
# T-06: Server registration
# ---------------------------------------------------------------------------


class TestServerRegistration:
    def test_analyze_job_importable(self):
        from tools.analyze import analyze_job

        assert callable(analyze_job)

    def test_analyze_job_registered_in_server(self):
        import server

        tool_names = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert "analyze_job" in tool_names

    def test_analyze_match_no_longer_registered(self):
        import server

        tool_names = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert "analyze_match" not in tool_names

    def test_analyze_job_result_is_pydantic_model(self):
        from pydantic import BaseModel
        from tools.analyze import AnalyzeJobResult

        assert issubclass(AnalyzeJobResult, BaseModel)

    def test_analyze_module_does_not_import_tools_jobs_or_visa(self):
        """Partial win toward SC-51 — module-level import graph clean."""
        import ast
        from pathlib import Path

        source = (
            Path(__file__)
            .resolve()
            .parent.parent.joinpath("tools", "analyze.py")
            .read_text(encoding="utf-8")
        )
        tree = ast.parse(source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        assert "tools.jobs" not in imported_modules
        assert "tools.visa" not in imported_modules
