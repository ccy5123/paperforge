"""LaTeX-safety integrity layer for the XML/HTML-entity -> LaTeX transcoding seam.

The problem this module closes is *structural*, not character-specific: registrar
BibTeX (Crossref's metadata is XML-rooted) reaches us with reserved characters
still encoded as HTML entities (``&amp;``, ``&lt;``, ``&#39;``) and with bare
LaTeX specials (``&``) that break ``pdflatex``/``bibtex``. The seam where
XML-convention text becomes LaTeX-convention text needs exactly one transcoding
step: **decode the source convention, then encode the target convention, once.**

Design (kept deliberately small so nobody "improves" it into casework):

* **Decode is universal and cheap** -- :func:`decode_entities` is one
  ``html.unescape`` call. It covers *every* HTML entity class -- named, decimal,
  hex -- with no per-entity branches.
* **Encode is a bounded, justified set** -- the body is already mostly valid
  LaTeX, so :func:`escape_specials` only escapes specials that *leak bare*. The
  set is one named constant, :data:`DEFAULT_SPECIALS` (``("&",)``); load-bearing
  characters (``\\``, ``{``, ``}``, ``$``, ``^``, ``_``, ``~``) are deliberately
  *excluded* -- escaping them is the double-escape failure mode.
* **The generalization is the residue net, not a longer list** --
  :func:`find_residue` / :func:`assert_seam_closed` are the postcondition that
  makes the fix *complete* rather than enumerative: anything the escape set
  missed is surfaced loudly instead of leaking silently.

Pure, string-in/string-out, no I/O. Only ``html`` and ``re`` (stdlib) are used.
"""
from __future__ import annotations

import html
import re

# The only special that leaks bare from registrar BibTeX in practice. A bare
# ``&`` is a tabular alignment-tab character -> "Misplaced alignment tab" at
# compile time. Excluded on purpose (each with a reason):
#   \\       -- the escape char itself; touching it *is* the double-escape bug.
#   { }      -- grouping is load-bearing ("{ACS}", "{DNA}").
#   $ ^ _    -- math syntax; ``_`` also appears legitimately in url/doi values.
#   ~        -- may be an intentional non-breaking space from the registrar.
#   % #      -- rare in titles; extensible via the ``specials`` parameter, but
#               kept out of the default so the default is maximally safe. The
#               I6 residue net (below) is the real safety mechanism, not a
#               longer escape list.
DEFAULT_SPECIALS = ("&",)

# Residue patterns (I6). (a) any HTML-entity shape that should already be
# decoded, and (b) any ``&`` not already escaped as ``\&``.
_ENTITY_RE = re.compile(r"&[A-Za-z][A-Za-z0-9]*;|&#\d+;|&#x[0-9A-Fa-f]+;")
_BARE_AMP_RE = re.compile(r"(?<!\\)&")


class SeamLeak(Exception):
    """Raised when :func:`assert_seam_closed` finds un-transcoded residue."""


def decode_entities(text: str) -> str:
    """Decode every HTML entity class (named, decimal, hex) in one pass.

    ``html.unescape`` is total over the entity grammar, so there is no
    per-entity casework (I3). Text with no entities is returned unchanged.
    """
    return html.unescape(text)


def escape_specials(text: str, specials=DEFAULT_SPECIALS) -> str:
    """Escape each special in *specials* that is not *already* preceded by ``\\``.

    Conservative and idempotent (I1): an already-correct ``\\&`` is left as
    ``\\&`` (never ``\\\\&``), because the lookbehind skips a special that a
    backslash already escapes. Only characters in *specials* are touched (I4);
    every other substring is identical (I7).
    """
    for ch in specials:
        pattern = re.compile(r"(?<!\\)" + re.escape(ch))
        text = pattern.sub("\\\\" + ch, text)
    return text


def sanitize(text: str, specials=DEFAULT_SPECIALS) -> str:
    """Decode the source convention, then encode the target convention -- once.

    ``sanitize == escape_specials ∘ decode_entities``. Idempotent (I1),
    field-value-only (I2), minimal (I7), and (by construction + :func:`find_residue`)
    seam-closing (I6).
    """
    return escape_specials(decode_entities(text), specials)


def find_residue(text: str) -> list:
    """Return un-transcoded residue; empty list means the seam is closed (I6).

    Residue is (a) any leftover HTML-entity pattern, or (b) any bare ``&`` not
    escaped as ``\\&``. After :func:`sanitize` both are absent, so this returns
    ``[]`` -- the postcondition that makes the fix complete rather than
    enumerative.
    """
    return _ENTITY_RE.findall(text) + _BARE_AMP_RE.findall(text)


def assert_seam_closed(text: str) -> None:
    """Raise :class:`SeamLeak` if :func:`find_residue` is non-empty."""
    residue = find_residue(text)
    if residue:
        raise SeamLeak(f"un-transcoded residue at the LaTeX seam: {residue!r}")
