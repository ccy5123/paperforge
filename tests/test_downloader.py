from paperforge.config import Config
from paperforge.downloader import OADownloader, Resolution, resolve_arxiv


class FakeResp:
    def __init__(self, status=200, json_data=None, content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def close(self):
        pass


def make_downloader(**cfg):
    return OADownloader(Config(unpaywall_email="t@e.org", **cfg))


# ---- arXiv (pure, no network) ----

def test_resolve_arxiv_modern():
    r = resolve_arxiv("10.48550/arxiv.2301.12345", None, None)
    assert r.downloadable
    assert r.pdf_url == "https://arxiv.org/pdf/2301.12345"
    assert r.license is None          # not hardcoded anymore


def test_resolve_arxiv_legacy_slash_id():
    r = resolve_arxiv("10.48550/arxiv.math.GT/0309136", None, None)
    assert r.pdf_url == "https://arxiv.org/pdf/math.GT/0309136"


def test_resolve_arxiv_non_arxiv_returns_none():
    assert resolve_arxiv("10.1038/abc", None, None) is None


# ---- license policy ----

def test_unknown_license_kept_by_default():
    d = make_downloader(allowed_licenses={"cc-by"})
    res = Resolution(doi="x", source="s", pdf_url="u", license=None, is_oa=True)
    assert d._license_ok(res) is True


def test_unknown_license_dropped_when_required():
    d = make_downloader(allowed_licenses={"cc-by"}, require_known_license=True)
    res = Resolution(doi="x", source="s", pdf_url="u", license=None, is_oa=True)
    assert d._license_ok(res) is False


def test_license_substring_match():
    d = make_downloader(allowed_licenses={"cc-by"})
    assert d._license_ok(Resolution("x", "s", pdf_url="u", license="cc-by-nc", is_oa=True))
    assert not d._license_ok(Resolution("x", "s", pdf_url="u", license="cc0", is_oa=True))


def test_no_filter_accepts_everything():
    d = make_downloader()
    assert d._license_ok(Resolution("x", "s", pdf_url="u", license=None, is_oa=True))


# ---- PDF fetch validation ----

def test_fetch_pdf_rejects_html(monkeypatch):
    d = make_downloader()
    monkeypatch.setattr(
        d.session, "get",
        lambda url, **kw: FakeResp(200, content=b"<html>nope</html>",
                                   headers={"content-type": "text/html"}),
    )
    ok, content = d._fetch_pdf("http://x")
    assert ok is False


def test_fetch_pdf_accepts_pdf_magic(monkeypatch):
    d = make_downloader()
    monkeypatch.setattr(
        d.session, "get",
        lambda url, **kw: FakeResp(200, content=b"%PDF-1.7 stuff"),
    )
    ok, content = d._fetch_pdf("http://x")
    assert ok and content.startswith(b"%PDF-")


# ---- end-to-end via arXiv branch (no real network) ----

def test_fetch_writes_pdf_and_sidecar(monkeypatch, tmp_path):
    d = make_downloader(output_dir=tmp_path)
    monkeypatch.setattr(
        d.session, "get",
        lambda url, **kw: FakeResp(200, content=b"%PDF-1.7 data"),
    )
    outcome = d.fetch("10.48550/arxiv.2301.12345", 1, "Smith", "2021", "Title")
    assert outcome.ok and outcome.source == "arxiv"
    assert outcome.filename == "Smith2021.pdf"
    assert (tmp_path / "pdfs" / "Smith2021.pdf").exists()
    assert (tmp_path / "pdfs" / "Smith2021.json").exists()


def test_fetch_enriches_filename_when_metadata_missing(monkeypatch, tmp_path):
    d = make_downloader(output_dir=tmp_path)

    def fake_get(url, **kw):
        if "openalex.org" in url:
            return FakeResp(200, json_data={
                "authorships": [{"author": {"display_name": "Ashish Vaswani"}}],
                "publication_year": 2017,
                "title": "Attention Is All You Need",
            })
        return FakeResp(200, content=b"%PDF-1.7 data")

    monkeypatch.setattr(d.session, "get", fake_get)
    outcome = d.fetch("10.48550/arxiv.1706.03762", 1, "", "", "")   # no author/year given
    assert outcome.ok
    assert outcome.filename == "Vaswani2017.pdf"
    assert (tmp_path / "pdfs" / "Vaswani2017.pdf").exists()


def test_no_metadata_flag_disables_lookup(monkeypatch, tmp_path):
    d = make_downloader(output_dir=tmp_path, enrich_metadata=False)
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return FakeResp(200, content=b"%PDF-1.7 data")

    monkeypatch.setattr(d.session, "get", fake_get)
    outcome = d.fetch("10.48550/arxiv.1706.03762", 1, "", "", "")
    assert outcome.ok
    assert (tmp_path / "pdfs" / "Unknown.pdf").exists()
    assert all("openalex" not in u for u in seen)   # no metadata call made


def test_filename_collision_disambiguates(monkeypatch, tmp_path):
    d = make_downloader(output_dir=tmp_path)
    monkeypatch.setattr(d.session, "get",
                        lambda url, **kw: FakeResp(200, content=b"%PDF-1.7 data"))

    # Two different DOIs, same author+year -> must not clobber each other.
    o1 = d.fetch("10.48550/arxiv.1111.1", 1, "Smith", "2021", "")
    o2 = d.fetch("10.48550/arxiv.2222.2", 2, "Smith", "2021", "")
    assert o1.filename == "Smith2021.pdf"
    assert o2.filename == "Smith2021a.pdf"          # letter suffix, matching bib keys
    assert (tmp_path / "pdfs" / "Smith2021.pdf").exists()
    assert (tmp_path / "pdfs" / "Smith2021a.pdf").exists()

    # Re-saving the SAME DOI reuses its name (overwrite in place, no new entry).
    o3 = d.fetch("10.48550/arxiv.1111.1", 1, "Smith", "2021", "")
    assert o3.filename == "Smith2021.pdf"
    assert not (tmp_path / "pdfs" / "Smith2021b.pdf").exists()
