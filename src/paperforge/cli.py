"""Command-line entry point.

    paperforge refs.xlsx dois.txt -o out/ --email you@example.org
    paperforge 10.1038/s41586-020-2649-2 --email you@example.org

Inputs may be files (.xlsx/.csv/.txt) or bare DOIs typed on the command line.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config
from .inputs import PaperRecord, load_many
from .orchestrator import BatchProcessor
from .utils import extract_dois, normalize_doi


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paperforge",
        description="Resolve DOIs to Open Access PDFs and download them in batch.",
    )
    p.add_argument("inputs", nargs="+",
                   help=".xlsx/.csv/.txt files, or bare DOIs, to process")
    p.add_argument("-o", "--output", default="paperforge_out",
                   help="output directory (default: paperforge_out)")
    p.add_argument("--email", default=None,
                   help="contact email for Unpaywall/OpenAlex (or $UNPAYWALL_EMAIL)")
    p.add_argument("--licenses", default=None,
                   help="comma-separated license filter, e.g. 'cc-by,cc0'")
    p.add_argument("--require-known-license", action="store_true",
                   help="with --licenses, also drop OA PDFs whose license is unknown")
    p.add_argument("--source-order", default=None,
                   help="comma-separated resolver order "
                        "(arxiv,unpaywall,openalex,europepmc,semantic_scholar)")
    p.add_argument("--overwrite", action="store_true",
                   help="re-download DOIs already marked success in the manifest")
    p.add_argument("--no-metadata", action="store_true",
                   help="don't look up author/year from OpenAlex/Crossref for filenames")
    p.add_argument("--no-bib", action="store_true",
                   help="don't generate references.bib (DOI->BibTeX via doi.org)")
    p.add_argument("--no-download", action="store_true",
                   help="skip OA PDF downloads; only build references.bib (bib-only run)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def _split_csv(value: str | None):
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    # habanero's HTTP stack (httpx2/httpcore2) logs every request at INFO; that
    # floods the batch output with "HTTP Request: GET ..." lines. Keep our own
    # progress visible but quiet the transport unless the user asked for -v.
    if not args.verbose:
        for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("paperforge")

    licenses = _split_csv(args.licenses)
    config = Config.from_env(
        unpaywall_email=args.email,
        output_dir=args.output,
        allowed_licenses=(set(licenses) if licenses else None),
        require_known_license=args.require_known_license or None,
        source_order=_split_csv(args.source_order),
        overwrite=args.overwrite or None,
        enrich_metadata=(False if args.no_metadata else None),
        generate_bib=(False if args.no_bib else None),
        download_pdfs=(False if args.no_download else None),
    )

    if not config.unpaywall_email:
        log.warning("No --email / $UNPAYWALL_EMAIL set: Unpaywall is skipped and "
                    "OpenAlex won't use the polite pool. Results will be weaker.")

    # Separate real files from bare DOIs typed as arguments.
    paths, inline = [], []
    for item in args.inputs:
        (paths if Path(item).exists() else inline).append(item)

    records = list(load_many(paths)) if paths else []
    for token in inline:
        found = extract_dois(token)
        if not found:
            norm = normalize_doi(token)
            if norm:
                found = [norm]
        for doi in found:
            records.append(PaperRecord(doi=doi, origin="cli"))
        if not found:
            log.warning("Ignoring input (not a file or DOI): %s", token)

    if not records:
        log.error("No DOIs found in the given inputs.")
        return 2

    processor = BatchProcessor(config, logger=log)
    result = processor.run(records)
    log.info(result.summary())
    log.info("Manifest: %s", processor.manifest_path)
    if config.generate_bib:
        log.info("Bibliography: %s", processor.bib_path)
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
