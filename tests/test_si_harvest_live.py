"""Live checks of tools/si_harvest.py against the real services (verified Oct 2026).

Skipped unless SI_LIVE=1; they use the network and take minutes:

    SI_LIVE=1 SI_EMAIL=you@example.org pytest tests/test_si_harvest_live.py

Cases marked ``browser`` also need Playwright's Chromium; they run headless, with
no window for a person, so a site that answers with a picture puzzle fails them.
Cloudflare judges each visit anew, so those can fail on one run and pass on the
next. The Elsevier case uses the Object API when $ELSEVIER_API_KEY is set, else
URL probing.
"""
from __future__ import annotations

import csv
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SI_LIVE") != "1",
                                reason="live network checks; set SI_LIVE=1")

_PATH = Path(__file__).resolve().parents[1] / "tools" / "si_harvest.py"
if "si_harvest" in sys.modules:
    si = sys.modules["si_harvest"]
else:
    _spec = importlib.util.spec_from_file_location("si_harvest", _PATH)
    si = importlib.util.module_from_spec(_spec)
    sys.modules["si_harvest"] = si
    _spec.loader.exec_module(si)

ELSEVIER_KEY = os.environ.get("ELSEVIER_API_KEY")

# (DOI, at least this many files, route they came by, needs the browser)
CASES = [
    ("10.1021/es301921w", 2, "figshare", False),                    # ACS
    ("10.1080/1062936x.2024.2337011", 1, "figshare", False),        # Taylor & Francis
    ("10.1371/journal.pone.0298059", 5, "figshare", False),         # PLOS
    ("10.3389/fmed.2026.1868648", 1, "figshare", False),            # Frontiers
    ("10.1038/s41467-024-47016-x", 8, "pmc", False),                # Nature Communications
    ("10.3390/toxics12040259", 1, "pmc", False),                    # MDPI
    ("10.1039/d4ra00562g", 1, "pmc", False),                        # RSC
    ("10.1073/pnas.2307220121", 1, "pmc", False),                   # PNAS
    ("10.1126/sciadv.adk3201", 1, "pmc", False),                    # Science Advances
    ("10.5194/acp-24-4193-2024", 1, "page", False),                 # Copernicus
    ("10.1002/anie.202401465", 1, "page", False),                   # Wiley
    ("10.1139/a06-005", 1, "page", False),                          # Canadian Science Publishing
    ("10.1177/15563316241234045", 7, "page", False),                # SAGE
    ("10.1101/2024.04.17.589894", 1, "page", False),                # bioRxiv
    ("10.1016/j.scitotenv.2024.170841", 1,
     "elsevier-api" if ELSEVIER_KEY else "elsevier-probe", False),  # Elsevier
    ("10.1093/inteam/vjae046", 1, "page-browser", True),            # Oxford University Press
    ("10.26434/chemrxiv-2024-7kqg1", 1, "page-browser", True),      # ChemRxiv
    # IOP: its /data page, over plain HTTP or, behind Radware's picture puzzle, only
    # after a person passed it in a window (not in this headless run)
    ("10.1088/1748-9326/ad3a81", 1, "page", True),
]


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.mark.parametrize("doi,min_files,source,browser", CASES, ids=[c[0] for c in CASES])
def test_live_harvest(tmp_path, doi, min_files, source, browser):
    if browser and not _chromium_ok():
        pytest.skip("playwright/chromium not available")
    http = si.Http(email=os.environ.get("SI_EMAIL", ""))
    h = si.Harvester(http, tmp_path / "out", browser="headless" if browser else "off",
                     browser_profile=tmp_path / "profile", elsevier_key=ELSEVIER_KEY, retries=0)
    try:
        h.run([doi])
    finally:
        h.close()
    with open(tmp_path / "out" / "si_manifest.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    files = [r for r in rows if r.get("file")]
    assert len(files) >= min_files, rows[0].get("note")
    # "page" covers "page-browser": the same page, reached by the browser
    assert any(r["source"].startswith(source) for r in files)
