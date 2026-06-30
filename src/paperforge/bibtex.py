"""DOI -> BibTeX via doi.org content negotiation.

We *request* the registrar's canonical BibTeX (Crossref/DataCite/…) rather than
constructing one, so we never fabricate bibliographic data. Sourcing is owned by
:mod:`paperforge.sourcing` (a habanero wrapper -- the single network boundary);
everything here (parsing, key rewriting, accumulation) is pure and unit-testable
by injecting the raw BibTeX text.

The registrar body is XML-rooted, so it can arrive with HTML entities (``&amp;``)
or bare LaTeX specials (``&``) that break ``pdflatex``/``bibtex``. Before an entry
is stored it is run through :func:`paperforge.latex_safety.sanitize` and asserted
seam-closed -- the transcoding step the raw body lacks. That transform touches
only ``&`` and HTML entities, which never occur in a cite key or ``@type``, so it
is provably field-value-only without reserializing the entry.

Cite keys are rewritten to paperforge's citation style — ``<Surname><Year>`` —
the same stem :func:`paperforge.utils.generate_filename` derives for PDF names,
with ``a``/``b``/… suffixes on collision so every key is unique.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .latex_safety import assert_seam_closed, sanitize
from .sourcing import fetch_canonical_bibtex
from .utils import clean_year, collision_suffixes, generate_filename, normalize_doi

# A single content-negotiation response is one entry: @type{key, field = ...}
_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,]*?)\s*,(.*)\}\s*\Z", re.DOTALL)
_KEY_RE = re.compile(r"^(@\w+\s*\{)\s*[^,]*?\s*,", re.DOTALL)


@dataclass
class BibEntry:
    key: str       # final, rekeyed citation key
    text: str      # full entry: key rewritten, body seam-closed (sanitized)
    doi: str


# ---------------------------------------------------------------------------
# Sourcing (thin delegate; inject raw text in tests instead of calling this)
# ---------------------------------------------------------------------------

def fetch_bibtex(doi: str, session=None, config=None) -> Optional[str]:
    """Return the registrar's canonical BibTeX for *doi*, or ``None`` on a miss.

    A thin delegate to :func:`paperforge.sourcing.fetch_canonical_bibtex`, which
    owns the doi.org content negotiation (HTTP, etiquette, retries, multi-registrar
    normalization) via habanero. ``session`` is accepted for call-site
    compatibility but unused -- habanero owns the transport now. Never raises;
    never fabricates.
    """
    mailto = getattr(config, "unpaywall_email", "") or ""
    sourced = fetch_canonical_bibtex(doi, mailto=mailto)
    return sourced.raw_bibtex if sourced is not None else None


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

        # Close the XML/HTML-entity -> LaTeX transcoding seam *before* storing.
        # Applied to the whole entry string: sanitize only touches ``&`` and HTML
        # entities, which never appear in the cite key or ``@type``, so this is
        # field-value-only (I2) without reserializing the registrar's formatting.
        safe_text = sanitize(rekey(raw, final_key).strip())
        assert_seam_closed(safe_text)
        entry = BibEntry(key=final_key, text=safe_text, doi=doi)

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
