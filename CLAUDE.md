# Runway

A memory for your job hunt, shipped as a Claude Code plugin backed by a local MCP server.
`README.md` explains what it does; `RELEASING.md` explains how it ships. This file covers
only what an agent working here would otherwise get wrong.

## The name is deliberately split — do not "fix" it

The project was renamed from `runwayMCP` to **Runway** in 0.4.x. The rename was applied to
every user-visible surface and deliberately stopped there:

| Surface | Value | |
|---|---|---|
| Repo, plugin, `mcpServers` key, `FastMCP()` | `runway` | renamed |
| PyPI distribution + `[project.scripts]` | `runway-mcp` | **kept** |
| Database path `~/.config/runway-mcp/runway.db` | `runway-mcp` | **kept** |

The `mcpServers` key in `.mcp.json` is only a local label; the `--from runway-mcp==X.Y.Z`
inside it names the real package. They are independent, which is what made the rename cheap.

Renaming the PyPI distribution means publishing a new one for zero user-visible gain.
Renaming the database directory orphans every existing user's data and needs a migration.
If you see `runway-mcp` in `pyproject.toml`, `tools/_db.py`, or a `--from` pin, it is correct.

## Versions must agree across six sites

`pyproject.toml` is the source of truth. `manifest.json` (version *and* its `--from` pin),
`plugins/runway/.claude-plugin/plugin.json`, `plugins/runway/.mcp.json`, and README's
manual-install snippet must all name the same version. `RELEASING.md` has the checklist;
`tests/test_docs_audit.py` enforces it and hardcodes no version number.

That test also hardcodes the plugin directory path, the `mcpServers` key, and both pin
forms — it is the first thing to break in any rename.

## Config that lives outside the repo

No test can see these, and two of the three have already caused real incidents:

- **PyPI Trusted Publishing** is scoped to the GitHub *repository name*. Renaming the repo
  without updating the publisher kills the next tag at upload with `invalid-publisher`.
- **`PYPI_PUBLISH_ENABLED`** repo variable gates the `publish` job. Unset means the upload
  is skipped silently — grey, not red.
- **The GitHub repo description and topics.** The description advertised the project's
  original purpose long after that purpose was gone, because nothing ever diffed it.

A failed upload does **not** burn the version: `invalid-publisher` fails at the OIDC token
exchange, before anything is published. Delete the tag, fix, re-cut.

## Commands

```bash
uv run --extra dev pytest -q     # full suite
ruff check . && ruff format --check .
claude plugin validate .
```

## Conventions

- Conventional commits. No AI attribution lines.
- The server persists and shapes data; Claude does the judgment (scoring, tailoring, drafting).
  No MCP sampling is used, so it works on any host.
- The server never fetches job postings and never checks visa sponsorship against an external
  source. `README.md` has the reasoning — this is a product decision, not a missing feature.
- `resume_versions` is append-only, enforced by the database, not by convention.
