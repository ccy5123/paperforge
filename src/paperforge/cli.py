"""Command-line entry point.

paperforge separates its two concerns into subcommands:

    paperforge bib       dois.txt --email you@example.org      # references.bib only
    paperforge download  refs.xlsx --email you@example.org     # OA PDFs only
    paperforge all       refs.xlsx dois.txt --email you@...    # both

Inputs may be files (.xlsx/.csv/.txt) or bare DOIs typed on the command line.
For backward compatibility, omitting the subcommand is treated as ``all``:

    paperforge refs.xlsx 10.1038/s41586-020-2649-2 --email you@example.org
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

_COMMANDS = ("bib", "download", "all")
_HELP_FLAGS = ("-h", "--help")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paperforge",
        description="Resolve DOIs to Open Access PDFs and/or a references.bib, in batch.",
    )
    sub = p.add_subparsers(dest="command", required=True,
                           metavar="{bib,download,all}")

    # Options shared by every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("inputs", nargs="+",
                        help=".xlsx/.csv/.txt files, or bare DOIs, to process")
    common.add_argument("-o", "--output", default="paperforge_out",
                        help="output directory (default: paperforge_out)")
    common.add_argument("--email", default=None,
                        help="contact email for Unpaywall/OpenAlex/doi.org "
                             "(or $UNPAYWALL_EMAIL)")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging (also un-quiets the HTTP transport logs)")

    # Options that only make sense when downloading PDFs.
    download = argparse.ArgumentParser(add_help=False)
    download.add_argument("--licenses", default=None,
                          help="comma-separated license filter, e.g. 'cc-by,cc0'")
    download.add_argument("--require-known-license", action="store_true",
                          help="with --licenses, also drop OA PDFs whose license is unknown")
    download.add_argument("--source-order", default=None,
                          help="comma-separated resolver order "
                               "(arxiv,unpaywall,openalex,europepmc,semantic_scholar)")
    download.add_argument("--overwrite", action="store_true",
                          help="re-download DOIs already marked success in the manifest")
    download.add_argument("--no-metadata", action="store_true",
                          help="don't look up author/year from OpenAlex/Crossref for filenames")

    sub.add_parser("bib", parents=[common],
                   help="build references.bib only (DOI->BibTeX via doi.org)")
    sub.add_parser("download", parents=[common, download],
                   help="download Open Access PDFs only")
    sub.add_parser("all", parents=[common, download],
                   help="download OA PDFs and build references.bib")
    return p


def parse_args(argv=None) -> argparse.Namespace:
    """Parse args, defaulting to the ``all`` subcommand for the bare form."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backward compat: `paperforge refs.xlsx ...` (no subcommand) == `all`.
    if argv and argv[0] not in _COMMANDS and argv[0] not in _HELP_FLAGS:
        argv = ["all"] + argv
    return build_parser().parse_args(argv)


def _split_csv(value: str | None):
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _build_config(args: argparse.Namespace) -> Config:
    """Map parsed args to a Config, gating the two phases by subcommand."""
    licenses = _split_csv(getattr(args, "licenses", None))
    return Config.from_env(
        unpaywall_email=args.email,
        output_dir=args.output,
        allowed_licenses=(set(licenses) if licenses else None),
        require_known_license=getattr(args, "require_known_license", False) or None,
        source_order=_split_csv(getattr(args, "source_order", None)),
        overwrite=getattr(args, "overwrite", False) or None,
        enrich_metadata=(False if getattr(args, "no_metadata", False) else None),
        # 'bib' => no downloads; 'download' => no bib; 'all' => both (defaults).
        generate_bib=(False if args.command == "download" else None),
        download_pdfs=(False if args.command == "bib" else None),
    )


def main(argv=None) -> int:
    args = parse_args(argv)
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

    config = _build_config(args)

    if not config.unpaywall_email:
        log.warning("No --email / $UNPAYWALL_EMAIL set: Unpaywall is skipped and "
                    "OpenAlex/doi.org won't use the polite pool. Results will be weaker.")

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
