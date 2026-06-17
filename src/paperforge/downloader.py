"""Open Access paper downloader.

Walks a fallback chain of OA sources for a given DOI and saves the first
downloadable PDF along with a metadata sidecar (source, license, version,
all attempts).

Sources, in order:
  1. arXiv       — direct URL for 10.48550/arXiv.* DOIs (no API call)
  2. Unpaywall   — broadest OA discovery API (email required)
  3. OpenAlex    — complementary aggregator
  4. Europe PMC  — biomedical / environmental health
  5. Semantic Scholar — last-resort complement

Interface preserved from the previous OADownloader:
  OADownloader(config).download(doi, index, author, year, title) -> bool

A richer ``fetch(...) -> DownloadOutcome`` is also exposed for the batch layer.

Required config attributes:
  - unpaywall_email : str         (required for Unpaywall; also identifies you to OpenAlex)
  - output_dir      : str | Path  (PDFs go to <output_dir>/pdfs/)

Optional config attributes:
  - user_agent              : str            (default: paperforge/<v> + email)
  - allowed_licenses        : set[str]|None  (license substring filter, e.g. {"cc-by","cc0"})
  - require_known_license   : bool           (with a filter, drop unknown-license PDFs)
  - source_order            : list[str]|None (override default chain by name)
  - semantic_scholar_api_key: str|None       (lifts Semantic Scholar rate limits)
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .metadata import Metadata, fetch_metadata
from .utils import collision_suffixes, generate_filename, normalize_doi


def _enc(doi: str) -> str:
    """URL-encode a DOI for use in a path segment, preserving its slashes."""
    return quote(doi, safe="/")


# ============================================================
# Resolution: a single source's verdict on a DOI
# ============================================================

@dataclass
class Resolution:
    doi: str
    source: str
    pdf_url: Optional[str] = None
    landing_url: Optional[str] = None
    license: Optional[str] = None
    version: Optional[str] = None     # publishedVersion / acceptedVersion / submittedVersion
    host_type: Optional[str] = None   # publisher / repository
    is_oa: bool = False

    @property
    def downloadable(self) -> bool:
        return self.is_oa and bool(self.pdf_url)


@dataclass
class DownloadOutcome:
    """What the batch layer needs to know about one DOI's fate."""
    doi: str
    ok: bool
    source: Optional[str] = None
    license: Optional[str] = None
    filename: Optional[str] = None
    pdf_path: Optional[str] = None
    error: Optional[str] = None
    attempts: list[str] = field(default_factory=list)


# ============================================================
# Source resolvers — each returns Optional[Resolution].
#   None                    → "I have no info, try the next source"
#   Resolution(is_oa=False) → "I confirm this is closed, no need to retry me"
#   Resolution(downloadable)→ "Here is a PDF URL, go fetch"
# ============================================================

# Allow '/' so legacy arXiv ids (e.g. math.GT/0309136) survive; stop at query/fragment.
_ARXIV_RE = re.compile(r"arxiv\.([^\s?#]+)", re.IGNORECASE)


def resolve_arxiv(doi: str, session: requests.Session, config) -> Optional[Resolution]:
    m = _ARXIV_RE.search(doi)
    if not m:
        return None
    arxiv_id = m.group(1).strip().strip(".")
    if not arxiv_id:
        return None
    return Resolution(
        doi=doi,
        source="arxiv",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        landing_url=f"https://arxiv.org/abs/{arxiv_id}",
        # arXiv's default grant is a non-exclusive license, NOT necessarily CC.
        # Leave it unknown so the license policy (not a hardcoded guess) decides.
        license=None,
        version="submittedVersion",
        host_type="repository",
        is_oa=True,
    )


def resolve_unpaywall(doi: str, session: requests.Session, config) -> Optional[Resolution]:
    email = getattr(config, "unpaywall_email", None)
    if not email:
        return None
    try:
        r = session.get(
            f"https://api.unpaywall.org/v2/{_enc(doi)}",
            params={"email": email},
            timeout=(10, 20),
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    if not data.get("is_oa"):
        return Resolution(doi=doi, source="unpaywall", is_oa=False)

    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf")
    if not pdf_url:
        # walk other locations for any direct PDF
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                best = loc
                pdf_url = loc["url_for_pdf"]
                break

    return Resolution(
        doi=doi,
        source="unpaywall",
        pdf_url=pdf_url,
        landing_url=best.get("url"),
        license=best.get("license"),
        version=best.get("version"),
        host_type=best.get("host_type"),
        is_oa=True,
    )


def resolve_openalex(doi: str, session: requests.Session, config) -> Optional[Resolution]:
    params = {}
    email = getattr(config, "unpaywall_email", None)
    if email:
        params["mailto"] = email  # polite pool
    try:
        r = session.get(
            f"https://api.openalex.org/works/doi:{_enc(doi)}",
            params=params, timeout=(10, 20),
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    oa = data.get("open_access") or {}
    if not oa.get("is_oa"):
        return Resolution(doi=doi, source="openalex", is_oa=False)

    primary = data.get("primary_location") or {}
    pdf_url = primary.get("pdf_url")
    landing = primary.get("landing_page_url")
    license_ = primary.get("license")
    version = primary.get("version")

    if not pdf_url:
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("pdf_url")
        landing = landing or best.get("landing_page_url")
        license_ = license_ or best.get("license")
        version = version or best.get("version")

    if not pdf_url:
        pdf_url = oa.get("oa_url")  # last fallback (may be landing, not PDF)

    src = primary.get("source") or {}
    host_type = "repository" if src.get("type") == "repository" else (
        "publisher" if src.get("type") else None
    )

    return Resolution(
        doi=doi, source="openalex",
        pdf_url=pdf_url, landing_url=landing,
        license=license_, version=version,
        host_type=host_type, is_oa=True,
    )


def resolve_europepmc(doi: str, session: requests.Session, config) -> Optional[Resolution]:
    try:
        r = session.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={
                "query": f'DOI:"{doi}"',
                "format": "json",
                "resultType": "core",
                "pageSize": 1,
            },
            timeout=(10, 20),
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    results = (data.get("resultList") or {}).get("result") or []
    if not results:
        return None
    result = results[0]

    is_oa = result.get("isOpenAccess") == "Y"
    pmcid = result.get("pmcid")
    if not (is_oa and pmcid):
        return Resolution(doi=doi, source="europepmc", is_oa=is_oa)

    return Resolution(
        doi=doi, source="europepmc",
        pdf_url=f"https://europepmc.org/articles/{pmcid}?pdf=render",
        landing_url=f"https://europepmc.org/article/PMC/{pmcid}",
        license=result.get("license"),
        version="publishedVersion",
        host_type="repository",
        is_oa=True,
    )


def resolve_semantic_scholar(doi: str, session: requests.Session, config) -> Optional[Resolution]:
    headers = {}
    api_key = getattr(config, "semantic_scholar_api_key", None)
    if api_key:
        headers["x-api-key"] = api_key
    try:
        r = session.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{_enc(doi)}",
            params={"fields": "openAccessPdf,isOpenAccess"},
            headers=headers,
            timeout=(10, 20),
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    oa_pdf = data.get("openAccessPdf") or {}
    pdf_url = oa_pdf.get("url")
    if not pdf_url:
        return Resolution(doi=doi, source="semantic_scholar",
                          is_oa=bool(data.get("isOpenAccess")))

    return Resolution(
        doi=doi, source="semantic_scholar",
        pdf_url=pdf_url, license=oa_pdf.get("license"),
        is_oa=True,
    )


# Default chain. Order matters: cheap+specific first, broadest next, complement last.
_DEFAULT_CHAIN: list[tuple[str, Callable]] = [
    ("arxiv", resolve_arxiv),
    ("unpaywall", resolve_unpaywall),
    ("openalex", resolve_openalex),
    ("europepmc", resolve_europepmc),
    ("semantic_scholar", resolve_semantic_scholar),
]


# ============================================================
# OADownloader: orchestrator
# ============================================================

class OADownloader:
    """Open Access paper downloader with multi-source fallback."""

    def __init__(self, config):
        self.config = config
        self.logger = None
        self.session = self._build_session(config)
        self._chain = self._resolve_chain(config)
        self._md_cache: dict[str, Metadata] = {}

    # ---- construction ------------------------------------------------

    @staticmethod
    def _build_session(config) -> requests.Session:
        s = requests.Session()
        email = getattr(config, "unpaywall_email", None) or "anonymous@example.org"
        ua = getattr(config, "user_agent", None) or f"paperforge/0.1 (mailto:{email})"
        s.headers.update({"User-Agent": ua, "Accept": "application/json,*/*"})
        retry = Retry(
            total=3, connect=3, read=2,
            backoff_factor=1.0,                                     # 1s, 2s, 4s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"GET"},
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    @staticmethod
    def _resolve_chain(config) -> list[tuple[str, Callable]]:
        order = getattr(config, "source_order", None)
        if not order:
            return list(_DEFAULT_CHAIN)
        by_name = dict(_DEFAULT_CHAIN)
        return [(n, by_name[n]) for n in order if n in by_name]

    # ---- lifecycle ---------------------------------------------------

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- public API --------------------------------------------------

    def fetch(self, doi: str, index: int, author: str, year: str,
              title: str = "") -> DownloadOutcome:
        """Try each OA source until one yields a downloadable PDF.

        Writes <output_dir>/pdfs/<filename>.pdf and a .json sidecar with
        license / source / version / all attempts. Returns a DownloadOutcome.
        """
        if not doi:
            return DownloadOutcome(doi=doi or "", ok=False, error="empty doi")

        doi = normalize_doi(doi)
        self.log(f"[{index:04d}] DOI: {doi}")

        attempts: list[Resolution] = []
        for name, resolver in self._chain:
            self.log(f"  trying {name}...", "DEBUG")
            try:
                res = resolver(doi, self.session, self.config)
            except Exception as e:
                self.log(f"  [{name}] resolver crashed: {e}", "WARNING")
                continue
            if res is None:
                continue
            attempts.append(res)

            if not res.downloadable:
                if res.is_oa is False:
                    self.log(f"  [{name}] confirmed closed", "DEBUG")
                continue

            if not self._license_ok(res):
                self.log(f"  [{name}] license filtered: {res.license}", "INFO")
                continue

            self.log(f"  [{name}] {res.pdf_url}")
            ok, content = self._fetch_pdf(res.pdf_url)
            if not ok:
                continue

            # Fill in author/year/title from OpenAlex/Crossref when the caller
            # didn't supply them, so files aren't all named Unknown_Unknown.
            author, year, title = self._enrich(doi, author, year, title)
            pdf_path, filename = self._save(content, res, author, year, title, attempts)
            kb = len(content) / 1024
            self.log(f"✓ [{res.source}] {filename}  ({kb:.1f} KB, license={res.license or '?'})")
            return DownloadOutcome(
                doi=doi, ok=True, source=res.source, license=res.license,
                filename=filename, pdf_path=str(pdf_path),
                attempts=[a.source for a in attempts],
            )

        tried = ",".join(a.source for a in attempts) or "none"
        self.log(f"✗ no OA PDF for {doi}  [tried: {tried}]", "WARNING")
        return DownloadOutcome(doi=doi, ok=False, error="no OA PDF",
                               attempts=[a.source for a in attempts])

    def download(self, doi: str, index: int, author: str, year: str,
                 title: str = "") -> bool:
        """Backwards-compatible boolean wrapper around :meth:`fetch`."""
        return self.fetch(doi, index, author, year, title).ok

    # ---- internals ---------------------------------------------------

    def _enrich(self, doi: str, author: str, year: str, title: str):
        """Return (author, year, title), filling gaps from a metadata lookup."""
        if not getattr(self.config, "enrich_metadata", True):
            return author, year, title
        if author and year:
            return author, year, title
        if doi not in self._md_cache:
            self._md_cache[doi] = fetch_metadata(doi, self.session, self.config)
        md = self._md_cache[doi]
        return author or md.author, year or md.year, title or md.title

    def _license_ok(self, res: Resolution) -> bool:
        allowed = getattr(self.config, "allowed_licenses", None)
        if not allowed:
            return True
        lic = (res.license or "").lower()
        if not lic:
            # Unknown license: keep it unless the caller explicitly demands a known one.
            return not getattr(self.config, "require_known_license", False)
        return any(a.lower() in lic for a in allowed)

    def _fetch_pdf(self, url: str) -> tuple[bool, bytes]:
        """Download bytes from url. Verifies %PDF- magic. No stream-vs-.content trap.

        Uses tuple (connect=10s, read=30s) timeouts so a slow trickle gets killed.
        """
        try:
            r = self.session.get(
                url, timeout=(10, 30), allow_redirects=True,
                headers={"Accept": "application/pdf,*/*"},
            )
        except requests.Timeout:
            self.log("    timeout", "WARNING")
            return False, b""
        except requests.RequestException as e:
            self.log(f"    request error: {e}", "WARNING")
            return False, b""

        try:
            if r.status_code != 200:
                self.log(f"    HTTP {r.status_code}", "WARNING")
                return False, b""
            content = r.content
            if not content.startswith(b"%PDF-"):
                ctype = r.headers.get("content-type", "?")
                self.log(
                    f"    not a PDF (content-type={ctype}, magic={content[:8]!r})",
                    "WARNING",
                )
                return False, b""
            return True, content
        finally:
            r.close()

    @staticmethod
    def _unique_path(out_dir: Path, base: str, doi: str) -> Path:
        """Resolve <out_dir>/<base>.pdf, avoiding clobbering a *different* paper.

        Re-saving the same DOI reuses its existing name; a collision with a
        different DOI (same author+year) gets an ``a``/``b``/… suffix — the same
        scheme BibTeX keys use, so the PDF name and its cite key stay in step.
        """
        base = base or "Unknown"
        for suffix in itertools.chain([""], collision_suffixes()):
            pdf_path = out_dir / f"{base}{suffix}.pdf"
            if not pdf_path.exists():
                return pdf_path
            sidecar = pdf_path.with_suffix(".json")
            if sidecar.exists():
                try:
                    prev = json.loads(sidecar.read_text(encoding="utf-8"))
                    if (prev.get("doi") or "").lower() == (doi or "").lower():
                        return pdf_path        # same paper → overwrite in place
                except (ValueError, OSError):
                    pass
        raise AssertionError("unreachable: collision_suffixes is infinite")

    def _save(self, content: bytes, res: Resolution,
              author: str, year: str, title: str,
              attempts: list[Resolution]) -> tuple[Path, str]:
        out_dir = Path(self.config.output_dir) / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)

        base = generate_filename(author, year, ext="")
        pdf_path = self._unique_path(out_dir, base, res.doi)
        filename = pdf_path.name
        pdf_path.write_bytes(content)

        sidecar_path = pdf_path.with_suffix(".json")
        sidecar_path.write_text(json.dumps({
            "doi": res.doi,
            "title": title or None,
            "author": author or None,
            "year": year or None,
            "resolution": asdict(res),
            "attempts": [asdict(a) for a in attempts],
        }, indent=2, ensure_ascii=False))

        return pdf_path, filename

    def log(self, message: str, level: str = "INFO") -> None:
        if self.logger:
            getattr(self.logger, level.lower())(message)
        else:
            print(f"[{level}] {message}")
