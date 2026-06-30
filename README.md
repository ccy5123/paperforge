# paperforge

Resolve DOIs to **Open Access PDFs** and download them in batch.

Give it a DOI list or a messy spreadsheet, and paperforge extracts the DOIs,
checks each one against a chain of OA discovery services, and saves the first
genuinely downloadable PDF — with a metadata sidecar and a resumable manifest.

## How it works

For every DOI it walks a fallback chain, stopping at the first source that
hands back a real PDF:

1. **arXiv** — direct, no API call, for `10.48550/arXiv.*` DOIs
2. **Unpaywall** — broadest OA discovery (needs a contact email)
3. **OpenAlex** — complementary aggregator (uses the polite pool via your email)
4. **Europe PMC** — biomedical / environmental health
5. **Semantic Scholar** — last-resort complement (set an API key to lift rate limits)

Every download is verified by its `%PDF-` magic bytes, so HTML landing pages
are never mistaken for papers. Each PDF gets a `.json` sidecar recording the
winning source, license, version, and every attempt.

## Install

```bash
pip install -e ".[dev]"     # editable, with test extras
```

Requires Python ≥ 3.10. Runtime deps: `requests`, `openpyxl`, and
`habanero[bibtex]` (DOI → BibTeX sourcing). If a fresh environment hits a build
error on the transitive `pylatexenc`, upgrade your build tools first:
`pip install -U pip setuptools wheel`.

## Usage

```bash
# A spreadsheet, a DOI list, and a bare DOI — all at once
paperforge refs.xlsx dois.txt 10.1038/s41586-020-2649-2 \
    --email you@example.org -o ./out

# Only redistributable Creative Commons PDFs
paperforge refs.csv --email you@example.org --licenses cc-by,cc-by-sa,cc0

# Just build references.bib, skip the PDF downloads (fast)
paperforge examples/dois.txt --no-download --email you@example.org -o ./out
```

A ready-to-run DOI list lives at [`examples/dois.txt`](examples/dois.txt).

Set the email once via the environment instead of repeating `--email`:

```bash
export UNPAYWALL_EMAIL=you@example.org
export SEMANTIC_SCHOLAR_API_KEY=...   # optional
```

### Inputs

| Type | DOI detection |
| --- | --- |
| `.xlsx` / `.xlsm` | header column named like *DOI* on any sheet; else every cell is regex-scanned |
| `.csv` / `.tsv` | same header logic (delimiter auto-sniffed) |
| `.txt` (or no extension) | one DOI per line / scanned from text |
| bare DOI argument | taken as-is |

Files are named in citation style, `<Author><Year>.pdf` (e.g.
`Vaswani2017.pdf`). Author/year/title columns are used when present;
**when they're missing** — a plain DOI list, or a spreadsheet without an
author column — paperforge looks them up from OpenAlex (falling back to
Crossref) so you get `Vaswani2017.pdf` instead of `Unknown.pdf`. Disable the
lookup with `--no-metadata`. Two different papers that map to the same name get
an `a`/`b`/… suffix (the same scheme as the bib keys, so PDF name and cite key
stay in step); re-downloading the same DOI overwrites its file in place. DOIs
wrapped as `https://doi.org/...` or `doi:...` are normalized automatically.
Accented names are transliterated to ASCII rather than dropped, so *Könemann*
becomes `Konemann2018` (not `Knemann2018`).

### Output

```
out/
  pdfs/
    Vaswani2017.pdf
    Vaswani2017.json         # source, license, version, all attempts
  manifest.csv               # index, doi, status, source, license, filename, bib, error
  references.bib             # full BibTeX for the input DOIs (surname+year keys)
```

`manifest.csv` makes runs **resumable**: a DOI already marked `success` is
skipped on the next run (use `--overwrite` to force a re-download). Failures
are retried automatically.

### Bibliography (`references.bib`)

paperforge also emits a `references.bib` so a downstream paper can `\cite{...}`
the batch with no hand-authoring. For each DOI it requests the registrar's
canonical BibTeX via **doi.org content negotiation** — sourcing is delegated to
[habanero](https://github.com/sckott/habanero), which routes through doi.org and
returns the **registrar-of-record's own** BibTeX, covering Crossref, DataCite,
and mEDRA DOIs. It never fabricates entries. Notes:

- **Every input DOI is attempted**, not just OA-PDF successes — a paywalled
  classic still gets cited. (Bib availability ≠ PDF availability.)
- Each entry's key is rewritten to the **same `<Surname><Year>` stem as the PDF
  filename** (e.g. `Vaswani2017`), with `a`/`b`/… on collision. Keys are unique
  and entries are sorted deterministically.
- A DOI whose BibTeX can't be resolved is recorded (`bib=miss` in the manifest,
  a `% unresolved` comment in the file) and **omitted** — never synthesized.
- Disable with `--no-bib`, or keep the bib and skip the PDF phase with
  `--no-download`.

#### LaTeX safety

Registrar BibTeX is XML-rooted, so a field value can arrive with HTML entities
(`Environmental Science &amp; Technology`), bare LaTeX specials (`&`), or inline
HTML markup (`<i>Cyprinus carpio</i>`) — all of which break `pdflatex`/`bibtex`.
Before an entry is stored, paperforge transcodes it at that seam:

- HTML entities are decoded (`&amp;`/`&lt;`/`&#39;`/`&#x2019;` → `&`/`<`/`'`/’).
- Bare `&` is escaped to `\&` (idempotent — an existing `\&` is left alone).
- Predictable inline markup becomes LaTeX: `<i>`/`<em>` → `\textit{}`/`\emph{}`,
  `<b>`/`<strong>` → `\textbf{}`, `<sub>`/`<sup>` → `\textsubscript{}`/
  `\textsuperscript{}`, `<sc>` → `\textsc{}`, and so on.

The transform is conservative (only field values change — never keys, types, or
field names), minimal, and residue-checked: anything left at the entity/`&` seam
is raised loudly rather than leaking into the file. Raw Unicode (`δ¹³C`,
`Gonçalo`) is preserved as-is. This is the `paperforge.latex_safety` module, a
pure, dependency-free string transform you can reuse on its own.

### Options

| Flag | Meaning |
| --- | --- |
| `-o, --output DIR` | output directory (default `paperforge_out`) |
| `--email ADDR` | contact email for Unpaywall/OpenAlex (or `$UNPAYWALL_EMAIL`) |
| `--licenses a,b,c` | only keep PDFs whose license matches one of these substrings |
| `--require-known-license` | with `--licenses`, also drop PDFs of unknown license |
| `--source-order ...` | reorder/limit the resolver chain |
| `--overwrite` | re-download DOIs already recorded as success |
| `--no-metadata` | skip the OpenAlex/Crossref lookup used to name files |
| `--no-bib` | don't generate `references.bib` |
| `--no-download` | skip OA PDF downloads; only build `references.bib` |
| `-v, --verbose` | debug logging (also un-quiets the HTTP transport logs) |

## Library use

```python
from paperforge import Config, BatchProcessor, load_many

config = Config(unpaywall_email="you@example.org", output_dir="out")
records = load_many(["refs.xlsx", "dois.txt"])
result = BatchProcessor(config).run(records)
print(result.summary())
```

## Develop

```bash
pytest        # network-free unit tests (resolvers are stubbed)
```

## Notes & limits

- Only **Open Access** PDFs are fetched; paywalled papers are reported as failures.
- Processing is currently sequential. For large lists, be a good API citizen —
  always pass an email so OpenAlex uses its polite pool.
- arXiv's default grant is a non-exclusive license, not necessarily Creative
  Commons, so arXiv hits report an *unknown* license rather than guessing.
