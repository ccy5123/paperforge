"""Configuration for a paperforge batch run."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """All knobs for resolving DOIs and downloading OA PDFs.

    Only ``unpaywall_email`` and ``output_dir`` really matter to get going;
    everything else has a sensible default.
    """

    # --- identity / HTTP ---
    unpaywall_email: str = ""          # required by Unpaywall; identifies you to OpenAlex
    user_agent: Optional[str] = None   # default derived from email in OADownloader
    semantic_scholar_api_key: Optional[str] = None

    # --- output ---
    output_dir: Path = Path("paperforge_out")

    # --- OA / license policy ---
    allowed_licenses: Optional[set[str]] = None   # e.g. {"cc-by", "cc0"}; None = accept any
    require_known_license: bool = False           # with a filter, drop unknown-license PDFs
    source_order: Optional[list[str]] = None      # override default resolver chain

    # --- batch behavior ---
    overwrite: bool = False            # re-download DOIs already marked success in the manifest
    enrich_metadata: bool = True       # look up author/year/title for filenames when missing

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if self.allowed_licenses is not None and not isinstance(self.allowed_licenses, set):
            self.allowed_licenses = {str(s).lower() for s in self.allowed_licenses}

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build from environment variables, then apply non-None keyword overrides."""
        base: dict = {
            "unpaywall_email": os.environ.get("UNPAYWALL_EMAIL", ""),
            "semantic_scholar_api_key": os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None,
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)
