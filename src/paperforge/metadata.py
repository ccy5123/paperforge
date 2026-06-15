"""Bibliographic metadata lookup, used to name files when the input lacks
author/year (e.g. a plain DOI list or a spreadsheet with no author column).

OpenAlex is primary — it covers both Crossref and DataCite works, including
arXiv preprints (10.48550/*). Crossref is a fallback for the rare gaps.
The author is reduced to a surname-ish token suitable for a filename.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import requests


def _enc(doi: str) -> str:
    return quote(doi, safe="/")


@dataclass
class Metadata:
    author: str = ""   # surname-ish token, e.g. "Vaswani"
    year: str = ""
    title: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.author or self.year or self.title)


def _surname(display_name: str) -> str:
    """Best-effort surname from a full display name ("Ashish Vaswani" -> "Vaswani")."""
    name = (display_name or "").strip()
    return name.split()[-1] if name else ""


def _from_openalex(doi: str, session: requests.Session, email) -> Metadata:
    params = {"select": "authorships,publication_year,title,display_name"}
    if email:
        params["mailto"] = email
    r = session.get(f"https://api.openalex.org/works/doi:{_enc(doi)}",
                    params=params, timeout=(10, 20))
    if r.status_code != 200:
        return Metadata()
    data = r.json() or {}

    author = ""
    auths = data.get("authorships") or []
    if auths:
        author = _surname((auths[0].get("author") or {}).get("display_name") or "")
    year = data.get("publication_year")
    title = data.get("title") or data.get("display_name") or ""
    return Metadata(author=author, year=str(year) if year else "", title=title or "")


def _from_crossref(doi: str, session: requests.Session, email) -> Metadata:
    params = {}
    if email:
        params["mailto"] = email
    r = session.get(f"https://api.crossref.org/works/{_enc(doi)}",
                    params=params, timeout=(10, 20))
    if r.status_code != 200:
        return Metadata()
    msg = (r.json() or {}).get("message") or {}

    author = ""
    authors = msg.get("author") or []
    if authors:
        a = authors[0]
        author = a.get("family") or a.get("name") or ""

    year = ""
    for key in ("issued", "published-print", "published-online", "published", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break

    titles = msg.get("title") or []
    title = titles[0] if titles else ""
    return Metadata(author=author, year=year, title=title)


def fetch_metadata(doi: str, session: requests.Session, config) -> Metadata:
    """Look up author/year/title for a DOI. Never raises — returns empty on failure."""
    email = getattr(config, "unpaywall_email", None)
    for source in (_from_openalex, _from_crossref):
        try:
            md = source(doi, session, email)
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            md = Metadata()
        if not md.is_empty:
            return md
    return Metadata()
