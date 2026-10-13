"""Network-free tests for the standalone tools/si_harvest.py.

HTTP is served by a routing fake session; the browser fallback is exercised
against a local server with a Cloudflare-style challenge (skipped when
Playwright / Chromium aren't available).
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import socket
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

_PATH = Path(__file__).resolve().parents[1] / "tools" / "si_harvest.py"
_spec = importlib.util.spec_from_file_location("si_harvest", _PATH)
si = importlib.util.module_from_spec(_spec)
sys.modules["si_harvest"] = si
_spec.loader.exec_module(si)


# ---------------------------------------------------------------- fakes

PDF = b"%PDF-1.4 fake pdf body"
PDF2 = b"%PDF-1.5 another pdf"


def _office(kind: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(f"{kind}/document.xml", "<doc/>")
    return buf.getvalue()


DOCX, XLSX = _office("word"), _office("xl")
CHALLENGE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
             b"<body><div id='cf-browser-verification'>Checking your browser</div></body></html>")


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, url=None):
        self.status_code = status
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = CaseInsensitiveDict(headers or {})
        self.url = url

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


def J(obj):
    return FakeResp(200, json.dumps(obj), {"content-type": "application/json"})


def page(*anchors, extra=""):
    links = "".join(f'<a href="{href}">{label}</a>' for href, label in anchors)
    filler = "".join(f'<a href="/nav/{i}">nav {i}</a>' for i in range(20))
    return FakeResp(200, f"<html><head>{extra}</head><body>{links}{filler}"
                         f"{'<p>text</p>' * 4000}</body></html>",
                    {"content-type": "text/html; charset=utf-8"})


class FakeSession:
    """Routes by substring of the full URL (query included); first match wins."""

    def __init__(self, routes):
        self.routes = list(routes.items())
        self.calls: list[str] = []
        self.cookies = RequestsCookieJar()
        self.headers = {}

    def _route(self, url, params):
        full = url + ("?" + urlencode(params) if params else "")
        self.calls.append(full)
        for needle, resp in self.routes:
            if needle in full:
                if isinstance(resp, Exception):
                    raise resp
                return FakeResp(resp.status_code, resp.body, resp.headers, resp.url or full)
        return FakeResp(404, b"not found", {"content-type": "text/plain"}, full)

    def get(self, url, params=None, **kw):
        return self._route(url, params)

    def post(self, url, json=None, params=None, **kw):
        return self._route(url + "#post=" + __import__("json").dumps(json, sort_keys=True), params)

    def close(self):
        pass


def make_harvester(tmp_path, routes, **kw):
    session = FakeSession(routes)
    http = si.Http(email="t@example.org", delay=0, session=session)
    kw.setdefault("browser", "off")
    return si.Harvester(http, tmp_path / "out", **kw), session


def crossref(family, year, **extra):
    return J({"message": {"author": [{"family": family}],
                          "issued": {"date-parts": [[year, 1, 1]]}, **extra}})


# ---------------------------------------------------------------- inputs & naming

def test_read_dois_from_files_and_bare_arguments(tmp_path):
    txt = tmp_path / "dois.txt"
    txt.write_text("# comment 10.9999/ignored\n10.1021/es301921w\n"
                   "https://doi.org/10.1002/etc.2572.\n10.1016/0045-6535(80)90150-2\n")
    csv_file = tmp_path / "refs.csv"
    csv_file.write_text("title,doi\nA,10.1021/ES301921W\nB,doi:10.1371/journal.pcbi.1006100\n")
    got = si.read_dois([str(txt), str(csv_file), "(10.1039/c9ee01234a)", "nonsense"])
    assert got == ["10.1021/es301921w", "10.1002/etc.2572", "10.1016/0045-6535(80)90150-2",
                   "10.1371/journal.pcbi.1006100", "10.1039/c9ee01234a"]


def test_stem_transliterates_and_falls_back_to_doi():
    assert si.stem_base("Könemann", "2018", "10.1/x") == "Konemann2018"
    assert si.stem_base("", "", "10.1021/es301921w") == "10_1021_es301921w"


def test_assign_stem_suffixes_collisions_but_keeps_same_doi(tmp_path):
    h, _ = make_harvester(tmp_path, {})
    meta = si.Meta(author="Smith", year="2012")
    assert h.assign_stem("10.1/a", meta) == "Smith2012"
    assert h.assign_stem("10.1/b", meta) == "Smith2012a"
    assert h.assign_stem("10.1/A", meta) == "Smith2012"


def test_detect_ext_prefers_server_name_then_url_then_bytes():
    assert si.detect_ext(PDF, names=['', 'es1_si_001.xlsx']) == ".xlsx"
    assert si.detect_ext(PDF, url="https://x.org/a/b/file.pdf?x=1") == ".pdf"
    assert si.detect_ext(DOCX, url="https://x.org/download/123") == ".docx"
    assert si.detect_ext(XLSX) == ".xlsx"
    assert si.detect_ext(b"a,b\n1,2\n", content_type="text/csv") == ".csv"
    assert si._cd_filename("attachment; filename*=UTF-8''S1%20Data.xlsx") == "S1 Data.xlsx"
    assert si._cd_filename('attachment; filename="pcbi.s001.pdf"') == "pcbi.s001.pdf"


# ---------------------------------------------------------------- link detection

@pytest.mark.parametrize("url,label,expected", [
    ("https://pubs.acs.org/doi/suppl/10.1021/es301921w/suppl_file/es301921w_si_001.pdf", "", True),
    ("https://setac.onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fetc.2572"
     "&file=etc2572-sup-0001-SupData.pdf", "", True),
    ("https://static-content.springer.com/esm/art%3A10.1007%2Fs1/MediaObjects/1_MOESM1_ESM.docx", "", True),
    ("https://static-content.springer.com/esm/art%3A10.1007%2Fs1/MediaObjects/1_MOESM2_ESM.jpg", "", True),
    ("https://ars.els-cdn.com/content/image/1-s2.0-S0045653520303878-mmc1.pdf", "", True),
    ("https://journals.plos.org/ploscompbiol/article/file?type=supplementary"
     "&id=10.1371/journal.pcbi.1006100.s001", "S1 Text", True),
    ("https://www.rsc.org/suppdata/c9/ee/c9ee01234a/c9ee01234a1.pdf", "ESI", True),
    ("https://www.mdpi.com/2073-4441/12/1/123/s1", "Download", True),
    ("https://oup.silverchair-cdn.com/x/vjae046_supplementary_data.zip?Expires=1", "Data", True),
    ("https://example.org/files/table.xlsx", "Supporting Information", True),
    # not SI
    ("https://pubs.acs.org/doi/pdf/10.1021/es301921w", "PDF", False),
    ("https://example.org/files/report.pdf", "Download PDF", False),
    ("https://example.org/fig/figure1.jpg", "Supplementary Figure 1", False),
    ("https://pubs.acs.org/doi/suppl/10.1021/es301921w", "Supporting Information", False),
    ("mailto:someone@example.org", "supplement", False),
])
def test_is_si_link(url, label, expected):
    assert si.is_si_link(url, label) is expected


def test_si_links_resolves_relative_and_drops_the_paper_pdf():
    html = ('<html><head><meta name="citation_pdf_url" content="/files/paper_si_main.pdf">'
            '</head><body><a href="/files/paper_si_main.pdf">PDF</a>'
            '<a href="suppl_file/x_si_001.pdf">SI</a>'
            '<a href="/doi/suppl/10.1/x/suppl_file/x_si_001.pdf#frag">again</a></body></html>')
    page_ = si.parse_page(html, "https://pubs.acs.org/doi/suppl/10.1/x/")
    assert si.si_links(page_) == ["https://pubs.acs.org/doi/suppl/10.1/x/suppl_file/x_si_001.pdf"]


def test_looks_blocked():
    assert si.looks_blocked(403, "")
    assert si.looks_blocked(200, CHALLENGE.decode())
    big_normal = "<script src='/cdn-cgi/challenge-platform/x.js'></script>" + "x" * 70000
    assert not si.looks_blocked(200, big_normal)
    assert not si.looks_blocked(404, "")


def test_pages_for_publishers():
    meta = si.Meta()
    assert si.Harvester.pages_for("10.1021/x", "https://pubs.acs.org/doi/10.1021/x", meta) == [
        "https://pubs.acs.org/doi/suppl/10.1021/x", "https://pubs.acs.org/doi/10.1021/x"]
    assert si.Harvester.pages_for(
        "10.1002/etc.1", "https://setac.onlinelibrary.wiley.com/doi/10.1002/etc.1", meta)[0] == \
        "https://setac.onlinelibrary.wiley.com/doi/full/10.1002/etc.1"
    assert si.Harvester.pages_for(
        "10.1016/j.x", "https://linkinghub.elsevier.com/retrieve/pii/S0045653520303878", meta) == [
        "https://www.sciencedirect.com/science/article/pii/S0045653520303878"]
    assert si.Harvester.pages_for(
        "10.1101/2020.01.01.1", "https://www.biorxiv.org/content/10.1101/2020.01.01.1v2", meta,
        pmcid="PMC1") == [
        "https://www.biorxiv.org/content/10.1101/2020.01.01.1v2.supplementary-material",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"]


# ---------------------------------------------------------------- sources

def test_figshare_keeps_only_items_for_this_article(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "api.figshare.com/v2/articles/search": J([{"id": 1}, {"id": 2}]),
        "api.figshare.com/v2/articles/1": J({"resource_doi": "10.1021/ES1", "files": [
            {"download_url": "https://ndownloader.figshare.com/files/11", "name": "es1_si_001.pdf"}]}),
        "api.figshare.com/v2/articles/2": J({"resource_doi": "10.1021/other", "files": [
            {"download_url": "https://ndownloader.figshare.com/files/22", "name": "nope.pdf"}]}),
    })
    cands = h.src_figshare(si.Job("10.1021/es1", "S"), si.Meta())
    assert [(c.url, c.name, c.complete) for c in cands] == [
        ("https://ndownloader.figshare.com/files/11", "es1_si_001.pdf", True)]


def test_europepmc_bundle_is_unpacked(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pcbi.1006100.s001.pdf", PDF)
        z.writestr("__MACOSX/._junk", b"x")
        z.writestr("sub/pcbi.1006100.s002.xlsx", XLSX)
    h, _ = make_harvester(tmp_path, {
        "europepmc/webservices/rest/search": J({"resultList": {"result": [
            {"doi": "10.1371/journal.pcbi.1006100", "pmcid": "PMC5"}]}}),
        "rest/PMC5/supplementaryFiles": FakeResp(200, buf.getvalue(),
                                                 {"content-type": "application/zip"}),
    })
    job = si.Job("10.1371/journal.pcbi.1006100", "S")
    blobs = h.src_europepmc(job, si.Meta())
    assert job.pmcid == "PMC5"
    assert [b.name for b in blobs] == ["pcbi.1006100.s001.pdf", "pcbi.1006100.s002.xlsx"]


def test_linked_deposits_expand_zenodo_and_figshare(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "api.datacite.org/dois": J({"data": [
            {"attributes": {"doi": "10.5281/zenodo.77", "url": "https://zenodo.org/records/77",
                            "relatedIdentifiers": [{"relationType": "IsSupplementTo",
                                                    "relatedIdentifier": "https://doi.org/10.1/Art"}]}},
            {"attributes": {"doi": "10.5281/zenodo.88",
                            "relatedIdentifiers": [{"relationType": "References",
                                                    "relatedIdentifier": "10.1/art"}]}}]}),
        "zenodo.org/api/records/77": J({"files": [
            {"key": "data.csv", "links": {"self": "https://zenodo.org/api/records/77/files/data.csv/content"}}]}),
        "api.figshare.com/v2/articles/555": J({"files": [
            {"download_url": "https://ndownloader.figshare.com/files/9", "name": "d.xlsx"}]}),
    })
    meta = si.Meta(supplement_dois=["10.6084/m9.figshare.555.v1"])
    cands = h.src_linked_deposits(si.Job("10.1/art", "S"), meta)
    assert sorted((c.source, c.name, c.complete) for c in cands) == [
        ("figshare-linked", "d.xlsx", False), ("zenodo", "data.csv", False)]


def test_elsevier_probe_walks_mmc_until_a_gap(tmp_path):
    base = "https://ars.els-cdn.com/content/image/1-s2.0-S0045653520303878"
    h, _ = make_harvester(tmp_path, {
        f"{base}-mmc1.pdf": FakeResp(200, PDF, {"content-type": "application/pdf"}),
        f"{base}-mmc2.docx": FakeResp(200, DOCX, {"content-type": "application/octet-stream"}),
        f"{base}-mmc4.pdf": FakeResp(200, PDF2, {"content-type": "application/pdf"}),  # after the gap
    })
    cands = h.probe_candidates(si.Job("10.1016/j.chemosphere.2020.126001", "S"),
                               si.Meta(pii="S0045653520303878"))
    assert [c.url for c in cands] == [f"{base}-mmc1.pdf", f"{base}-mmc2.docx"]


def test_plos_probe_ignores_html_error_pages(tmp_path):
    prefix = "https://journals.plos.org/ploscompbiol/article/file?type=supplementary&id=10.1371/journal.pcbi.1006100"
    h, _ = make_harvester(tmp_path, {
        f"{prefix}.s001": FakeResp(200, PDF, {"content-type": "application/pdf"}),
        f"{prefix}.s002": FakeResp(200, "<html>error</html>", {"content-type": "text/html"}),
    })
    cands = h.probe_candidates(si.Job("10.1371/journal.pcbi.1006100", "S"), si.Meta())
    assert [c.url for c in cands] == [f"{prefix}.s001"]


# ---------------------------------------------------------------- end to end

ACS, WILEY, ELS, NONE = ("10.1021/es301921w", "10.1002/etc.2572",
                         "10.1016/j.chemosphere.2020.126001", "10.9999/nothing")
ELS_BASE = "https://ars.els-cdn.com/content/image/1-s2.0-S0045653520303878"


def e2e_routes():
    return {
        # metadata
        f"api.crossref.org/works/{ACS}": crossref("Smith", 2012),
        f"api.crossref.org/works/{WILEY}": crossref("Könemann", 2018),
        f"api.crossref.org/works/{ELS}": crossref("Lee", 2020, **{"alternative-id": ["S0045653520303878"]}),
        f"api.crossref.org/works/{NONE}": crossref("Smith", 2012),
        # ACS: SI mirrored on figshare; the publisher itself is behind a challenge
        f'"resource_doi": "{ACS}"': J([{"id": 101}, {"id": 102}]),
        "api.figshare.com/v2/articles/101": J({"resource_doi": ACS, "files": [
            {"download_url": "https://ndownloader.figshare.com/files/1001", "name": "es301921w_si_001.pdf"}]}),
        "api.figshare.com/v2/articles/102": J({"resource_doi": ACS, "files": [
            {"download_url": "https://ndownloader.figshare.com/files/1002", "name": "es301921w_si_002.xlsx"}]}),
        "ndownloader.figshare.com/files/1001": FakeResp(200, PDF, {"content-type": "application/pdf"}),
        "ndownloader.figshare.com/files/1002": FakeResp(200, XLSX, {"content-type": "application/octet-stream"}),
        f"doi.org/api/handles/{ACS}": J({"values": [{"type": "URL", "data": {
            "value": f"https://pubs.acs.org/doi/{ACS}"}}]}),
        "pubs.acs.org/": FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
        # Wiley: SI listed on the full-text page; one file comes back as a challenge
        f"doi.org/api/handles/{WILEY}": J({"values": [{"type": "URL", "data": {
            "value": f"https://setac.onlinelibrary.wiley.com/doi/{WILEY}"}}]}),
        f"setac.onlinelibrary.wiley.com/doi/full/{WILEY}": page(
            ("/action/downloadSupplement?doi=10.1002%2Fetc.2572&file=etc2572-sup-0001.pdf", "Data S1"),
            ("/action/downloadSupplement?doi=10.1002%2Fetc.2572&file=etc2572-sup-0002.docx", "Data S2"),
            (f"/doi/pdf/{WILEY}", "PDF"),
            extra=f'<meta name="citation_pdf_url" content="https://setac.onlinelibrary.wiley.com/doi/pdf/{WILEY}">'),
        "file=etc2572-sup-0001.pdf": FakeResp(200, PDF2, {
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="etc2572-sup-0001-SupData.pdf"'}),
        "file=etc2572-sup-0002.docx": FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
        f"setac.onlinelibrary.wiley.com/doi/{WILEY}": page(),
        # Elsevier: ScienceDirect refuses us; the CDN serves the mmc files
        f"doi.org/api/handles/{ELS}": J({"values": [{"type": "URL", "data": {
            "value": "https://linkinghub.elsevier.com/retrieve/pii/S0045653520303878"}}]}),
        "www.sciencedirect.com/": FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
        f"{ELS_BASE}-mmc1.pdf": FakeResp(200, b"%PDF-1.7 elsevier si", {"content-type": "application/pdf"}),
        f"{ELS_BASE}-mmc2.docx": FakeResp(200, DOCX, {"content-type": "application/octet-stream"}),
        # nothing at all
        f"doi.org/api/handles/{NONE}": J({"values": [{"type": "URL", "data": {
            "value": "https://journal.example.org/article/nothing"}}]}),
        "journal.example.org/article/nothing": page(("/files/paper.pdf", "Download PDF")),
    }


def read_manifest(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_end_to_end_builds_flat_zip_and_manifest(tmp_path):
    h, _ = make_harvester(tmp_path, e2e_routes())
    summary = h.run([ACS, WILEY, ELS, NONE])

    with zipfile.ZipFile(tmp_path / "out" / "SI.zip") as z:
        names = sorted(z.namelist())
        manifest = read_manifest(z.read("si_manifest.csv").decode())
        assert z.read("Smith2012_SI1.pdf") == PDF
    assert names == sorted([
        "Smith2012_SI1.pdf", "Smith2012_SI2.xlsx",      # ACS via figshare
        "Konemann2018_SI1.pdf",                          # Wiley page
        "Lee2020_SI1.pdf", "Lee2020_SI2.docx",          # Elsevier via mmc probing
        "si_manifest.csv",
    ])
    by_doi = {}
    for row in manifest:
        by_doi.setdefault(row["doi"], []).append(row)
    assert [r["source"] for r in by_doi[ACS]] == ["figshare", "figshare"]
    assert by_doi[ACS][0]["original_name"] == "es301921w_si_001.pdf"
    assert by_doi[WILEY][0]["original_name"] == "etc2572-sup-0001-SupData.pdf"
    assert "etc2572-sup-0002.docx" in by_doi[WILEY][0]["note"]           # the blocked one
    assert [r["source"] for r in by_doi[ELS]] == ["elsevier-probe", "elsevier-probe"]
    assert [(r["status"], r["stem"], r["file"]) for r in by_doi[NONE]] == [("none", "Smith2012a", "")]
    assert (summary.total, summary.with_si, summary.files) == (4, 3, 5)
    assert summary.none == [NONE]


def test_same_file_from_two_routes_is_kept_once(tmp_path):
    routes = e2e_routes()
    # the ACS page now opens and lists the very same PDF the figshare mirror has
    routes = {
        f"pubs.acs.org/doi/suppl/{ACS}/suppl_file/": FakeResp(200, PDF, {"content-type": "application/pdf"}),
        f"pubs.acs.org/doi/suppl/{ACS}": page((f"/doi/suppl/{ACS}/suppl_file/es301921w_si_001.pdf", "SI")),
        **routes}
    routes["pubs.acs.org/"] = page()
    h, _ = make_harvester(tmp_path, routes)
    h.run([ACS])
    assert sorted(p.name for p in (tmp_path / "out" / "files").iterdir()) == [
        "Smith2012_SI1.pdf", "Smith2012_SI2.xlsx"]


def test_rerun_skips_harvested_dois(tmp_path):
    h, _ = make_harvester(tmp_path, e2e_routes())
    h.run([ACS, NONE])
    h2, session2 = make_harvester(tmp_path, e2e_routes())
    h2.run([ACS, NONE])
    assert not any(ACS in c for c in session2.calls)                       # ACS skipped
    assert any(NONE in c for c in session2.calls)                          # no-SI retried
    with zipfile.ZipFile(tmp_path / "out" / "SI.zip") as z:
        assert sorted(z.namelist()) == ["Smith2012_SI1.pdf", "Smith2012_SI2.xlsx", "si_manifest.csv"]


def test_unreachable_publisher_is_an_error_not_none(tmp_path):
    import requests
    h, _ = make_harvester(tmp_path, {
        "doi.org/": requests.exceptions.ConnectionError("Tunnel connection failed: 403"),
    })
    summary = h.run([NONE])
    assert summary.errors == [NONE] and summary.none == []
    assert "Network errors (1; rerun to retry)" in summary.text()


def test_main_cli(tmp_path, monkeypatch):
    session = FakeSession(e2e_routes())
    monkeypatch.setattr(si.Http, "_build_session", staticmethod(lambda: session))
    monkeypatch.setattr(si.Http, "_throttle", lambda self, url: None)
    dois = tmp_path / "dois.txt"
    dois.write_text(f"{ACS}\n{NONE}\n")
    rc = si.main([str(dois), "-o", str(tmp_path / "o"), "--email", "t@example.org",
                  "--browser", "off", "--zip", "bundle.zip"])
    assert rc == 0
    assert (tmp_path / "o" / "bundle.zip").is_file()
    assert si.main(["not-a-doi", "-o", str(tmp_path / "o")]) == 2


# ---------------------------------------------------------------- real browser

def _chromium_ok() -> bool:
    if not si.BrowserSession.installed():
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


class _ChallengeSite(BaseHTTPRequestHandler):
    """/landing answers 403 + a JS challenge until the 'clear' cookie is set;
    the SI files are served only to a cleared client."""

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cleared = "clear=1" in (self.headers.get("Cookie") or "")
        if self.path == "/landing":
            if not cleared:
                body = (CHALLENGE[:-14] + b"<script>document.cookie='clear=1; path=/';"
                        b"setTimeout(() => location.reload(), 300)</script></body></html>")
                return self._send(403, body, "text/html")
            links = "".join(f'<a href="/nav/{i}">n</a>' for i in range(20))
            body = (f'<html><body><h2>Supporting Information</h2>'
                    f'<a href="/doi/suppl/10.5555/t/suppl_file/t_si_001.pdf">t_si_001.pdf</a>'
                    f'<a href="/doi/suppl/10.5555/t/suppl_file/t_si_002.pdf">t_si_002.pdf</a>'
                    f'{links}</body></html>').encode()
            return self._send(200, body, "text/html")
        if self.path.endswith("t_si_001.pdf"):
            if not cleared:
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, PDF, "application/pdf")
        if self.path.endswith("t_si_002.pdf"):
            # stands in for a check only the real browser passes (here: a fetch() made
            # from the page), so this file must come through the in-page fetch
            if not cleared or self.headers.get("Sec-Fetch-Mode") != "cors":
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, PDF2, "application/pdf")
        return self._send(404, b"nope", "text/plain")


@pytest.mark.skipif(not _chromium_ok(), reason="playwright/chromium not available")
def test_browser_fallback_passes_a_js_challenge(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    landing = f"http://127.0.0.1:{server.server_address[1]}/landing"
    try:
        http = si.Http(delay=0)
        h = si.Harvester(http, tmp_path / "out", browser="auto",
                         browser_profile=tmp_path / "profile", probe=False)
        for name in ("src_chemrxiv", "src_figshare", "src_europepmc", "src_elsevier_api",
                     "src_linked_deposits"):
            setattr(h, name, lambda job, meta: [])
        h.fetch_meta = lambda doi: si.Meta(author="Test", year="2024")
        h.landing_url = lambda doi: (landing, None)
        h.pages_for = lambda doi, url, meta, pmcid="": [url]
        try:
            summary = h.run(["10.5555/t"])
        finally:
            h.close()
    finally:
        server.shutdown()
    assert (summary.with_si, summary.files) == (1, 2)
    files = tmp_path / "out" / "files"
    assert (files / "Test2024_SI1.pdf").read_bytes() == PDF     # requests + adopted cookie
    assert (files / "Test2024_SI2.pdf").read_bytes() == PDF2    # in-page fetch
