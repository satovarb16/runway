# runwayMCP

[![Release](https://img.shields.io/github/v/tag/satovarb16/runwayMCP?label=release)](https://github.com/satovarb16/runwayMCP/releases)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

runwayMCP is a memory for your job hunt. You paste a job description into the conversation;
Claude scores it, tailors a resume, and runwayMCP remembers which jobs you applied to, what
state each one is in, and — the part that matters most — **which resume version you sent for
each job**. Ask "did I apply to Datadog?" months later and get back "yes, and here's the exact
resume you sent."

## Quick install

### Option A: Claude Code plugin (recommended — two commands)

```
/plugin marketplace add satovarb16/runwayMCP
/plugin install runway-mcp@satovarb
```

Claude Code wires up the MCP server for you — no JSON to edit.

**Updating:** new releases arrive through the normal plugin flow — no need to touch PyPI.

```
/plugin marketplace update satovarb
/plugin update runway-mcp@satovarb
```

Then run `/reload-plugins` (or restart Claude Code) to load the new version. The plugin
pins an exact release version, so updating it pulls the matching server release.

**You will not be told a new version exists.** Claude Code can update plugins on its own
and announce it — `Plugins updated: runway-mcp` — but by default that only happens for
marketplaces on its built-in allowlist, which covers Anthropic's own and nothing else.
Every third-party marketplace, this one included, stays on whatever version you installed
until you run the two commands above. No banner, no prompt, no notice.

To get the announcement instead of the silence, turn auto-update on once:

```
/plugin
```

Pick the `satovarb` marketplace, then **Enable auto-update**. Claude Code then refreshes
this marketplace and its installed plugins for you, and tells you when it did.

That switch lives on your machine, not in this repo — a plugin author cannot enable it for
you, which is exactly why it is written down here.

### Option B: manual `.mcp.json`

Create a `.mcp.json` file in the directory where you run Claude Code:

```json
{
  "mcpServers": {
    "runway-mcp": {
      "command": "uvx",
      "args": ["--from", "runway-mcp==0.4.0", "runway-mcp"]
    }
  }
}
```

That's it. Open Claude Code — `uvx` downloads and runs the server automatically.

> **Don't have `uv`?** Install it: `pip install uv` (or see [uv docs](https://docs.astral.sh/uv/getting-started/installation/))

### Alternative: install from source

```bash
git clone https://github.com/satovarb16/runwayMCP
cd runwayMCP
pip install -e ".[dev]"
```

Then use `python -m server` in place of the `uvx` command and its `--from` arguments in
your `.mcp.json`, and add `"cwd": "/path/to/runwayMCP"`.

<!-- historical:start -->
## Why runwayMCP doesn't fetch job postings, and never checked visa sponsorship this way

Earlier versions of this server fetched job postings from Greenhouse/Ashby/Lever
(`fetch_job_posting`) and checked H-1B sponsorship history against USCIS data
(`check_visa_sponsorship`). Both tools, along with the read-only `get_profile` migration
hatch, are **removed as of this release** — not deprecated, deleted.

The fetch/scrape approach was fragile by construction: every job board changes its markup on
its own schedule (and increasingly blocks headless scraping outright), so a server that parses
HTML is permanently one layout change — or one anti-bot update — away from silently returning
garbage. Claude, on the other hand, already reads the job posting you paste into the
conversation. It does not need the server to fetch a second copy of the same text over HTTP.
The server's job is to **persist and shape data**, not to scrape it.

The USCIS H-1B sponsorship lookup went with it. It's replaced by something smaller and more
honest: `set_work_authorization` lets you declare, once, the countries you may legally work in,
and `analyze_job` compares each job's country against that declaration live, on every call —
never a stored, staleness-prone verdict. See [Work authorization](#step-0b-required-declare-your-work-authorization)
below.
<!-- historical:end -->

## Step 0 (required): save your resume

**Do this once before anything else.** `analyze_job` needs a stored resume — without one
it returns a `no_resume` error asking you to save one first.

```
You: "Here's my CV, save it as my general resume: /path/to/resume.pdf"
```

Claude reads your CV, drafts the resume as plain text, and saves it with
`save_resume_version` (see [Tools](#tools) below for the full reference).
Resume versions are **raw text, versioned, and append-only** — nothing is ever
overwritten or deleted. The first version you save (`parent_id=None`) establishes the
base of the tree. Later, when you decide to apply somewhere, Claude saves a version
*tailored* for that job pointing back at an existing one (`parent_id=<some version's
id>`, `job_id=<the id save_job_analysis returned for that job>`) without touching it —
`analyze_job` never scores a job against a resume already rewritten for that same job,
since that would just be scoring the resume against itself.

**Your "general" resume is the most recent version with no `job_id`** — not
necessarily the first one. Those are the same thing until you update your CV, and
different afterwards.

Updated your CV? Save it as a new version with `parent_id` set to any existing version's
id and **no `job_id`**. It becomes your general resume by being the most recent
untailored one. Passing `parent_id=None` a second time is rejected — there is only ever
one root.

## Step 0b (required): declare your work authorization

`analyze_job` also needs to know where you're allowed to work — without it, it returns a
`no_work_authorization` error asking you to declare this first.

```
You: "I can legally work in the United States and Germany."
Claude: set_work_authorization(countries=["United States", "Germany"])
```

This **replaces** any prior declaration — it's a statement about the whole set, not an
addition. From then on, every `analyze_job` call compares that job's country against your
current declaration **live** (never cached, never stored on the job record) and returns one
of three outcomes in `work_authorization.status`:

- `"authorized"` — the job's country matches a declared one. No warning.
- `"warned"` — it doesn't match. `work_authorization.warning` names both the job's country
  and your declared list, exactly as you and the job posting stated them, so a false warning
  caused by an unrecognized spelling is self-diagnosing.
- `"undetermined"` — the job's country couldn't be read at all (empty, or something like
  `"..."`). Never silently treated as authorized or as a mismatch.

The warning is advisory only — it never blocks `analyze_job` from returning a result.

## Usage: paste a job, don't link one

runwayMCP never fetches a job posting. Paste the description into the conversation, and
Claude extracts what it needs:

```
You: "Evaluate this for me — [paste the full job description]"
Claude:
  1. Extracts title, company, and country from the pasted text
  2. analyze_job(title=..., company=..., country=..., url=<link, if you have one>)
     → your general resume + a scoring guide + a work-authorization check
  3. Scores the match against the posting text already in this conversation
     → APPLY / CONSIDER / SKIP + reasoning
  4. If APPLY or CONSIDER: tailors your resume and saves it
     → save_job_analysis(..., jd_text=<the full posting>) returns an id
     → save_resume_version(..., job_id=<that id>)
```

**`url` is optional.** If the posting has no URL (a referral, a screenshot, a DM), give it a
`custom_title` instead — a short, memorable label like `"Acme referral role"`. **You need to
remember that title**, because unlike a URL it isn't unique: two jobs can share the same
`custom_title`, and a later lookup by title alone can be ambiguous (`get_job` or `analyze_job`
will refuse to guess and ask you to specify an `id` instead). Claude will remind you of this
the first time you save a job with no URL.

The job's full pasted text (`jd_text`) is stored only if you pass it to `save_job_analysis` —
`analyze_job` itself never sees or stores it, and a job you analyze but never save leaves no
trace.

## Tracking applications

```
You: "I applied to that Datadog role, sent the one-page version."
Claude: set_application_status(id=<job id>, status="applied",
                               notes="sent the one-page version")

You: "Did I apply to Datadog?"
Claude: list_jobs(company="Datadog")
        → get_job(id=<the match>) → linked resume version summaries
        → get_resume_version(id=<that version>) → the exact text you sent
```

Application status is one of 7 values: `not_applied`, `applied`, `interviewing`, `offer`,
`rejected`, `withdrawn`, `ghosted`. Transitions are deliberately unvalidated — any status can
move to any other (a reopened process is real), and `list_jobs(status=...)` takes either a
single value or a list, so "what's currently in progress" (`applied`, `interviewing`, `offer`)
is one call, not three.

### Two kinds of notes, and why they're separate

Each job carries **two** note fields, written by different tools:

- **`notes`** — the analysis. Why the score is what it is, what matched, what's missing.
  Written by `save_job_analysis`, and nothing else ever touches it.
- **`status_notes`** — the application timeline. `set_application_status` appends one dated
  line per update and never replaces what's already there.

```
notes         Score 78. Matched: Python, agents, production evidence.
              Missing: Docker, AWS. The wall is the 3–5 years.

status_notes  [2026-09-17] applied — sent the one-page version
              [2026-09-24] interviewing — recruiter screen, 30 min
              [2026-10-02] rejected — call, no reason given
```

These were one field until 0.4.0, and the collision was not theoretical: recording "applied
on the 17th" overwrote the analysis, so the highest-scoring jobs in a store were exactly the
ones whose reasoning had been destroyed. Splitting them makes that structurally impossible
instead of a rule you have to remember. To revise the analysis itself, call
`save_job_analysis` with the job's `id` — omitted fields keep their previous values.

### Deleting a job

`delete_job(id=...)` is for a record that should never have existed — a mistyped entry, a
duplicate, leftover test data. It is **not** the same as the `withdrawn` status, which says
you pulled out of a live process: that's a real event worth keeping, and treating the two as
interchangeable poisons every later query.

The job and its captured posting are deleted. **Tailored resume versions are kept**, with
their `job_id` set to `NULL` — a resume you actually sent outlives the posting it was aimed
at, so the append-only tree stays intact and only the pointer dies. There's no undo, so the
result is a receipt: it names the job and every resume version that was unlinked.

## Storage and migration

All data lives in one local SQLite database at `~/.config/runway-mcp/runway.db`. If you're
upgrading from a pre-0.3.0 install with `jobs.json`/`resumes.json`, the **first** tool call
after upgrading migrates both files into the database automatically — a `.bak` copy of each
original is written first, and the JSON files themselves are left untouched. Nothing to run,
nothing to configure.

**Schema upgrades are automatic, additive, and run once.** When a release adds something to
the database schema, the first tool call after upgrading applies it to your existing file
inside a single transaction and stamps the new schema version, so it never runs twice.
Upgrades only ever *add* — nothing is dropped, rewritten, or reordered, and your rows are
never touched. Going the other way is refused rather than attempted: a database written by a
**newer** runway-mcp than the one you're running raises an error telling you to upgrade the
package, because the file is fine and silently rewriting it to an older shape is not.

If you have an even older, pre-0.2.0 `~/.config/runway-mcp/profile.json` (a structured profile
from before resume versioning existed), it is **not** migrated automatically — the tool that
used to read it back out, and the whole structured-profile system it belonged to, are gone in
this release with no replacement. Read the file yourself (or ask Claude to), and save its
content as your general resume:

```
You: "Here's my old profile.json content — save it as my general resume text."
Claude: save_resume_version(content=<rewritten as resume text>, label="General", parent_id=None)
```

The server never reads, writes, or deletes `profile.json` — delete it yourself once you've
migrated by hand.

## How it works

Claude Code launches this server over stdio and calls its tools when relevant. You don't
invoke the tools directly — Claude decides when to call them based on the conversation.

The tools **persist and shape data**; Claude does the reasoning. The server never calls
back to the model (no MCP sampling), so it works on any MCP host — including Claude Code,
which does not support sampling. Claude drafts and tailors your resume text itself, scores
the job-vs-resume match using the rubric `analyze_job` returns, and decides what to save;
the server only persists what Claude gives it.

## Status

| Tool | Status |
|------|--------|
| `analyze_job` | Working — loads your general resume, a scoring guide, and a live work-authorization check |
| `save_job_analysis` | Working — persists an analyzed job (upserts by id, then by url) |
| `get_job` | Working — retrieves one job by id/url/custom_title, with linked resume version summaries |
| `list_jobs` | Working — lists stored jobs, filterable by company, status, score, date |
| `set_application_status` | Working — sets a stored job's application status and appends to its timeline |
| `delete_job` | Working — permanently deletes a job by id/url, keeping tailored resume versions |
| `save_resume_version` | Working — saves a resume version (raw text, append-only) |
| `get_resume_version` | Working — retrieves a resume version by id or `"latest"` |
| `list_resume_versions` | Working — lists saved resume versions, newest first |
| `set_work_authorization` | Working — declares the countries you may legally work in |

## Tools

### `analyze_job(title: str, company: str, country: str, url: str | None = None, custom_title: str | None = None) -> AnalyzeJobResult`

Read-only — writes nothing. Loads your **general resume** and a scoring guide so Claude can
score the match against the job posting text already in this conversation (the server never
fetches it). Also runs a live work-authorization comparison for `country` against your current
declaration.

Preconditions, checked in order:
- `error="no_resume"` — no usable general resume exists yet. Run `save_resume_version` first.
- `error="no_work_authorization"` — you haven't called `set_work_authorization` yet.
- `error="corrupt"` — the store exists but couldn't be read. Distinct from the two above:
  telling you to run the tool that writes to the same broken file wouldn't help.
- `error="ambiguous_custom_title"` — more than one saved job shares the `custom_title` you
  passed; re-analyze with `url` instead, or use `get_job` with a specific `id` first.

On success, returns:

```json
{
  "extracted": { "title": "...", "company": "...", "country": "...", "url": null, "custom_title": "Acme referral role" },
  "resume": { "id": "...", "label": "...", "content": "...", "parent_id": null, "job_id": null, "created_at": "..." },
  "scoring_guide": { "instructions": "...", "recommendation_rules": ["SKIP if the match score is below 40.", "..."] },
  "work_authorization": { "status": "warned", "warning": "This job's country ('Germany') is not among your declared work-authorized countries (United States)." },
  "notice": null
}
```

**Recommendation rules** (Claude applies these from `scoring_guide`):
- `SKIP` if the match score is below 40.
- `APPLY` if the match score is 70 or higher.
- `CONSIDER` in every other case.

The general resume is selected so it was never written for the job being analyzed: the most
recently saved version with no `job_id`, or — if every version is tailored to some job — the
most recent root version, excluding any tailored to the job you're analyzing now.

### `save_job_analysis(title: str, company: str, country: str, id: str | None = None, url: str | None = None, custom_title: str | None = None, jd_text: str | None = None, score: int | None = None, recommendation: str | None = None, notes: str | None = None) -> SaveJobResult`

Persists an analyzed job record. At least one of `url` or `custom_title` is required — that's
how you (or Claude) find the record again later. Resolution order: `id` given → updates that
exact record (`error="not_found"` if unknown); no `id` but `url` matches an existing record →
upsert by URL, unchanged semantics from prior releases (an omitted optional argument leaves
the existing value in place, so a bare re-save never wipes your score/notes); otherwise → a
new record. **Never matches by title** — editing a title for clarity never silently creates or
merges a duplicate.

Setting a `url` that another job already has returns `error="duplicate_url"`; neither record
changes. Saving without a `url` returns a `message` reminding you the `custom_title` is now the
only handle to this record — worth surfacing to the user, since they'll need to recall it.

### `get_job(id: str | None = None, url: str | None = None, custom_title: str | None = None, include_description: bool = False) -> GetJobResult`

Retrieves one job by exactly one of `id`, `url`, or `custom_title`. `has_description` is always
present so you can tell a description exists without paying to load it; pass
`include_description=True` to get the full pasted text back in `description`. Also returns the
linked resume version **summaries** (no content) — this is the headline query: "did I apply to
X?" → "yes, and here's the resume." Fetch the actual text with `get_resume_version`.

Because `custom_title` isn't unique, matching more than one job returns `error="ambiguous"`
naming every matching id, rather than silently picking one. Unknown id/url/custom_title →
`error="not_found"`.

### `list_jobs(since: str | None = None, status: str | list[str] | None = None, min_score: int | None = None, company: str | None = None, limit: int | None = None, sort_by: str = "analyzed_at") -> ListJobsResult`

Lists stored jobs, filtered → sorted → limited. `company` is a case-insensitive substring
match — this is the headline query, "did I apply to Acme?" `status` takes **one status or a
list of statuses**, so "what have I applied to" (`applied`, `interviewing`, `offer`, and
whatever else you consider active) is a single call. `sort_by` is `"analyzed_at"` (default,
newest first) or `"score"` (descending, unscored jobs last). Job records never include the full
pasted description (`jd_text`) — only `get_job(include_description=True)` returns it.

### `set_application_status(status: str, id: str | None = None, url: str | None = None, notes: str | None = None) -> SetStatusResult`

Sets a stored job's application status to one of the 7 values: `not_applied`, `applied`,
`interviewing`, `offer`, `rejected`, `withdrawn`, `ghosted`. **Transitions are deliberately
unvalidated** — any status can move to any other (e.g. `rejected` → `interviewing` succeeds),
because reopened hiring processes are real and the server doesn't get to say otherwise. Prefer
`id` over `url` — it always resolves, even for jobs saved without a URL. Returns
`error="not_found"` for an unknown id/url, `error="invalid_status"` for an unrecognized value
(record left unchanged).

`notes` **appends** one dated line to the job's `status_notes` timeline — `[YYYY-MM-DD] applied
— sent the one-page version` — and **never touches `notes`**, which holds the analysis. Omit it
and the timeline is left alone; only the status changes. These were a single field until the
two purposes collided in practice: recording "applied on the 17th" wiped the reasoning behind
the score, so the highest-scoring jobs in the store were exactly the ones whose analysis was
gone. To edit the analysis, call `save_job_analysis` with an `id` — omitted fields keep their
previous values.

### `delete_job(id: str | None = None, url: str | None = None) -> DeleteJobResult`

Permanently deletes a stored job. For a record that should never have existed — a mistyped
entry, a posting that turned out to be a duplicate, leftover test data. This is **not** the
same as the `withdrawn` status, which says "I pulled out of this process": that's a real event
worth keeping, and conflating the two poisons every later query.

Lookup is by `id` or `url` **only** — deliberately not `custom_title`, which `get_job` does
accept. `custom_title` isn't unique, and deleting by an ambiguous key is precisely how the
wrong record gets destroyed.

What happens to everything pointing at the job:

- The captured job description is **deleted** — it belongs to the job and means nothing without it.
- Tailored resume versions are **kept**, with `job_id` set to `NULL`. A resume you actually sent
  outlives the posting it was aimed at, so the append-only tree stays intact; only the pointer
  dies. This is why the append-only UPDATE trigger guards every column *except* `job_id`.

There is **no confirmation flag and no undo**. The result is a receipt — it names the job and
every resume version that was unlinked, so you can see exactly what disappeared. Returns
`error="invalid_input"` when neither key is given, `error="not_found"` for an unknown id/url.

### `save_resume_version(content: str, label: str, parent_id: str | None = None, job_id: str | None = None) -> SaveResumeVersionResult`

Saves a new resume version as raw text. **Append-only** — no version is ever mutated or
deleted (enforced by the database, not just application discipline), so your history is always
intact. The store enforces a single-root tree:
- The **first** version you ever save must have `parent_id=None` — this establishes your
  general resume.
- Every version after that **must** set `parent_id` to an existing version's id. Passing
  `parent_id=None` again (or an unknown id) is rejected.
- `job_id` is optional — set it when a version is tailored for a specific job. It must
  reference a job that already exists: **call `save_job_analysis` first** and pass the `id` it
  returns. An unknown `job_id` returns `error="job_not_found"`, not a raw database error.

Returns `error="invalid_parent"` (empty store expects `parent_id=None`, non-empty store
requires it) or `error="parent_not_found"` (unknown `parent_id`) on failure — no version is
written in either case.

### `get_resume_version(id: str) -> GetResumeVersionResult`

Retrieves one resume version by its exact `id`, or the most recently *created* version via
`id="latest"` (not necessarily the general one — if the newest version is tailored to some
job, `"latest"` returns that). Returns `error="not_found"` if no such version exists.

### `list_resume_versions(job_id: str | None = None, limit: int | None = None) -> ListResumeVersionsResult`

Lists saved resume versions **newest first**, as summaries — no `content` field, so listing 20
versions doesn't dump 20 full resumes into context. Call `get_resume_version` once you know
which id you want. Filter to versions tailored for one job with `job_id` (the id
`save_job_analysis` returned for that job).

### `set_work_authorization(countries: list[str]) -> SetWorkAuthorizationResult`

Declares the **full** list of countries you may legally work in — **replaces** any prior
declaration, it does not add to it. Pass an empty list to explicitly declare you're authorized
nowhere (distinct from never having called this tool at all — `analyze_job` treats the two
differently). Country names are free text; the server canonicalizes common spellings (`"USA"`,
`"United States"`, `"U.S."` all match) but never rejects an unrecognized one — it echoes back
both the raw text you gave and the canonical form it stored, so a misread is visible
immediately rather than causing a silent false warning later.

## Tool vs. reasoning boundary

These tools only **persist and shape data**. Claude handles all reasoning:
- Scoring the match between the resume and the pasted posting
- Interpreting the work-authorization warning in context
- Whether the role is a good fit overall

This is intentional — tools that encode judgment make Claude less useful, not more.

## Tests

```bash
pytest -m contract      # fast contract tests
pytest -m integration   # server tool registration
pytest                  # full suite
```

## Contributing

```bash
pip install -e ".[dev]"
pre-commit install       # runs ruff lint + format before every commit
```

PRs welcome.

## Releasing

Publishing is driven by a tag. Bump the version in **all six** places listed in
[RELEASING.md](RELEASING.md) — `pyproject.toml`, `manifest.json` (both its `version` and
its `--from` pin), the plugin's `plugin.json`, the plugin's `.mcp.json` pin, and the
manual-install snippet above. Pins are `runway-mcp==X.Y.Z`, resolved from PyPI.
`test_every_version_site_agrees_with_pyproject` fails the build if any of them drift.
Then push the tag **from the release branch, and merge only once the `publish` job is
green**:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

`.github/workflows/release.yml` takes it from there: it checks that the tag names the same
version as `pyproject.toml` — the remaining five sites are checked against `pyproject.toml`
by `test_every_version_site_agrees_with_pyproject` in the same run, so the two together pin
all six — then runs lint and the full test suite on Python 3.11/3.12/3.13, builds the sdist
and wheel, runs `twine check`, and only then uploads.
Merge to `master` once that upload is done — the marketplace serves `master`, so a pin that
lands there before PyPI has the version breaks every install until the upload catches up.

Everything that can fail runs *before* the upload on purpose — PyPI never lets a
version number be reused, even after a delete, so a bad publish cannot be undone,
only superseded.

**The upload is currently switched off.** The `publish` job is gated on the repository
variable `PYPI_PUBLISH_ENABLED`, because the Trusted Publishing publisher below was never
configured and every tag died at the upload with `invalid-publisher`. A tag today runs the
full verify job and then *skips* the upload rather than failing it. Set that variable to
`"true"` once the publisher exists — it needs no code change. Meanwhile the pins point at
the git tag, so releases work; PyPI just stays at `0.1.2`.

**One-time setup.** The workflow authenticates with [PyPI Trusted
Publishing](https://docs.pypi.org/trusted-publishers/) rather than an API token,
so there is no long-lived secret in the repo to leak or rotate. GitHub signs a
short-lived token per run and PyPI verifies the signature.

`runway-mcp` already exists on PyPI, so this is a publisher on an existing
project — not a *pending* publisher, which is the separate flow for a name that
has never been published. Go to
<https://pypi.org/manage/project/runway-mcp/settings/publishing/>, choose
**GitHub** under *Add a new publisher*, and fill in exactly:

| Field | Value |
|---|---|
| Owner | `satovarb16` |
| Repository name | `runwayMCP` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

All four must match or PyPI rejects the token — the environment name in
particular, since it is what scopes the trust to the gated job rather than to
any workflow in the repo.

On the GitHub side, the `pypi` environment is created automatically the first
time the workflow runs. Create it yourself under *Settings → Environments* if you
want to add required reviewers first, which gates the upload behind a manual
approval — worth doing, given that a published version can never be reused.

## License

[MIT](LICENSE) © satovarb
