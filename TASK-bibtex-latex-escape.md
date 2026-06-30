# Task: Make `bibtex.py` output LaTeX-safe (fix `&amp;` / unescaped `&`)

You are working in the **paperforge** repository (`~/paperforge`, GitHub `ccy5123/paperforge`).
paperforge is a research tool: given a list of DOIs it (a) downloads Open-Access PDFs and
(b) produces a merged `references.bib`. This task fixes a single, real defect in (b).

Do this with **TDD**: write the failing reproduction test first, confirm it fails, then make
the minimal change to pass it, keeping all existing tests green.

---

## Background (what already exists — do NOT rebuild it)

`src/paperforge/bibtex.py` already implements DOI → BibTeX:

- **Source**: `fetch_bibtex()` requests the registrar's canonical BibTeX via doi.org
  content-negotiation (`Accept: application/x-bibtex`). The design philosophy is explicit in
  the module docstring: *"We request the registrar's canonical BibTeX rather than constructing
  one, so we never fabricate bibliographic data."* The entry **body is left as-returned**.
- **Keys**: `cite_key_stem()` rewrites the cite key to `<Surname><Year>` (first author + year,
  with `a`/`b`/… collision suffixes via `collision_suffixes()`). This is correct — do not change it.
- **Merge**: `BibCollection.render()` emits a deterministic, key-sorted merged `references.bib`
  plus `% unresolved` comment lines. Correct — do not change it.
- `src/paperforge/metadata.py` (OpenAlex primary + Crossref fallback) supplies author/year for
  keying when the input lacks them. Correct — do not change it.

So keying, merging, dedup, and the never-fabricate sourcing are all done and tested
(`tests/test_bibtex.py`, 16 tests). **The only defect is escaping.**

## The defect

Because the body is left exactly as the registrar returned it, registrar BibTeX that contains
HTML entities or an unescaped `&` flows straight into `references.bib` and **breaks LaTeX
compilation**. Concretely, Crossref returns:

    container-title: "QSAR &amp; Combinatorial Science"
    journal: "Environmental Science &amp; Technology"

`&amp;` is invalid in LaTeX; a bare `&` is a column separator. A downstream `pdflatex`/`bibtex`
pass on such a `.bib` fails. This was hit in a real manuscript. There is currently **zero escape
coverage** in `tests/test_bibtex.py`.

## The fix (decided approach — option 1)

Add a small, targeted **LaTeX-safety pass over the entry body** in `bibtex.py`, applied to the
content-negotiation text before it is stored/rendered. Two transforms, in order:

1. **Decode HTML entities** with the stdlib: `html.unescape(...)` turns `&amp;`→`&`,
   `&lt;`→`<`, `&gt;`→`>`, `&quot;`→`"`, `&#39;`/`&#x2019;`→the character, etc.
2. **Escape the LaTeX special `&`**: turn every remaining `&` that is **not already** `\&`
   into `\&`. Must be **idempotent** — an already-correct `\&` stays `\&` (do not produce `\\&`).

### Why exactly this, and NOT the alternatives (methodology — so you don't "improve" it back)

- **Why not pylatexenc / `unicode_to_latex`**: the content-negotiation body is **already LaTeX**
  (it may already contain `\"o`, `--`, `\&`, etc.). Running a full unicode→LaTeX converter over
  already-LaTeX text double-escapes it. Only the entity-decode + `&`-escape transform is safe here.
- **Why not rebuild the bib from Crossref/OpenAlex JSON**: that would discard the registrar's
  canonical entry and have us *construct* bibliographic data — exactly the "never fabricate"
  property this module deliberately preserves. Keep content-negotiation as the source.
- **Why not betterbib**: betterbib 7.x on PyPI is a stonefish-licensed compiled binary that fails
  to import without a license; the old MIT versions are gone from PyPI. Unusable, and a closed
  binary is wrong for an open/reproducible tool anyway.
- **No new dependencies**: `html` is stdlib. Keep `dependencies = ["requests>=2.28"]` unchanged.

### Scope discipline (hard limits)

- Escape **only**. Do not add, drop, reorder, or reformat any BibTeX field.
- Do not touch the cite key, the entry type, field names, or `render()`'s ordering.
- Apply the transform to field **values / body text**, not to the `@type{key,` head — though
  applying it to the whole entry string is acceptable since keys/types contain no `&`/entities.
- Preserve non-ASCII exactly as today (`test_non_ascii_roundtrips_without_corruption` must stay green).

## TDD steps

1. **RED**: add tests to `tests/test_bibtex.py`, e.g.:
   - feed a raw entry whose body has `journal = {QSAR &amp; Combinatorial Science}` through the
     same path `add()` uses; assert the stored/rendered text contains `QSAR \& Combinatorial
     Science` and contains **no** `&amp;` and no bare unescaped `&`.
   - feed a body that already contains `\&`; assert it is **unchanged** (idempotent — no `\\&`).
   - feed `&lt;`/`&gt;`/`&#39;` entities; assert they decode correctly.
   - confirm the new test FAILS before the fix.
2. **GREEN**: implement the escape function in `bibtex.py` and wire it into the body path
   (e.g. inside `add()` after parsing, or where the entry text is finalized). Minimal change.
3. **REFACTOR**: keep it pure and unit-testable (string in → string out), matching the module's
   existing pure-parsing style.

## Verify

- `pytest tests/test_bibtex.py -q` — all tests (old 16 + new) pass.
- Real-DOI reproduction: fetch `10.1002/qsar.200390023` (its `container-title` is
  `QSAR &amp; Combinatorial Science`) and confirm the rendered entry is LaTeX-safe
  (`\&`, no `&amp;`).
- Optional end-to-end: a tiny `.tex` with `\bibliography{references}` + `\nocite{*}` compiles
  under `pdflatex`+`bibtex` with no `&`-related error.

## Branch / repo state

- The local working copy may be checked out on a **stale `claude/festive-tesla-*` branch**
  (scaffold only — it predates `bibtex.py`). **Base your work on `origin/main`** (commit
  `2cec69b`, the version that actually contains `bibtex.py`/`metadata.py`). Create a fresh
  feature branch from `origin/main`.
- Commit message style: this repo uses plain imperative subjects (e.g.
  "Add automatic DOI→BibTeX generation"). Match it.
- Do not push or open a PR unless asked.

## Definition of done

- New reproduction tests added and passing; all prior tests still green.
- `bibtex.py` body output is LaTeX-safe: HTML entities decoded, `&`→`\&`, idempotent.
- No new dependencies; keying/merge/sourcing untouched.
- Real `&amp;` DOI case verified.

---

## FOLLOW-UP (found in a real compile test): non-standard Unicode spaces break LaTeX

This is the **same body LaTeX-safety pass**, one more transform. `&amp;` / `<i>` tags / diacritic
keys were already fixed; this is the remaining defect a `pdflatex`+`bibtex` compile surfaced.

### Symptom (measured)

An isolated `pdflatex`+`bibtex` compile of the generated `references.bib` (with
`\usepackage[utf8]{inputenc}`) raised, four times:

    ! LaTeX Error: Unicode character ⁠ (U+2005) not set up for use with LaTeX.

### Root cause

Crossref encodes author given names with **non-standard typographic spaces**. Example from the
Crossref record for `10.1002/qsar.200390023`:

    "author": [{ "given": "Jon A.", "family": "Arnot", ... }]

That is U+2005 (FOUR-PER-EM SPACE) between "Jon" and "A.". The content-negotiation BibTeX carries
the same byte. `inputenc(utf8)` does not define U+2005 (nor U+2009 thin space, U+202F narrow NBSP,
U+00A0 NBSP, …), so each one errors at compile. The PDF is still produced under `nonstopmode`, but
the character is dropped/garbled — unacceptable for a submission.

### Scope — what IS vs IS NOT the bug (from a full non-ASCII scan of the output)

- **LaTeX-unsafe → MUST normalize**: Unicode Space-Separator (category `Zs`) other than ASCII
  space — the U+2000–U+200A range (en/em/three-/four-(U+2005)/six-per-em, figure, punctuation,
  thin, hair spaces), U+202F (narrow NBSP), U+00A0 (NBSP); plus U+200B ZERO WIDTH SPACE (→ drop).
- **LaTeX-SAFE → DO NOT touch**: U+2013 EN DASH and U+2014 EM DASH (inputenc utf8 handles them and
  they are meaningful — they appear 15× in `pages`), and Latin-1 accents `ç`/`ö`/`ø` (handled).
  Only the **space** characters are the defect. Do not "normalize" dashes or accents away.

### Fix

In the same body LaTeX-safety pass, map every non-ASCII Unicode space → one ASCII space (U+0020)
and drop zero-width spaces. Idempotent, pure. Prefer `unicodedata.category(ch) == "Zs"` (plus an
explicit U+200B/U+200C/U+200D drop) over a hand-maintained list, so future exotic spaces are
covered too — but keep U+2013/U+2014 and accented letters untouched (they are not `Zs`).

### TDD

Add a reproduction test feeding an author body containing `"Jon A."`; assert the rendered
entry has a plain ASCII space there and no `Zs`-but-not-`0x20` characters remain. Confirm it FAILS
before the fix. Then verify the real `references.bib` compiles with **zero** "Unicode character …
not set up" errors (`pdflatex`+`bibtex` round-trip).
