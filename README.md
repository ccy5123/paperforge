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

Requires Python ≥ 3.10. Runtime deps: `requests`, `openpyxl`.

## Usage

```bash
# A spreadsheet, a DOI list, and a bare DOI — all at once
paperforge refs.xlsx dois.txt 10.1038/s41586-020-2649-2 \
    --email you@example.org -o ./out

# Only redistributable Creative Commons PDFs
paperforge refs.csv --email you@example.org --licenses cc-by,cc-by-sa,cc0
```

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

Author/year/title columns are picked up when present (used for filenames and
the sidecar). **When they're missing** — a plain DOI list, or a spreadsheet
without an author column — paperforge looks them up from OpenAlex (falling back
to Crossref) so files are named e.g. `0007_Vaswani_2017.pdf` instead of
`0007_Unknown_Unknown.pdf`. Disable with `--no-metadata`. DOIs wrapped as
`https://doi.org/...` or `doi:...` are normalized automatically.

### Output

```
out/
  pdfs/
    0001_Smith_2021.pdf
    0001_Smith_2021.json     # source, license, version, all attempts
  manifest.csv               # index, doi, status, source, license, filename, error
```

`manifest.csv` makes runs **resumable**: a DOI already marked `success` is
skipped on the next run (use `--overwrite` to force a re-download). Failures
are retried automatically.

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
| `-v, --verbose` | debug logging |

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
