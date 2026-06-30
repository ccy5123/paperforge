import os

import pytest

from paperforge.bibtex import (
    BibCollection,
    cite_key_stem,
    fetch_bibtex,
    parse_bibtex,
    rekey,
)
from paperforge.config import Config
from paperforge.latex_safety import find_residue, sanitize

SAMPLE = (
    "@article{vaswani_2017_attention,\n"
    "  title = {Attention Is All You Need},\n"
    "  author = {Vaswani, Ashish and Shazeer, Noam},\n"
    "  year = {2017},\n"
    "  journal = {NeurIPS}\n"
    "}"
)


# ---- pure parsing / rekeying ----

def test_parse_bibtex_splits_entry():
    typ, key, body = parse_bibtex(SAMPLE)
    assert typ == "article"
    assert key == "vaswani_2017_attention"
    assert "Attention Is All You Need" in body


def test_parse_bibtex_rejects_garbage():
    assert parse_bibtex("not bibtex at all") is None
    assert parse_bibtex("") is None
    assert parse_bibtex(None) is None


def test_rekey_replaces_only_the_key():
    out = rekey(SAMPLE, "Vaswani2017")
    assert out.startswith("@article{Vaswani2017,")
    assert "vaswani_2017_attention" not in out
    assert "author = {Vaswani, Ashish and Shazeer, Noam}" in out  # body untouched


def test_cite_key_stem_matches_filename_style():
    assert cite_key_stem("Vaswani, Ashish and Shazeer, Noam", "2017") == "Vaswani2017"
    assert cite_key_stem("", "") == "Unknown"


# ---- accumulation, keys, collisions, dedup, order ----

def test_add_rewrites_key_from_bib_metadata():
    coll = BibCollection()
    e = coll.add("10.48550/arxiv.1706.03762", SAMPLE)
    assert e.key == "Vaswani2017"
    assert e.text.startswith("@article{Vaswani2017,")


def test_collision_gets_letter_suffix():
    coll = BibCollection()
    e1 = coll.add("10.1000/a", "@article{a, author={Smith, J}, year={2020}}")
    e2 = coll.add("10.1000/b", "@article{b, author={Smith, K}, year={2020}}")
    e3 = coll.add("10.1000/c", "@article{c, author={Smith, L}, year={2020}}")
    assert e1.key == "Smith2020"
    assert e2.key == "Smith2020a"
    assert e3.key == "Smith2020b"


def test_explicit_author_year_override_bib_content():
    coll = BibCollection()
    e = coll.add("10.1000/x", "@article{x, author={WRONG, Z}, year={1900}}",
                 author="Smith, John", year="2020")
    assert e.key == "Smith2020"


def test_same_doi_deduped():
    coll = BibCollection()
    coll.add("10.1000/z", "@misc{z, author={Zed, A}, year={2019}}")
    coll.add("10.1000/z", "@misc{z2, author={Zed, A}, year={2019}}")  # same DOI
    assert coll.count == 1


def test_render_sorted_and_deterministic():
    coll = BibCollection()
    coll.add("10.1000/z", "@misc{z, author={Zed, A}, year={2019}}")
    coll.add("10.1000/a", "@article{a, author={Adams, B}, year={2018}}")
    out = coll.render()
    assert out.index("Adams2018") < out.index("Zed2019")   # sorted by key
    assert out.count("@") == 2


# ---- rigor: misses are recorded, omitted, never fabricated ----

def test_miss_recorded_and_omitted():
    coll = BibCollection()
    assert coll.add("10.1000/empty", "") is None
    assert coll.add("10.1000/garbage", "<html>nope</html>") is None
    assert coll.add("10.1000/none", None) is None
    assert coll.count == 0
    assert set(coll.misses) == {"10.1000/empty", "10.1000/garbage", "10.1000/none"}
    out = coll.render()
    assert "% unresolved (no BibTeX): 10.1000/garbage" in out


def test_non_ascii_roundtrips_without_corruption():
    coll = BibCollection()
    raw = "@article{x, title={Café Möld}, author={Müller, Ångström}, year={2021}}"
    e = coll.add("10.1000/u", raw)
    assert e.key == "Mller2021"            # key is ASCII-slugged (matches PDF name)
    assert "Café Möld" in e.text           # body preserved verbatim
    assert "Müller" in e.text


# ---- integrity layer wired into add(): seam is closed at storage time ------

def test_add_sanitizes_entity_at_the_seam():
    coll = BibCollection()
    raw = ("@article{src, title={Environmental Science &amp; Technology}, "
           "author={Doe, Jane}, year={2021}}")
    e = coll.add("10.1021/es", raw)
    assert "&amp;" not in e.text
    assert "Environmental Science \\& Technology" in e.text
    assert not find_residue(e.text)        # I6: stored entry is seam-closed
    assert "&amp;" not in coll.render()


def test_add_structural_identity_field_value_only():
    """I2: only field *values* change; key, type, field names and counts stay."""
    bibtexparser = pytest.importorskip("bibtexparser")
    raw = ("@article{Doe2021, title={A &amp; B}, author={Doe, Jane}, "
           "year={2021}, journal={X &amp; Y}}")
    # sanitize in isolation is field-value-only and structurally identical
    before = bibtexparser.parse_string(raw).entries[0]
    after = bibtexparser.parse_string(sanitize(raw)).entries[0]
    assert before.key == after.key
    assert before.entry_type == after.entry_type
    assert [f.key for f in before.fields] == [f.key for f in after.fields]
    assert len(before.fields) == len(after.fields)
    assert after["journal"] == "X \\& Y"   # the value, and only the value, changed

    # and through the real add() path: stored entry parses with the same shape
    coll = BibCollection()
    e = coll.add("10.1/d", raw, author="Doe, Jane", year="2021")
    stored = bibtexparser.parse_string(e.text).entries[0]
    assert stored.entry_type == "article"
    assert [f.key for f in stored.fields] == ["title", "author", "year", "journal"]


def test_add_decimal_and_hex_entities_decode_at_seam():
    coll = BibCollection()
    raw = "@article{x, title={Don&#39;t &#x2019;quote&#x2019;}, author={A, B}, year={2020}}"
    e = coll.add("10.1/q", raw)
    assert "Don't" in e.text
    assert "’quote’" in e.text
    assert not find_residue(e.text)


# ---- fetch_bibtex is now a thin delegate to the habanero-backed sourcing layer ----

def test_fetch_bibtex_delegates_to_sourcing_and_returns_raw(monkeypatch):
    import paperforge.bibtex as bibtex_mod
    from paperforge.sourcing import SourcedEntry

    seen = {}

    def fake_sourced(doi, *, mailto):
        seen["doi"] = doi
        seen["mailto"] = mailto
        return SourcedEntry(doi=doi, raw_bibtex="@article{x, title={Ünïcøde}}",
                            agency=None, retrieved_at=__import__("datetime").datetime.now())

    monkeypatch.setattr(bibtex_mod, "fetch_canonical_bibtex", fake_sourced)
    out = fetch_bibtex("10.1000/x", None, Config(unpaywall_email="e@example.org"))
    assert out == "@article{x, title={Ünïcøde}}"     # raw body passed straight through
    assert seen["doi"] == "10.1000/x"
    assert seen["mailto"] == "e@example.org"          # config email -> polite-pool mailto


def test_fetch_bibtex_miss_returns_none(monkeypatch):
    import paperforge.bibtex as bibtex_mod
    monkeypatch.setattr(bibtex_mod, "fetch_canonical_bibtex",
                        lambda doi, *, mailto: None)
    assert fetch_bibtex("10.1000/x", None, Config()) is None


# ---- one real-network smoke test, skipped unless explicitly enabled ----

@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_NETWORK_TESTS") != "1",
    reason="network test; set PAPERFORGE_NETWORK_TESTS=1 to run",
)
def test_smoke_real_doi_content_negotiation():
    out = fetch_bibtex("10.1145/3292500.3330701", None,
                       Config(unpaywall_email="test@example.org"))
    assert out and out.lstrip().startswith("@")
