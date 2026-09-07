# Releasing

`runway-mcp` ships in two layers that must stay in lockstep:

1. **Server code** — built by `uvx` from the pinned source (a git tag today, a PyPI
   release once publishing is unblocked).
2. **Claude Code plugin** — pins that exact version and is what users install/update.

The plugin's `.mcp.json` pins an exact version, so updating the plugin pulls exactly that
server build. Every release uses a **new** tag, so the pin changes and `uvx` never serves a
cached build of the previous one. For that to work, **every release bumps the same version
in all six places below.**

> **PyPI publishing is currently blocked.** The `pypi` Trusted Publishing publisher was
> never configured, and the account's 2FA is lost, so the `publish` job failed with
> `invalid-publisher` on every tag. It is now **gated off** behind the repository
> variable `PYPI_PUBLISH_ENABLED`, so a tag runs the full verify job and then skips the
> upload instead of failing it. Set that variable to `"true"` (Settings → Secrets and
> variables → Actions → Variables) once the publisher exists — no code change needed.
> Until then the plugin pins the **git tag**
> (`git+https://github.com/satovarb16/runwayMCP@vX.Y.Z`) instead of the PyPI spec
> (`runway-mcp==X.Y.Z`), and the latest release on PyPI stays at `0.1.2`. The version
> gate accepts either pin form, so switching back is a one-line change per pin.

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

   Pins are currently `git+https://github.com/satovarb16/runwayMCP@vX.Y.Z`; they become
   `runway-mcp==X.Y.Z` once PyPI works again.

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
   `.github/workflows/release.yml` re-checks that the tag agrees with all four version
   sites, lints, tests on Python 3.11/3.12/3.13, builds, runs `twine check`, and only
   then uploads to PyPI via Trusted Publishing. There is no token to set — do **not**
   `uv publish` by hand.

   > **Why the tag goes first.** The marketplace serves `master`. The moment the bumped
   > `.mcp.json` lands there, every new install — and every existing user who runs
   > `/plugin marketplace update` — resolves that pin. If the tag it names doesn't exist
   > yet, `uvx` fails outright and the server never starts. Tagging first closes that
   > window instead of just shortening it. The tag points at the branch tip, where all
   > four version sites already agree, which is exactly what the gate checks.
   >
   > If the run fails *before* the upload step, delete the tag
   > (`git push origin :vX.Y.Z`), fix, and re-cut it. Once PyPI accepts an upload that
   > number is burned for good — but nothing is published until every check has passed.
5. **Then merge to `master`.** That is the signal Claude Code uses to offer/apply the
   plugin update, and users get the new server build automatically via the pin. A squash
   merge leaves the tagged commit off `master`'s history; it stays reachable through the
   tag, so `uvx` still builds it. Use a merge commit if you want it in the history too.

## Versioning

- A published version on PyPI is immutable — never reuse a number; always bump.
- **Never move a tag.** `uvx` resolves a git ref once and caches the commit it found, so
  force-pushing `vX.Y.Z` somewhere new leaves anyone who already installed it building the
  old commit forever. Cut a new version instead.
- Bump patch (`Z`) for fixes, minor (`Y`) for features, major (`X`) for breaking changes.
