"""DOI -> canonical BibTeX via habanero's content negotiation.

This is the single network boundary for *sourcing* bibliographic data. We
delegate to ``habanero.cn.content_negotiation``, which routes through
**doi.org** and therefore returns the **registrar-of-record's own** BibTeX --
the same content-negotiation mechanism paperforge used by hand, now owned by a
maintained library. Coverage spans Crossref + DataCite + mEDRA DOIs.

We *request* canonical BibTeX; we never *construct* it. Crossref REST / OpenAlex
return JSON, and building BibTeX from JSON would be fabrication -- those sources
stay in :mod:`paperforge.metadata`, for keying author/year only.

:class:`SourcedEntry` keeps ``raw_bibtex`` verbatim as an audit anchor, so the
never-fabricate claim stays checkable: the sanitized output is always diffable
against the registrar original (I8).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from habanero import cn

from .utils import normalize_doi


@dataclass(frozen=True)
class SourcedEntry:
    doi: str
    raw_bibtex: str            # exactly as habanero returned it (audit anchor, I8)
    agency: Optional[str]      # registrar of record, if exposed (None here -- cn
                               # returns only the body, no agency side-channel)
    retrieved_at: datetime


def fetch_canonical_bibtex(doi: str, *, mailto: str) -> Optional[SourcedEntry]:
    """Fetch one DOI's registrar BibTeX. ``None`` on the unresolved path.

    Wraps ``cn.content_negotiation(ids=doi, format="bibtex")``. ``mailto`` is
    forwarded to habanero as the polite-pool query parameter (same etiquette
    paperforge's hand-rolled HTTP implied). Never raises and never fabricates:
    a missing DOI, an HTTP error, or a non-BibTeX body all return ``None`` so
    the caller emits the existing ``% unresolved`` line unchanged.
    """
    doi = normalize_doi(doi)
    if not doi:
        return None
    try:
        raw = cn.content_negotiation(
            ids=doi, format="bibtex", params={"mailto": mailto} if mailto else None,
        )
    except Exception:
        # habanero raises (e.g. httpx2.HTTPStatusError) on a 4XX/5XX / not-found.
        return None
    if not raw or not raw.lstrip().startswith("@"):
        return None
    return SourcedEntry(
        doi=doi,
        raw_bibtex=raw,
        agency=None,
        retrieved_at=datetime.now(timezone.utc),
    )
