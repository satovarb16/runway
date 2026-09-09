"""Tests proving fetch_job_posting/check_visa_sponsorship/get_profile and
their supporting modules are affirmatively GONE, not merely unregistered.

Derived from the source of truth (import machinery, the live tool registry,
manifest.json, and pyproject.toml) rather than a hardcoded list — the lesson
from obs #364: a test that hardcodes "the tool is absent" can pass while the
module still exists and is merely unregistered.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


# ---------------------------------------------------------------------------
# SC-49: the modules that defined the removed tools no longer exist
# ---------------------------------------------------------------------------


class TestModulesDeleted:
    def test_tools_jobs_not_importable(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.jobs")

    def test_tools_visa_not_importable(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.visa")

    def test_tools_profile_not_importable(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.profile")

    def test_tools_uscis_cache_not_importable(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.uscis_cache")

    def test_tools_utils_not_importable(self):
        """tools/_utils.py loses both its consumers (visa.py, uscis_cache.py)
        in this same change and is deleted alongside them."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools._utils")


# ---------------------------------------------------------------------------
# SC-50: removed tools are absent from both live registration and the
# manifest, by set-equality — not a hardcoded name list
# ---------------------------------------------------------------------------


class TestToolsDeregistered:
    def test_removed_tools_absent_from_server_registry(self):
        import server

        registered_names = {t.name for t in server.mcp._tool_manager.list_tools()}
        for removed in ("fetch_job_posting", "check_visa_sponsorship", "get_profile"):
            assert removed not in registered_names

    def test_removed_tools_absent_from_manifest(self):
        import json

        manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_names = {tool["name"] for tool in manifest["tools"]}
        for removed in ("fetch_job_posting", "check_visa_sponsorship", "get_profile"):
            assert removed not in manifest_names


# ---------------------------------------------------------------------------
# SC-51: analyze_job no longer transitively imports the deleted modules
# ---------------------------------------------------------------------------


class TestAnalyzeImportGraph:
    def test_analyze_module_does_not_import_tools_jobs_or_visa(self):
        import sys

        sys.modules.pop("tools.analyze", None)
        importlib.import_module("tools.analyze")
        assert "tools.jobs" not in sys.modules
        assert "tools.visa" not in sys.modules


# ---------------------------------------------------------------------------
# SC-52: runtime dependency surface — exactly mcp + pydantic (full
# satisfaction, task 2.6e: filelock drops out with tools/_storage.py)
# ---------------------------------------------------------------------------


class TestDependencySurface:
    def test_runtime_deps_are_exactly_mcp_and_pydantic(self):
        """SC-52, full satisfaction: the [project] dependencies list contains
        exactly mcp and pydantic — no requests, beautifulsoup4, rapidfuzz,
        filelock, and no browser/Playwright optional group.

        This subsumes the two substring tests that used to sit above it (one
        for requests/beautifulsoup4/rapidfuzz, one for filelock): set
        equality forbids every name, named or not, and reads the parsed
        dependency list rather than grepping raw TOML text, where a mention
        in a comment counted as a failure.
        """
        import tomllib

        data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
        deps = data["project"]["dependencies"]
        dep_names = {
            d.split(">=")[0].split("<")[0].split("==")[0].strip() for d in deps
        }
        assert dep_names == {"mcp", "pydantic"}

    def test_browser_optional_group_removed(self):
        content = _PYPROJECT_PATH.read_text(encoding="utf-8")
        assert "[project.optional-dependencies]" in content
        opt_section = content.split("[project.optional-dependencies]")[1]
        # the browser group (playwright) must be gone; dev group survives
        assert "playwright" not in opt_section.lower()
