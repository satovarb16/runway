"""Cross-cutting audit (PR5, spec R26 SC-53/SC-54; tasks 5.1-5.4).

Two independent, source-derived audits — neither hardcodes "the 9 tool
names" the way the docs themselves drifted (obs #364's lesson: a test that
hardcodes the current tool list only moves the staleness into the test and
stops catching drift the next time a tool is added, removed, or renamed):

1. TestSC53/TestSC54 (error-envelope integrity) — samples the failure paths
   already proven individually in test_jobs_store.py/test_resumes.py/
   test_work_auth.py/test_db.py/test_migration.py and re-asserts the
   CROSS-CUTTING invariant in one place: every one of them returns a
   structured envelope (never raises), and a "missing" precondition never
   shares an error code with "the store exists but is broken."

2. TestDocsMatchSourceOfTruth (docs staleness) — README.md/manifest.json/
   pyproject.toml must describe the tool surface that actually exists.
   Forbidden terms are derived from what the live source says no longer
   exists (registered tool names, live function signatures, the actual
   dependency list) rather than typed in as a fresh guess at what "looks
   stale". An explicit historical block (delimited by an HTML comment) is
   exempted, because D12 requires documenting the abandoned scraping
   attempt BY NAME, in prose, not just in a commit message.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import tomllib
from pathlib import Path

import pytest

import server
from tools.analyze import analyze_job
from tools.jobs_store import (
    delete_job,
    get_job,
    list_jobs,
    save_job_analysis,
    set_application_status,
)
from tools.resumes import get_resume_version, list_resume_versions, save_resume_version
from tools.work_auth import set_work_authorization

_ROOT = Path(__file__).resolve().parent.parent
_README_PATH = _ROOT / "README.md"
_MANIFEST_PATH = _ROOT / "manifest.json"
_PYPROJECT_PATH = _ROOT / "pyproject.toml"
_PLUGIN_JSON_PATH = _ROOT / "plugins" / "runway" / ".claude-plugin" / "plugin.json"
_PLUGIN_MCP_PATH = _ROOT / "plugins" / "runway" / ".mcp.json"

_HISTORICAL_RE = re.compile(
    r"<!--\s*historical:start\s*-->.*?<!--\s*historical:end\s*-->",
    re.DOTALL,
)

# The live functions actually wired to server.py's registrations (imported
# directly above, from the same source server.py itself imports from) — used
# below to derive real parameter names via inspect.signature rather than
# guessing at what "the new contract" looks like.
_REGISTERED_TOOL_FUNCTIONS = {
    "analyze_job": analyze_job,
    "save_job_analysis": save_job_analysis,
    "get_job": get_job,
    "list_jobs": list_jobs,
    "set_application_status": set_application_status,
    "delete_job": delete_job,
    "save_resume_version": save_resume_version,
    "get_resume_version": get_resume_version,
    "list_resume_versions": list_resume_versions,
    "set_work_authorization": set_work_authorization,
}


def _readme_without_historical_sections() -> str:
    """README.md text with any <!-- historical:start/end --> block removed.

    D12 requires documenting, by name, that fetch_job_posting and
    check_visa_sponsorship existed and were abandoned — that mention is
    deliberate, not staleness. Everywhere else in the file, those names
    must be gone.
    """
    return _HISTORICAL_RE.sub("", _README_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 0. Sanity: the map above must match the LIVE registry, or the rest of this
#    file could be silently checking the wrong (or a stale) set of functions.
# ---------------------------------------------------------------------------


def test_registered_tool_function_map_matches_live_server_registration():
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert set(_REGISTERED_TOOL_FUNCTIONS) == registered


# ---------------------------------------------------------------------------
# 1. SC-53 — every documented failure path returns an envelope, never raises
# ---------------------------------------------------------------------------


class TestSC53NoToolRaisesOnADocumentedFailure:
    """Every documented failure path across the tool surface returns
    success=False with a coded error/error_message — none of them raises
    past the MCP boundary."""

    def test_save_job_analysis_neither_url_nor_custom_title(self, db_path):
        result = save_job_analysis(title="T", company="C", country="USA")
        assert result.success is False
        assert result.error == "invalid_input"

    def test_save_job_analysis_unknown_id(self, db_path):
        result = save_job_analysis(id="ghost", title="T", company="C", country="USA")
        assert result.success is False
        assert result.error == "not_found"

    def test_save_job_analysis_duplicate_url(self, db_path):
        save_job_analysis(url="https://ex.com/1", title="A", company="X", country="USA")
        second = save_job_analysis(
            title="B", company="Y", country="USA", custom_title="B role"
        )
        assert second.success is True
        result = save_job_analysis(
            id=second.id, title="B", company="Y", country="USA", url="https://ex.com/1"
        )
        assert result.success is False
        assert result.error == "duplicate_url"

    def test_get_job_unknown_id(self, db_path):
        result = get_job(id="ghost")
        assert result.success is False
        assert result.error == "not_found"

    def test_save_resume_version_unknown_job_id(self, db_path):
        base = save_resume_version(content="base text", label="Base", parent_id=None)
        assert base.success is True
        result = save_resume_version(
            content="tailored", label="Tailored", parent_id=base.id, job_id="ghost"
        )
        assert result.success is False
        assert result.error == "job_not_found"

    def test_set_application_status_unknown_id(self, db_path):
        result = set_application_status(id="ghost", status="applied")
        assert result.success is False
        assert result.error == "not_found"

    def test_set_application_status_invalid_status(self, db_path):
        saved = save_job_analysis(
            url="https://ex.com/2", title="T", company="C", country="USA"
        )
        result = set_application_status(id=saved.id, status="not-a-real-status")
        assert result.success is False
        assert result.error == "invalid_status"

    def test_set_work_authorization_all_entries_uninterpretable(self, db_path):
        result = set_work_authorization(countries=["", "   ", "..."])
        assert result.success is False
        assert result.error == "invalid_input"

    def test_analyze_job_no_work_authorization_declared(self, db_path):
        save_resume_version(content="base text", label="Base", parent_id=None)
        result = analyze_job(title="T", company="C", country="USA")
        assert result.error == "no_work_authorization"


class TestSC34UnexpectedArgumentIsAProgrammingErrorNotAnEnvelope:
    """analyze_job's signature has no jd_text parameter (D2/R15) — an
    unexpected keyword is a caller programming error and raises normally,
    it is NOT swallowed into an error envelope (that would let the JD text
    travel as call payload a second time, the exact cost D2 exists to
    avoid)."""

    def test_jd_text_kwarg_raises_typeerror(self, db_path):
        save_resume_version(content="base text", label="Base", parent_id=None)
        set_work_authorization(countries=["USA"])
        with pytest.raises(TypeError):
            analyze_job(title="T", company="C", country="USA", jd_text="oops")


# ---------------------------------------------------------------------------
# 2. SC-54 — a missing store and a broken store never share an error code
# ---------------------------------------------------------------------------


class TestSC54MissingVsCorruptNeverShareCode:
    def test_list_jobs_missing_store_is_normal_not_an_error(self, db_path):
        result = list_jobs()
        assert result.success is True
        assert result.jobs == []

    def test_list_jobs_corrupt_store_returns_an_error(self, db_path):
        db_path.write_bytes(b"not a database")
        result = list_jobs()
        assert result.success is False
        assert result.error_message is not None

    def test_get_job_missing_store_is_not_found_not_corrupt(self, db_path):
        result = get_job(id="whatever")
        assert result.error == "not_found"

    def test_get_job_corrupt_store_is_corrupt_not_not_found(self, db_path):
        db_path.write_bytes(b"not a database")
        result = get_job(id="whatever")
        assert result.error == "corrupt"

    def test_analyze_job_missing_resume_store_is_no_resume(self, db_path):
        result = analyze_job(title="T", company="C", country="USA")
        assert result.error == "no_resume"

    def test_analyze_job_corrupt_store_is_corrupt_not_no_resume(self, db_path):
        db_path.write_bytes(b"not a database")
        result = analyze_job(title="T", company="C", country="USA")
        assert result.error == "corrupt"

    def test_analyze_job_corrupt_store_is_corrupt_not_no_work_authorization(
        self, db_path
    ):
        db_path.write_bytes(b"not a database")
        result = analyze_job(title="T", company="C", country="USA")
        # Same corrupt file breaks both preconditions; the resume check runs
        # first (module docstring), so this pins that "corrupt" — never
        # "no_work_authorization" — is what a caller sees either way.
        assert result.error == "corrupt"
        assert result.error != "no_work_authorization"

    def test_get_resume_version_missing_store_is_not_found(self, db_path):
        result = get_resume_version(id="whatever")
        assert result.error == "not_found"

    def test_get_resume_version_corrupt_store_is_corrupt(self, db_path):
        db_path.write_bytes(b"not a database")
        result = get_resume_version(id="whatever")
        assert result.error == "corrupt"

    def test_list_resume_versions_missing_store_is_normal_not_an_error(self, db_path):
        result = list_resume_versions()
        assert result.success is True
        assert result.versions == []

    def test_list_resume_versions_corrupt_store_returns_an_error(self, db_path):
        db_path.write_bytes(b"not a database")
        result = list_resume_versions()
        assert result.success is False
        assert result.error_message is not None


# ---------------------------------------------------------------------------
# 3. Docs vs. source of truth — set-equality, no network call at import
# ---------------------------------------------------------------------------

# Tool names that existed at some point in this project's history and are
# now gone. NOT derivable from live source — the modules that defined them
# are deleted in full, so nothing in the current tree can enumerate them.
# Maintained by hand, exactly as tests/test_removed_tools.py and
# test_server.py's own `stale` set already do for the same names.
_REMOVED_TOOL_NAMES = {
    "fetch_job_posting",
    "check_visa_sponsorship",
    "get_profile",
    "mark_applied",
    "setup_profile",
    "update_profile",
}


def test_registered_tools_equal_manifest_tools_and_exclude_every_removed_name():
    """5.4: derived from both sides, never a hardcoded 'the 9 names' list."""
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_names = {t["name"] for t in manifest["tools"]}

    assert registered == manifest_names
    assert not (_REMOVED_TOOL_NAMES & registered)
    assert not (_REMOVED_TOOL_NAMES & manifest_names)


def test_importing_server_makes_no_network_call(monkeypatch):
    """Regression guard for the import-time refresh_to_latest_fy() USCIS call
    PR1 removed — asserted by blocking socket creation during import, not by
    trusting the removal stays in place by inspection alone."""
    import socket
    import sys

    def _blocked(*args, **kwargs):
        raise AssertionError("import server attempted to open a network socket")

    monkeypatch.setattr(socket, "socket", _blocked)
    sys.modules.pop("server", None)
    importlib.import_module("server")


# ---------------------------------------------------------------------------
# 4. Docs staleness — README.md / manifest.json / pyproject.toml
# ---------------------------------------------------------------------------


def test_no_removed_tool_name_appears_in_readme_prose_outside_history():
    prose = _readme_without_historical_sections()
    for name in _REMOVED_TOOL_NAMES:
        assert name not in prose, (
            f"{name!r} still appears in README.md outside a "
            f"<!-- historical:start/end --> block."
        )


def test_no_removed_tool_name_appears_in_manifest_prose():
    manifest_text = _MANIFEST_PATH.read_text(encoding="utf-8")
    for name in _REMOVED_TOOL_NAMES:
        assert name not in manifest_text


class TestNoStaleTechnologyMentioned:
    """USCIS/Playwright/the [browser] extra were removed with the scraping
    and visa-lookup pipeline (PR1) — re-confirmed absent from dependencies
    here (already proven in test_removed_tools.py::TestDependencySurface),
    then checked that the absence is reflected in prose too, everywhere
    except the historical section."""

    def test_playwright_and_browser_extra_absent_from_pyproject(self):
        content = _PYPROJECT_PATH.read_text(encoding="utf-8")
        assert "playwright" not in content.lower()
        assert "[browser]" not in content

    def test_playwright_browser_extra_and_uscis_absent_from_readme_prose(self):
        prose = _readme_without_historical_sections()
        assert "playwright" not in prose.lower()
        assert "[browser]" not in prose
        assert "uscis" not in prose.lower()

    def test_playwright_and_uscis_absent_from_manifest(self):
        manifest_text = _MANIFEST_PATH.read_text(encoding="utf-8")
        assert "playwright" not in manifest_text.lower()
        assert "uscis" not in manifest_text.lower()


class TestParamRenameJobUrlToJobId:
    """D1: job_url -> job_id. Derived from the LIVE signature of every
    registered tool, not a fixed list of "the 3 tools that changed" — a
    future tool gaining a job-linking parameter is automatically covered as
    long as it takes job_id and is registered."""

    def test_manifest_never_documents_job_url_for_a_job_id_tool(self):
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        entries = {t["name"]: t for t in manifest["tools"]}
        for name, fn in _REGISTERED_TOOL_FUNCTIONS.items():
            params = inspect.signature(fn).parameters
            if "job_id" in params and "job_url" not in params:
                assert "job_url" not in entries[name]["description"], (
                    f"manifest.json's {name!r} description still says "
                    f"'job_url' but the live parameter is 'job_id'."
                )

    def test_readme_tool_heading_never_documents_job_url_for_a_job_id_tool(self):
        readme = _README_PATH.read_text(encoding="utf-8")
        for name, fn in _REGISTERED_TOOL_FUNCTIONS.items():
            params = inspect.signature(fn).parameters
            if "job_id" not in params or "job_url" in params:
                continue
            heading_match = re.search(rf"### `{re.escape(name)}\(([^)]*)\)", readme)
            assert heading_match is not None, (
                f"README.md has no '### `{name}(...)`' heading to document "
                f"the live signature."
            )
            assert "job_url" not in heading_match.group(1), (
                f"README's {name!r} heading still names 'job_url', which is "
                f"not a real parameter of the live function."
            )


def test_manifest_save_job_analysis_does_not_mention_visa_verdict():
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = {t["name"]: t for t in manifest["tools"]}
    assert "visa_verdict" not in entries["save_job_analysis"]["description"].lower()
    assert "visa verdict" not in entries["save_job_analysis"]["description"].lower()


def test_readme_does_not_reference_get_profile_migration_hatch_outside_history():
    """get_profile was removed in this release (it does not exist in
    tools/jobs_store.py, tools/resumes.py, or any registered tool) — the
    README's pre-0.3.0 migration instructions describing it as a working,
    read-only tool must be gone."""
    prose = _readme_without_historical_sections()
    assert "get_profile" not in prose


class TestPyprojectProseNoLongerClaimsVisaScoring:
    def test_description_and_keywords_do_not_claim_removed_capabilities(self):
        data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
        description = data["project"]["description"].lower()
        keywords = [k.lower() for k in data["project"]["keywords"]]
        for stale_word in ("visa", "sponsorship", "h-1b", "h1b", "f-1", "opt"):
            assert stale_word not in description, (
                f"pyproject.toml's description still says {stale_word!r}"
            )
            assert stale_word not in keywords, (
                f"pyproject.toml's keywords still include {stale_word!r}"
            )


def test_manifest_description_and_keywords_do_not_claim_removed_capabilities():
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    description = manifest["description"].lower()
    long_description = manifest["long_description"].lower()
    keywords = [k.lower() for k in manifest["keywords"]]
    for stale_word in ("visa", "sponsorship", "h1b", "h-1b", "f1", "f-1", "opt"):
        assert stale_word not in description
        assert stale_word not in keywords
    for stale_word in ("fetches the job posting", "greenhouse", "ashby", "lever"):
        assert stale_word not in long_description


def test_every_version_site_agrees_with_pyproject():
    """The five version sites downstream of pyproject.toml name its version.

    Those five plus pyproject.toml itself are the six places RELEASING.md
    lists. The release workflow adds the only site pytest cannot see — the
    git tag — by comparing it against pyproject.toml, so the two checks
    together cover all six with no overlapping second implementation.


    This deliberately hardcodes no version number. The previous incarnation
    asserted the literal "0.3.0" (in its name, too), which is the exact
    failure this module's docstring warns about: it moved the staleness into
    the test, so every release had to edit the test that was supposed to be
    guarding the release. Deriving the expected value from pyproject.toml
    means this never needs touching again -- and it covers three sites the
    old one missed entirely, including README's copy-paste install snippet,
    which nothing checked and which every manual installer pastes verbatim.
    """
    version = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    plugin = json.loads(_PLUGIN_JSON_PATH.read_text(encoding="utf-8"))

    assert manifest["version"] == version, "manifest.json version"
    assert plugin["version"] == version, "plugin.json version"

    # These two pins are what users actually run; README's third is asserted
    # separately below, since it is prose rather than JSON. A pin that lags
    # the package version installs a different server than the release
    # claims to ship.
    pins = {
        "manifest.json": manifest["server"]["mcp_config"]["args"],
        "plugins/runway/.mcp.json": json.loads(
            _PLUGIN_MCP_PATH.read_text(encoding="utf-8")
        )["mcpServers"]["runway"]["args"],
    }
    for where, args in pins.items():
        # Default None rather than letting next() raise: a bare StopIteration
        # here would report as an error with no message, in a module whose
        # every other failure names the file and what is wrong with it.
        spec = next((a for a in args if a.startswith(("runway-mcp==", "git+"))), None)
        assert spec, f"{where}: no pin-shaped argument among {args!r}"
        found = re.search(r"(?:==|@v)([0-9]+\.[0-9]+\.[0-9]+)$", spec)
        assert found, f"{where}: cannot read a version out of the pin {spec!r}"
        assert found.group(1) == version, (
            f"{where}: pin says {found.group(1)}, not {version}"
        )

    # Either pin form, matching what the JSON pins above accept: PyPI is the
    # default, the git ref is the fallback for a release where PyPI is
    # unreachable. Asserting only one form would fail the build for a pin that
    # is correct, just written the other way.
    readme = _README_PATH.read_text(encoding="utf-8")
    assert f"runway-mcp=={version}" in readme or f"runway@v{version}" in readme, (
        f"README's install snippet does not pin {version} -- "
        "anyone copy-pasting it installs a different version"
    )
