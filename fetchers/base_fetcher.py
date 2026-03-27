from abc import ABC, abstractmethod
from pathlib import Path

import config


class FetcherError(RuntimeError):
    """Raised when any fetcher fails to retrieve or write its documents."""


class BaseFetcher(ABC):
    """Abstract base class for all data-source fetchers.

    Subclasses must implement :meth:`fetch`.  The constructor creates the
    per-run output directory automatically so callers never have to manage it
    themselves.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id: str = run_id
        self.output_dir: Path = config.FETCHED_DOCS_DIR / run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def fetch(self, topic: str) -> list[Path]:
        """Retrieve documents relevant to *topic* and write them to disk.

        Returns a list of absolute :class:`~pathlib.Path` objects, one per
        document written.  Implementations should raise :class:`FetcherError`
        on failure.
        """

    def _write_doc(self, filename: str, content: str) -> Path:
        """Write *content* as UTF-8 to ``self.output_dir / filename``.

        Returns the absolute :class:`~pathlib.Path` of the written file.
        """
        dest: Path = self.output_dir / filename
        dest.write_text(content, encoding="utf-8")
        return dest.resolve()
