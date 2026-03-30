"""Tests for updown.polymarket_ws -- REST bootstrap seeding.

Proves that ``seed_book_from_rest()`` populates the internal order book
so that ``get_yes_price()`` returns a real value (not None and not the
0.500 default) without requiring a live service or WebSocket connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from updown.polymarket_ws import PolymarketWSClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN_ID = "abc123-fake-token-id-for-testing"

# Realistic CLOB REST /book response: best bid 0.62, best ask 0.64.
# Expected mid-price: (0.62 + 0.64) / 2 = 0.63
MOCK_BOOK_RESPONSE = {
    "bids": [
        {"price": "0.62", "size": "150"},
        {"price": "0.60", "size": "300"},
    ],
    "asks": [
        {"price": "0.64", "size": "200"},
        {"price": "0.66", "size": "100"},
    ],
}


def _make_mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeedBookFromRest:
    """Verify that REST seeding populates the book with real prices."""

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_sets_non_default_yes_price(self, mock_get: MagicMock) -> None:
        """After seed_book_from_rest(), get_yes_price() must return a value
        that is not None and not the 0.500 default -- proving the REST
        bootstrap path works."""
        mock_get.return_value = _make_mock_response(MOCK_BOOK_RESPONSE)

        client = PolymarketWSClient()

        # Before seeding: no data for this token.
        assert client.get_yes_price(FAKE_TOKEN_ID) is None

        # Seed from mocked REST.
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)

        # Core assertion: price is real, not None, and not the 0.500 default.
        assert yes_price is not None, "get_yes_price() returned None after REST seed"
        assert yes_price != 0.500, (
            f"get_yes_price() returned 0.500 (the default) after REST seed; "
            f"expected a real price from the mocked response"
        )

        # Verify the exact mid-price: (0.62 + 0.64) / 2 = 0.63
        assert yes_price == pytest.approx(0.63, abs=1e-6), (
            f"Expected mid-price 0.63, got {yes_price}"
        )

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_sets_correct_no_price(self, mock_get: MagicMock) -> None:
        """NO price should be 1 - YES price after REST seeding."""
        mock_get.return_value = _make_mock_response(MOCK_BOOK_RESPONSE)

        client = PolymarketWSClient()
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        no_price = client.get_no_price(FAKE_TOKEN_ID)
        assert no_price is not None
        assert no_price == pytest.approx(0.37, abs=1e-6)

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_populates_book_metadata(self, mock_get: MagicMock) -> None:
        """The internal _AssetBook should have bid/ask prices and a
        non-zero last_update_ms after seeding."""
        mock_get.return_value = _make_mock_response(MOCK_BOOK_RESPONSE)

        client = PolymarketWSClient()
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book is not None
        assert book.best_bid.price == pytest.approx(0.62)
        assert book.best_ask.price == pytest.approx(0.64)
        assert book.best_bid.size == pytest.approx(150.0)
        assert book.best_ask.size == pytest.approx(200.0)
        assert book.last_update_ms > 0

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_bid_only_returns_bid_as_price(self, mock_get: MagicMock) -> None:
        """When only bids are present (no asks), mid-price falls back to
        the best bid."""
        mock_get.return_value = _make_mock_response({
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [],
        })

        client = PolymarketWSClient()
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price is not None
        assert yes_price != 0.500
        assert yes_price == pytest.approx(0.55)

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_ask_only_returns_ask_as_price(self, mock_get: MagicMock) -> None:
        """When only asks are present (no bids), mid-price falls back to
        the best ask."""
        mock_get.return_value = _make_mock_response({
            "bids": [],
            "asks": [{"price": "0.71", "size": "50"}],
        })

        client = PolymarketWSClient()
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price is not None
        assert yes_price != 0.500
        assert yes_price == pytest.approx(0.71)

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_failure_does_not_crash(self, mock_get: MagicMock) -> None:
        """A network error during REST seed should be handled gracefully --
        get_yes_price() returns None (not 0.500) and no exception propagates."""
        mock_get.side_effect = ConnectionError("mocked network failure")

        client = PolymarketWSClient()
        # Should not raise.
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        # Price remains unknown (None), not a stale default.
        assert client.get_yes_price(FAKE_TOKEN_ID) is None

    @patch("updown.polymarket_ws.requests.get")
    def test_seed_calls_correct_rest_endpoint(self, mock_get: MagicMock) -> None:
        """Verify the REST call targets the /book endpoint with the
        correct token_id parameter."""
        mock_get.return_value = _make_mock_response(MOCK_BOOK_RESPONSE)

        client = PolymarketWSClient()
        client.seed_book_from_rest(FAKE_TOKEN_ID)

        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "/book" in call_args[0][0] or "/book" in str(call_args)
        assert call_args[1]["params"]["token_id"] == FAKE_TOKEN_ID
