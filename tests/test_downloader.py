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
    assert (tmp_path / "pdfs" / "0001_Smith_2021.pdf").exists()
    assert (tmp_path / "pdfs" / "0001_Smith_2021.json").exists()
