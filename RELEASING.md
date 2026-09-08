# Releasing

`runway-mcp` ships in two layers that must stay in lockstep:

1. **Server code** — downloaded by `uvx` from the pinned PyPI release.
2. **Claude Code plugin** — pins that exact version and is what users install/update.

The plugin's `.mcp.json` pins an exact version, so updating the plugin pulls exactly that
server build. Every release uses a **new** tag, so the pin changes and `uvx` never serves a
cached build of the previous one. For that to work, **every release bumps the same version
in all six places below.**

> **The `publish` job is gated on a repository variable.** `PYPI_PUBLISH_ENABLED` must be
> `"true"` (Settings → Secrets and variables → Actions → Variables) or the upload is
> skipped — grey and silent, not a failure. It exists because Trusted Publishing was
> unconfigured for a long stretch and every tag died at the upload with
> `invalid-publisher`, painting a red X on releases that were otherwise fine. The
> publisher is configured now and the variable is set; the switch stays so publishing can
> be parked again without editing this workflow.

## Release checklist

1. Bump the version to `X.Y.Z` in all of:

   | File | What |
   |---|---|
   | `pyproject.toml` | `version` — **the source of truth**; everything else is checked against it |
   | `manifest.json` | `version` (Desktop Extension) |
   | `manifest.json` | `server.mcp_config.args` — the `--from` pin |
   | `plugins/runway-mcp/.claude-plugin/plugin.json` | `version` (plugin update signal) |
   | `plugins/runway-mcp/.mcp.json` | the `--from` pin |
   | `README.md` | the *Option B: manual `.mcp.json`* snippet — people copy-paste it verbatim |

   Pins are `runway-mcp==X.Y.Z`. The version gate also accepts a git ref
   (`git+https://github.com/satovarb16/runwayMCP@vX.Y.Z`), which is the escape hatch if
   PyPI is ever unreachable at release time — but it costs users a source build and a
   working `git`, so it is a fallback, not the default.

   > This list said "four places" until 0.3.1 and was wrong: it omitted `manifest.json`'s
   > pin, README's install snippet, and the test that asserted the version. The README
   > omission is the dangerous one — nothing was checking the snippet every manual
   > installer pastes.

2. Verify they all match:
   ```bash
   uv run --extra dev pytest tests/test_docs_audit.py -q
   ```
   `test_every_version_site_agrees_with_pyproject` reads the version out of
   `pyproject.toml` and asserts the other five name it too. It hardcodes no version
   number, so it never needs editing at release time. CI runs it, and the tag workflow
   re-checks four of the six sites independently before it will publish.
3. Sanity-check locally (CI runs all of this again before it uploads):
   ```bash
   uv run --extra dev pytest -q
   claude plugin validate .
   ```
4. **Push the tag from the release branch, before merging** — this is what publishes:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
   `.github/workflows/release.yml` re-checks that the tag agrees with `pyproject.toml`
   (the test suite it runs covers the other five sites), lints, tests on Python
   3.11/3.12/3.13, builds, runs `twine check`, and only
   then uploads to PyPI via Trusted Publishing. There is no token to set — do **not**
   `uv publish` by hand.

   > If the run fails *before* the upload step, delete the tag
   > (`git push origin :vX.Y.Z`), fix, and re-cut it. Once PyPI accepts an upload that
   > number is burned for good — but nothing is published until every check has passed.
5. **Wait for the `publish` job to go green, then merge to `master`.** Not "wait for the
   tag" — the tag is up the moment you push it, but the pin names a *PyPI version*, and
   that version does not exist until the upload finishes a few minutes later.

   > **Why the wait is the whole point.** The marketplace serves `master`. The moment the
   > bumped `.mcp.json` lands there, every new install — and every existing user who runs
   > `/plugin marketplace update` — resolves that pin. If PyPI doesn't have the version
   > yet, `uvx` fails outright and the server never starts. That is exactly how 0.3.0
   > shipped a pin to a version PyPI had never seen. Merging after the upload closes the
   > window instead of shortening it.
   >
   > This is the one cost of pinning PyPI instead of a git tag: a git ref is valid the
   > instant it is pushed, a PyPI version is valid only after CI says so. In exchange,
   > users stop needing `git` and a source build, and the pin becomes immutable —
   > a published version can never point at different code, while a tag can be moved.

   A squash merge leaves the tagged commit off `master`'s history; it stays reachable
   through the tag. Use a merge commit if you want it in the history too.

## Versioning

- A published version on PyPI is immutable — never reuse a number; always bump.
- **Never move a tag.** `uvx` resolves a git ref once and caches the commit it found, so
  force-pushing `vX.Y.Z` somewhere new leaves anyone who already installed it building the
  old commit forever. Cut a new version instead.
- Bump patch (`Z`) for fixes, minor (`Y`) for features, major (`X`) for breaking changes.
