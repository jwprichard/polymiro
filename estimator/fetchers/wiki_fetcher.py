"""WikiFetcher — retrieves Wikipedia page summaries for a given topic.

Uses the Wikipedia REST API (no API key required).  Falls back to the
MediaWiki search API when the direct page lookup returns 404.
"""

import urllib.parse
from pathlib import Path

import requests

from estimator.fetchers.base_fetcher import BaseFetcher, FetcherError

_REST_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
_SEARCH_BASE = "https://en.wikipedia.org/w/api.php"
_TIMEOUT = 15  # seconds


class WikiFetcher(BaseFetcher):
    """Fetches a Wikipedia page summary for *topic* and writes it as plain text."""

    def fetch(self, topic: str) -> list[Path]:
        """Retrieve the Wikipedia summary for *topic*.

        Returns a list containing the single Path written, or an empty list
        when no matching article is found.  Raises :class:`FetcherError` on
        network failures.
        """
        try:
            result = self._lookup(topic)
            if result is None:
                # Direct lookup failed — try search fallback
                title = self._search(topic)
                if title is None:
                    return []
                result = self._lookup(title)
                if result is None:
                    return []

            title, extract, url = result
            content = f"Title: {title}\nURL: {url}\n\n{extract[:3000]}"

            topic_slug = topic.lower().replace(" ", "_")[:50]
            path = self._write_doc(f"wiki_{topic_slug}.txt", content)
            return [path]

        except requests.RequestException as exc:
            raise FetcherError(f"WikiFetcher failed for {topic!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lookup(self, title: str) -> tuple[str, str, str] | None:
        """Call the REST summary endpoint for *title*.

        Returns ``(title, extract, page_url)`` on success, or ``None`` on 404.
        Raises :class:`requests.RequestException` for other HTTP errors.
        """
        url = f"{_REST_BASE}/{urllib.parse.quote(title, safe='')}"
        response = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "polymiro/1.0"})

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json()
        page_title: str = data["title"]
        extract: str = data.get("extract", "")
        page_url: str = data["content_urls"]["desktop"]["page"]
        return page_title, extract, page_url

    def _search(self, topic: str) -> str | None:
        """Query the MediaWiki search API and return the top result title.

        Returns ``None`` when no results are found.
        Raises :class:`requests.RequestException` on network failure.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": urllib.parse.quote(topic),
            "format": "json",
            "srlimit": "1",
        }
        response = requests.get(
            _SEARCH_BASE,
            params=params,
            timeout=_TIMEOUT,
            headers={"User-Agent": "polymiro/1.0"},
        )
        response.raise_for_status()
        results = response.json().get("query", {}).get("search", [])
        if not results:
            return None
        return results[0]["title"]


if __name__ == "__main__":
    import sys
    from datetime import date

    topic = sys.argv[1] if len(sys.argv) > 1 else "Bitcoin"
    run_id = f"standalone_{date.today()}"
    paths = WikiFetcher(run_id).fetch(topic)
    for p in paths:
        print(p)
