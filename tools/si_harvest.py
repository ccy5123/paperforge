#!/usr/bin/env python3
"""si_harvest - gather the Supporting Information (SI) of a DOI list into one zip.

Standalone script, independent of the paperforge package. It needs only
``requests``; ``openpyxl`` adds .xlsx input, ``curl_cffi`` lets publisher
requests present Chrome's TLS fingerprint, and ``playwright`` adds the
real-browser fallback for publisher pages that refuse plain HTTP clients.

    python tools/si_harvest.py dois.txt --email you@example.org -o si_out

    si_out/
      files/Smith2020_SI1.pdf, Smith2020_SI2.xlsx, Lee2019a_SI1.docx, ...
      si_manifest.csv     # one row per SI file (and one per DOI without SI)
      SI.zip              # the files above, flat, plus si_manifest.csv
      retry.txt           # DOIs that were blocked or unreachable, for a rerun
      debug/              # the HTML that came back instead of a page or file
      .metadata_cache.json

Discovery, per DOI, cheap and reliable routes first:

  1. APIs and open mirrors: figshare (ACS, T&F, SAGE and PLOS mirror their SI
     there), the PMC open-access bucket on AWS (the files the PMC article XML
     lists as supplementary material), the Elsevier Object API (with an API
     key), and deposits that DataCite or Crossref record as supplementing the
     article (Zenodo / figshare).
  2. Publisher pages over plain HTTP: the DOI landing page plus the known SI
     pages (Atypon ``/doi/suppl/``, Wiley ``/doi/full/``, IOP ``/data``,
     ScienceDirect, bioRxiv ``.supplementary-material``, PMC), scanned for SI links.
  3. URL probing, if nothing complete yet: predictable SI locations
     (Elsevier ``ars.els-cdn.com/...-mmcN``, PLOS ``.s00N``).
  4. A real browser, if still nothing: blocked pages (Cloudflare and co.) are
     reopened in a headless Chromium via Playwright, which ticks a Turnstile or
     hCaptcha box itself, and the SI is fetched inside that browser session. A
     check still standing (a picture puzzle) is reopened in a visible window for
     a person; ``--browser headless`` never opens one, ``--browser show`` does
     everything in a window. The profile keeps the cookies a passed check leaves.

Every download is checked: an HTML page (a login wall or bot challenge) is
never saved as SI, and the same file reached by two routes is kept once.
Runs are resumable: DOIs already harvested are skipped on the next run.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import http.cookiejar
import io
import itertools
import json
import logging
import mimetypes
import os
import random
import re
import string
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, quote, unquote, urldefrag, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3.util.retry import Retry

try:  # Chrome's TLS fingerprint: Cloudflare/Fastly/AWS WAF pass it where requests' is refused
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

_NET_ERRORS: tuple = (requests.RequestException,)
if curl_requests is not None:
    _NET_ERRORS += (curl_requests.exceptions.RequestException,)

__version__ = "1.1"

log = logging.getLogger("si_harvest")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Metadata APIs and CDNs tolerate a much tighter request pace than publisher pages.
_FAST_HOSTS = {
    "api.crossref.org", "api.openalex.org", "api.datacite.org", "api.figshare.com",
    "ndownloader.figshare.com", "zenodo.org", "www.ebi.ac.uk", "doi.org",
    "api.elsevier.com", "ars.els-cdn.com", "static-content.springer.com",
    "pmc-oa-opendata.s3.amazonaws.com",
}

BLOCK_STATUSES = {401, 403, 429, 503}
WALL_LIMIT = 2         # refusals in a row after which a host goes straight to the browser
_RETRY_STATUSES = {429, 500, 502, 504}

# Bot walls (Cloudflare, Akamai, Imperva, PerimeterX, DataDome, Fastly, DDoS-Guard,
# Radware, Google reCAPTCHA, Altcha). Only checked on small pages: real article pages are
# far larger, and Cloudflare injects its challenge-platform script into ordinary
# pages too.
_BLOCK_MARKERS = (
    "cf-browser-verification", "cf_chl_", "challenge-platform", "just a moment",
    "checking your browser", "verify you are human", "attention required",
    "px-captcha", "_incapsula_resource", "captcha-delivery", "access denied",
    "request unsuccessful", "are you a robot", "unusual traffic",
    "enable javascript and cookies", "<title>client challenge</title>", "bm-verify=",
    "ddos-guard", "<altcha-widget", "radware bot manager", "cdn.perfdrive.com",
)

_WALL_TITLES = ("just a moment", "client challenge", "attention required", "ddos-guard",
                "are you a robot", "checking your browser", "verify you are human",
                "access denied", "radware bot manager")

PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

DOC_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "xlsm", "csv", "tsv", "txt", "rtf", "odt",
    "ods", "ppt", "pptx", "zip", "gz", "tgz", "7z", "rar", "tar", "cif", "mol",
    "mol2", "sdf", "xyz", "pdb", "mp4", "avi", "mov", "wmv", "mpg", "mp3", "wav",
    "xml", "json", "r", "py", "m", "mat", "h5", "nc", "fasta", "fa", "gb", "ipynb",
}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "tif", "tiff", "bmp", "svg", "eps", "webp"}
SI_EXTS = DOC_EXTS | IMAGE_EXTS
_WEB_EXTS = {"html", "htm", "php", "asp", "aspx", "jsp", "do", "cgi", "action", "cfm"}

# URL shapes that are SI by construction, whatever the link text says.
_SI_URL_RE = re.compile("|".join([
    r"/suppl_file/",                              # Atypon: ACS, T&F, SAGE, CSP, Science, PNAS
    r"/action/downloadSupplement\b",              # Wiley Online Library
    r"/suppdata/",                                # RSC (before its 2026 move to Silverchair)
    r"/article-supplement/\d+/",                  # Silverchair: RSC, OUP
    r"static-content\.springer\.com/esm/",        # Springer Nature ESM
    r"_MOESM\d+_ESM\.",                           # Springer Nature ESM file names
    r"ars\.els-cdn\.com/content/image/[^?#]*-mmc\d+\.",  # Elsevier multimedia components
    r"[?&]type=supplementary\b",                  # PLOS
    r"/DC\d+/embed/media-\d+",                    # HighWire: bioRxiv, medRxiv
    r"ndownloader\.figshare\.com/files/\d+",
    r"figshare\.com/ndownloader/files/\d+",
    r"frontiersin\.org/api/v\d+/articles/\d+/file/",  # Frontiers data sheets
    r"mdpi\.com/[^?#]+/s\d+(?:[?#]|$)",           # MDPI .../s1
    r"ncbi\.nlm\.nih\.gov/(?:pmc/)?articles/[^?#]+/bin/",  # PMC
    r"iop(?:-org|\.org)[^?#]*/[a-z]+[a-z0-9]+supp\d+\.",  # IOP: erlad3a81supp1.pdf
]), re.IGNORECASE)

_SI_WORDS = re.compile(
    r"supplement|supporting[\s_-]*info|suppl|(?:^|[^a-z])si(?:[^a-z]|$)|\besm\b|moesm"
    r"|additional[\s_-]*file|data[\s_-]*sheet|appendix|mmc\d|\.s\d{3}\."
    r"|\bs\d+[\s_-]*(?:file|table|fig|figure|data|dataset|text|appendix)",
    re.IGNORECASE,
)

_EXCLUDE_RE = re.compile(
    r"^(?:mailto|javascript|tel|data):|/doi/(?:e?pdf|pdfdirect|epdf|reader)/|/pdfft\b"
    r"|showCitFormats|type=printable|\.(?:ris|bib|enw|nbib)(?:[?#]|$)",
    re.IGNORECASE,
)

PLOS_JOURNALS = {
    "pone": "plosone", "pbio": "plosbiology", "pmed": "plosmedicine",
    "pcbi": "ploscompbiol", "pgen": "plosgenetics", "ppat": "plospathogens",
    "pntd": "plosntds", "pclm": "climate", "pwat": "water",
    "pgph": "globalpublichealth", "pdig": "digitalhealth",
    "pstr": "sustainabilitytransformation", "pmen": "mentalhealth",
    "pcsy": "complexsystems",
}
# ScienceDirect itself rarely opens to a script, so these guesses are the Elsevier route:
# commonest first, then figures, media, archives and data formats.
_ELSEVIER_PROBE_EXTS = ("pdf", "docx", "xlsx", "doc", "xls", "zip", "csv", "txt", "pptx", "mp4",
                        "jpg", "tif", "png", "gif", "xml", "avi", "mov", "wmv", "mp3", "wav",
                        "rar", "7z", "gz", "ppt", "rtf", "odt", "sdf", "cif", "mol", "kmz",
                        "mat", "py", "r", "eps")

MANIFEST_FIELDS = ["doi", "stem", "status", "file", "source", "original_name",
                   "url", "bytes", "sha256", "note"]
META_FIELDS = ("author", "year", "pii", "supplement_dois")


# ============================================================
# DOIs and names
# ============================================================

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:<>A-Za-z0-9]+", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(doi: str) -> str:
    """Bare DOI: no resolver prefix, no clinging punctuation (balanced parens kept)."""
    doi = _DOI_PREFIX_RE.sub("", unquote(str(doi or "").strip())).strip()
    while doi:
        if doi[-1] in ".,;]}\"' \t\r\n":
            doi = doi[:-1]
        elif doi[-1] == ")" and doi.count("(") < doi.count(")"):
            doi = doi[:-1]
        elif doi[-1] == ">" and doi.count("<") < doi.count(">"):
            doi = doi[:-1]
        else:
            break
    return doi


def extract_dois(text: str) -> list[str]:
    out, seen = [], set()
    for m in DOI_RE.finditer(str(text or "")):
        doi = normalize_doi(m.group(0))
        if doi and doi.lower() not in seen:
            seen.add(doi.lower())
            out.append(doi)
    return out


def _dois_from_file(path: Path) -> list[str]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("Reading .xlsx needs openpyxl: pip install openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            cells = (str(v) for ws in wb.worksheets
                     for row in ws.iter_rows(values_only=True) for v in row if v is not None)
            return extract_dois("\n".join(cells))
        finally:
            wb.close()
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return extract_dois("\n".join(line for line in text.splitlines()
                                  if not line.lstrip().startswith("#")))


def read_dois(inputs: Iterable[str]) -> list[str]:
    """DOIs from files (.txt/.csv/.tsv/.xlsx) and bare DOI arguments, de-duplicated."""
    found: list[str] = []
    for item in inputs:
        path = Path(item)
        if path.is_file():
            found += _dois_from_file(path)
            continue
        dois = extract_dois(item)
        if not dois:
            log.warning("Ignoring input (not a file or DOI): %s", item)
        found += dois
    out, seen = [], set()
    for doi in found:
        if doi.lower() not in seen:
            seen.add(doi.lower())
            out.append(doi)
    return out


_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i",
})


def _slug(value: str, maxlen: int = 40) -> str:
    folded = unicodedata.normalize("NFKD", (value or "").translate(_TRANSLIT))
    ascii_ = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "", ascii_)[:maxlen]


def stem_base(author: str, year: str, doi: str) -> str:
    """``<Surname><Year>`` (``Konemann2018``); the DOI itself when both are unknown."""
    base = f"{_slug(author)}{_slug(year)}"
    return base or re.sub(r"[^A-Za-z0-9]+", "_", doi).strip("_")[:60] or "Unknown"


def collision_suffixes():
    """``a, b, ..., z, aa, ab, ...`` - the same scheme paperforge uses for keys."""
    for n in itertools.count(1):
        for combo in itertools.product(string.ascii_lowercase, repeat=n):
            yield "".join(combo)


# ============================================================
# HTTP
# ============================================================

def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


@dataclass
class Fetched:
    url: str
    status: int
    headers: Any = field(default_factory=CaseInsensitiveDict)
    content: bytes = b""
    error: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()


class Http:
    """Two faces: an identified API client (requests), and a browser-like one
    for publisher pages - curl_cffi impersonating Chrome when installed, else
    requests with Chrome headers - with cookies and Referer. Paced per host."""

    def __init__(self, email: str = "", delay: float = 1.0, user_agent: Optional[str] = None,
                 cookies_file: Optional[str] = None, max_bytes: int = 200 * 2**20,
                 session: Optional[requests.Session] = None, web_session: Any = None,
                 impersonate: Optional[str] = "chrome"):
        self.email = email
        self.delay = delay
        self.browser_ua = user_agent or BROWSER_UA
        self._ua_override = user_agent      # else curl_cffi sends its own, matching its TLS
        self.max_bytes = max_bytes
        self.session = session if session is not None else self._build_session()
        # An injected ``session`` means "exactly this stack": no impersonation then.
        if web_session is None and session is None and impersonate and curl_requests is not None:
            web_session = curl_requests.Session(impersonate=impersonate)
        self.web = web_session
        self._plain_hosts: set[str] = set()  # hosts that refuse curl_cffi but not requests
        self._walls: dict[str, int] = {}     # host -> refusals in a row, by every client
        self._last: dict[str, float] = {}
        if cookies_file:
            jar = http.cookiejar.MozillaCookieJar(str(cookies_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                if not cookie.expires:   # exporters write 0 for a session cookie, not 1970
                    cookie.expires, cookie.discard = None, True
                self.session.cookies.set_cookie(cookie)
                if self.web is not None:
                    self.web.cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                                         path=cookie.path)

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        # A refused connection (DNS, proxy, firewall, TLS) rarely heals in seconds:
        # retry it once; transient server answers get the full backoff.
        retry = Retry(total=3, connect=1, other=1, read=2, backoff_factor=1.5,
                      status_forcelist=[429, 500, 502, 504],
                      allowed_methods={"GET", "POST"}, raise_on_status=False,
                      respect_retry_after_header=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    @property
    def api_ua(self) -> str:
        contact = f"; mailto:{self.email}" if self.email else ""
        return f"si_harvest/{__version__} (+https://github.com/ccy5123/paperforge{contact})"

    def _sessions(self) -> list:
        return [self.session] + ([self.web] if self.web is not None else [])

    def close(self) -> None:
        for s in self._sessions():
            s.close()

    def adopt_browser(self, cookies: list[dict], user_agent: Optional[str]) -> None:
        """Reuse a browser's clearance: its cookies and (they are bound to it) its UA."""
        for c in cookies:
            for s in self._sessions():
                s.cookies.set(c["name"], c["value"],
                              domain=c.get("domain", ""), path=c.get("path", "/"))
        if user_agent:   # requests only: curl_cffi keeps the UA that matches its TLS
            self.browser_ua = user_agent

    def _throttle(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        gap = min(self.delay, 0.2) if host in _FAST_HOSTS else self.delay
        if gap <= 0:
            return
        last = self._last.get(host)
        if last is not None:
            wait = gap + random.uniform(0, gap / 2) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    def _headers(self, url: str, api: bool, referer: Optional[str],
                 accept: Optional[str], extra: Optional[dict], impersonating: bool = False) -> dict:
        if api:
            h = {"User-Agent": self.api_ua, "Accept": accept or "application/json"}
        elif impersonating:   # curl_cffi supplies Chrome's own header set
            h = {k: v for k, v in (("User-Agent", self._ua_override), ("Accept", accept),
                                   ("Referer", referer)) if v}
        else:
            h = dict(_BROWSER_HEADERS, **{"User-Agent": self.browser_ua})
            if accept:
                h["Accept"] = accept
            if referer:
                h["Referer"] = referer
                h["Sec-Fetch-Site"] = ("same-origin" if _origin(referer) == _origin(url)
                                       else "cross-site")
        h.update(extra or {})
        return h

    def request(self, url: str, *, api: bool = False, referer: Optional[str] = None,
                accept: Optional[str] = None, params: Optional[dict] = None,
                json_body: Any = None, headers: Optional[dict] = None,
                timeout=(10, 30)) -> Fetched:
        """GET (or POST with ``json_body``), body capped at ``max_bytes``. Never raises.

        Publisher requests go through curl_cffi first; when that is refused they are
        retried once over requests, and a host where only requests gets through
        keeps using requests."""
        kw = dict(referer=referer, accept=accept, params=params, json_body=json_body,
                  headers=headers, timeout=timeout)
        host = (urlparse(url).hostname or "").lower()
        if api:
            return self._send(self.session, url, api=True, **kw)
        if self._walls.get(host, 0) >= WALL_LIMIT:   # straight to the browser
            log.debug("  %s keeps refusing plain HTTP; not asked again: %s", host, url)
            return Fetched(url=url, status=403, error="", headers=CaseInsensitiveDict(
                {"x-si-skipped": "walled host"}))
        if self.web is None or host in self._plain_hosts:
            f = self._send(self.session, url, **kw)
        else:
            f = self._send(self.web, url, impersonating=True, **kw)
            if not f.status or _refused(f):
                g = self._send(self.session, url, **kw)
                if g.status and not _refused(g):
                    self._plain_hosts.add(host)
                f = g if g.status and (not _refused(g) or not f.status) else f
        if f.status and _refused(f):
            self._walls[host] = self._walls.get(host, 0) + 1
        elif f.status:
            self._walls.pop(host, None)
        return f

    def reset_walls(self) -> None:
        self._walls.clear()

    def _send(self, session, url: str, *, api: bool = False, impersonating: bool = False,
              referer=None, accept=None, params=None, json_body=None, headers=None,
              timeout=(10, 30)) -> Fetched:
        hdrs = self._headers(url, api, referer, accept, headers, impersonating)
        for attempt in range(3):
            self._throttle(url)
            f = self._send_once(session, url, hdrs, params, json_body, timeout)
            # requests' own adapter already retried these; curl_cffi has none
            if not impersonating or f.status not in _RETRY_STATUSES or attempt == 2:
                return f
            time.sleep(1.5 * 2 ** attempt)
        return f

    def _send_once(self, session, url, hdrs, params, json_body, timeout) -> Fetched:
        try:
            if json_body is not None:
                r = session.post(url, json=json_body, params=params, headers=hdrs,
                                 timeout=timeout, stream=True)
            else:
                r = session.get(url, params=params, headers=hdrs, timeout=timeout,
                                stream=True, allow_redirects=True)
        except _NET_ERRORS as e:
            return Fetched(url=url, status=0, error=_short_error(e))
        if session is self.web:   # urllib3 logs the requests side
            log.debug("curl_cffi %s %s -> %s", "POST" if json_body is not None else "GET",
                      url, r.status_code)
        try:
            final, rh = str(r.url or url), CaseInsensitiveDict(r.headers)
            if int(rh.get("content-length") or 0) > self.max_bytes:
                return Fetched(url=final, status=r.status_code, headers=rh, error="too large")
            buf = bytearray()
            # curl_cffi picks its own chunk size (and warns when given one)
            for chunk in (r.iter_content() if session is self.web else r.iter_content(65536)):
                buf += chunk
                if len(buf) > self.max_bytes:
                    return Fetched(url=final, status=r.status_code, headers=rh,
                                   error="too large")
            return Fetched(url=final, status=r.status_code, headers=rh, content=bytes(buf))
        except _NET_ERRORS + (ValueError,) as e:
            return Fetched(url=url, status=0, error=_short_error(e))
        finally:
            r.close()

    def get_json(self, url: str, *, ok: tuple = (200,), **kw) -> Any:
        f = self.request(url, api=True, **kw)
        if f.status not in ok:
            return None
        try:
            return json.loads(f.content)
        except ValueError:
            return None

    def probe(self, url: str) -> bool:
        """Is there a (non-HTML) file at ``url``? Reads the headers only."""
        self._throttle(url)
        try:
            r = self.session.get(url, headers=self._headers(url, False, None, "*/*", None),
                                 timeout=(10, 20), stream=True, allow_redirects=True)
        except requests.RequestException:
            return False
        try:
            return r.status_code == 200 and "text/html" not in (
                r.headers.get("content-type") or "").lower()
        finally:
            r.close()


def _short_error(e: Exception) -> str:
    """``ProxyError (Tunnel connection failed: 403 Forbidden)`` rather than a pool dump."""
    reason = str(e)
    m = re.findall(r"\(([^()]*)\)\)*$", reason) or re.findall(r"'([^']{4,})'\)*$", reason)
    return f"{type(e).__name__} ({(m[-1] if m else reason)[:120]})"


def looks_blocked(status: int, text: str, headers: Any = None) -> bool:
    """A bot wall / challenge rather than the page we asked for?"""
    if status in BLOCK_STATUSES:
        return True
    if headers is not None and headers.get("x-amzn-waf-action"):   # AWS WAF: 202, empty body
        return True
    if not 200 <= status < 300 or not text:
        return False
    low = text.lower()
    # A wall's title gives it away at any size (ScienceDirect's runs to 1.2 MB of script)
    title = re.search(r"<title[^>]*>([^<]{0,200})</title>", low[:200000])
    if title and any(t in title.group(1) for t in _WALL_TITLES):
        return True
    return len(low) <= 60000 and any(m in low for m in _BLOCK_MARKERS)


def _refused(f: Fetched) -> bool:
    """Did a bot wall answer instead of the server? (HTML is only scanned when small.)"""
    if f.status in BLOCK_STATUSES or f.headers.get("x-amzn-waf-action"):
        return True
    return (len(f.content) <= 60000 and _looks_html(f.content, f.content_type)
            and looks_blocked(f.status, f.text))


def _looks_html(content: bytes, content_type: str) -> bool:
    if content_type in ("text/html", "application/xhtml+xml"):
        return True
    head = content[:1024].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:200]


def reject_reason(f: Fetched) -> str:
    """Why a download is not an SI file ('' when it is one)."""
    if f.error:
        return f.error
    if f.status in BLOCK_STATUSES or f.headers.get("x-amzn-waf-action"):
        return "blocked"
    if f.status != 200:
        return f"HTTP {f.status}"
    if not f.content:
        return "empty"
    if _looks_html(f.content, f.content_type):
        return "blocked" if looks_blocked(200, f.text) else "html"
    return ""


# ============================================================
# Pages and links
# ============================================================

@dataclass
class PageLinks:
    url: str
    anchors: list[tuple[str, str]]      # (absolute href, visible label)
    pdf_urls: set[str] = field(default_factory=set)   # citation_pdf_url: the paper itself
    blocked: bool = False


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self.pdf_urls: list[str] = []
        self.base: Optional[str] = None
        self._open: list[list] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            extra = " ".join(filter(None, (a.get("title"), a.get("aria-label"), a.get("download"))))
            self._open.append([a["href"], [extra]])
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "meta" and (a.get("name") or "").lower() == "citation_pdf_url":
            self.pdf_urls.append(a.get("content", ""))

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            self._flush(self._open.pop())

    def handle_data(self, data):
        for item in self._open:
            item[1].append(data)

    def _flush(self, item):
        self.anchors.append((item[0], " ".join(" ".join(item[1]).split())))

    def close(self):
        super().close()
        while self._open:
            self._flush(self._open.pop())


def parse_page(html: str, base_url: str) -> PageLinks:
    p = _LinkParser()
    try:
        p.feed(html)
        p.close()
    except Exception as e:  # malformed markup: keep whatever was parsed
        log.debug("HTML parse error on %s: %s", base_url, e)
    base = urljoin(base_url, p.base) if p.base else base_url
    anchors = [(urldefrag(urljoin(base, href.strip()))[0], label) for href, label in p.anchors]
    pdfs = {urljoin(base, u) for u in p.pdf_urls if u}
    return PageLinks(url=base_url, anchors=anchors, pdf_urls=pdfs)


def _url_ext(url: str) -> str:
    """The file extension a URL points at, from its path or a ``file=`` parameter."""
    p = urlparse(url)
    names = [unquote(p.path.rsplit("/", 1)[-1])]
    q = parse_qs(p.query)
    for key in ("file", "filename", "name"):
        names += [v.rsplit("/", 1)[-1] for v in q.get(key, [])]
    for name in names:
        if "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in SI_EXTS:
                return ext
    return ""


def is_si_link(url: str, label: str = "") -> bool:
    if not url.lower().startswith(("http://", "https://")) or _EXCLUDE_RE.search(url):
        return False
    explicit = bool(_SI_URL_RE.search(url))
    worded = bool(_SI_WORDS.search(unquote(url)) or _SI_WORDS.search(label or ""))
    ext = _url_ext(url)
    if ext in IMAGE_EXTS:  # figures are links too; take images only when plainly SI
        return explicit and worded
    return explicit or (bool(ext) and worded)


def _names_other_doi(url: str, doi: str) -> bool:
    """Does the URL carry some other article's DOI? Reference lists link the SI of
    cited papers (``doi.org/10.1021/X/SUPPL_FILE/X_SI_001.PDF``); a page's own SI
    links carry its DOI, a version-less form of it, or none at all."""
    if not doi:
        return False
    text = unquote(unquote(url)).lower()
    if not re.search(r"10\.\d{4,9}/", text):
        return False
    base = re.sub(r"[-/.]v\d+$", "", doi.lower())
    return base not in text


def si_links(page: PageLinks, doi: str = "") -> list[str]:
    out, seen = [], set()
    for url, label in page.anchors:
        if url in page.pdf_urls or url in seen or _names_other_doi(url, doi):
            continue
        if is_si_link(url, label):
            seen.add(url)
            out.append(url)
    return out


# ============================================================
# File naming
# ============================================================

def _cd_filename(cd: str) -> str:
    if not cd:
        return ""
    m = re.search(r"filename\*\s*=\s*(?:[\w-]+'[^']*')?([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    # figshare's S3 percent-encodes this one too; Chrome decodes it, so do we
    return unquote(m.group(1).strip()) if m else ""


def _name_from_url(url: str) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    for key in ("file", "filename", "id"):
        if q.get(key):
            return q[key][0].rsplit("/", 1)[-1]
    return unquote(p.path.rstrip("/").rsplit("/", 1)[-1])


def _ext_of(name: str) -> str:
    name = (name or "").strip().rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].lower()
    return ext if re.fullmatch(r"[a-z0-9]{1,6}", ext) and ext not in _WEB_EXTS else ""


def _sniff_ext(content: bytes, content_type: str) -> str:
    if content.startswith(b"%PDF-"):
        return ".pdf"
    if content.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except zipfile.BadZipFile:
            return ".zip"
        for prefix, ext in (("word/", ".docx"), ("xl/", ".xlsx"), ("ppt/", ".pptx")):
            if any(n.startswith(prefix) for n in names):
                return ext
        return ".zip"
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # legacy Office
        return ".xls" if "excel" in content_type else ".ppt" if "powerpoint" in content_type else ".doc"
    for magic, ext in ((b"\x1f\x8b", ".gz"), (b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"),
                       (b"GIF8", ".gif"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"),
                       (b"{\\rtf", ".rtf"), (b"7z\xbc\xaf\x27\x1c", ".7z"), (b"Rar!", ".rar")):
        if content.startswith(magic):
            return ext
    if content[4:8] == b"ftyp":
        return ".mp4"
    guess = mimetypes.guess_extension(content_type) if content_type else None
    if guess and guess not in (".html", ".htm", ".bin", ".a", ".exe"):
        return guess
    return ".txt" if b"\x00" not in content[:4096] else ".bin"


def detect_ext(content: bytes, *, names: Iterable[str] = (), url: str = "",
               content_type: str = "") -> str:
    """Extension for a saved SI file: a name the server/source gave, else the URL,
    else the bytes themselves."""
    for name in names:
        ext = _ext_of(name)
        if ext:
            return "." + ext
    ext = _url_ext(url)
    return "." + ext if ext else _sniff_ext(content, content_type)


# ============================================================
# Browser fallback (Playwright)
# ============================================================

class BrowserSession:
    """A real Chromium for pages that refuse plain HTTP clients.

    Uses a persistent profile, so a challenge passed (or an institutional login
    done) in a visible window is remembered across runs.
    """

    _ANCHORS_JS = r"""els => els.map(e => [e.href,
        [e.innerText || e.textContent || '', e.getAttribute('title') || '',
         e.getAttribute('aria-label') || ''].join(' ').replace(/\s+/g, ' ').trim()])"""

    # Same-origin fetch inside the page: rides the page's cookies, TLS and headers.
    _FETCH_JS = r"""async ([u, maxBytes]) => {
        const r = await fetch(u, {credentials: 'include'});
        const len = parseInt(r.headers.get('content-length') || '0', 10);
        if (len && len > maxBytes) return {status: -1, url: r.url};
        const b = new Uint8Array(await r.arrayBuffer());
        if (b.length > maxBytes) return {status: -1, url: r.url};
        let s = '';
        for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
        return {status: r.status, url: r.url, ct: r.headers.get('content-type') || '',
                cd: r.headers.get('content-disposition') || '', b64: btoa(s)};
    }"""

    # Checks a page shows besides its text. DDoS-Guard clears by itself in seconds;
    # an Altcha proof-of-work box needs ticking, and once verified its form submitting.
    _CHALLENGE_JS = r"""() => {
        const t = (document.title || '').toLowerCase();
        if (t.includes('ddos-guard') || document.querySelector('script[src*="ddos-guard"]'))
            return 'wait';
        const w = document.querySelector('altcha-widget');
        if (!w || document.querySelectorAll('a[href]').length >= 15) return '';
        const root = w.shadowRoot || w;
        const holder = w.hasAttribute('data-state') ? w : root.querySelector('[data-state]');
        if ((holder && holder.getAttribute('data-state')) !== 'verified') return 'altcha';
        const form = w.closest('form');
        if (form && !window.__siAltchaSubmitted) {
            window.__siAltchaSubmitted = true;
            form.requestSubmit ? form.requestSubmit() : form.submit();
        }
        return 'wait';
    }"""

    _ALTCHA_CLICK_JS = r"""() => {
        const w = document.querySelector('altcha-widget');
        const cb = w && (w.shadowRoot || w).querySelector('input[type="checkbox"]');
        if (!cb) return false;
        cb.click();
        cb.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }"""

    # Human checks that sit in a cross-origin frame and start with one tick of a box:
    # Cloudflare Turnstile, and hCaptcha (which mostly answers the tick with a picture
    # puzzle, left to a person). (host, part of the frame URL)
    _CHECKBOX_FRAMES = (("challenges.cloudflare.com", "/turnstile/"), ("hcaptcha.com", "frame=checkbox"))
    _TICK_AFTER = 3.0   # seconds a check is left to clear by itself before the tick

    # A viewer page that wraps the file rather than serving it: where the file is.
    _EMBEDDED_JS = r"""() => {
        for (const sel of ['iframe#pdf', 'embed#pdf', 'iframe[src*=".pdf"]',
                           'embed[type="application/pdf"]', 'object[type="application/pdf"]',
                           'iframe[src*="/storage/"]', 'iframe[src*="/downloads/"]']) {
            const e = document.querySelector(sel);
            const u = e && (e.src || e.data);
            if (u && /^https?:/.test(u)) return u;
        }
        const pdfs = document.querySelectorAll('a[href$=".pdf"]');   // a bare download page
        if (pdfs.length === 1 && document.querySelectorAll('a[href]').length < 15) return pdfs[0].href;
        return '';
    }"""

    # navigator.webdriver is left alone: --disable-blink-features=AutomationControlled
    # already makes it the plain ``false`` a real Chrome reports, whereas redefining it
    # gives ``undefined`` and an own property on navigator, which no real Chrome has.
    _STEALTH_JS = "window.chrome = window.chrome || {runtime: {}};"

    def __init__(self, profile_dir: Path, show: bool = False):
        self.profile_dir = Path(profile_dir)
        self.show = show
        self.user_agent: Optional[str] = None
        self._pw = None
        self._ctx = None
        self._pages: dict[str, Any] = {}
        self._altcha_ticked: set[tuple[int, str]] = set()
        # (page, URL) -> when a check was first seen there; None once its box is ticked
        self._check_seen: dict[tuple[int, str], Optional[float]] = {}

    @staticmethod
    def installed() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def start(self) -> None:
        from playwright.sync_api import sync_playwright
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._download_pdfs()
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._launch(None)
            ua = self._first_page().evaluate("navigator.userAgent")
            if "HeadlessChrome" in ua:  # an easy tell; present as the Chrome it is
                ua = ua.replace("HeadlessChrome", "Chrome")
                self._ctx.close()
                self._ctx = self._launch(ua)
            self.user_agent = ua
        except Exception:
            self.close()
            raise

    def _download_pdfs(self) -> None:
        """A PDF Chromium shows in its viewer reaches Playwright as the viewer's HTML,
        not the file; with this profile preference set, Chromium downloads it."""
        path = self.profile_dir / "Default" / "Preferences"
        try:
            prefs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prefs = {}
        if not isinstance(prefs.get("plugins"), dict):
            prefs["plugins"] = {}
        prefs["plugins"]["always_open_pdf_externally"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs), encoding="utf-8")

    def _launch(self, user_agent: Optional[str]):
        kw: dict = dict(headless=not self.show, accept_downloads=True, locale="en-US",
                        args=["--disable-blink-features=AutomationControlled"])
        if user_agent:
            kw["user_agent"] = user_agent
        if os.environ.get("SI_CHROMIUM"):
            kw["executable_path"] = os.environ["SI_CHROMIUM"]
        elif not self.show:
            # Chromium's new headless mode: the full browser, without a window. Playwright's
            # default headless shell says "HeadlessChrome" in Sec-CH-UA even under a UA
            # override, and Cloudflare and Akamai turn it away.
            kw["channel"] = "chromium"
        try:
            ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
        except Exception as e:
            if "channel" not in kw:
                raise
            log.debug("  browser: new headless unavailable (%s); using the headless shell", e)
            kw.pop("channel")
            ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
        ctx.add_init_script(self._STEALTH_JS)
        return ctx

    def _first_page(self):
        return self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _page_for(self, url: str):
        origin = _origin(url)
        if origin not in self._pages:
            free = [p for p in self._ctx.pages if p not in self._pages.values()]
            self._pages[origin] = free[0] if free else self._ctx.new_page()
        return self._pages[origin]

    def cookies(self) -> list[dict]:
        return self._ctx.cookies() if self._ctx else []

    def _challenged(self, page) -> bool:
        """Is the page a bot check right now? Ticks an Altcha, Turnstile or hCaptcha
        box on the way."""
        try:
            kind = page.evaluate(self._CHALLENGE_JS)
            if kind == "altcha":
                self._tick_altcha(page)
            # the first response's status is stale once a challenge reloads: read the page
            blocked = bool(kind) or looks_blocked(200, page.content())
        except Exception:
            return True  # mid-navigation
        if blocked:
            self._tick_checkbox(page)
        return blocked

    def _tick_checkbox(self, page) -> None:
        """Click a Turnstile or hCaptcha box once per page URL, as a person would: the
        mouse on the box, which lives in a cross-origin frame. The site still decides."""
        key = (id(page), page.url)
        first = self._check_seen.setdefault(key, time.monotonic())
        if first is None or time.monotonic() - first < self._TICK_AFTER:
            return
        for frame in page.frames:
            if not any(host in frame.url and part in frame.url for host, part in self._CHECKBOX_FRAMES):
                continue
            try:
                box = frame.frame_element().bounding_box()
                if not box or box["width"] < 50:   # not drawn yet: next poll
                    return
                self._check_seen[key] = None
                page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                log.debug("  browser: ticked the check box on %s", page.url)
            except Exception as e:
                log.debug("  browser: could not tick the check box: %s", e)
            return

    def _tick_altcha(self, page) -> None:
        key = (id(page), page.url)
        if key in self._altcha_ticked:   # a second click would cancel the proof-of-work
            return
        self._altcha_ticked.add(key)
        try:
            page.locator("altcha-widget input[type='checkbox']").first.click(force=True, timeout=5_000)
        except Exception:
            page.evaluate(self._ALTCHA_CLICK_JS)

    def _deadline(self) -> float:
        return time.monotonic() + (180 if self.show else 25)

    def _warn_once(self, warned: bool, url: str) -> bool:
        if self.show and not warned:
            log.warning("  browser: pass the check in the open window: %s", url)
        return True

    def open(self, url: str) -> Optional[PageLinks]:
        page = self._page_for(url)
        for attempt in range(2):   # a challenge redirecting mid-load can abort the first goto
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                break
            except Exception as e:
                log.debug("  browser: could not open %s: %s", url, str(e).splitlines()[0])
                if attempt:
                    return None
                page.wait_for_timeout(2000)
        deadline, warned = self._deadline(), False
        while (blocked := self._challenged(page)) and time.monotonic() < deadline:
            warned = self._warn_once(warned, url)
            page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        try:
            anchors = page.eval_on_selector_all("a[href]", self._ANCHORS_JS)
            pdfs = page.eval_on_selector_all('meta[name="citation_pdf_url"]',
                                             "els => els.map(e => e.content)")
            final = page.url
        except Exception:
            anchors, pdfs, final = [], [], url
        # After a redirect (doi.org -> publisher) the page serves the new origin's fetches.
        self._pages.setdefault(_origin(final), page)
        return PageLinks(url=final, anchors=[(urldefrag(a)[0], label) for a, label in anchors],
                         pdf_urls=set(pdfs), blocked=blocked)

    def fetch(self, url: str, referer: Optional[str], max_bytes: int) -> Fetched:
        origin = _origin(url)
        if origin not in self._pages and referer and _origin(referer) == origin:
            self.open(referer)
        page = self._pages.get(origin)
        if page is not None:
            try:
                r = page.evaluate(self._FETCH_JS, [url, max_bytes])
                if r.get("status") == -1:
                    return Fetched(url=url, status=200, error="too large")
                content = base64.b64decode(r.get("b64", ""))
                if r.get("status") == 200 and not _looks_html(content, r.get("ct", "")):
                    return Fetched(url=r.get("url") or url, status=200,
                                   headers=CaseInsensitiveDict({"content-type": r.get("ct", ""),
                                                                "content-disposition": r.get("cd", "")}),
                                   content=content)
                log.debug("  browser: in-page fetch got HTTP %s for %s", r.get("status"), url)
            except Exception as e:
                log.debug("  browser: in-page fetch failed for %s: %s", url, e)
        f = self._navigate(url, max_bytes)
        if f is not None:
            return f
        try:
            resp = self._ctx.request.get(url, headers={"Referer": referer} if referer else None,
                                         timeout=120_000)
            return Fetched(url=resp.url, status=resp.status,
                           headers=CaseInsensitiveDict(resp.headers), content=resp.body())
        except Exception as e:
            return Fetched(url=url, status=0, error=f"browser: {e}")

    def _navigate(self, url: str, max_bytes: int, depth: int = 0) -> Optional[Fetched]:
        """Open the file URL in a tab of its own, as clicking its link would. A check
        guarding the file itself (Cloudflare on Atypon ``suppl_file``, AWS WAF on
        figshare) runs and clears there, which an in-page fetch() cannot do; then the
        browser downloads the file, shows it inline, or wraps it in a viewer page."""
        page = self._ctx.new_page()
        downloads: list = []
        documents: list = []
        page.on("download", lambda d: downloads.append(d))   # Playwright wraps plain functions only
        page.on("response", lambda r: documents.append(r)
                if r.frame == page.main_frame and r.request.is_navigation_request() else None)

        def shown_file():
            return next((r for r in reversed(documents) if r.status == 200 and "html" not in
                         (r.headers.get("content-type") or "text/html")), None)
        try:
            try:
                page.goto(url, wait_until="commit", timeout=45_000)
            except Exception as e:   # "Download is starting": the usual way a file arrives
                log.debug("  browser: navigating to %s: %s", url, str(e).splitlines()[0])
            deadline, warned, calm = self._deadline(), False, 0
            while not downloads and not shown_file() and time.monotonic() < deadline:
                if self._challenged(page):
                    warned, calm = self._warn_once(warned, url), 0
                elif (calm := calm + 1) >= 3:     # an ordinary page, and no file coming
                    break
                page.wait_for_timeout(1000)
            if not downloads and shown_file():
                page.wait_for_timeout(500)        # a download announces itself after its response
            if downloads:
                return self._read_download(downloads[0], url, max_bytes)
            doc = shown_file()
            if doc is not None:
                body = doc.body()
                if len(body) > max_bytes:
                    return Fetched(url=url, status=200, error="too large")
                return Fetched(url=url, status=200, headers=CaseInsensitiveDict(doc.headers),
                               content=body)
            if depth == 0 and not self._challenged(page):
                inner = page.evaluate(self._EMBEDDED_JS)
                if inner and urldefrag(inner)[0] != urldefrag(url)[0]:
                    log.debug("  browser: %s wraps %s", url, inner)
                    return self._navigate(inner, max_bytes, depth=1)
            return None
        except Exception as e:
            log.debug("  browser: navigation download failed for %s: %s", url, e)
            return None
        finally:
            page.close()

    @staticmethod
    def _read_download(download, url: str, max_bytes: int) -> Fetched:
        path = download.path()                    # waits for the download to finish
        if os.path.getsize(path) > max_bytes:
            return Fetched(url=url, status=200, error="too large")
        name = download.suggested_filename or ""
        headers = {"content-disposition": f'attachment; filename="{name}"'} if name else {}
        return Fetched(url=url, status=200, headers=CaseInsensitiveDict(headers),
                       content=Path(path).read_bytes())

    def close(self) -> None:
        for closer in (getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._ctx = self._pw = None
        self._pages = {}


# ============================================================
# Harvesting
# ============================================================

@dataclass
class Meta:
    author: str = ""
    year: str = ""
    pii: str = ""                        # Elsevier
    supplement_dois: list[str] = field(default_factory=list)   # Crossref is-supplemented-by
    from_crossref: bool = False          # Crossref answered: worth caching


@dataclass
class Candidate:
    url: str
    source: str
    name: str = ""                       # file name, when the source states one
    referer: Optional[str] = None
    complete: bool = True                # source carries the article's whole SI set


@dataclass
class SIFile:
    filename: str
    source: str
    original_name: str
    url: str
    size: int
    sha256: str


@dataclass
class Job:
    doi: str
    stem: str
    files: list[SIFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hashes: set[str] = field(default_factory=set)
    names: dict[str, str] = field(default_factory=dict)   # original name (lowercased) -> source
    mirror_hits: dict[str, int] = field(default_factory=dict)  # API/mirror source -> candidates
    page_log: dict[str, str] = field(default_factory=dict)     # page URL -> what it gave
    probed: list[str] = field(default_factory=list)            # probe sources that ran
    done_urls: set[str] = field(default_factory=set)
    blocked_pages: list[str] = field(default_factory=list)
    thin_pages: list[str] = field(default_factory=list)
    blocked_files: list[Candidate] = field(default_factory=list)
    pmcid: str = ""                      # set when PMC has the article but not in its open bucket
    complete: bool = False
    blocked: bool = False
    failed: bool = False                 # the publisher was unreachable (network error)

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def explain(self) -> None:
        """For a DOI that ends without files: what each route gave, so the manifest
        tells "the pages say there is no SI" from "the pages never opened"."""
        mirrors = ", ".join(f"{k} {v}" for k, v in self.mirror_hits.items() if v) or "nothing"
        parts = [f"mirrors: {mirrors}"] + [f"{u} {o}" for u, o in self.page_log.items()]
        if self.probed:
            parts.append("probes (" + ", ".join(self.probed) + "): nothing")
        self.note("checked " + "; ".join(parts))

    @property
    def status(self) -> str:
        if self.files:
            return "ok"
        return "blocked" if self.blocked else "error" if self.failed else "none"

    def rows(self) -> list[dict]:
        note = "; ".join(self.notes)[:1000]
        if not self.files:
            return [{"doi": self.doi, "stem": self.stem, "status": self.status, "note": note}]
        return [{"doi": self.doi, "stem": self.stem, "status": "ok", "file": f.filename,
                 "source": f.source, "original_name": f.original_name, "url": f.url,
                 "bytes": f.size, "sha256": f.sha256, "note": note if i == 0 else ""}
                for i, f in enumerate(self.files)]


def _lucene_phrase(value: str) -> str:
    """A quoted query term. DataCite rejects a backslash-escaped ``/`` (HTTP 400)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pmc_supplement_names(xml: str) -> list[str]:
    """File names a JATS article lists under <supplementary-material> (not its figures)."""
    out: list[str] = []
    for m in re.finditer(r"<supplementary-material\b([^>]*)>", xml):
        block = m.group(1)
        if not block.rstrip().endswith("/"):
            end = xml.find("</supplementary-material>", m.end())
            block += xml[m.end():end if end >= 0 else len(xml)]
        for href in re.findall(r'xlink:href="([^"]+)"', block):
            name = html.unescape(href)
            if not re.match(r"[a-z][a-z0-9+.-]*:", name, re.I) and name not in out:
                out.append(name)
    return out


def _norm_pii(value: str) -> str:
    pii = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return pii if re.fullmatch(r"S?\d{7}[\dX]\d{7}[\dX]", pii) else ""


class Harvester:
    def __init__(self, http: Http, out_dir, *, browser: str = "auto",
                 browser_profile=None, elsevier_key: Optional[str] = None,
                 probe: bool = True, overwrite: bool = False, zip_name: str = "SI.zip",
                 pause: tuple[float, float] = (0.0, 0.0), retries: int = 1,
                 skip_exts: Iterable[str] = ()):
        self.http = http
        self.out_dir = Path(out_dir)
        self.files_dir = self.out_dir / "files"
        self.debug_dir = self.out_dir / "debug"
        self.manifest_path = self.out_dir / "si_manifest.csv"
        self.retry_path = self.out_dir / "retry.txt"
        self.cache_path = self.out_dir / ".metadata_cache.json"
        self.zip_path = self.out_dir / zip_name
        self.pause = pause                   # seconds between two DOIs, drawn uniformly
        self.retries = retries               # extra passes over DOIs left blocked/unreachable
        self.skip_exts = {e.lower().lstrip(".") for e in skip_exts if e}
        self.browser_mode = browser          # auto | headless | show | off
        self.browser_profile = Path(browser_profile) if browser_profile else (
            Path.home() / ".cache" / "si_harvest" / "browser-profile")
        self._windows = True                 # may still open a window for a person (auto)
        self.elsevier_key = elsevier_key
        self.probe = probe
        self.overwrite = overwrite
        self._browser: Optional[BrowserSession] = None
        self._stems: dict[str, str] = {}     # stem -> doi (lowercase) that owns it

    # ---- run ---------------------------------------------------------

    def run(self, dois: list[str]) -> "Summary":
        self.files_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest()
        for rows in manifest.values():
            if rows and rows[0].get("stem"):
                self._stems.setdefault(rows[0]["stem"], rows[0]["doi"].lower())
        cache = self._load_cache()

        worked: list[str] = []
        for i, doi in enumerate(dois, start=1):
            old = manifest.get(doi.lower())
            if old and not self.overwrite and self._is_done(old):
                log.info("[%d/%d] %s -> already harvested (%d files)",
                         i, len(dois), doi, len(old))
                continue
            self._process(doi, f"{i}/{len(dois)}", manifest, cache, pause=bool(worked))
            worked.append(doi)

        # Bot walls judge each visit anew: what one pass could not open, the next may.
        for round_ in range(1, self.retries + 1):
            again = [d for d in worked if manifest[d.lower()][0].get("status") in ("blocked", "error")]
            if not again:
                break
            log.info("Retry %d: %d DOI(s) blocked or unreachable", round_, len(again))
            if self._browser is not None:        # a fresh browser for a fresh look
                self._browser.close()
                self._browser = None
            self.http.reset_walls()
            for i, doi in enumerate(again, start=1):
                self._process(doi, f"retry {round_}, {i}/{len(again)}", manifest, cache, pause=True)

        # The whole output directory, not just this run's DOIs: a rerun of retry.txt
        # must leave SI.zip complete.
        groups = list(manifest.values())
        self._write_zip(groups)
        summary = Summary.from_rows(groups, self.zip_path)
        self._write_retry(summary.blocked + summary.errors)
        return summary

    def _process(self, doi: str, label: str, manifest: dict, cache: dict, pause: bool) -> None:
        if pause and self.pause[1] > 0:
            time.sleep(random.uniform(*self.pause))
        key = doi.lower()
        old = manifest.get(key)
        for row in old or []:          # start this DOI clean
            if row.get("file"):
                (self.files_dir / row["file"]).unlink(missing_ok=True)
        meta = self._cached_meta(doi, cache)
        # Keep a stem files were already named with; else name afresh (an
        # earlier run may have had no metadata to name by).
        had_files = bool(old) and old[0].get("status") == "ok"
        stem = (old[0].get("stem") if had_files else "") or self.assign_stem(doi, meta)
        log.info("[%s] %s -> %s", label, doi, stem)
        job = self.harvest(doi, stem, meta)
        manifest[key] = job.rows()
        self._write_manifest(manifest)
        log.info("  = %s", {"ok": f"{len(job.files)} SI file(s)",
                            "blocked": "blocked, no SI saved",
                            "error": "network error, no SI saved",
                            "none": "no SI found"}[job.status])

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self.http.close()

    def assign_stem(self, doi: str, meta: Meta) -> str:
        base = stem_base(meta.author, meta.year, doi)
        for suffix in itertools.chain([""], collision_suffixes()):
            stem = base + suffix
            owner = self._stems.get(stem)
            if owner is None or owner == doi.lower():
                self._stems[stem] = doi.lower()
                return stem
        raise AssertionError("unreachable: collision_suffixes is infinite")

    def _is_done(self, rows: list[dict]) -> bool:
        return all(r.get("status") == "ok" and r.get("file")
                   and (self.files_dir / r["file"]).is_file() for r in rows)

    # ---- one DOI -----------------------------------------------------

    def harvest(self, doi: str, stem: str, meta: Meta) -> Job:
        job = Job(doi=doi, stem=stem)

        # 1. APIs and open mirrors.
        for name, source in (("figshare", self.src_figshare), ("pmc", self.src_pmc),
                             ("elsevier-api", self.src_elsevier_api),
                             ("linked", self.src_linked_deposits)):
            try:
                found = source(job, meta)
                job.mirror_hits[name] = len(found)
                self._take(job, found)
            except Exception as e:  # a broken source must not sink the DOI
                log.debug("  %s crashed: %r", name, e)
                job.note(f"{name}: error ({type(e).__name__})")

        # 2. Publisher pages over plain HTTP.
        landing, prefetched = self.landing_url(doi)
        pages = self.pages_for(doi, landing, meta, job.pmcid)
        for page_url in pages:
            f = prefetched if prefetched is not None and page_url == landing else \
                self.http.request(page_url)
            self._scan_page(job, page_url, f)

        # 3. Predictable SI locations.
        if not job.complete and self.probe:
            self._take(job, self.probe_candidates(job, meta))

        # 4. A real browser for what plain HTTP could not get: blocked SI files
        #    always, blocked or JS-only pages only while no complete SI set is in hand.
        if job.files and job.complete and job.blocked_pages:
            job.note("pages blocked but SI found elsewhere: " + " ".join(job.blocked_pages))
        self._browser_pass(job, pages=not job.complete)
        if not job.files:
            job.explain()
        return job

    def _scan_page(self, job: Job, page_url: str, f: Fetched) -> None:
        blocked = looks_blocked(f.status, f.text, f.headers)
        if f.status == 200 and not f.error:
            page = parse_page(f.text, f.url or page_url)
            found = si_links(page, job.doi)
            if found:
                job.page_log[page_url] = f"{len(found)} SI links"
                self._take(job, [Candidate(u, "page", referer=page.url) for u in found])
                return
            if not blocked:
                job.page_log[page_url] = "opened, no SI links"
                if len(page.anchors) < 15:   # likely a JS shell: worth a real browser
                    job.thin_pages.append(page_url)
                return
        if blocked:
            job.page_log[page_url] = ("skipped (host keeps refusing)" if f.headers.get("x-si-skipped")
                                      else f"walled (HTTP {f.status})")
            job.blocked_pages.append(page_url)
            self._save_debug(job, f, "page")
        elif f.status == 0:
            job.page_log[page_url] = f"error ({f.error})"
            job.failed = True
            job.note(f"page error: {f.error} {page_url}")
        else:
            job.page_log[page_url] = f"HTTP {f.status}"
            log.debug("  page HTTP %s: %s", f.status, page_url)

    def _take(self, job: Job, items: Iterable[Candidate]) -> None:
        for cand in items:
            self._download(job, cand)

    def _save_debug(self, job: Job, f: Fetched, kind: str) -> None:
        """Keep the HTML that came back in place of a page or file, to see why."""
        if not f.content or not _looks_html(f.content, f.content_type):
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        host = re.sub(r"[^A-Za-z0-9.-]+", "_", urlparse(f.url).hostname or "unknown")
        n = len(list(self.debug_dir.glob(f"{job.stem}_*"))) + 1
        (self.debug_dir / f"{job.stem}_{n}_{kind}_{host}_{f.status}.html").write_bytes(f.content)

    def _download(self, job: Job, cand: Candidate,
                  browser: Optional[BrowserSession] = None) -> Optional[tuple[str, Fetched]]:
        """Fetch and store one candidate. With a ``browser``, returns (reason, response)
        when even the browser was refused, for the caller to judge."""
        key = urldefrag(cand.url)[0]
        if key in job.done_urls:
            return None
        ext = _ext_of(cand.name) or _url_ext(cand.url)
        if ext and ext in self.skip_exts:
            job.done_urls.add(key)
            job.note(f"skipped .{ext}: {cand.name or cand.url}")
            return None
        f = self.http.request(cand.url, referer=cand.referer, accept="*/*", timeout=(10, 120))
        reason = reject_reason(f)
        if reason in ("blocked", "html") and browser is not None:
            f = browser.fetch(cand.url, cand.referer, self.http.max_bytes)
            reason = reject_reason(f)
        if reason in ("blocked", "html"):
            if browser is not None:
                return reason, f
            if all(c.url != cand.url for c in job.blocked_files):
                job.blocked_files.append(cand)   # retried in the browser pass
            return None
        job.done_urls.add(key)
        if reason:
            job.note(f"{cand.source}: {reason}: {cand.url}")
            return None
        names = [_cd_filename(f.headers.get("content-disposition", "")), cand.name]
        if f.url and f.url != cand.url:     # a redirect target (signed, short-lived) may name the file
            names.append(_name_from_url(f.url))
        self._store(job, f.content, source=cand.source, url=cand.url,
                    names=names, complete=cand.complete, content_type=f.content_type)
        return None

    def _store(self, job: Job, content: bytes, *, source: str, url: str,
               names: list[str], complete: bool, content_type: str = "") -> None:
        if complete:
            job.complete = True
        sha = hashlib.sha256(content).hexdigest()
        original = next((n for n in names if n), "") or _name_from_url(url)
        # Same file through another route. Publishers stamp the copy they serve, so the
        # bytes can differ from a mirror's (ACS vs figshare): a real file name decides
        # too (not a bare "download" or a numeric id).
        name_key = original.lower() if _ext_of(original) else ""
        if sha in job.hashes:
            return
        if name_key in job.names:
            job.note(f"{original}: {job.names[name_key]} and {source} copies differ; "
                     f"kept the {job.names[name_key]} one")
            return
        ext = detect_ext(content, names=names, url=url, content_type=content_type)
        if ext.lstrip(".") in self.skip_exts:
            job.note(f"skipped {ext}: {original or url}")
            return
        job.hashes.add(sha)
        if name_key:
            job.names[name_key] = source
        filename = f"{job.stem}_SI{len(job.files) + 1}{ext}"
        (self.files_dir / filename).write_bytes(content)
        job.files.append(SIFile(filename, source, original, url, len(content), sha))
        log.info("  + %s  (%s, %s)", filename, source, _human(len(content)))

    # ---- step 1: APIs and open mirrors ---------------------------------

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _cached_meta(self, doi: str, cache: dict) -> Meta:
        """Metadata from the cache, else Crossref/OpenAlex. Only Crossref answers are
        cached, so a lookup that failed (429, 5xx, timeout) is asked again next run."""
        hit = cache.get(doi.lower())
        if isinstance(hit, dict):
            return Meta(**{k: hit[k] for k in META_FIELDS if k in hit}, from_crossref=True)
        meta = self.fetch_meta(doi)
        if meta.from_crossref:
            cache[doi.lower()] = {k: getattr(meta, k) for k in META_FIELDS}
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        return meta

    def fetch_meta(self, doi: str) -> Meta:
        meta = Meta()
        data = self.http.get_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}",
                                  params={"mailto": self.http.email} if self.http.email else None)
        msg = (data or {}).get("message") or {}
        if msg:
            meta.from_crossref = True
            authors = msg.get("author") or []
            if authors:
                meta.author = authors[0].get("family") or authors[0].get("name") or ""
            for key in ("issued", "published-print", "published-online", "created"):
                parts = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
                if parts and parts[0]:
                    meta.year = str(parts[0])
                    break
            for alt in msg.get("alternative-id") or []:
                meta.pii = meta.pii or _norm_pii(alt)
            for link in msg.get("link") or []:
                m = re.search(r"PII:([^?&]+)", link.get("URL", ""))
                meta.pii = meta.pii or (_norm_pii(m.group(1)) if m else "")
            for rel in (msg.get("relation") or {}).get("is-supplemented-by") or []:
                if rel.get("id-type") == "doi" and rel.get("id"):
                    meta.supplement_dois.append(normalize_doi(rel["id"]))
        if not (meta.author and meta.year):
            work = self.http.get_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}",
                                      params={"mailto": self.http.email} if self.http.email else None)
            if work:
                name = (((work.get("authorships") or [{}])[0].get("author") or {})
                        .get("display_name") or "")
                meta.author = meta.author or (name.split()[-1] if name.split() else "")
                meta.year = meta.year or str(work.get("publication_year") or "")
        return meta

    def src_figshare(self, job: Job, meta: Meta) -> list:
        hits: Any = None
        for body in ({"resource_doi": job.doi, "page_size": 100},
                     {"search_for": f':resource_doi: "{job.doi}"', "page_size": 100}):
            hits = self.http.get_json("https://api.figshare.com/v2/articles/search", json_body=body)
            if hits:
                break
        out, misses = [], 0
        # PLOS deposits each figure and table too (<doi>.g001, <doi>.t001); SI is <doi>.s001
        rendition = re.compile(re.escape(job.doi) + r"\.[gt]\d+", re.IGNORECASE)
        for hit in (hits if isinstance(hits, list) else [])[:50]:
            if hit.get("resource_doi") and normalize_doi(hit["resource_doi"]).lower() != job.doi.lower():
                continue
            if rendition.fullmatch(hit.get("doi") or ""):
                continue
            art = self.http.get_json(f"https://api.figshare.com/v2/articles/{hit.get('id')}")
            # Search is fuzzy: keep only items that name this article as their resource.
            if art and normalize_doi(art.get("resource_doi") or "").lower() == job.doi.lower():
                out += self._figshare_files(art, "figshare", complete=True)
            else:
                misses += 1
                if misses >= 5 and not out:   # the search isn't about this article
                    break
        return out

    @staticmethod
    def _figshare_files(art: dict, source: str, complete: bool) -> list:
        return [Candidate(f["download_url"], source, name=f.get("name") or "", complete=complete)
                for f in art.get("files") or [] if f.get("download_url")]

    def src_pmc(self, job: Job, meta: Meta) -> list:
        """The files the PMC article XML lists as supplementary material, from the PMC
        open-access bucket on AWS. (Europe PMC's supplementaryFiles bundle holds the
        same files plus every figure, and arrives at ~10 KB/s.)"""
        data = self.http.get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f'DOI:"{job.doi}"', "format": "json", "resultType": "lite",
                    "pageSize": 1})
        results = ((data or {}).get("resultList") or {}).get("result") or []
        if not results or (results[0].get("doi") or "").lower() != job.doi.lower():
            return []
        pmcid = results[0].get("pmcid") or ""
        if not pmcid:
            return []
        listing = self.http.request(f"{PMC_BUCKET}/", api=True, accept="application/xml",
                                    params={"list-type": "2", "prefix": f"{pmcid}."})
        keys = [html.unescape(k) for k in re.findall(r"<Key>([^<]+)</Key>", listing.text)] \
            if listing.status == 200 else []
        versions = {k.split("/", 1)[0] for k in keys if re.match(rf"{pmcid}\.\d+/", k)}
        if not versions:   # not in the open-access set: the PMC page may still list SI
            job.pmcid = pmcid
            return []
        latest = max(versions, key=lambda v: int(v.rsplit(".", 1)[1]))
        present = {k.split("/", 1)[1] for k in keys if k.startswith(latest + "/")}
        xml = self.http.request(f"{PMC_BUCKET}/{latest}/{latest}.xml", api=True,
                                accept="application/xml")
        names = [n for n in pmc_supplement_names(xml.text if xml.status == 200 else "")
                 if n in present]
        return [Candidate(f"{PMC_BUCKET}/{latest}/{quote(n)}", "pmc", name=n) for n in names]

    def src_elsevier_api(self, job: Job, meta: Meta) -> list:
        """The article's objects as the Object API lists them (HTTP 300, one "choice"
        each): SI is ``mmcN``; the rest are figures, formula images and the accepted
        manuscript. The files come from ars.els-cdn.com, which needs no key."""
        if not (self.elsevier_key and meta.pii):
            return []
        data = self.http.get_json(f"https://api.elsevier.com/content/object/pii/{meta.pii}",
                                  ok=(200, 300), params={"httpAccept": "application/json"},
                                  headers={"X-ELS-APIKey": self.elsevier_key})
        # An unknown PII gets {"choices": null} too, so only a non-empty list says anything.
        choices = ((data if isinstance(data, dict) else {}).get("choices") or {}).get("choice") or []
        choices = [choices] if isinstance(choices, dict) else choices
        out = []
        for c in choices:
            name = urlparse(c.get("$") or "").path.rsplit("/", 1)[-1]
            if re.fullmatch(r"mmc\d+", c.get("@ref") or "") and name:
                out.append(Candidate(f"https://ars.els-cdn.com/content/image/{quote(name)}",
                                     "elsevier-api", name=name, complete=True))
        if choices and not out:   # the article is listed and has no SI object
            job.complete = True
            job.note("Elsevier API lists no supplementary files")
        return out

    def src_linked_deposits(self, job: Job, meta: Meta) -> list:
        """Zenodo / figshare deposits that declare themselves this article's supplement."""
        deposits = [(d, "") for d in meta.supplement_dois]
        variants = sorted({job.doi, job.doi.lower()})
        query = ("relatedIdentifiers.relatedIdentifier:("
                 + " OR ".join(_lucene_phrase(v) for v in variants) + ")")
        data = self.http.get_json("https://api.datacite.org/dois",
                                  params={"query": query, "page[size]": 100})
        for item in (data or {}).get("data") or []:
            attrs = item.get("attributes") or {}
            if any(ri.get("relationType") == "IsSupplementTo"
                   and normalize_doi(ri.get("relatedIdentifier") or "").lower() == job.doi.lower()
                   for ri in attrs.get("relatedIdentifiers") or []):
                deposits.append((attrs.get("doi") or item.get("id") or "", attrs.get("url") or ""))
        out, seen = [], set()
        for dep_doi, url in deposits:
            if dep_doi.lower() in seen:
                continue
            seen.add(dep_doi.lower())
            out += self._expand_deposit(job, dep_doi, url)
        return out

    def _expand_deposit(self, job: Job, dep_doi: str, url: str) -> list:
        for attempt in range(2):
            m = (re.search(r"zenodo\.(\d+)", dep_doi, re.IGNORECASE)
                 or re.search(r"zenodo\.org/records?/(\d+)", url))
            if m:
                rec = self.http.get_json(f"https://zenodo.org/api/records/{m.group(1)}")
                return [Candidate(f["links"].get("self") or f["links"].get("download"), "zenodo",
                                  name=f.get("key") or f.get("filename") or "", complete=False)
                        for f in (rec or {}).get("files") or []
                        if (f.get("links") or {}).get("self") or (f.get("links") or {}).get("download")]
            m = (re.search(r"figshare\.(\d+)", dep_doi, re.IGNORECASE)
                 or re.search(r"figshare\.com/articles/(?:[^/?#]+/)*?(\d{4,})(?:/\d+)?/?(?:[?#]|$)", url))
            if m:
                art = self.http.get_json(f"https://api.figshare.com/v2/articles/{m.group(1)}")
                return self._figshare_files(art or {}, "figshare-linked", complete=False)
            if attempt == 0 and not url:
                url = self._handle_url(dep_doi)
        job.note(f"linked deposit not fetched: {dep_doi}")
        return []

    # ---- step 2: publisher pages -------------------------------------

    def _handle_url(self, doi: str) -> str:
        data = self.http.get_json(f"https://doi.org/api/handles/{quote(doi, safe='/')}")
        for value in (data or {}).get("values") or []:
            if value.get("type") == "URL":
                return (value.get("data") or {}).get("value") or ""
        return ""

    def landing_url(self, doi: str) -> tuple[str, Optional[Fetched]]:
        """Where the DOI points, asked of the handle API so the publisher isn't hit
        yet; else by following doi.org (that response is kept for reuse)."""
        url = self._handle_url(doi)
        if url:
            return url, None
        f = self.http.request(f"https://doi.org/{quote(doi, safe='/')}")
        return f.url or f"https://doi.org/{doi}", f

    @staticmethod
    def pages_for(doi: str, landing: str, meta: Meta, pmcid: str = "") -> list[str]:
        u = landing or f"https://doi.org/{doi}"
        p = urlparse(u)
        host = (p.hostname or "").lower()
        root = f"{p.scheme}://{p.netloc}"
        pages: list[str] = []
        m = re.search(r"/pii/([^/?#]+)", u, re.IGNORECASE)
        pii = meta.pii or (_norm_pii(m.group(1)) if m else "")
        if "elsevier.com" in host or "sciencedirect.com" in host:
            if pii:
                pages.append(f"https://www.sciencedirect.com/science/article/pii/{pii}")
        elif host.endswith("onlinelibrary.wiley.com"):
            pages += [f"{root}/doi/full/{doi}", u]
        elif p.path.startswith("/doi/"):   # Atypon: ACS, T&F, SAGE, CSP, Science, PNAS...
            pages += [f"{root}/doi/suppl/{doi}", u]
        elif host == "iopscience.iop.org" and p.path.startswith("/article/"):
            pages += [u.rstrip("/") + "/data", u]   # IOP lists SI on a page of its own
        elif re.search(r"(?:bio|med)rxiv\.org$", host):
            base = re.sub(r"\.(?:full|abstract|supplementary-material)$", "", u.rstrip("/"))
            pages.append(base + ".supplementary-material")
        else:
            pages.append(u)
        if pmcid:
            pages.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/")
        return list(dict.fromkeys(pages))

    # ---- step 3: probing ---------------------------------------------

    def probe_candidates(self, job: Job, meta: Meta) -> list:
        doi = job.doi.lower()
        out: list = []
        if meta.pii and doi.startswith("10.1016/"):
            job.probed.append("Elsevier mmc")
            out += self._probe_series(
                lambda n, ext: f"https://ars.els-cdn.com/content/image/1-s2.0-{meta.pii}-mmc{n}.{ext}",
                _ELSEVIER_PROBE_EXTS, "elsevier-probe")
        m = re.fullmatch(r"10\.1371/journal\.([a-z]+)\.\d+", doi)
        if m and m.group(1) in PLOS_JOURNALS:
            job.probed.append("PLOS .s00N")
            journal = PLOS_JOURNALS[m.group(1)]
            out += self._probe_series(
                lambda n, _: (f"https://journals.plos.org/{journal}/article/file"
                              f"?type=supplementary&id={job.doi}.s{n:03d}"),
                ("",), "plos-probe")
        return out

    def _probe_series(self, make, exts, source: str, limit: int = 30) -> list:
        """Walk SI1, SI2, ... until an index has no file under any extension."""
        out = []
        for n in range(1, limit + 1):
            hit = next((make(n, ext) for ext in exts if self.http.probe(make(n, ext))), None)
            if hit is None:
                break
            out.append(Candidate(hit, source))
        return out

    # ---- step 4: real browser ----------------------------------------

    def _get_browser(self) -> Optional[BrowserSession]:
        if self.browser_mode == "off":
            return None
        if self._browser is None:
            if not BrowserSession.installed():
                log.warning("Pages were blocked. For the real-browser fallback: "
                            "pip install playwright && playwright install chromium")
                self.browser_mode = "off"
                return None
            browser = BrowserSession(self.browser_profile, show=self.browser_mode == "show")
            try:
                browser.start()
            except Exception as e:
                log.warning("Browser fallback unavailable: %s", e)
                self.browser_mode = "off"
                return None
            self._browser = browser
        return self._browser

    def _browser_pass(self, job: Job, pages: bool = True) -> None:
        targets = list(dict.fromkeys(job.blocked_pages + job.thin_pages)) if pages else []
        files, job.blocked_files = job.blocked_files, []
        if not targets and not files:
            return
        browser = self._get_browser()
        if browser is None:
            if job.blocked_pages or files:
                job.blocked = True
                job.note("blocked: " + " ".join(job.blocked_pages + [c.url for c in files]))
            return
        walled, refused = self._browse(job, browser, targets, files, "browser")
        if (walled or any(reason == "blocked" for _, reason, _ in refused)) and self._window_allowed():
            walled, refused = self._window_pass(job, walled, refused)
        for page_url in walled:
            job.blocked = True
            job.note(f"blocked even in browser: {page_url}")
        for cand, reason, f in refused:
            job.blocked = True
            job.note(f"{cand.source}: still {reason} in browser: {cand.url}")
            self._save_debug(job, f, "file")

    def _browse(self, job: Job, browser: BrowserSession, pages: list[str],
                files: list[Candidate], label: str) -> tuple[list[str], list]:
        """Open ``pages`` and fetch ``files`` (plus the SI the pages list) in ``browser``.
        Returns the walled pages that stayed walled, and the files refused even there
        as (candidate, reason, response)."""
        files, walled = list(files), []
        for page_url in pages:
            page = browser.open(page_url)
            if page is not None:
                self.http.adopt_browser(browser.cookies(), browser.user_agent)
            found = si_links(page, job.doi) if page is not None else []
            log.debug("  %s: %s -> %s", label, page_url, "not opened" if page is None else
                      f"{page.url} (blocked={page.blocked}, {len(page.anchors)} links, "
                      f"{len(found)} SI)")
            outcome = ("not opened" if page is None else f"{len(found)} SI links" if found
                       else "still walled" if page.blocked else "opened, no SI links")
            job.page_log[page_url] = (job.page_log.get(page_url, "") + f" -> {label}: " + outcome
                                      ).lstrip(" ->")
            if not found and page_url in job.blocked_pages and (page is None or page.blocked):
                walled.append(page_url)
            files += [Candidate(u, "page-browser", referer=page.url) for u in found]
        refused = []
        for cand in files:
            failure = self._download(job, cand, browser=browser)
            if failure is not None:
                refused.append((cand, *failure))
        return walled, refused

    def _window_allowed(self) -> bool:
        if self.browser_mode != "auto" or not self._windows:
            return False
        # a window needs a screen; on Linux (WSLg included) that is X11 or Wayland
        return not sys.platform.startswith("linux") or bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def _window_pass(self, job: Job, walled: list[str], refused: list) -> tuple[list[str], list]:
        """Checks the headless browser could not pass (a picture puzzle, say), once more
        in a visible window for a person. A window in which nothing got through means
        nobody is there: no further windows in this run."""
        retry = [cand for cand, reason, _ in refused if reason == "blocked"]
        kept = [r for r in refused if r[1] != "blocked"]
        if self._browser is not None:   # one Chromium per profile at a time
            self._browser.close()
            self._browser = None
        window = BrowserSession(self.browser_profile, show=True)
        try:
            window.start()
        except Exception as e:
            log.warning("  browser: no window could be opened (%s)", e)
            self._windows = False
            return walled, refused
        log.warning("  %s: a check needs a person; pass it in the browser window", job.doi)
        before = len(job.files)
        try:
            still_walled, still_refused = self._browse(job, window, walled, retry, "window")
        finally:
            window.close()
        if len(job.files) == before and len(still_walled) == len(walled):
            self._windows = False
            log.warning("  Nothing was passed in the window: no more windows in this run "
                        "(blocked DOIs go to retry.txt)")
        return still_walled, kept + still_refused

    # ---- manifest and zip ----------------------------------------------

    def _load_manifest(self) -> dict[str, list[dict]]:
        manifest: dict[str, list[dict]] = {}
        if self.manifest_path.exists():
            with open(self.manifest_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("doi"):
                        manifest.setdefault(row["doi"].lower(), []).append(row)
        return manifest

    @staticmethod
    def _csv_text(groups: Iterable[list[dict]]) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        w.writeheader()
        for rows in groups:
            for row in rows:
                w.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})
        return buf.getvalue()

    def _write_manifest(self, manifest: dict[str, list[dict]]) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(self._csv_text(manifest.values()), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    def _write_retry(self, dois: list[str]) -> None:
        """DOIs worth another run (blocked, unreachable), one per line; gone when none."""
        if dois:
            self.retry_path.write_text("".join(d + "\n" for d in dois), encoding="utf-8")
        else:
            self.retry_path.unlink(missing_ok=True)

    def _write_zip(self, groups: list[list[dict]]) -> None:
        tmp = self.zip_path.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for rows in groups:
                for row in rows:
                    if row.get("status") == "ok" and row.get("file"):
                        zf.write(self.files_dir / row["file"], arcname=row["file"])
            zf.writestr("si_manifest.csv", self._csv_text(groups))
        os.replace(tmp, self.zip_path)


def _human(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 1024:.0f} KB"


@dataclass
class Summary:
    total: int
    with_si: int
    files: int
    size: int
    blocked: list[str]
    errors: list[str]
    none: list[str]
    zip_path: Path

    @classmethod
    def from_rows(cls, groups: list[list[dict]], zip_path: Path) -> "Summary":
        def with_status(status):
            return [rows[0]["doi"] for rows in groups if rows and rows[0].get("status") == status]
        ok = [rows for rows in groups if rows and rows[0].get("status") == "ok"]
        return cls(
            total=len(groups), with_si=len(ok),
            files=sum(len(rows) for rows in ok),
            size=sum(int(r.get("bytes") or 0) for rows in ok for r in rows),
            blocked=with_status("blocked"), errors=with_status("error"),
            none=with_status("none"), zip_path=zip_path,
        )

    def text(self) -> str:
        lines = [f"Done: {self.with_si}/{self.total} DOIs with SI, {self.files} files "
                 f"({_human(self.size)}) -> {self.zip_path}"]
        if self.blocked or self.errors:
            lines.append(f"Retry list: {self.zip_path.parent / 'retry.txt'}")
        if self.blocked:
            lines.append(f"Blocked ({len(self.blocked)}; try --browser show, or --cookies "
                         f"from a logged-in browser): " + ", ".join(self.blocked))
        if self.errors:
            lines.append(f"Network errors ({len(self.errors)}; rerun to retry): "
                         + ", ".join(self.errors))
        if self.none:
            lines.append(f"No SI found ({len(self.none)}): " + ", ".join(self.none))
        return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="si_harvest",
        description="Find and download the Supporting Information of a DOI list, "
                    "and bundle it into one zip.")
    p.add_argument("inputs", nargs="+", help=".txt/.csv/.tsv/.xlsx files, or bare DOIs")
    p.add_argument("-o", "--output", default="si_out", help="output directory (default: si_out)")
    p.add_argument("--zip", default="SI.zip", help="zip file name in the output dir (default: SI.zip)")
    p.add_argument("--email", default=None,
                   help="contact email for the Crossref/OpenAlex polite pools "
                        "(or $SI_EMAIL / $UNPAYWALL_EMAIL)")
    p.add_argument("--browser", choices=("auto", "headless", "show", "off"), default="auto",
                   help="real-browser fallback for blocked pages (needs playwright): auto = "
                        "headless, plus a window only for a check that needs a person; "
                        "headless = never a window; show = always a window; off")
    p.add_argument("--browser-profile", default=None,
                   help="persistent Chromium profile (default: ~/.cache/si_harvest/browser-profile)")
    p.add_argument("--cookies", default=None,
                   help="Netscape cookies.txt exported from your browser (e.g. institutional login)")
    p.add_argument("--user-agent", default=None, help="browser User-Agent for publisher pages")
    p.add_argument("--elsevier-key", default=None,
                   help="Elsevier API key for the Object API (or $ELSEVIER_API_KEY)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="min seconds between requests to one publisher host (default: 1.0)")
    p.add_argument("--pause", type=float, nargs=2, default=(2.0, 4.0), metavar=("MIN", "MAX"),
                   help="random pause between two DOIs, in seconds (default: 2 4)")
    p.add_argument("--no-impersonate", action="store_true",
                   help="use plain requests even when curl_cffi is installed")
    p.add_argument("--retries", type=int, default=1,
                   help="extra passes over DOIs left blocked or unreachable (default: 1)")
    p.add_argument("--skip-ext", default="", metavar="EXTS",
                   help="file types not to download, comma-separated (e.g. mp4,avi,mov)")
    p.add_argument("--max-mb", type=float, default=200, help="skip files larger than this (default: 200)")
    p.add_argument("--no-probe", action="store_true", help="don't guess predictable SI URLs")
    p.add_argument("--overwrite", action="store_true", help="re-harvest DOIs already in the manifest")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    if not args.verbose:  # urllib3 warns on every retry; our notes already say what failed
        logging.getLogger("urllib3").setLevel(logging.ERROR)

    email = args.email or os.environ.get("SI_EMAIL") or os.environ.get("UNPAYWALL_EMAIL") or ""
    if not email:
        log.warning("No --email: Crossref/OpenAlex won't use their polite pools.")
    dois = read_dois(args.inputs)
    if not dois:
        log.error("No DOIs found in the given inputs.")
        return 2

    if curl_requests is None and not args.no_impersonate:
        log.info("curl_cffi not installed: publisher requests use plain requests "
                 "(pip install curl_cffi gets past more bot walls).")
    http_ = Http(email=email, delay=args.delay, user_agent=args.user_agent,
                 cookies_file=args.cookies, max_bytes=int(args.max_mb * 2**20),
                 impersonate=None if args.no_impersonate else "chrome")
    harvester = Harvester(http_, args.output, browser=args.browser,
                          browser_profile=args.browser_profile,
                          elsevier_key=args.elsevier_key or os.environ.get("ELSEVIER_API_KEY"),
                          probe=not args.no_probe, overwrite=args.overwrite, zip_name=args.zip,
                          pause=tuple(args.pause), retries=args.retries,
                          skip_exts=args.skip_ext.split(","))
    try:
        summary = harvester.run(dois)
    finally:
        harvester.close()
    log.info(summary.text())
    return 0 if summary.with_si else 1


if __name__ == "__main__":
    sys.exit(main())
