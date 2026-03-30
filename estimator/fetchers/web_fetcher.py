"""WebFetcher — fetches a URL, strips boilerplate, and writes plain-text output.

Topic argument for :meth:`fetch` is a URL string.

Usage (standalone)::

    python3 -m fetchers.web_fetcher "https://example.com"
"""

import logging
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from estimator.fetchers.base_fetcher import BaseFetcher, FetcherError

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; PolyResearch/1.0)"
_MAX_CHARS = 5000
_BOILERPLATE_TAGS = ["nav", "footer", "script", "style", "header", "aside"]


class WebFetcher(BaseFetcher):
    """Fetch a single URL, extract paragraph text, and write it as a document."""

    def fetch(self, topic: str) -> list[Path]:
        """Fetch *topic* (a URL) and return a list containing the written Path.

        Returns an empty list when the URL is unreachable, times out, or
        returns a non-200 status code.  Raises :class:`FetcherError` only for
        unexpected exceptions that are not network-related.
        """
        url = topic
        try:
            response = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": _USER_AGENT},
            )
        except requests.Timeout:
            logger.warning("WebFetcher: request timed out for %s", url)
            return []
        except requests.RequestException as exc:
            logger.warning("WebFetcher: request error for %s: %s", url, exc)
            return []
        except Exception as exc:
            raise FetcherError(f"WebFetcher failed for {topic!r}: {exc}") from exc

        if response.status_code != 200:
            logger.warning(
                "WebFetcher: non-200 response %s for %s",
                response.status_code,
                url,
            )
            return []

        try:
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove boilerplate elements in-place.
            for tag in soup.find_all(_BOILERPLATE_TAGS):
                tag.decompose()

            # Gather paragraph text.
            text = " ".join(
                p.get_text(strip=True)
                for p in soup.find_all("p")
                if p.get_text(strip=True)
            )

            # Truncate to avoid oversized documents.
            text = text[:_MAX_CHARS]

            # Build a filesystem-safe slug from the URL.
            url_slug = re.sub(r"[^a-zA-Z0-9]", "_", url.removeprefix("https://"))
            url_slug = url_slug[:50]

            filename = f"web_{url_slug}.txt"
            path = self._write_doc(filename, text)
            return [path]
        except Exception as exc:
            raise FetcherError(f"WebFetcher failed for {topic!r}: {exc}") from exc


if __name__ == "__main__":
    import sys
    from datetime import date

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    run_id = f"standalone_{date.today()}"
    paths = WebFetcher(run_id).fetch(url)
    for p in paths:
        print(p)
    if not paths:
        print("(no output — URL may have been unreachable)")
