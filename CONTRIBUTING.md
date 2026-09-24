# Contributing to Runway

PRs are welcome. This file covers only what you would otherwise have to discover by having
a PR sent back to you.

## Setup

```bash
pip install -e ".[dev]"
pre-commit install       # ruff lint + format, before every commit
```

## Before you open a PR

```bash
uv run --extra dev pytest -q
ruff check . && ruff format --check .
claude plugin validate .
```

Conventional commit messages (`feat:`, `fix:`, `docs:`, `refactor!:`). No AI attribution
lines in commits.

## Two things that will fail your build if you don't know them

**The name is split on purpose.** The project is **Runway**, but the PyPI distribution is
`runway-mcp` and the database lives in `~/.config/runway-mcp/`. Both were deliberately left
alone during the 0.4.x rename — renaming the distribution means publishing a new one for no
user-visible gain, and renaming the database directory orphans every existing user's data.
If you see `runway-mcp` in `pyproject.toml`, `tools/_db.py`, or a `--from` pin, it is
correct. `CLAUDE.md` has the full table.

**A version number lives in six places.** `pyproject.toml` is the source of truth;
`manifest.json` (its `version` *and* its `--from` pin), the plugin's `plugin.json`, the
plugin's `.mcp.json` pin, and README's manual-install snippet must all agree.
`tests/test_docs_audit.py` fails the build if any of them drift. See
[RELEASING.md](RELEASING.md).

## What this project deliberately does not do

Two are the most common feature requests, and both are settled product decisions rather
than missing work:

- **It never fetches a job posting from a URL.** You paste the description. README explains
  why.
- **It never checks visa sponsorship against an external source.** It only reports the work
  authorization *you* declared.

The server persists and shapes data; Claude does the judgment — scoring, tailoring,
drafting. No MCP sampling is used, so Runway works on any MCP host.

If you want to propose something that crosses one of those lines, open a discussion first
rather than a PR — it will save you the implementation.

## Scope

Runway tracks jobs, their status, and which resume version went to each. It is not a CRM:
no contacts, no recruiter pipelines, no cover letters, no analytics dashboards.
