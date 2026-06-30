"""Tests for the LaTeX-safety integrity layer (transcoding-seam contracts I1-I7).

These are pure, network-free unit/property tests. The integrity layer must
make the entity/ampersand seam provably closed, minimal, and idempotent.
"""
import random
import string
import unicodedata

import pytest

from paperforge.latex_safety import (
    DEFAULT_SPECIALS,
    SeamLeak,
    assert_seam_closed,
    convert_markup,
    decode_entities,
    escape_specials,
    find_residue,
    normalize_spaces,
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
        " ", " ", "​",   # exotic spaces / zero-width too
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


# ---- HTML inline markup -> LaTeX commands ----------------------------------

def test_convert_markup_known_inline_tags():
    assert convert_markup("<i>Cyprinus carpio</i>") == r"\textit{Cyprinus carpio}"
    assert convert_markup("<em>y</em>") == r"\emph{y}"
    assert convert_markup("<b>x</b>") == r"\textbf{x}"
    assert convert_markup("<strong>x</strong>") == r"\textbf{x}"
    assert convert_markup("Fe<sub>3</sub>O<sub>4</sub>") == r"Fe\textsubscript{3}O\textsubscript{4}"
    assert convert_markup("E<sup>2</sup>") == r"E\textsuperscript{2}"
    assert convert_markup("<sc>dna</sc>") == r"\textsc{dna}"


def test_convert_markup_case_insensitive_and_attributes():
    assert convert_markup("<I>a</I>") == r"\textit{a}"
    assert convert_markup('<i xmlns="x">a</i>') == r"\textit{a}"


def test_convert_markup_nested():
    assert convert_markup("<i><b>x</b></i>") == r"\textit{\textbf{x}}"


def test_convert_markup_leaves_non_markup_alone():
    assert convert_markup("a < b and c > d") == "a < b and c > d"   # bare angle brackets
    assert convert_markup("<unknown>z</unknown>") == "<unknown>z</unknown>"  # unknown tag
    assert convert_markup("<i>open only") == "<i>open only"          # unpaired


def test_sanitize_converts_markup_and_escapes_amp_together():
    assert sanitize("<i>Salmo &amp; trutta</i>") == r"\textit{Salmo \& trutta}"


def test_sanitize_handles_entity_encoded_markup():
    # Crossref occasionally serves the tags entity-encoded.
    assert sanitize("&lt;i&gt;abc&lt;/i&gt;") == r"\textit{abc}"


# ---- non-standard Unicode spaces -> ASCII (inputenc utf8 compile safety) ----

def test_normalize_spaces_unicode_separators_become_ascii():
    # U+2005 FOUR-PER-EM SPACE, as Crossref encodes "Jon A." given names.
    assert normalize_spaces("Jon A.") == "Jon A."
    # a representative spread of Zs separators all fold to one ASCII space:
    # NBSP, en/em quad, three/four-per-em, six-per-em, thin, hair, narrow NBSP.
    for cp in (" ", " ", " ", " ", " ",
               " ", " ", " ", " "):
        assert normalize_spaces("a" + cp + "b") == "a b"


def test_normalize_spaces_drops_zero_width():
    assert normalize_spaces("a​b") == "ab"   # ZERO WIDTH SPACE
    assert normalize_spaces("a‌b") == "ab"   # ZERO WIDTH NON-JOINER
    assert normalize_spaces("a‍b") == "ab"   # ZERO WIDTH JOINER


def test_normalize_spaces_preserves_dashes_accents_and_ascii():
    assert normalize_spaces("337–345") == "337–345"   # en dash (Pd) kept
    assert normalize_spaces("a—b") == "a—b"           # em dash (Pd) kept
    assert normalize_spaces("Könemann ø ç") == "Könemann ø ç"   # accents untouched
    assert normalize_spaces("plain  ascii spaces") == "plain  ascii spaces"


def test_normalize_spaces_idempotent():
    once = normalize_spaces("Jon A. Arnot​")
    assert normalize_spaces(once) == once


def test_sanitize_normalizes_nbsp_entity():
    # &nbsp; decodes to U+00A0, then normalizes to a plain ASCII space.
    assert sanitize("Jon&nbsp;A.") == "Jon A."


def test_sanitize_repro_four_per_em_space_in_author():
    out = sanitize("author = {Arnot, Jon A.}")
    assert out == "author = {Arnot, Jon A.}"
    # no Zs-but-not-ASCII-space character survives
    leftover = [c for c in out if c != " " and unicodedata.category(c) == "Zs"]
    assert leftover == []


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
