"""Batch driver: dedupe DOIs, skip already-done, download, write a manifest,
and emit a ``references.bib`` for the whole input set.

The manifest (``<output_dir>/manifest.csv``) makes runs resumable: a DOI
previously recorded as ``success`` is skipped on re-run unless ``overwrite``
is set. Failures are retried on the next run.

BibTeX is fetched for *every* input DOI regardless of OA-PDF outcome — a
paywalled classic still needs to be citeable — and written to
``<output_dir>/references.bib`` with surname+year keys aligned to the PDF
filenames. A DOI whose BibTeX can't be resolved is recorded (manifest ``bib``
column = ``miss``) and omitted from the file, never synthesized.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from .bibtex import BibCollection, fetch_bibtex
from .downloader import OADownloader
from .utils import normalize_doi

_MANIFEST_FIELDS = ["index", "doi", "status", "source", "license",
                    "filename", "origin", "bib", "error"]


@dataclass
class BatchResult:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    bib_entries: int = 0
    bib_misses: int = 0
    rows: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        base = (f"{self.succeeded} downloaded, {self.failed} failed, "
                f"{self.skipped} skipped (of {self.total} unique DOIs)")
        if self.bib_entries or self.bib_misses:
            base += f"; bib: {self.bib_entries} entries, {self.bib_misses} unresolved"
        return base


class BatchProcessor:
    def __init__(self, config, downloader: Optional[OADownloader] = None, logger=None,
                 bib_fetcher: Optional[Callable[[str], Optional[str]]] = None):
        self.config = config
        self.downloader = downloader or OADownloader(config)
        self.logger = logger
        if logger is not None and getattr(self.downloader, "logger", None) is None:
            self.downloader.logger = logger
        self._bib_fetcher = bib_fetcher
        self.manifest_path = Path(config.output_dir) / "manifest.csv"
        self.bib_path = Path(config.output_dir) / "references.bib"

    # ---- helpers -----------------------------------------------------

    def _load_done(self) -> dict[str, dict]:
        done: dict[str, dict] = {}
        if self.manifest_path.exists():
            with open(self.manifest_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    doi = (row.get("doi") or "").lower()
                    if doi:
                        done[doi] = row
        return done

    @staticmethod
    def _dedupe(records) -> list:
        seen: set[str] = set()
        out = []
        for rec in records:
            doi = normalize_doi(rec.doi)
            if not doi:
                continue
            key = doi.lower()
            if key in seen:
                continue
            seen.add(key)
            rec.doi = doi
            out.append(rec)
        return out

    def _bib_fetch(self) -> Callable[[str], Optional[str]]:
        if self._bib_fetcher is not None:
            return self._bib_fetcher
        session = getattr(self.downloader, "session", None) or requests.Session()
        return lambda doi: fetch_bibtex(doi, session, self.config)

    def _write_manifest(self, rows: list[dict]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_MANIFEST_FIELDS)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in _MANIFEST_FIELDS})

    # ---- main --------------------------------------------------------

    def run(self, records: Iterable) -> BatchResult:
        records = self._dedupe(list(records))
        done = self._load_done()
        result = BatchResult(total=len(records))
        rows: dict[str, dict] = dict(done)

        bib = BibCollection() if self.config.generate_bib else None
        fetch_bib = self._bib_fetch() if bib is not None else None

        for index, rec in enumerate(records, start=1):
            key = rec.doi.lower()
            prev = done.get(key)
            skipping = bool(prev and prev.get("status") == "success"
                            and not self.config.overwrite)

            if skipping:
                result.skipped += 1
                self.log(f"[{index:04d}] skip (already done): {rec.doi}", "DEBUG")
                row = dict(prev)            # keep prior download result
            else:
                outcome = self.downloader.fetch(rec.doi, index, rec.author,
                                                rec.year, rec.title)
                row = {
                    "index": index,
                    "doi": rec.doi,
                    "status": "success" if outcome.ok else "failed",
                    "source": outcome.source or "",
                    "license": outcome.license or "",
                    "filename": outcome.filename or "",
                    "origin": rec.origin or "",
                    "error": outcome.error or "",
                }
                if outcome.ok:
                    result.succeeded += 1
                else:
                    result.failed += 1

            # BibTeX for every DOI, regardless of OA-PDF status. A failure here
            # is a recorded null, never an aborted batch.
            if bib is not None:
                try:
                    raw = fetch_bib(rec.doi)
                except Exception as e:  # belt-and-suspenders: never crash the batch
                    raw = None
                    self.log(f"[{index:04d}] bib fetch error for {rec.doi}: {e}", "WARNING")
                entry = bib.add(rec.doi, raw, author=rec.author, year=rec.year)
                row["bib"] = "ok" if entry else "miss"

            rows[key] = row

        if bib is not None:
            result.bib_entries = bib.count
            result.bib_misses = len(bib.misses)
            self.bib_path.parent.mkdir(parents=True, exist_ok=True)
            self.bib_path.write_text(bib.render(), encoding="utf-8")
            self.log(f"references.bib: {result.bib_entries} entries, "
                     f"{result.bib_misses} unresolved")

        result.rows = list(rows.values())
        self._write_manifest(result.rows)
        return result

    def log(self, message: str, level: str = "INFO") -> None:
        if self.logger:
            getattr(self.logger, level.lower())(message)
        else:
            print(f"[{level}] {message}")
