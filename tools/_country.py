"""Pure country-name canonicalization (design D7).

No I/O, no pydantic — deliberately not folded into tools/_db.py (#334 D1's
"a shared module must not become a grab-bag" rule). Ported logic from the
deleted tools/jobs.py::_normalize_country, with a different canonical
spelling chosen at design time ("UNITED STATES"/"UNITED KINGDOM" rather than
the old "USA"/"UK") so the canonical form is unambiguous prose, matching
what a work-authorization mismatch warning shows the user verbatim.

Free text in, canonicalized at write, both forms stored — see
tools/work_auth.py (PR3b). This module only does the string transform.
"""

from __future__ import annotations

import re

_ALIASES: dict[str, str] = {
    "US": "UNITED STATES",
    "USA": "UNITED STATES",
    "UNITED STATES OF AMERICA": "UNITED STATES",
    "UK": "UNITED KINGDOM",
    "GB": "UNITED KINGDOM",
    "GREAT BRITAIN": "UNITED KINGDOM",
    "ENGLAND": "UNITED KINGDOM",
}

_PUNCTUATION_RE = re.compile(r"[.,]")
_WHITESPACE_RE = re.compile(r"\s+")


def _canonical_country(raw: str) -> str:
    """Canonicalize a free-text country name for comparison purposes only.

    Strips leading/trailing whitespace, collapses internal whitespace,
    strips periods/commas, uppercases, then looks the result up in a small
    alias table. Unknown values pass through as their uppercased,
    punctuation-stripped selves — NEVER rejected. Rejecting anything not in
    the table would break for every country nobody thought to list; the
    warning this feeds (design D7) must fail visibly (a wrong echoed country
    name) rather than silently (a rejected input).

    Args:
        raw: Free-text country name, as extracted by Claude or declared by
             the user.

    Returns:
        The canonical uppercase form for comparison. Not intended for
        display — the raw form is what gets echoed back to the user.
    """
    stripped = _PUNCTUATION_RE.sub("", raw.strip())
    collapsed = _WHITESPACE_RE.sub(" ", stripped).strip()
    upper = collapsed.upper()
    return _ALIASES.get(upper, upper)
