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
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
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
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
    bp = BatchProcessor(cfg, downloader=FakeDownloader(succeed=False))
    res = bp.run([PaperRecord(doi="10.1038/aaa")])
    assert res.failed == 1 and res.succeeded == 0


def test_resume_skips_prior_success(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
    BatchProcessor(cfg, downloader=FakeDownloader()).run([PaperRecord(doi="10.1038/aaa")])

    fake2 = FakeDownloader()
    res = BatchProcessor(cfg, downloader=fake2).run([PaperRecord(doi="10.1038/aaa")])
    assert res.skipped == 1
    assert fake2.calls == []          # not re-fetched


def test_overwrite_forces_refetch(tmp_path):
    cfg = Config(unpaywall_email="t@e.org", output_dir=tmp_path)
    BatchProcessor(cfg, downloader=FakeDownloader()).run([PaperRecord(doi="10.1038/aaa")])

    cfg.overwrite = True
    fake2 = FakeDownloader()
    res = BatchProcessor(cfg, downloader=fake2).run([PaperRecord(doi="10.1038/aaa")])
    assert res.skipped == 0
    assert fake2.calls == ["10.1038/aaa"]
