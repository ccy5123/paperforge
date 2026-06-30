"""Tests for the LaTeX-safety integrity layer (transcoding-seam contracts I1-I7).

These are pure, network-free unit/property tests. The integrity layer must
make the entity/ampersand seam provably closed, minimal, and idempotent.
"""
import random
import string

import pytest

from paperforge.latex_safety import (
    DEFAULT_SPECIALS,
    SeamLeak,
    assert_seam_closed,
    decode_entities,
    escape_specials,
    find_residue,
    sanitize,
)


# ---- core seam behavior ----------------------------------------------------

def test_amp_entity_becomes_escaped_amp():
    out = sanitize("journal = {QSAR &amp; Combinatorial Science}")
    assert "QSAR \\& Combinatorial Science" in out
    assert "&amp;" not in out
    # no *bare* ampersand survives
    assert "&" not in out.replace("\\&", "")


def test_already_escaped_amp_is_unchanged_idempotent():
    body = "title = {Acids \\& Bases}"
    assert sanitize(body) == body          # I1: \& stays \&
    assert "\\\\&" not in sanitize(body)   # never \\&


def test_bare_amp_is_escaped():
    assert sanitize("A & B") == "A \\& B"


# ---- I3: decode totality (named / decimal / hex) ---------------------------

@pytest.mark.parametrize("raw,expect", [
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&#39;", "'"),
    ("&#x2019;", "’"),
    ("&quot;", '"'),
])
def test_decode_entities_all_classes(raw, expect):
    assert decode_entities(raw) == expect


# ---- I1: idempotence (property) --------------------------------------------

def _rand_text(rng):
    pool = list(string.ascii_letters + "0123456789 .,&;#%{}\\$_^~<>'\"") + [
        "&amp;", "&lt;", "&gt;", "&#39;", "&#x2019;", "δ", "ë",
    ]
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, 40)))


def test_idempotence_property():
    rng = random.Random(1234)
    for _ in range(500):
        x = _rand_text(rng)
        once = sanitize(x)
        assert sanitize(once) == once       # I1


# ---- I7: minimality (non-&/non-entity chars are untouched) -----------------

def test_minimality_no_amp_no_entity_is_identity():
    rng = random.Random(99)
    safe_pool = string.ascii_letters + "0123456789 .,{}\\$_^~<>'\"" + "δëå"
    for _ in range(500):
        x = "".join(rng.choice(safe_pool) for _ in range(rng.randint(0, 30)))
        # nothing here is an ampersand or an HTML entity -> untouched
        assert sanitize(x) == x             # I7


def test_minimality_only_amp_region_changes():
    out = sanitize("prefix δ13C & Daëron suffix")
    assert out == "prefix δ13C \\& Daëron suffix"


# ---- I6: seam-closed postcondition -----------------------------------------

def test_find_residue_flags_then_clears():
    leaky = "Environmental Science &amp; Technology & more <foo> &#39;x&#39;"
    assert find_residue(leaky)              # non-empty before
    assert not find_residue(sanitize(leaky))  # empty after  (I6)


def test_assert_seam_closed_raises_on_leak_and_passes_when_clean():
    with pytest.raises(SeamLeak):
        assert_seam_closed("a &amp; b")
    with pytest.raises(SeamLeak):
        assert_seam_closed("bare & amp")
    assert_seam_closed(sanitize("a &amp; b"))  # no raise


# ---- I4/I5: conservative escape set, non-ASCII preserved -------------------

def test_default_specials_is_only_ampersand():
    assert DEFAULT_SPECIALS == ("&",)


def test_load_bearing_chars_untouched():
    body = r'title = {{ACS} $\alpha$ a\_b 50\% C\#}'
    # no &, no entities -> exact passthrough; braces/backslash/math left alone
    assert sanitize(body) == body


def test_non_ascii_preserved():
    s = "δ13C Daëron Ångström café"
    assert sanitize(s) == s                  # I5


def test_escape_specials_extensible_set_still_idempotent():
    once = escape_specials("50% off", specials=("&", "%"))
    assert once == "50\\% off"
    assert escape_specials(once, specials=("&", "%")) == once  # I1 across the set
