"""paperforge — resolve DOIs to Open Access PDFs and download them in batch."""
from .config import Config
from .downloader import DownloadOutcome, OADownloader, Resolution
from .inputs import PaperRecord, load_many, load_records
from .orchestrator import BatchProcessor, BatchResult

__version__ = "0.1.0"

__all__ = [
    "Config",
    "OADownloader",
    "Resolution",
    "DownloadOutcome",
    "PaperRecord",
    "load_records",
    "load_many",
    "BatchProcessor",
    "BatchResult",
]
