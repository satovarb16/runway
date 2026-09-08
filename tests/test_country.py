"""Tests for tools/_country.py — pure country canonicalization (design D7).

No I/O, no pydantic, deliberately not folded into _db.py (#334 D1's
grab-bag rule).
"""

from __future__ import annotations


def test_us_aliases_canonicalize_to_united_states():
    from tools._country import _canonical_country

    # "U.S."/"U.S.A." have no _ALIASES entry of their own and must not get
    # one: punctuation is stripped BEFORE the lookup, so they arrive as
    # "US"/"USA". "united states" needs no entry either -- an unknown key
    # falls through to itself, which is already the canonical form.
    for raw in [
        "US",
        "USA",
        "U.S.",
        "U.S.A.",
        "United States of America",
        "united states",
    ]:
        assert _canonical_country(raw) == "UNITED STATES"


def test_uk_aliases_canonicalize_to_united_kingdom():
    from tools._country import _canonical_country

    for raw in ["UK", "U.K.", "GB", "Great Britain", "England", "united kingdom"]:
        assert _canonical_country(raw) == "UNITED KINGDOM"


def test_unknown_value_passes_through_uppercased_never_rejected():
    from tools._country import _canonical_country

    assert _canonical_country("Narnia") == "NARNIA"


def test_whitespace_and_punctuation_are_collapsed_before_comparison():
    from tools._country import _canonical_country

    assert _canonical_country("  u.s.a.  ") == "UNITED STATES"


def test_canonicalization_is_case_insensitive():
    from tools._country import _canonical_country

    assert _canonical_country("uSa") == _canonical_country("USA")
