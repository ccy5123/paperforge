"""Shared helpers: DOI normalization/extraction and safe filename generation."""
from __future__ import annotations

import re

# A permissive DOI matcher (Crossref-style). Case-insensitive.
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

# Prefixes that wrap a bare DOI when it is pasted as a URL or "doi:" string.
_DOI_PREFIXES = (
    "https://doi.org/", "http://doi.org/",
    "https://dx.doi.org/", "http://dx.doi.org/",
    "doi:", "doi ",
)

# Trailing punctuation that commonly clings to a DOI scraped from prose/cells.
_DOI_TRAILING = ".,;)]}>\"' \t\r\n"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
# Plausible publication years (1500-2199); guards against grabbing random digits.
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|2[01]\d{2})\b")


def normalize_doi(doi: str) -> str:
    """Strip URL/``doi:`` prefixes and surrounding junk; return the bare DOI."""
    if not doi:
        return ""
    doi = str(doi).strip()
    low = doi.lower()
    for prefix in _DOI_PREFIXES:
        if low.startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.strip().strip(_DOI_TRAILING)


def extract_dois(text: str) -> list[str]:
    """Find every DOI-looking substring in free text, normalized and de-duped."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in DOI_RE.finditer(str(text)):
        d = normalize_doi(m.group(0))
        key = d.lower()
        if d and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def first_author_token(author: str) -> str:
    """Best-effort first-author label from a messy author string."""
    if not author:
        return ""
    return re.split(r"[;,/&]| and ", str(author), maxsplit=1)[0].strip()


def clean_year(year) -> str:
    """Pull a 4-digit year out of whatever was in the cell (e.g. '2021-03-01')."""
    if not year:
        return ""
    m = _YEAR_RE.search(str(year))
    return m.group(1) if m else ""


def _slug(value: str, maxlen: int = 40) -> str:
    return _NON_ALNUM.sub("", value or "")[:maxlen]


def generate_filename(index: int, author: str, year, ext: str = ".pdf") -> str:
    """Stable, filesystem-safe name like ``0007_Smith_2021.pdf``.

    ``index`` guarantees uniqueness; author/year are for human readability only.
    """
    a = _slug(first_author_token(author)) or "Unknown"
    y = _slug(clean_year(year)) or _slug(str(year)) or "Unknown"
    return f"{int(index):04d}_{a}_{y}{ext}"
