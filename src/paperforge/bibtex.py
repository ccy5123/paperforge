"""DOI -> BibTeX via doi.org content negotiation.

We *request* the registrar's canonical BibTeX (Crossref/DataCite/…) rather than
constructing one, so we never fabricate bibliographic data. The network call is
isolated in :func:`fetch_bibtex`; everything else (parsing, key rewriting,
accumulation) is pure and unit-testable by injecting the raw BibTeX text.

Cite keys are rewritten to paperforge's citation style — ``<Surname><Year>`` —
the same stem :func:`paperforge.utils.generate_filename` derives for PDF names,
with ``a``/``b``/… suffixes on collision so every key is unique.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from .utils import clean_year, collision_suffixes, generate_filename, normalize_doi

# A single content-negotiation response is one entry: @type{key, field = ...}
_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,]*?)\s*,(.*)\}\s*\Z", re.DOTALL)
_KEY_RE = re.compile(r"^(@\w+\s*\{)\s*[^,]*?\s*,", re.DOTALL)


@dataclass
class BibEntry:
    key: str       # final, rekeyed citation key
    text: str      # full entry with the key rewritten; body left as-returned
    doi: str


# ---------------------------------------------------------------------------
# Network (thin; inject raw text in tests instead of calling this)
# ---------------------------------------------------------------------------

def _decode(content: bytes) -> str:
    """Decode BibTeX bytes, tolerating UTF-8 or latin-1 (never raises)."""
    for enc in ("utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _user_agent(config, session) -> str:
    ua = session.headers.get("User-Agent") if session is not None else None
    if ua and "paperforge" in ua.lower():
        return ua
    email = getattr(config, "unpaywall_email", "") or "anonymous@example.org"
    return f"paperforge/0.1 (mailto:{email})"


def fetch_bibtex(doi: str, session: requests.Session, config,
                 *, timeout=(10, 20), max_attempts: int = 3) -> Optional[str]:
    """GET https://doi.org/<doi> with ``Accept: application/x-bibtex``.

    Returns the raw BibTeX string, or ``None`` on any failure (network error,
    non-200, or a body that isn't a BibTeX entry). Never raises; never
    fabricates. ``max_attempts`` = 1 try + up to 2 retries on transient errors.
    """
    doi = normalize_doi(doi)
    if not doi:
        return None
    url = "https://doi.org/" + requests.utils.quote(doi, safe="/")
    headers = {
        "Accept": "application/x-bibtex",
        "User-Agent": _user_agent(config, session),
    }
    for attempt in range(max_attempts):
        try:
            r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            continue  # transient connection issue -> retry
        if r.status_code == 200:
            text = _decode(r.content).strip()
            return text if text.startswith("@") else None
        if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
            continue  # transient server state -> retry
        return None   # 404 and other definitive misses
    return None


# ---------------------------------------------------------------------------
# Pure parsing / key rewriting
# ---------------------------------------------------------------------------

def parse_bibtex(raw: Optional[str]):
    """Split one entry into (type, original_key, body). ``None`` if not an entry."""
    if not raw:
        return None
    m = _ENTRY_RE.match(raw.strip())
    if not m:
        return None
    return m.group(1), m.group(2).strip(), m.group(3)


def _extract_field(body: str, name: str) -> str:
    """Value of a BibTeX field, handling ``{...}``, ``"..."`` and barewords."""
    m = re.search(rf"(?i)\b{re.escape(name)}\s*=\s*", body)
    if not m:
        return ""
    i = m.end()
    if i >= len(body):
        return ""
    ch = body[i]
    if ch == "{":
        depth = 0
        chars: list[str] = []
        for c in body[i:]:
            if c == "{":
                depth += 1
                if depth == 1:
                    continue
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            chars.append(c)
        return "".join(chars).strip()
    if ch == '"':
        j = body.find('"', i + 1)
        return body[i + 1:j].strip() if j != -1 else ""
    m2 = re.match(r"[^,\n}]+", body[i:])
    return m2.group(0).strip() if m2 else ""


def _year_from_body(body: str) -> str:
    for fieldname in ("year", "date", "issued"):
        y = clean_year(_extract_field(body, fieldname))
        if y:
            return y
    return ""


def cite_key_stem(author: str, year: str) -> str:
    """``<Surname><Year>`` stem — the same one used for PDF filenames."""
    return generate_filename(author, year, ext="")


def rekey(raw: str, new_key: str) -> str:
    """Replace only the entry's cite key, leaving type and body untouched."""
    return _KEY_RE.sub(lambda m: f"{m.group(1)}{new_key},", raw.strip(), count=1)


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------

@dataclass
class BibCollection:
    """Collects rekeyed, de-duplicated BibTeX entries for one run."""

    _by_doi: dict = field(default_factory=dict)
    _used_keys: set = field(default_factory=set)
    _misses: list = field(default_factory=list)
    _miss_set: set = field(default_factory=set)

    def add(self, doi: str, raw: Optional[str],
            author: str = "", year: str = "") -> Optional[BibEntry]:
        """Add one DOI's BibTeX. Returns the entry, or None on a miss.

        A failed/garbage/empty ``raw`` is recorded as a miss and omitted — never
        synthesized. Re-adding the same DOI returns the existing entry.
        """
        key_doi = (doi or "").strip().lower()
        if key_doi and key_doi in self._by_doi:
            return self._by_doi[key_doi]

        parsed = parse_bibtex(raw)
        if parsed is None:
            self._note_miss(doi)
            return None

        _type, _orig_key, body = parsed
        stem = cite_key_stem(author or _extract_field(body, "author"),
                             year or _year_from_body(body))
        final_key = self._unique_key(stem)
        entry = BibEntry(key=final_key, text=rekey(raw, final_key).strip(), doi=doi)

        self._by_doi[key_doi or final_key] = entry
        self._used_keys.add(final_key)
        return entry

    def _unique_key(self, stem: str) -> str:
        stem = stem or "Unknown"
        if stem not in self._used_keys:
            return stem
        for suffix in collision_suffixes():
            candidate = stem + suffix
            if candidate not in self._used_keys:
                return candidate

    def _note_miss(self, doi: str) -> None:
        if doi and doi not in self._miss_set:
            self._miss_set.add(doi)
            self._misses.append(doi)

    @property
    def misses(self) -> list:
        return list(self._misses)

    @property
    def count(self) -> int:
        return len(self._by_doi)

    def render(self) -> str:
        """Deterministic ``references.bib`` text: entries sorted by key, then
        comment lines naming any unresolved DOIs."""
        entries = sorted(self._by_doi.values(), key=lambda e: e.key)
        out = "\n\n".join(e.text for e in entries)
        if out:
            out += "\n"
        if self._misses:
            if out:
                out += "\n"
            out += "".join(f"% unresolved (no BibTeX): {d}\n" for d in self._misses)
        return out
