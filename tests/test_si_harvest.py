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

    def iter_content(self, n=65536):
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
    """Routes by substring of the full URL (query included); first match wins. A
    list of responses is served in turn, its last one repeating."""

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
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
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
    # figshare's S3 percent-encodes the plain parameter (Frontiers, Oct 2026)
    assert si._cd_filename('attachment; filename="Data%20Sheet%201_Res%2C%20x.pdf"') == \
        "Data Sheet 1_Res, x.pdf"


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
    # RSC since its move to Silverchair: no extension in the URL
    ("https://pubs.rsc.org/sc/article-supplement/868835/pdf/d4sc00808a1_suppl/",
     "Supplementary information (PDF) Download Supplementary information (PDF)", True),
    # nature.com / link.springer.com now serve ESM from media.springernature.com
    ("https://media.springernature.com/original/springer-static/esm/art%3A10.1007%2Fs11356-024-"
     "32658-7/MediaObjects/11356_2024_32658_MOESM1_ESM.docx", "Supplementary file1 (DOCX 1181 KB)", True),
    ("https://example.org/files/table.xlsx", "Supporting Information", True),
    # IOP's /data page links signed S3 copies named <journal><id>supp<n>
    ("https://cfn-live-content-bucket-iop-org.s3.eu-west-1.amazonaws.com/journals/1748-9326/19/5/"
     "054002/revision2/erlad3a81supp1.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261013T110901Z",
     "", True),
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


def test_si_links_skip_the_si_of_cited_papers():
    # link.springer.com reference list: a cited ACS paper's SI, then the article's own ESM
    doi = "10.1007/s11356-024-32658-7"
    html = ('<a href="https://doi.org/10.1021/ES202423F/SUPPL_FILE/ES202423F_SI_001.PDF">'
            'https://doi.org/10.1021/ES202423F/SUPPL_FILE/ES202423F_SI_001.PDF</a>'
            '<a href="https://doi.org/10.1021%2FES202423F%2FSUPPL_FILE%2FES202423F_SI_001.PDF">'
            'Article reference 29</a>'
            '<a href="https://media.springernature.com/original/springer-static/esm/art%3A10.1007'
            '%2Fs11356-024-32658-7/MediaObjects/11356_2024_32658_MOESM1_ESM.docx">Supplementary file1</a>')
    page_ = si.parse_page(html, f"https://link.springer.com/article/{doi}")
    assert [u.rsplit("/", 1)[-1] for u in si.si_links(page_, doi)] == [
        "11356_2024_32658_MOESM1_ESM.docx"]
    # a versioned preprint DOI still owns the version-less SI path
    chem = si.parse_page('<a href="/doi/suppl/10.26434/chemrxiv-2024-jkhk9/suppl_file/si.pdf">SI</a>',
                         "https://chemrxiv.org/doi/full/10.26434/chemrxiv-2024-jkhk9-v3")
    assert si.si_links(chem, "10.26434/chemrxiv-2024-jkhk9-v3") == [
        "https://chemrxiv.org/doi/suppl/10.26434/chemrxiv-2024-jkhk9/suppl_file/si.pdf"]


# Trimmed from what the sites actually sent in October 2026.
FASTLY_CHALLENGE = ("<!DOCTYPE html><html lang=\"en\"><head><title>Client Challenge</title></head><body>"
                    "<noscript>JavaScript is disabled in your browser.</noscript>"
                    "<script>loadScript('/_fs-ch-1T1wmsGaOgGaSxcX/errors.js')</script></body></html>")
AKAMAI_CHALLENGE = ("<!DOCTYPE html><html><head><meta http-equiv=\"refresh\" content=\"5; URL='/2073-4441"
                    "/16/5/700?bm-verify=AAQAAAAO_____6Ds'\" /><title>&nbsp;</title></head></html>")
RECAPTCHA_WALL = ("<!doctype html><html><head><title>Checking your browser - reCAPTCHA</title></head>"
                  "<body><div>Checking your browser before accessing pmc.ncbi.nlm.nih.gov ...</div></body></html>")
DDOS_GUARD = "<html><head><title>DDoS-Guard</title></head><body>One moment, please...</body></html>"
RADWARE = ("<head><title>Radware Bot Manager Captcha</title><script type=\"text/javascript\">"
           "window.SSJSInternal = 73287;(function(w, d, e, u, c, g, a, b){a = d.createElement(e);"
           "a.src = u;})(window,document,\"script\",\"https://cdn.perfdrive.com/aperture/aperture.js\","
           "\"b8c3\",\"auto\");</script></head>")   # IOP, HTTP 200


def test_looks_blocked():
    assert si.looks_blocked(403, "")
    assert si.looks_blocked(200, CHALLENGE.decode())
    big_normal = "<script src='/cdn-cgi/challenge-platform/x.js'></script>" + "x" * 70000
    assert not si.looks_blocked(200, big_normal)
    assert not si.looks_blocked(404, "")
    for wall in (FASTLY_CHALLENGE, AKAMAI_CHALLENGE, RECAPTCHA_WALL, DDOS_GUARD, RADWARE):
        assert si.looks_blocked(200, wall)
    assert si.looks_blocked(203, RECAPTCHA_WALL)         # PMC answers curl_cffi with a 203
    # ScienceDirect's captcha page as the browser renders it (status 200): 1.2 MB of page
    sd_wall = ("<html><head><title>Just a moment...</title><script>" + "var a=1;" * 150000 +
               "</script></head><body><h1>Are you a robot?</h1><p>Please confirm you are a human by "
               "completing the captcha challenge below.</p>" + "<i>x</i>" * 50000 + "</body></html>")
    assert si.looks_blocked(200, sd_wall)
    big_article = "<html><body>" + "<p>Results and discussion of the access denied rate.</p>" * 2000
    assert not si.looks_blocked(200, big_article)
    # figshare's download host: AWS WAF wants JavaScript first
    waf = CaseInsensitiveDict({"x-amzn-waf-action": "challenge", "content-length": "0"})
    assert si.looks_blocked(202, "", waf)
    assert si.reject_reason(si.Fetched("u", 202, waf)) == "blocked"


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
    # ChemRxiv moved from its Engage API to an Atypon-style site in 2026
    assert si.Harvester.pages_for(
        "10.26434/chemrxiv.15002330/v1",
        "https://chemrxiv.org/doi/full/10.26434/chemrxiv.15002330/v1", meta) == [
        "https://chemrxiv.org/doi/suppl/10.26434/chemrxiv.15002330/v1",
        "https://chemrxiv.org/doi/full/10.26434/chemrxiv.15002330/v1"]
    assert si.Harvester.pages_for(
        "10.1101/2020.01.01.1", "https://www.biorxiv.org/content/10.1101/2020.01.01.1v2", meta,
        pmcid="PMC1") == [
        "https://www.biorxiv.org/content/10.1101/2020.01.01.1v2.supplementary-material",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"]
    # IOP lists SI on a page of its own
    assert si.Harvester.pages_for(
        "10.1088/1748-9326/ad3a81", "https://iopscience.iop.org/article/10.1088/1748-9326/ad3a81",
        meta) == ["https://iopscience.iop.org/article/10.1088/1748-9326/ad3a81/data",
                  "https://iopscience.iop.org/article/10.1088/1748-9326/ad3a81"]


# ---------------------------------------------------------------- sources

def fs_hit(id_, doi, resource_doi, type_name):
    """A figshare /v2/articles/search hit, trimmed."""
    return {"id": id_, "doi": doi, "resource_doi": resource_doi, "defined_type_name": type_name,
            "url": f"https://api.figshare.com/v2/articles/{id_}", "title": "t"}


def test_figshare_keeps_only_items_for_this_article(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "api.figshare.com/v2/articles/search": J([
            fs_hit(1, "10.1021/es1.s001", "10.1021/ES1", "journal contribution"),
            fs_hit(2, "10.1021/other.s001", "10.1021/other", "journal contribution")]),
        "api.figshare.com/v2/articles/1": J({"resource_doi": "10.1021/ES1", "files": [
            {"download_url": "https://ndownloader.figshare.com/files/11", "name": "es1_si_001.pdf"}]}),
        "api.figshare.com/v2/articles/2": J({"resource_doi": "10.1021/other", "files": [
            {"download_url": "https://ndownloader.figshare.com/files/22", "name": "nope.pdf"}]}),
    })
    cands = h.src_figshare(si.Job("10.1021/es1", "S"), si.Meta())
    assert [(c.url, c.name, c.complete) for c in cands] == [
        ("https://ndownloader.figshare.com/files/11", "es1_si_001.pdf", True)]


def test_figshare_skips_plos_figure_items(tmp_path):
    doi = "10.1371/journal.pone.0297686"
    h, session = make_harvester(tmp_path, {
        "api.figshare.com/v2/articles/search": J([
            fs_hit(25446041, f"{doi}.g006", doi, "figure"),
            fs_hit(25446026, f"{doi}.g001", doi, "figure"),
            fs_hit(25446024, f"{doi}.s001", doi, "journal contribution")]),
        "api.figshare.com/v2/articles/25446024": J({"resource_doi": doi, "files": [
            {"download_url": "https://ndownloader.figshare.com/files/45229431",
             "name": "pone.0297686.s001.pdf"}]}),
    })
    cands = h.src_figshare(si.Job(doi, "S"), si.Meta())
    assert [c.name for c in cands] == ["pone.0297686.s001.pdf"]
    assert not any("/articles/25446041" in c or "/articles/25446026" in c for c in session.calls)


def test_figshare_download_behind_aws_waf_goes_to_the_browser(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "ndownloader.figshare.com/files/4134121": FakeResp(202, b"", {
            "x-amzn-waf-action": "challenge", "content-length": "0",
            "content-type": "text/html; charset=UTF-8", "server": "awselb/2.0"}),
    })
    job = si.Job("10.1021/es301921w", "S")
    h._download(job, si.Candidate("https://ndownloader.figshare.com/files/4134121", "figshare"))
    assert [c.url for c in job.blocked_files] == ["https://ndownloader.figshare.com/files/4134121"]
    assert not job.files and not job.done_urls


def s3_listing(*keys):
    """ListObjectsV2 XML, as https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2 sends it."""
    contents = "".join(f"<Contents><Key>{k}</Key><Size>1</Size><StorageClass>STANDARD</StorageClass>"
                       f"</Contents>" for k in keys)
    return FakeResp(200, '<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns='
                         '"http://s3.amazonaws.com/doc/2006-03-01/"><Name>pmc-oa-opendata</Name>'
                         f"<KeyCount>{len(keys)}</KeyCount>{contents}</ListBucketResult>",
                    {"content-type": "application/xml"})


PLOS_JATS = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
<fig id="pone.0297686.g001"><graphic xlink:href="pone.0297686.g001.jpg"/></fig>
<sec><supplementary-material id="pone.0297686.s001" position="float" content-type="local-data">
<label>S1 File</label><caption><p>List of S1-S12 Tables.
<ext-link ext-link-type="uri" xlink:href="https://example.org/related">link</ext-link></p></caption>
<media xlink:href="pone.0297686.s001.pdf" position="float"><caption><p>(PDF)</p></caption></media>
</supplementary-material>
<supplementary-material xlink:href="pone.0297686.s002.xlsx" content-type="local-data"/>
<supplementary-material id="s3"><media xlink:href="missing_from_bucket.pdf"/></supplementary-material>
</sec></body></article>"""


def test_pmc_takes_the_listed_supplements_from_the_open_bucket(tmp_path):
    doi, bucket = "10.1371/journal.pone.0297686", si.PMC_BUCKET
    h, session = make_harvester(tmp_path, {
        "europepmc/webservices/rest/search": J({"resultList": {"result": [
            {"doi": doi, "pmcid": "PMC10954143", "hasSuppl": "Y"}]}}),
        f"{bucket}/?list-type=2&prefix=PMC10954143.": s3_listing(
            "PMC10954143.1/PMC10954143.1.xml", "PMC10954143.1/pone.0297686.s001.pdf",
            "PMC10954143.2/PMC10954143.2.json", "PMC10954143.2/PMC10954143.2.xml",
            "PMC10954143.2/pone.0297686.g001.jpg", "PMC10954143.2/pone.0297686.s001.pdf",
            "PMC10954143.2/pone.0297686.s002.xlsx"),
        f"{bucket}/PMC10954143.2/PMC10954143.2.xml": FakeResp(200, PLOS_JATS,
                                                              {"content-type": "application/xml"}),
    })
    job = si.Job(doi, "S")
    cands = h.src_pmc(job, si.Meta())
    assert [(c.url, c.name, c.complete) for c in cands] == [
        (f"{bucket}/PMC10954143.2/pone.0297686.s001.pdf", "pone.0297686.s001.pdf", True),
        (f"{bucket}/PMC10954143.2/pone.0297686.s002.xlsx", "pone.0297686.s002.xlsx", True)]
    assert job.pmcid == ""                      # the PMC page needn't be scanned now
    assert not any("supplementaryFiles" in c for c in session.calls)


def test_pmc_article_outside_the_open_bucket_leaves_the_pmc_page(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "europepmc/webservices/rest/search": J({"resultList": {"result": [
            {"doi": "10.1/x", "pmcid": "PMC77"}]}}),
        f"{si.PMC_BUCKET}/?list-type=2": s3_listing(),
    })
    job = si.Job("10.1/x", "S")
    assert h.src_pmc(job, si.Meta()) == []
    assert job.pmcid == "PMC77"


def test_linked_deposits_expand_zenodo_and_figshare(tmp_path):
    h, session = make_harvester(tmp_path, {
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
    cands = h.src_linked_deposits(si.Job("10.1/Art", "S"), meta)
    assert sorted((c.source, c.name, c.complete) for c in cands) == [
        ("figshare-linked", "d.xlsx", False), ("zenodo", "data.csv", False)]
    # DataCite answers HTTP 400 to a backslash-escaped "/": the DOIs go in as quoted phrases
    query = next(c for c in session.calls if "api.datacite.org" in c)
    assert "%5C" not in query
    assert ("query=relatedIdentifiers.relatedIdentifier%3A%28%2210.1%2FArt%22+OR+%2210.1%2Fart%22%29"
            in query)


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


def els_objects(pii, *refs):
    """The Object API's answer (HTTP 300, Oct 2026): one "choice" per object."""
    types = {"mmc": ("APPLICATION", "docx", "application/word"),
             "gr": ("IMAGE-DOWNSAMPLED", "jpg", "image/jpeg"),
             "si": ("ALTIMG", "svg", "image/svg+xml"), "am": ("AAM-PDF", "pdf", "application/pdf")}
    choice = []
    for ref in refs:
        kind, ext, accept = types[ref.rstrip("0123456789")]
        choice.append({"@ref": ref, "@type": kind, "$": (
            f"https://api.elsevier.com/content/object/eid/1-s2.0-{pii}-{ref}.{ext}"
            f"?httpAccept={accept}")})
    body = {"choices": {"choice": choice[0] if len(choice) == 1 else choice} if choice else None}
    return FakeResp(300, json.dumps(body), {"content-type": "application/json;charset=UTF-8"})


def test_elsevier_api_takes_the_mmc_objects_as_the_complete_si(tmp_path):
    pii = "S004896972400980X"
    h, session = make_harvester(tmp_path, {
        f"api.elsevier.com/content/object/pii/{pii}": els_objects(pii, "gr1", "mmc1", "si1", "am", "mmc2"),
    }, elsevier_key="k")
    job = si.Job("10.1016/j.scitotenv.2024.170841", "S")
    cands = h.src_elsevier_api(job, si.Meta(pii=pii))
    ars = "https://ars.els-cdn.com/content/image/1-s2.0-S004896972400980X"
    assert [(c.url, c.name, c.complete) for c in cands] == [
        (f"{ars}-mmc1.docx", "1-s2.0-S004896972400980X-mmc1.docx", True),
        (f"{ars}-mmc2.docx", "1-s2.0-S004896972400980X-mmc2.docx", True)]
    assert not job.complete and not job.notes
    assert session.calls[0].endswith("?httpAccept=application%2Fjson")
    # a single object comes as a dict, not a list
    h, _ = make_harvester(tmp_path, {"api.elsevier.com": els_objects(pii, "mmc1")}, elsevier_key="k")
    assert [c.name for c in h.src_elsevier_api(si.Job("10.1/x", "S"), si.Meta(pii=pii))] == [
        "1-s2.0-S004896972400980X-mmc1.docx"]


def test_elsevier_api_listing_without_mmc_means_no_si(tmp_path):
    pii = "S004896972400980X"
    h, _ = make_harvester(tmp_path, {"api.elsevier.com": els_objects(pii, "gr1", "si1", "am")},
                          elsevier_key="k")
    job = si.Job("10.1016/j.x", "S")
    assert h.src_elsevier_api(job, si.Meta(pii=pii)) == []
    assert job.complete and job.notes == ["Elsevier API lists no supplementary files"]


def test_elsevier_api_empty_or_failed_answer_proves_nothing(tmp_path):
    # An unknown PII gets the same {"choices": null} as an article with no objects;
    # a bad key gets HTTP 503.
    for resp in (els_objects("S0000000000000000"),
                 FakeResp(503, '{"error-response": {}}', {"content-type": "application/json"})):
        h, _ = make_harvester(tmp_path, {"api.elsevier.com": resp}, elsevier_key="k")
        job = si.Job("10.1016/j.x", "S")
        assert h.src_elsevier_api(job, si.Meta(pii="S0000000000000000")) == []
        assert not job.complete and not job.notes


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


def test_an_elsevier_api_answer_settles_the_doi_without_the_walled_page(tmp_path):
    pii = "S0045653520303878"
    for refs, want_status, want_files in ((("gr1", "mmc1", "am"), "ok", ["Lee2020_SI1.docx"]),
                                          (("gr1", "am"), "none", [])):
        routes = e2e_routes()
        routes[f"api.elsevier.com/content/object/pii/{pii}"] = els_objects(pii, *refs)
        routes[f"{ELS_BASE}-mmc1.docx"] = FakeResp(200, DOCX, {"content-type": "application/word"})
        h, session = make_harvester(tmp_path / want_status, routes, elsevier_key="k")
        h.run([ELS])
        rows = read_manifest((tmp_path / want_status / "out" / "si_manifest.csv").read_text())
        assert [r["status"] for r in rows] == [want_status] * max(1, len(want_files))
        assert [r["file"] for r in rows if r["file"]] == want_files
        # no extension probing (mmc1.pdf would have been found) and no "blocked"
        assert not any("mmc1.pdf" in c for c in session.calls)
        if want_status == "none":
            assert rows[0]["note"].startswith("Elsevier API lists no supplementary files; checked ")
            assert "sciencedirect.com" in rows[0]["note"]   # the page that stayed walled
            assert not (tmp_path / want_status / "out" / "retry.txt").exists()


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
    assert by_doi[NONE][0]["note"] == ("checked mirrors: nothing; "
                                       "https://journal.example.org/article/nothing opened, no SI links")
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


def test_publisher_copy_of_a_mirrored_file_is_kept_once(tmp_path):
    """ACS (now on Silverchair) serves a stamped copy of the SI figshare mirrors:
    different bytes, same file name. The manifest keeps the link followed, not the
    short-lived signed URL it redirected to."""
    supp = "https://pubs.acs.org/esthag/article-supplement/1575003/pdf/es301921w_si_00"
    s3 = "https://s3-eu-west-1.amazonaws.com/pstorage-acs-6854636/4134115/es301921w_si_001.pdf?X-Amz-Expires=10"
    routes = e2e_routes()
    routes = {
        "ndownloader.figshare.com/files/1001": FakeResp(200, PDF, {
            "content-type": "application/pdf", "content-disposition": "attachment"}, url=s3),
        f"pubs.acs.org/doi/suppl/{ACS}": page((supp + "1/", "Supporting Information (PDF)"),
                                              (supp + "2/", "Supporting Information (XLSX)")),
        supp + "1/": FakeResp(200, b"%PDF-1.5 stamped by the publisher", {
            "content-type": "application/pdf",
            "content-disposition": "attachment; filename=es301921w_si_001.pdf"}),
        supp + "2/": FakeResp(200, b"PK\x03\x04 stamped", {
            "content-type": "application/octet-stream",
            "content-disposition": "attachment; filename=es301921w_si_002.xlsx"}),
        **routes}
    routes["pubs.acs.org/"] = page()
    h, _ = make_harvester(tmp_path, routes)
    h.run([ACS])
    rows = read_manifest((tmp_path / "out" / "si_manifest.csv").read_text())
    assert [(r["file"], r["source"], r["original_name"], r["url"]) for r in rows] == [
        ("Smith2012_SI1.pdf", "figshare", "es301921w_si_001.pdf",
         "https://ndownloader.figshare.com/files/1001"),
        ("Smith2012_SI2.xlsx", "figshare", "es301921w_si_002.xlsx",
         "https://ndownloader.figshare.com/files/1002")]
    assert "es301921w_si_001.pdf: figshare and page copies differ; kept the figshare one" in rows[0]["note"]


def test_different_files_with_a_bare_link_name_are_both_kept(tmp_path):
    h, _ = make_harvester(tmp_path, {
        "x.org/a/1/download": FakeResp(200, PDF, {"content-type": "application/pdf"}),
        "x.org/a/2/download": FakeResp(200, PDF2, {"content-type": "application/pdf"}),
    })
    h.files_dir.mkdir(parents=True)
    job = si.Job("10.1/x", "S")
    h._take(job, [si.Candidate("https://x.org/a/1/download", "page"),
                  si.Candidate("https://x.org/a/2/download", "page")])
    assert [f.filename for f in job.files] == ["S_SI1.pdf", "S_SI2.pdf"]


def test_a_doi_blocked_on_the_first_pass_is_retried(tmp_path):
    doi = "10.1002/etc.9"

    def routes():   # Wiley refuses the first visit to each page, then serves it
        return {
            f"api.crossref.org/works/{doi}": crossref("Ito", 2020),
            f"doi.org/api/handles/{doi}": J({"values": [{"type": "URL", "data": {
                "value": f"https://setac.onlinelibrary.wiley.com/doi/{doi}"}}]}),
            f"setac.onlinelibrary.wiley.com/doi/full/{doi}": [
                FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
                page(("/action/downloadSupplement?doi=10.1002%2Fetc.9&file=etc9-sup-0001.pdf",
                      "Data S1"))],
            "file=etc9-sup-0001.pdf": FakeResp(200, PDF, {"content-type": "application/pdf"}),
            f"setac.onlinelibrary.wiley.com/doi/{doi}": [
                FakeResp(403, CHALLENGE, {"content-type": "text/html"}), page()],
        }
    h0, _ = make_harvester(tmp_path / "once", routes(), retries=0)
    assert h0.run([doi]).blocked == [doi]
    h, _ = make_harvester(tmp_path / "twice", routes())
    summary = h.run([doi])
    assert (summary.with_si, summary.blocked) == (1, [])
    assert not (tmp_path / "twice" / "out" / "retry.txt").exists()


def test_a_host_that_keeps_refusing_goes_straight_to_the_browser(tmp_path):
    plain = FakeSession({"academic.oup.com/": FakeResp(403, CHALLENGE, {"content-type": "text/html"})})
    http = si.Http(delay=0, session=plain)
    for n in range(4):
        f = http.request(f"https://academic.oup.com/etc/article/{n}")
        assert f.status == 403 and si.reject_reason(f) == "blocked"
    assert len(plain.calls) == si.WALL_LIMIT          # the rest were not sent
    http.reset_walls()                                 # a retry round asks again
    http.request("https://academic.oup.com/etc/article/9")
    assert len(plain.calls) == si.WALL_LIMIT + 1


class FakeBrowser:
    """Stands in for BrowserSession. Headless, it meets a check it cannot pass (a
    picture puzzle); in a window, ``passes`` says whether the person got through."""
    made: list = []
    passes = True
    SI = "https://files.example.org/x_supp1.pdf"

    def __init__(self, profile_dir, show=False):
        self.show, self.user_agent = show, "UA"
        FakeBrowser.made.append(self)

    @staticmethod
    def installed():
        return True

    def start(self):
        pass

    def close(self):
        pass

    def cookies(self):
        return []

    def open(self, url):
        through = self.show and FakeBrowser.passes
        return si.PageLinks(url=url, anchors=[(self.SI, "Supplementary data")] if through else [],
                            blocked=not through)

    def fetch(self, url, referer, max_bytes):
        return si.Fetched(url=url, status=403, content=CHALLENGE,
                          headers=CaseInsensitiveDict({"content-type": "text/html"}))


@pytest.fixture
def fake_browser(monkeypatch):
    FakeBrowser.made, FakeBrowser.passes = [], True
    monkeypatch.setattr(si, "BrowserSession", FakeBrowser)
    monkeypatch.setattr(si.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    return FakeBrowser


def walled_routes(*dois):
    routes = {FakeBrowser.SI: FakeResp(200, PDF, {"content-type": "application/pdf"}),
              "journal.example.org/": FakeResp(403, CHALLENGE, {"content-type": "text/html"})}
    for doi in dois:
        routes[f"doi.org/api/handles/{doi}"] = J({"values": [{"type": "URL", "data": {
            "value": f"https://journal.example.org/article/{doi}"}}]})
    return routes


def run_walled(tmp_path, dois, **kw):
    h, _ = make_harvester(tmp_path, walled_routes(*dois), probe=False, retries=0, **kw)
    h.run(dois)
    rows = read_manifest((tmp_path / "out" / "si_manifest.csv").read_text())
    return {r["doi"]: r for r in rows}


def test_auto_opens_a_window_only_for_a_check_left_standing(tmp_path, fake_browser):
    rows = run_walled(tmp_path, ["10.5555/a"], browser="auto")
    assert [b.show for b in fake_browser.made] == [False, True]   # headless first, then one window
    assert (rows["10.5555/a"]["status"], rows["10.5555/a"]["source"]) == ("ok", "page-browser")


def test_headless_mode_never_opens_a_window(tmp_path, fake_browser):
    rows = run_walled(tmp_path, ["10.5555/a"], browser="headless")
    assert [b.show for b in fake_browser.made] == [False]
    assert rows["10.5555/a"]["status"] == "blocked"


def test_a_window_nobody_answers_is_the_last_one(tmp_path, fake_browser):
    fake_browser.passes = False
    rows = run_walled(tmp_path, ["10.5555/a", "10.5555/b"], browser="auto")
    assert [b.show for b in fake_browser.made] == [False, True, False]   # no window for b
    assert [r["status"] for r in rows.values()] == ["blocked", "blocked"]
    assert "-> window: still walled" in rows["10.5555/a"]["note"]


def test_no_window_without_a_screen(tmp_path, fake_browser, monkeypatch):
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    rows = run_walled(tmp_path, ["10.5555/a"], browser="auto")
    assert [b.show for b in fake_browser.made] == [False]
    assert rows["10.5555/a"]["status"] == "blocked"


def test_skipped_file_types_are_not_downloaded_or_kept(tmp_path):
    mp4 = b"\x00\x00\x00\x18ftypmp42 video"
    h, session = make_harvester(tmp_path, {
        "x.org/movie.mp4": FakeResp(200, mp4, {"content-type": "video/mp4"}),
        "x.org/media/2": FakeResp(200, mp4, {"content-type": "application/octet-stream"}),
        "x.org/table.xlsx": FakeResp(200, XLSX, {"content-type": "application/octet-stream"}),
    }, skip_exts=["mp4", "AVI"])
    h.files_dir.mkdir(parents=True)
    job = si.Job("10.1/x", "S")
    h._take(job, [si.Candidate("https://x.org/movie.mp4", "page"),
                  si.Candidate("https://x.org/media/2", "page"),     # known as mp4 only once fetched
                  si.Candidate("https://x.org/table.xlsx", "page")])
    assert [f.filename for f in job.files] == ["S_SI1.xlsx"]
    assert not any("movie.mp4" in c for c in session.calls)
    assert "skipped .mp4: https://x.org/movie.mp4" in job.notes


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


def test_publisher_requests_impersonate_and_fall_back_per_host(tmp_path):
    plain = FakeSession({
        "pmc.ncbi.nlm.nih.gov/": page(("/articles/PMC1/bin/a.pdf", "Supplementary data")),
        "pubs.acs.org/": FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
        "api.crossref.org/": crossref("Smith", 2012),
    })
    web = FakeSession({   # curl_cffi: passes Wiley's Cloudflare, gets PMC's reCAPTCHA (a 203)
        "onlinelibrary.wiley.com/": page(),
        "pmc.ncbi.nlm.nih.gov/": FakeResp(203, RECAPTCHA_WALL, {"content-type": "text/html"}),
        "pubs.acs.org/": FakeResp(403, CHALLENGE, {"content-type": "text/html"}),
    })
    http = si.Http(email="t@example.org", delay=0, session=plain, web_session=web)
    assert http.request("https://onlinelibrary.wiley.com/doi/full/10.1002/x").status == 200
    assert not any("wiley" in c for c in plain.calls)
    pmc = "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"
    assert http.request(pmc).status == 200 and http.request(pmc).status == 200
    assert sum("pmc.ncbi" in c for c in web.calls) == 1     # second time straight to requests
    assert http.request("https://pubs.acs.org/doi/suppl/10.1021/x").status == 403
    assert http.get_json("https://api.crossref.org/works/10.1/x")
    assert not any("crossref" in c for c in web.calls)       # APIs never impersonate


def test_metadata_cache_keeps_only_crossref_answers(tmp_path):
    routes = {"api.crossref.org/works/10.1/ok": crossref("Smith", 2012),
              "api.crossref.org/works/10.1/flaky": FakeResp(500, "oops"),
              "api.openalex.org/works/doi:10.1/flaky": J({
                  "authorships": [{"author": {"display_name": "Ann Lee"}}], "publication_year": 2020})}
    h, _ = make_harvester(tmp_path, routes)
    h.run(["10.1/ok", "10.1/flaky"])
    cache = json.loads((tmp_path / "out" / ".metadata_cache.json").read_text())
    assert cache == {"10.1/ok": {"author": "Smith", "year": "2012", "pii": "",
                                 "supplement_dois": []}}
    h2, session2 = make_harvester(tmp_path, routes)
    h2.run(["10.1/ok", "10.1/flaky"])
    assert not any("crossref.org/works/10.1/ok" in c for c in session2.calls)
    assert any("crossref.org/works/10.1/flaky" in c for c in session2.calls)


def test_blocked_dois_go_to_retry_txt_and_their_html_to_debug(tmp_path):
    h, _ = make_harvester(tmp_path, e2e_routes())
    summary = h.run([ACS, NONE])
    assert summary.with_si == 1
    out = tmp_path / "out"
    assert not (out / "retry.txt").exists()
    assert sorted(p.name for p in (out / "debug").iterdir()) == [
        "Smith2012_1_page_pubs.acs.org_403.html", "Smith2012_2_page_pubs.acs.org_403.html"]

    routes = e2e_routes()
    routes[f"{ELS_BASE}-mmc1.pdf"] = FakeResp(403, CHALLENGE, {"content-type": "text/html"})
    routes[f"{ELS_BASE}-mmc2.docx"] = FakeResp(403, CHALLENGE, {"content-type": "text/html"})
    h2, _ = make_harvester(tmp_path, routes)
    summary = h2.run([ELS])
    assert summary.blocked == [ELS]
    assert (out / "retry.txt").read_text() == f"{ELS}\n"
    note = read_manifest((out / "si_manifest.csv").read_text())[-1]["note"]
    assert ("https://www.sciencedirect.com/science/article/pii/S0045653520303878 walled (HTTP 403)"
            in note and "probes (Elsevier mmc): nothing" in note)
    assert "retry.txt" in summary.text()

    # rerunning retry.txt alone keeps SI.zip whole and clears the list once it succeeds
    h3, _ = make_harvester(tmp_path, e2e_routes())
    summary = h3.run(si.read_dois([str(out / "retry.txt")]))
    assert (summary.total, summary.with_si) == (3, 2) and not summary.blocked
    assert not (out / "retry.txt").exists()
    with zipfile.ZipFile(out / "SI.zip") as z:
        assert sorted(z.namelist()) == ["Lee2020_SI1.pdf", "Lee2020_SI2.docx", "Smith2012_SI1.pdf",
                                        "Smith2012_SI2.xlsx", "si_manifest.csv"]


def test_pause_between_dois_only(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(si.time, "sleep", slept.append)
    monkeypatch.setattr(si.random, "uniform", lambda a, b: (a + b) / 2)
    h, _ = make_harvester(tmp_path, e2e_routes(), pause=(2.0, 4.0))
    h.run([ACS, NONE])
    assert slept == [3.0]                  # between the two DOIs, not before the first
    slept.clear()
    h2, _ = make_harvester(tmp_path, e2e_routes(), pause=(2.0, 4.0))
    h2.run([ACS, NONE])                    # ACS already harvested: no pause for it
    assert slept == []


def test_main_cli(tmp_path, monkeypatch):
    session = FakeSession(e2e_routes())
    monkeypatch.setattr(si.Http, "_build_session", staticmethod(lambda: session))
    monkeypatch.setattr(si.Http, "_throttle", lambda self, url: None)
    dois = tmp_path / "dois.txt"
    dois.write_text(f"{ACS}\n{NONE}\n")
    rc = si.main([str(dois), "-o", str(tmp_path / "o"), "--email", "t@example.org",
                  "--browser", "off", "--zip", "bundle.zip", "--no-impersonate",
                  "--pause", "0", "0"])
    assert rc == 0
    assert (tmp_path / "o" / "bundle.zip").is_file()
    assert si.main(["not-a-doi", "-o", str(tmp_path / "o")]) == 2


class _EchoCookie(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (self.headers.get("Cookie") or "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.skipif(si.curl_requests is None, reason="curl_cffi not installed")
def test_cookies_file_reaches_both_clients(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   "127.0.0.1\tFALSE\t/\tFALSE\t0\tsession\tinst-login\n"
                   ".other.example\tTRUE\t/\tFALSE\t0\tstray\tx\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoCookie)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/page"
    http = si.Http(delay=0, cookies_file=str(jar))
    try:
        assert http.web is not None
        for session, impersonating in ((http.web, True), (http.session, False)):
            f = http._send(session, url, impersonating=impersonating)
            assert (f.status, f.text) == (200, "session=inst-login")
    finally:
        http.close()
        server.shutdown()


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


needs_chromium = pytest.mark.skipif(not _chromium_ok(), reason="playwright/chromium not available")
PDF3 = b"%PDF-1.6 the file inside a viewer"
PDF_INLINE = b"%PDF-1.7 a file served for display, not download"
SI_NAMES = ("t_si_001.pdf", "t_si_002.pdf", "t_si_003.xlsx", "t_si_004.pdf")


class _ChallengeSite(BaseHTTPRequestHandler):
    """Pages and SI files behind checks that only a real browser passes; a passed
    check sets the 'clear' cookie. Each SI file stands in for a behaviour seen on a
    real site (see the comments)."""

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _landing(self):
        links = "".join(f'<a href="/nav/{i}">n</a>' for i in range(20))
        files = "".join(f'<a href="/doi/suppl/10.5555/t/suppl_file/{n}">{n}</a>' for n in SI_NAMES)
        return self._send(200, f"<html><body><h2>Supporting Information</h2>{files}{links}"
                               f"</body></html>".encode(), "text/html")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        cleared = "clear=1" in (self.headers.get("Cookie") or "")
        mode = self.headers.get("Sec-Fetch-Mode")
        if path == "/landing":            # Cloudflare-style: JS sets a cookie, reloads
            if not cleared:
                body = (CHALLENGE[:-14] + b"<script>document.cookie='clear=1; path=/';"
                        b"setTimeout(() => location.reload(), 300)</script></body></html>")
                return self._send(403, body, "text/html")
            return self._landing()
        if path == "/ddos-guard":         # passes by itself after a few seconds
            if not cleared:
                return self._send(403, b"<html><head><title>DDoS-Guard</title></head><body><script>"
                                       b"setTimeout(() => {document.cookie='clear=1; path=/';"
                                       b"location.reload()}, 1500)</script></body></html>", "text/html")
            return self._landing()
        if path == "/turnstile":          # Cloudflare Turnstile: a box in a cross-origin frame
            if not cleared:
                return self._send(403, b"<html><head><title>Just a moment...</title></head><body>"
                                       b"<iframe src='https://challenges.cloudflare.com/cdn-cgi/challenge-"
                                       b"platform/h/g/turnstile/f/av0' width='300' height='65'></iframe>"
                                       b"<script>addEventListener('message', () => {document.cookie="
                                       b"'clear=1; path=/'; location.reload()})</script></body></html>",
                                  "text/html")
            return self._landing()
        if path == "/altcha":             # proof-of-work box: tick it, then the form submits
            if not cleared:
                return self._send(200, b"<html><body><form method='get' action='/altcha'>"
                                       b"<altcha-widget><input type='checkbox' id='cb'></altcha-widget>"
                                       b"</form><script>document.getElementById('cb').addEventListener("
                                       b"'change', () => setTimeout(() => {document.cookie='clear=1; path=/';"
                                       b"document.querySelector('altcha-widget').setAttribute("
                                       b"'data-state', 'verified')}, 300))</script></body></html>",
                                  "text/html")
            return self._landing()
        if path.endswith("t_si_001.pdf"):  # any client holding the cookie
            if not cleared:
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, PDF, "application/pdf")
        if path.endswith("t_si_002.pdf"):  # only a fetch() made from the page
            if not cleared or mode != "cors":
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, PDF2, "application/pdf")
        if path.endswith("t_si_003.xlsx"):  # only a navigation (cdnsciencepub, ChemRxiv suppl_file)
            if not cleared or mode != "navigate":
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, XLSX, "application/vnd.ms-excel",
                              {"Content-Disposition": 'attachment; filename="t_si_003.xlsx"'})
        if path.endswith("t_si_004.pdf"):  # a viewer page wrapping the file
            if not cleared:
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, b"<html><body><iframe id='pdf' src='/storage/t4.pdf'></iframe>"
                                   b"</body></html>", "text/html")
        if path.endswith("t_si_inline.pdf"):  # a navigation only, shown inline (no attachment)
            if not cleared or mode != "navigate":
                return self._send(403, CHALLENGE, "text/html")
            return self._send(200, PDF_INLINE, "application/pdf")
        if path == "/storage/t4.pdf":
            return self._send(200, PDF3, "application/pdf",
                              {"Content-Disposition": 'attachment; filename="t4.pdf"'})
        return self._send(404, b"nope", "text/plain")


@pytest.fixture
def challenge_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@needs_chromium
def test_browser_fallback_passes_a_js_challenge(tmp_path, challenge_site):
    landing = f"{challenge_site}/landing"
    http = si.Http(delay=0)
    h = si.Harvester(http, tmp_path / "out", browser="auto",
                     browser_profile=tmp_path / "profile", probe=False)
    for name in ("src_figshare", "src_pmc", "src_elsevier_api", "src_linked_deposits"):
        setattr(h, name, lambda job, meta: [])
    h.fetch_meta = lambda doi: si.Meta(author="Test", year="2024")
    h.landing_url = lambda doi: (landing, None)
    h.pages_for = lambda doi, url, meta, pmcid="": [url]
    try:
        summary = h.run(["10.5555/t"])
    finally:
        h.close()
    assert (summary.with_si, summary.files) == (1, 4)
    files = tmp_path / "out" / "files"
    assert (files / "Test2024_SI1.pdf").read_bytes() == PDF     # plain HTTP + adopted cookie
    assert (files / "Test2024_SI2.pdf").read_bytes() == PDF2    # in-page fetch
    assert (files / "Test2024_SI3.xlsx").read_bytes() == XLSX   # navigation download
    assert (files / "Test2024_SI4.pdf").read_bytes() == PDF3    # file inside a viewer


@needs_chromium
def test_browser_gets_a_pdf_its_viewer_would_show(tmp_path, challenge_site):
    """Shown in Chromium's PDF viewer, a navigation's body is the viewer's HTML, not the file."""
    browser = si.BrowserSession(tmp_path / "profile")
    browser.start()
    try:
        browser.open(challenge_site + "/landing")
        f = browser.fetch(f"{challenge_site}/doi/suppl/10.5555/t/suppl_file/t_si_inline.pdf",
                          challenge_site + "/landing", 10**8)
    finally:
        browser.close()
    assert (f.status, f.content) == (200, PDF_INLINE)


@needs_chromium
@pytest.mark.parametrize("path", ["/ddos-guard", "/altcha"])
def test_browser_waits_out_ddos_guard_and_ticks_altcha(tmp_path, challenge_site, path):
    browser = si.BrowserSession(tmp_path / "profile")
    browser.start()
    try:
        page_ = browser.open(challenge_site + path)
    finally:
        browser.close()
    assert page_ is not None and not page_.blocked
    assert [u.rsplit("/", 1)[-1] for u in si.si_links(page_)] == list(SI_NAMES)


TURNSTILE_BOX = ("<html><body style='margin:0'><input type='checkbox' style='position:absolute;"
                 "left:15px;top:17px;width:30px;height:30px' onclick=\"parent.postMessage('ok', '*')\">"
                 "</body></html>")


@needs_chromium
def test_browser_ticks_a_turnstile_box(tmp_path, challenge_site):
    browser = si.BrowserSession(tmp_path / "profile")
    browser.start()
    clicks = []   # the box's frame is served here instead of by Cloudflare
    browser._ctx.route("https://challenges.cloudflare.com/**", lambda route: (
        clicks.append(route.request.url),
        route.fulfill(status=200, content_type="text/html", body=TURNSTILE_BOX)))
    try:
        page_ = browser.open(challenge_site + "/turnstile")
    finally:
        browser.close()
    assert clicks and page_ is not None and not page_.blocked
    assert [u.rsplit("/", 1)[-1] for u in si.si_links(page_)] == list(SI_NAMES)


@needs_chromium
def test_headless_browser_does_not_announce_itself(tmp_path, challenge_site):
    """The headless shell sends "HeadlessChrome" in Sec-CH-UA even under a UA override;
    Chromium's new headless mode, with the UA fixed, looks like the browser it is."""
    browser = si.BrowserSession(tmp_path / "profile")
    browser.start()
    try:
        page_ = browser._first_page()
        page_.goto(challenge_site + "/nav/1")      # a secure context: userAgentData exists
        ua = page_.evaluate("navigator.userAgent")
        brands = page_.evaluate("navigator.userAgentData.brands.map(b => b.brand)")
        webdriver = page_.evaluate("navigator.webdriver")
        own = page_.evaluate("Object.getOwnPropertyNames(navigator)")
    finally:
        browser.close()
    assert "Headless" not in ua and not any("Headless" in b for b in brands)
    assert webdriver is False and "webdriver" not in own
