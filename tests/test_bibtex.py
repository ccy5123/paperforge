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


# ---- thin network function: behavior is testable offline by injecting a session ----

class _Resp:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.last = None
        self.headers = {}

    def get(self, url, **kw):
        self.last = (url, kw)
        return self._resp


def test_fetch_bibtex_sends_accept_header_and_decodes_utf8():
    sess = _Session(_Resp(200, "@article{x, title={Ünïcøde}}".encode("utf-8")))
    out = fetch_bibtex("10.1000/x", sess, Config(unpaywall_email="e@example.org"))
    assert out.startswith("@article")
    assert "Ünïcøde" in out
    url, kw = sess.last
    assert url == "https://doi.org/10.1000/x"
    assert kw["headers"]["Accept"] == "application/x-bibtex"
    assert "paperforge" in kw["headers"]["User-Agent"]
    assert "e@example.org" in kw["headers"]["User-Agent"]


def test_fetch_bibtex_non_200_is_miss():
    assert fetch_bibtex("10.1000/x", _Session(_Resp(404)), Config()) is None


def test_fetch_bibtex_non_bibtex_body_is_miss():
    sess = _Session(_Resp(200, b"<!DOCTYPE html><html>landing page</html>"))
    assert fetch_bibtex("10.1000/x", sess, Config()) is None


# ---- one real-network smoke test, skipped unless explicitly enabled ----

@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_NETWORK_TESTS") != "1",
    reason="network test; set PAPERFORGE_NETWORK_TESTS=1 to run",
)
def test_smoke_real_doi_content_negotiation():
    import requests

    out = fetch_bibtex("10.1145/3292500.3330701", requests.Session(),
                       Config(unpaywall_email="test@example.org"))
    assert out and out.lstrip().startswith("@")
