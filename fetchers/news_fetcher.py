"""NewsFetcher — retrieves recent news articles via the Tavily Search API.

Graceful degradation:
- If tavily-python is not installed, returns [] with a WARNING log.
- If TAVILY_API_KEY is absent, returns [] with a WARNING log.
- Only unexpected API errors surface as FetcherError.
"""

import logging
from pathlib import Path

try:
    from tavily import TavilyClient
    _TAVILY_AVAILABLE = True
except ImportError:
    _TAVILY_AVAILABLE = False
    TavilyClient = None

from fetchers.base_fetcher import BaseFetcher, FetcherError


class NewsFetcher(BaseFetcher):
    """Fetches news articles for a given topic from the Tavily Search API."""

    def fetch(self, topic: str) -> list[Path]:
        """Search for news articles on *topic* and write each result as a file.

        Returns a list of :class:`~pathlib.Path` objects for the written files.
        Returns an empty list (without raising) when the Tavily package or API
        key is absent.  Raises :class:`~fetchers.base_fetcher.FetcherError` for
        unexpected API failures.
        """
        import config

        if not _TAVILY_AVAILABLE:
            logging.warning("NewsFetcher: tavily-python not installed, skipping")
            return []

        if not config.TAVILY_API_KEY:
            logging.warning("NewsFetcher: TAVILY_API_KEY not set, skipping")
            return []

        try:
            client = TavilyClient(api_key=config.TAVILY_API_KEY)
            results = client.search(query=topic, max_results=config.NEWS_MAX_RESULTS)
        except Exception as e:
            raise FetcherError(
                f"NewsFetcher Tavily error for {topic!r}: {e}"
            ) from e

        topic_slug = topic.lower().replace(" ", "_")[:40]
        paths: list[Path] = []

        for n, r in enumerate(results.get("results", [])):
            content = (
                f"Title: {r.get('title', '')}\n"
                f"URL: {r.get('url', '')}\n\n"
                f"{r.get('content', '')[:2000]}"
            )
            filename = f"news_{topic_slug}_{n}.txt"
            path = self._write_doc(filename, content)
            paths.append(path)

        return paths


if __name__ == "__main__":
    import sys
    from datetime import date

    topic = sys.argv[1] if len(sys.argv) > 1 else "inflation"
    run_id = f"standalone_{date.today()}"
    paths = NewsFetcher(run_id).fetch(topic)
    for p in paths:
        print(p)
    if not paths:
        print("(no files written — TAVILY_API_KEY may be absent)")
