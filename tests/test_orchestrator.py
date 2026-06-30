import csv

from paperforge.config import Config
from paperforge.downloader import DownloadOutcome
from paperforge.inputs import PaperRecord
from paperforge.orchestrator import BatchProcessor


class FakeDownloader:
    """Stand-in for OADownloader: records calls, returns a canned outcome."""

    def __init__(self, succeed=True):
        self.logger = None
        self.calls = []
        self.succeed = succeed

    def fetch(self, doi, index, author, year, title=""):
        self.calls.append(doi)
        if self.succeed:
            return DownloadOutcome(doi=doi, ok=True, source="fake",
                                   license="cc-by", filename=f"{index:04d}.pdf")
        return DownloadOutcome(doi=doi, ok=False, error="nope")


def test_dedupes_and_counts(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, generate_bib=False)
    fake = FakeDownloader(succeed=True)
    bp = BatchProcessor(cfg, downloader=fake)
    recs = [
        PaperRecord(doi="10.1038/aaa"),
        PaperRecord(doi="10.1038/AAA"),   # case-duplicate of the first
        PaperRecord(doi="10.1234/bbb"),
    ]
    res = bp.run(recs)
    assert res.total == 2
    assert res.succeeded == 2
    assert len(fake.calls) == 2
    assert bp.manifest_path.exists()


def test_failures_counted(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, generate_bib=False)
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=False))
    res = bp.run([PaperRecord(doi="10.1038/aaa")])
    assert res.failed == 1 and res.succeeded == 0


def test_resume_skips_prior_success(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, generate_bib=False)
    BatchProcessor(cfg, downloader=FakeDownloader()).run([PaperRecord(doi="10.1038/aaa")])

    fake2 = FakeDownloader()
    res = BatchProcessor(cfg, downloader=fake2).run([PaperRecord(doi="10.1038/aaa")])
    assert res.skipped == 1
    assert fake2.calls == []          # not re-fetched


def test_overwrite_forces_refetch(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, generate_bib=False)
    BatchProcessor(cfg, downloader=FakeDownloader()).run([PaperRecord(doi="10.1038/aaa")])

    cfg.overwrite = True
    fake2 = FakeDownloader()
    res = BatchProcessor(cfg, downloader=fake2).run([PaperRecord(doi="10.1038/aaa")])
    assert res.skipped == 0
    assert fake2.calls == ["10.1038/aaa"]


# ---- bibliography generation (bib_fetcher injected; no network) ----

def _bib_from(mapping):
    """A bib_fetcher returning canned BibTeX per DOI (None = miss)."""
    return lambda doi: mapping.get(doi)


def test_writes_references_bib_for_all_dois(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)   # bib ON by default
    bibs = {
        "10.1038/a": "@article{x, author={Smith, J}, year={2020}, title={A}}",
        "10.1038/b": "@article{y, author={Doe, K}, year={2019}, title={B}}",
    }
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=True),
                        bib_fetcher=_bib_from(bibs))
    res = bp.run([PaperRecord(doi="10.1038/a"), PaperRecord(doi="10.1038/b")])
    bib = (tmp_path / "references.bib").read_text(encoding="utf-8")
    assert "@article{Smith2020," in bib
    assert "@article{Doe2019," in bib
    assert res.bib_entries == 2 and res.bib_misses == 0


def test_bib_generated_for_non_oa_doi(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
    bibs = {"10.1038/closed": "@book{z, author={Knuth, D}, year={1968}, title={TAOCP}}"}
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=False),  # no OA PDF
                        bib_fetcher=_bib_from(bibs))
    res = bp.run([PaperRecord(doi="10.1038/closed")])
    assert res.failed == 1                            # paywalled: no PDF
    bib = (tmp_path / "references.bib").read_text(encoding="utf-8")
    assert "@book{Knuth1968," in bib                  # ...but still citeable


def test_bib_miss_recorded_and_batch_continues(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=True),
                        bib_fetcher=lambda doi: None)        # every bib is a miss
    res = bp.run([PaperRecord(doi="10.1038/a")])
    assert res.succeeded == 1                                # download unaffected
    assert res.bib_entries == 0 and res.bib_misses == 1
    rows = list(csv.DictReader(open(tmp_path / "manifest.csv", encoding="utf-8")))
    assert rows[0]["bib"] == "miss"
    bib = (tmp_path / "references.bib").read_text(encoding="utf-8")
    assert "% unresolved (no BibTeX): 10.1038/a" in bib


def test_bib_fetch_exception_does_not_crash(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)

    def boom(doi):
        raise RuntimeError("network exploded")

    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=True), bib_fetcher=boom)
    res = bp.run([PaperRecord(doi="10.1038/a")])
    assert res.succeeded == 1 and res.bib_misses == 1        # recorded null, no crash


def test_no_download_skips_pdfs_but_still_writes_bib(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, download_pdfs=False)
    fake = FakeDownloader(succeed=True)
    bp = BatchProcessor(
        cfg, downloader=fake,
        bib_fetcher=_bib_from({"10.1038/a":
                               "@article{x, author={Smith, J}, year={2020}, title={A}}"}))
    res = bp.run([PaperRecord(doi="10.1038/a")])
    assert fake.calls == []                       # no PDF fetch attempted
    assert res.succeeded == 0 and res.failed == 0
    assert res.skipped == 1                        # counted as skipped this run
    bib = (tmp_path / "references.bib").read_text(encoding="utf-8")
    assert "@article{Smith2020," in bib            # bib still produced
    rows = list(csv.DictReader(open(tmp_path / "manifest.csv", encoding="utf-8")))
    assert rows[0]["status"] == "skipped" and rows[0]["bib"] == "ok"


def test_no_bib_config_skips_file(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path, generate_bib=False)
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=True),
                        bib_fetcher=lambda doi: "@x{y, author={A, B}, year={2000}}")
    bp.run([PaperRecord(doi="10.1038/a")])
    assert not (tmp_path / "references.bib").exists()
