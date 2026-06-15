"""Batch driver: dedupe DOIs, skip already-done, download, write a manifest.

The manifest (``<output_dir>/manifest.csv``) makes runs resumable: a DOI
previously recorded as ``success`` is skipped on re-run unless ``overwrite``
is set. Failures are retried on the next run.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .downloader import OADownloader
from .utils import normalize_doi

_MANIFEST_FIELDS = ["index", "doi", "status", "source", "license",
                    "filename", "origin", "error"]


@dataclass
class BatchResult:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    rows: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.succeeded} downloaded, {self.failed} failed, "
                f"{self.skipped} skipped (of {self.total} unique DOIs)")


class BatchProcessor:
    def __init__(self, config, downloader: Optional[OADownloader] = None, logger=None):
        self.config = config
        self.downloader = downloader or OADownloader(config)
        self.logger = logger
        if logger is not None and getattr(self.downloader, "logger", None) is None:
            self.downloader.logger = logger
        self.manifest_path = Path(config.output_dir) / "manifest.csv"

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
        # Start from prior manifest rows so skipped entries are preserved.
        rows: dict[str, dict] = dict(done)

        for index, rec in enumerate(records, start=1):
            key = rec.doi.lower()
            prev = done.get(key)
            if prev and prev.get("status") == "success" and not self.config.overwrite:
                result.skipped += 1
                self.log(f"[{index:04d}] skip (already done): {rec.doi}", "DEBUG")
                continue

            outcome = self.downloader.fetch(rec.doi, index, rec.author, rec.year, rec.title)
            rows[key] = {
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

        result.rows = list(rows.values())
        self._write_manifest(result.rows)
        return result

    def log(self, message: str, level: str = "INFO") -> None:
        if self.logger:
            getattr(self.logger, level.lower())(message)
        else:
            print(f"[{level}] {message}")
