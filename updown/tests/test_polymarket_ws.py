"""Tests for updown.polymarket_ws -- Polymarket CLOB websocket client.

Covers:
- REST bootstrap seeding (seed_book_from_rest) -- updated from the original
  synchronous tests to match the current async aiohttp-based production API
- _parse_message() handling book and price_change events
- _mid_price() preference order (server_mid > local mid > bid-only > ask-only > None)
- subscribe() / unsubscribe() bookkeeping
- _handle_book() wide-spread guard (>50% should not clobber)
- _backoff_delay() exponential with cap
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updown.polymarket_ws import PolymarketWSClient, _AssetBook, _BookSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_TOKEN_ID = "abc123-fake-token-id-for-testing"
FAKE_TOKEN_ID_2 = "def456-second-token-id"

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


def _mock_aiohttp_session(
    book_response: dict | None = None,
    midpoint_response: dict | None = None,
    book_error: Exception | None = None,
) -> MagicMock:
    """Build a mock aiohttp.ClientSession for seed_book_from_rest()."""
    session = MagicMock()

    def _make_ctx(url: str, **kwargs) -> MagicMock:
        ctx = MagicMock()
        resp = MagicMock()

        if book_error is not None and "/book" in str(url):
            resp.raise_for_status = MagicMock(side_effect=book_error)
            resp.json = AsyncMock(return_value={})
        elif "/midpoint" in str(url):
            resp.raise_for_status = MagicMock()
            resp.json = AsyncMock(return_value=midpoint_response or {"mid": "0"})
        elif "/book" in str(url):
            resp.raise_for_status = MagicMock()
            resp.json = AsyncMock(return_value=book_response or MOCK_BOOK_RESPONSE)
        else:
            resp.raise_for_status = MagicMock()
            resp.json = AsyncMock(return_value={})

        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.get = MagicMock(side_effect=_make_ctx)
    return session


# ═══════════════════════════════════════════════════════════════════════════
# Original seed_book_from_rest tests -- updated to async aiohttp API
# ═══════════════════════════════════════════════════════════════════════════


class TestSeedBookFromRest:
    """Verify that REST seeding populates the book with real prices.

    These tests were originally written against a synchronous requests.get
    API and have been updated to match the current async aiohttp-based
    seed_book_from_rest(token_id, session) signature.
    """

    @pytest.mark.asyncio
    async def test_seed_sets_non_default_yes_price(self) -> None:
        """After seed_book_from_rest(), get_yes_price() must return a value
        that is not None and not the 0.500 default."""
        session = _mock_aiohttp_session()
        client = PolymarketWSClient()

        assert client.get_yes_price(FAKE_TOKEN_ID) is None

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price is not None, "get_yes_price() returned None after REST seed"
        assert yes_price != 0.500
        assert yes_price == pytest.approx(0.63, abs=1e-6)

    @pytest.mark.asyncio
    async def test_seed_sets_correct_no_price(self) -> None:
        """NO price should be 1 - YES price after REST seeding."""
        session = _mock_aiohttp_session()
        client = PolymarketWSClient()

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        no_price = client.get_no_price(FAKE_TOKEN_ID)
        assert no_price is not None
        assert no_price == pytest.approx(0.37, abs=1e-6)

    @pytest.mark.asyncio
    async def test_seed_populates_book_metadata(self) -> None:
        """The internal _AssetBook should have bid/ask prices and a
        non-zero last_update_ms after seeding."""
        session = _mock_aiohttp_session()
        client = PolymarketWSClient()

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book is not None
        assert book.best_bid.price == pytest.approx(0.62)
        assert book.best_ask.price == pytest.approx(0.64)
        assert book.best_bid.size == pytest.approx(150.0)
        assert book.best_ask.size == pytest.approx(200.0)
        assert book.last_update_ms > 0

    @pytest.mark.asyncio
    async def test_seed_bid_only_returns_bid_as_price(self) -> None:
        """When only bids are present, mid-price falls back to best bid."""
        session = _mock_aiohttp_session(book_response={
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [],
        })
        client = PolymarketWSClient()

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price is not None
        assert yes_price == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_seed_ask_only_returns_ask_as_price(self) -> None:
        """When only asks are present, mid-price falls back to best ask."""
        session = _mock_aiohttp_session(book_response={
            "bids": [],
            "asks": [{"price": "0.71", "size": "50"}],
        })
        client = PolymarketWSClient()

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price is not None
        assert yes_price == pytest.approx(0.71)

    @pytest.mark.asyncio
    async def test_seed_failure_does_not_crash(self) -> None:
        """A network error during REST seed should be handled gracefully."""
        session = MagicMock()
        # Make session.get raise on every call to simulate total failure.
        session.get = MagicMock(side_effect=ConnectionError("mocked failure"))

        client = PolymarketWSClient()
        # Should not raise -- seed_book_from_rest catches all exceptions.
        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        assert client.get_yes_price(FAKE_TOKEN_ID) is None

    @pytest.mark.asyncio
    async def test_seed_with_server_midpoint(self) -> None:
        """When /midpoint returns a valid mid, it should be preferred."""
        session = _mock_aiohttp_session(
            book_response=MOCK_BOOK_RESPONSE,
            midpoint_response={"mid": "0.635"},
        )
        client = PolymarketWSClient()

        await client.seed_book_from_rest(FAKE_TOKEN_ID, session)

        # Server mid (0.635) should take precedence over local (0.63).
        yes_price = client.get_yes_price(FAKE_TOKEN_ID)
        assert yes_price == pytest.approx(0.635)


# ═══════════════════════════════════════════════════════════════════════════
# _parse_message tests
# ═══════════════════════════════════════════════════════════════════════════


class TestParseMessage:
    """Verify _parse_message() routes events correctly."""

    def test_book_event_updates_book(self) -> None:
        """A 'book' event should update the internal book state."""
        client = PolymarketWSClient()
        msg = json.dumps({
            "event_type": "book",
            "asset_id": FAKE_TOKEN_ID,
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "200"}],
        })

        client._parse_message(msg)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book is not None
        assert book.best_bid.price == pytest.approx(0.55)
        assert book.best_ask.price == pytest.approx(0.60)

    def test_price_change_event_updates_book(self) -> None:
        """A 'price_change' event should update bid/ask for subscribed assets."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)

        msg = json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": FAKE_TOKEN_ID, "best_bid": "0.52", "best_ask": "0.58"},
            ],
        })

        client._parse_message(msg)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book is not None
        assert book.best_bid.price == pytest.approx(0.52)
        assert book.best_ask.price == pytest.approx(0.58)

    def test_price_change_ignores_unsubscribed_asset(self) -> None:
        """price_change events for unsubscribed assets should be ignored."""
        client = PolymarketWSClient()
        # Do NOT subscribe to FAKE_TOKEN_ID.

        msg = json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": FAKE_TOKEN_ID, "best_bid": "0.50", "best_ask": "0.60"},
            ],
        })

        client._parse_message(msg)

        assert client.get_book(FAKE_TOKEN_ID) is None

    def test_price_change_clears_server_mid(self) -> None:
        """A price_change event should clear server_mid (WS data is fresher)."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)

        # Pre-populate with a server_mid.
        client._books[FAKE_TOKEN_ID] = _AssetBook(server_mid=0.70)

        msg = json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": FAKE_TOKEN_ID, "best_bid": "0.52", "best_ask": "0.58"},
            ],
        })

        client._parse_message(msg)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book.server_mid is None
        # Should use local mid = (0.52 + 0.58) / 2 = 0.55.
        assert client.get_yes_price(FAKE_TOKEN_ID) == pytest.approx(0.55)

    def test_unparseable_json_ignored(self) -> None:
        """Malformed JSON should not raise."""
        client = PolymarketWSClient()
        client._parse_message("{{not valid json")
        # No assertion needed -- just verifying no exception.

    def test_unknown_event_type_ignored(self) -> None:
        """Unknown event types should be silently ignored."""
        client = PolymarketWSClient()
        msg = json.dumps({"event_type": "heartbeat_ack", "ts": 12345})

        client._parse_message(msg)
        # Should not modify any state.

    def test_list_of_events(self) -> None:
        """The WS may send a list of events in one message."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)
        client.subscribe(FAKE_TOKEN_ID_2)

        msg = json.dumps([
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": FAKE_TOKEN_ID, "best_bid": "0.40", "best_ask": "0.50"},
                ],
            },
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": FAKE_TOKEN_ID_2, "best_bid": "0.60", "best_ask": "0.70"},
                ],
            },
        ])

        client._parse_message(msg)

        assert client.get_yes_price(FAKE_TOKEN_ID) == pytest.approx(0.45)
        assert client.get_yes_price(FAKE_TOKEN_ID_2) == pytest.approx(0.65)


# ═══════════════════════════════════════════════════════════════════════════
# _mid_price tests
# ═══════════════════════════════════════════════════════════════════════════


class TestMidPrice:
    """Verify _mid_price() preference order."""

    def test_returns_none_for_unknown_asset(self) -> None:
        """Unknown asset_id should return None."""
        client = PolymarketWSClient()
        assert client._mid_price("nonexistent") is None

    def test_prefers_server_mid(self) -> None:
        """When server_mid is set, it should be returned over local mid."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.40, size=100),
            best_ask=_BookSide(price=0.60, size=100),
            server_mid=0.52,
        )

        assert client._mid_price(FAKE_TOKEN_ID) == 0.52

    def test_local_mid_when_no_server_mid(self) -> None:
        """When server_mid is None, use (bid + ask) / 2."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.40, size=100),
            best_ask=_BookSide(price=0.60, size=100),
        )

        assert client._mid_price(FAKE_TOKEN_ID) == pytest.approx(0.50)

    def test_bid_only_fallback(self) -> None:
        """When only bid is available, return bid."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.45, size=100),
            best_ask=_BookSide(price=0.0, size=0),
        )

        assert client._mid_price(FAKE_TOKEN_ID) == 0.45

    def test_ask_only_fallback(self) -> None:
        """When only ask is available, return ask."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.0, size=0),
            best_ask=_BookSide(price=0.72, size=50),
        )

        assert client._mid_price(FAKE_TOKEN_ID) == 0.72

    def test_returns_none_when_no_data(self) -> None:
        """When both bid and ask are zero and no server_mid, return None."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook()

        assert client._mid_price(FAKE_TOKEN_ID) is None


# ═══════════════════════════════════════════════════════════════════════════
# subscribe / unsubscribe bookkeeping tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSubscribeUnsubscribe:
    """Verify subscribe/unsubscribe state management."""

    def test_subscribe_adds_to_tracked_set(self) -> None:
        """subscribe() should add the asset_id to subscribed_assets."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)

        assert FAKE_TOKEN_ID in client.subscribed_assets

    def test_subscribe_creates_book_entry(self) -> None:
        """subscribe() should initialize a book entry for the asset."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)

        assert client.get_book(FAKE_TOKEN_ID) is not None

    def test_subscribe_idempotent(self) -> None:
        """Subscribing twice should not create duplicate entries."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)
        client.subscribe(FAKE_TOKEN_ID)

        assert len(client.subscribed_assets) == 1

    def test_unsubscribe_removes_from_tracked_set(self) -> None:
        """unsubscribe() should remove the asset from subscribed_assets."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)
        client.unsubscribe(FAKE_TOKEN_ID)

        assert FAKE_TOKEN_ID not in client.subscribed_assets

    def test_unsubscribe_removes_book(self) -> None:
        """unsubscribe() should remove the book entry."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)
        client.unsubscribe(FAKE_TOKEN_ID)

        assert client.get_book(FAKE_TOKEN_ID) is None

    def test_unsubscribe_nonexistent_no_error(self) -> None:
        """Unsubscribing a non-tracked asset should not raise."""
        client = PolymarketWSClient()
        client.unsubscribe("nonexistent_id")  # Should not raise.

    def test_subscribed_assets_returns_copy(self) -> None:
        """subscribed_assets property should return a copy, not the internal set."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)

        assets = client.subscribed_assets
        assets.add("should_not_affect_internal")

        assert "should_not_affect_internal" not in client.subscribed_assets

    def test_multiple_subscriptions(self) -> None:
        """Multiple different assets can be tracked simultaneously."""
        client = PolymarketWSClient()
        client.subscribe(FAKE_TOKEN_ID)
        client.subscribe(FAKE_TOKEN_ID_2)

        assert len(client.subscribed_assets) == 2
        assert FAKE_TOKEN_ID in client.subscribed_assets
        assert FAKE_TOKEN_ID_2 in client.subscribed_assets


# ═══════════════════════════════════════════════════════════════════════════
# _handle_book wide-spread guard tests
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleBookWideSpread:
    """Verify _handle_book() wide-spread guard (>50% should not clobber)."""

    def test_tight_spread_updates_bid_ask(self) -> None:
        """A book snapshot with spread < 50% should update bid/ask."""
        client = PolymarketWSClient()

        event = {
            "event_type": "book",
            "asset_id": FAKE_TOKEN_ID,
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "200"}],
        }

        client._handle_book(event)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book.best_bid.price == pytest.approx(0.55)
        assert book.best_ask.price == pytest.approx(0.60)
        assert book.server_mid is None

    def test_wide_spread_does_not_clobber_existing(self) -> None:
        """A book snapshot with spread >= 50% should NOT overwrite existing bid/ask."""
        client = PolymarketWSClient()

        # Pre-populate with tight prices from a price_change event.
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.55, size=100),
            best_ask=_BookSide(price=0.60, size=200),
        )

        # Wide-spread snapshot (0.01 / 0.99 = spread 0.98 > 0.50).
        event = {
            "event_type": "book",
            "asset_id": FAKE_TOKEN_ID,
            "bids": [{"price": "0.01", "size": "1000"}],
            "asks": [{"price": "0.99", "size": "1000"}],
            "last_trade_price": 0.57,
        }

        client._handle_book(event)

        book = client.get_book(FAKE_TOKEN_ID)
        # Bid/ask should NOT have been clobbered.
        assert book.best_bid.price == pytest.approx(0.55)
        assert book.best_ask.price == pytest.approx(0.60)
        # Instead, last_trade_price should be used as server_mid.
        assert book.server_mid == pytest.approx(0.57)

    def test_wide_spread_without_ltp_no_server_mid(self) -> None:
        """Wide spread without last_trade_price should not set server_mid."""
        client = PolymarketWSClient()

        event = {
            "event_type": "book",
            "asset_id": FAKE_TOKEN_ID,
            "bids": [{"price": "0.01", "size": "1000"}],
            "asks": [{"price": "0.99", "size": "1000"}],
            # No last_trade_price field.
        }

        client._handle_book(event)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book.server_mid is None

    def test_book_event_missing_asset_id_ignored(self) -> None:
        """A book event without an asset_id should be ignored."""
        client = PolymarketWSClient()

        event = {
            "event_type": "book",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        }

        client._handle_book(event)
        # No book should have been created for empty string.
        assert len(client._books) == 0

    def test_tight_spread_clears_server_mid(self) -> None:
        """A tight-spread book snapshot should clear any existing server_mid."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(server_mid=0.70)

        event = {
            "event_type": "book",
            "asset_id": FAKE_TOKEN_ID,
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "200"}],
        }

        client._handle_book(event)

        book = client.get_book(FAKE_TOKEN_ID)
        assert book.server_mid is None
        assert book.best_bid.price == pytest.approx(0.55)


# ═══════════════════════════════════════════════════════════════════════════
# _backoff_delay tests
# ═══════════════════════════════════════════════════════════════════════════


class TestBackoffDelay:
    """Verify exponential backoff with cap."""

    def test_exponential_growth(self) -> None:
        """Delays should grow with attempt number."""
        client = PolymarketWSClient(
            reconnect_base_delay_s=1.0,
            reconnect_max_delay_s=1000.0,
        )

        random.seed(0)

        delays = [client._backoff_delay(i) for i in range(1, 6)]

        # The exponential component grows: 1, 2, 4, 8, 16.
        # With jitter in [0, base], the trend should be upward.
        # Verify the last delay is larger than the first.
        assert delays[-1] > delays[0]

    def test_capped_at_max_delay(self) -> None:
        """Delay should never exceed max_delay."""
        client = PolymarketWSClient(
            reconnect_base_delay_s=1.0,
            reconnect_max_delay_s=10.0,
        )

        for attempt in range(1, 25):
            delay = client._backoff_delay(attempt)
            assert delay <= 10.0

    def test_first_attempt_small(self) -> None:
        """First attempt delay should be bounded by base + jitter."""
        client = PolymarketWSClient(
            reconnect_base_delay_s=2.0,
            reconnect_max_delay_s=60.0,
        )

        for _ in range(50):
            delay = client._backoff_delay(1)
            # attempt 1: exp = 2*2^0 = 2, jitter in [0, 2], total in [0, 4]
            assert 0 <= delay <= 4.0

    def test_delay_is_non_negative(self) -> None:
        """Delay should always be >= 0."""
        client = PolymarketWSClient(
            reconnect_base_delay_s=1.0,
            reconnect_max_delay_s=60.0,
        )

        for attempt in range(1, 50):
            assert client._backoff_delay(attempt) >= 0


# ═══════════════════════════════════════════════════════════════════════════
# get_yes_price / get_no_price public API tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPriceAPI:
    """Verify the public get_yes_price / get_no_price methods."""

    def test_get_no_price_complement(self) -> None:
        """get_no_price should return 1 - yes_price."""
        client = PolymarketWSClient()
        client._books[FAKE_TOKEN_ID] = _AssetBook(
            best_bid=_BookSide(price=0.60, size=100),
            best_ask=_BookSide(price=0.70, size=100),
        )

        no_price = client.get_no_price(FAKE_TOKEN_ID)
        yes_price = client.get_yes_price(FAKE_TOKEN_ID)

        assert yes_price == pytest.approx(0.65)
        assert no_price == pytest.approx(0.35)
        assert yes_price + no_price == pytest.approx(1.0)

    def test_get_no_price_none_when_no_data(self) -> None:
        """get_no_price should return None when no YES data exists."""
        client = PolymarketWSClient()
        assert client.get_no_price("nonexistent") is None

    def test_get_price_age_ms(self) -> None:
        """get_price_age_ms should return ms since last update."""
        client = PolymarketWSClient()
        now_ms = int(time.time() * 1000)
        client._books[FAKE_TOKEN_ID] = _AssetBook(last_update_ms=now_ms - 5000)

        age = client.get_price_age_ms(FAKE_TOKEN_ID)
        assert age is not None
        # Allow 1 second tolerance for test execution time.
        assert 4000 <= age <= 6000

    def test_get_price_age_ms_none_for_unknown(self) -> None:
        """get_price_age_ms should return None for unknown assets."""
        client = PolymarketWSClient()
        assert client.get_price_age_ms("nonexistent") is None
