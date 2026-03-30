"""Tests for updown/loop.py helper functions.

Covers:
- drain_to_latest() -- queue draining with backpressure
- _build_tick_contexts() -- TickContext assembly from tracked markets
- _handle_market_resolved() -- market removal and WS unsubscription
- TrackedMarket.has_open_position property

The run() coroutine is NOT tested here (integration-level).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from updown.loop import (
    TrackedMarket,
    _build_tick_contexts,
    _handle_market_resolved,
    drain_to_latest,
)
from updown.types import MarketState, PriceUpdate

# Import factories from conftest (available via conftest.py in tests/)
from updown.tests.conftest import make_strategy_config, make_tracked_market


# ═══════════════════════════════════════════════════════════════════════════
# drain_to_latest()
# ═══════════════════════════════════════════════════════════════════════════


class TestDrainToLatest:
    """Tests for drain_to_latest(): consumes all queued ticks, returns latest."""

    def _make_tick(self, price: float, ts_ms: int = 0) -> PriceUpdate:
        return PriceUpdate(symbol="BTCUSDT", price=price, timestamp_ms=ts_ms)

    def test_empty_queue_returns_current_tick(self):
        """When queue is empty, returns current_tick unchanged with count 0."""
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        current = self._make_tick(67_000.0, 1000)

        latest, drained = drain_to_latest(queue, current)

        assert latest is current
        assert drained == 0

    def test_single_item_in_queue(self):
        """Queue with one item: returns that item with count 1."""
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        current = self._make_tick(67_000.0, 1000)
        queued = self._make_tick(67_100.0, 2000)
        queue.put_nowait(queued)

        latest, drained = drain_to_latest(queue, current)

        assert latest is queued
        assert drained == 1
        assert queue.empty()

    def test_multiple_items_returns_last(self):
        """Queue with multiple items: returns the last one enqueued."""
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        current = self._make_tick(67_000.0, 1000)

        ticks = [
            self._make_tick(67_100.0, 2000),
            self._make_tick(67_200.0, 3000),
            self._make_tick(67_300.0, 4000),
        ]
        for t in ticks:
            queue.put_nowait(t)

        latest, drained = drain_to_latest(queue, current)

        assert latest is ticks[-1]
        assert latest.price == 67_300.0
        assert drained == 3
        assert queue.empty()

    def test_drain_count_matches_queue_depth(self):
        """Drain count equals the number of items consumed from the queue."""
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        current = self._make_tick(60_000.0)

        n = 100
        for i in range(n):
            queue.put_nowait(self._make_tick(60_000.0 + i))

        _, drained = drain_to_latest(queue, current)

        assert drained == n
        assert queue.empty()

    def test_latest_tick_never_none(self):
        """Return value is always a PriceUpdate, never None."""
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        current = self._make_tick(50_000.0)

        latest, _ = drain_to_latest(queue, current)

        assert latest is not None
        assert isinstance(latest, PriceUpdate)


# ═══════════════════════════════════════════════════════════════════════════
# _build_tick_contexts()
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildTickContexts:
    """Tests for _build_tick_contexts(): assembles TickContext per tracked market."""

    def _mock_polymarket(
        self,
        *,
        yes_prices: dict[str, float | None] | None = None,
        no_prices: dict[str, float | None] | None = None,
        price_ages: dict[str, int | None] | None = None,
    ) -> MagicMock:
        """Build a mock PolymarketWSClient with configurable price lookups."""
        yes_prices = yes_prices or {}
        no_prices = no_prices or {}
        price_ages = price_ages or {}

        mock = MagicMock()
        mock.get_yes_price.side_effect = lambda aid: yes_prices.get(aid)
        mock.get_no_price.side_effect = lambda aid: no_prices.get(aid)
        mock.get_price_age_ms.side_effect = lambda aid: price_ages.get(aid)
        return mock

    def test_basic_context_assembly(self):
        """Produces correct TickContext for a tracked market with known prices."""
        token = "token_abc"
        cond_id = "0xcond1"
        tracked = make_tracked_market(
            condition_id=cond_id,
            question="Will BTC go up?",
            asset_ids=[token],
            expiry_time=1_700_000_300.0,
            state=MarketState.IDLE,
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={token: 0.55},
            no_prices={token: 0.45},
            price_ages={token: 150},
        )
        cfg = make_strategy_config()

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_500.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=cfg,
        )

        assert cond_id in contexts
        ctx = contexts[cond_id]
        assert ctx.tick_price == 67_500.0
        assert ctx.open_price == 67_000.0
        assert ctx.yes_price == 0.55
        assert ctx.no_price == 0.45
        assert ctx.price_age_ms == 150
        assert ctx.market_id == cond_id
        assert ctx.question == "Will BTC go up?"
        assert ctx.token_id == token
        assert ctx.expiry_time == 1_700_000_300.0
        assert ctx.state == MarketState.IDLE
        assert ctx.tick_timestamp_ms == 1_700_000_000_000
        assert ctx.strategy_config is cfg

    def test_skips_market_with_no_yes_price(self):
        """Markets whose primary asset has no YES price are silently skipped."""
        token = "token_no_price"
        cond_id = "0xcond_no_price"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=[token],
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={},  # no YES price for this token
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        assert cond_id not in contexts
        assert len(contexts) == 0

    def test_no_price_derived_from_yes_when_missing(self):
        """When NO price is None, it is derived as 1.0 - yes_price."""
        token = "token_no_no"
        cond_id = "0xcond_derived_no"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=[token],
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={token: 0.60},
            no_prices={},  # no NO price
            price_ages={token: 50},
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        ctx = contexts[cond_id]
        assert ctx.yes_price == 0.60
        assert ctx.no_price == pytest.approx(0.40)

    def test_price_age_defaults_to_large_value_when_none(self):
        """When price_age_ms is None, context receives 999_999."""
        token = "token_no_age"
        cond_id = "0xcond_no_age"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=[token],
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={token: 0.50},
            price_ages={},  # no age info
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        assert contexts[cond_id].price_age_ms == 999_999

    def test_populates_entry_fields_from_tracked_market(self):
        """Entry fields (price, time, side, size) are copied from TrackedMarket."""
        token = "token_entered"
        cond_id = "0xcond_entered"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=[token],
            state=MarketState.ENTERED,
            entry_price=0.48,
            entry_time=1_699_999_900.0,
            entry_side="yes",
            entry_size_usdc=5.0,
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={token: 0.52},
            no_prices={token: 0.48},
            price_ages={token: 200},
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_500.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=make_strategy_config(),
        )

        ctx = contexts[cond_id]
        assert ctx.state == MarketState.ENTERED
        assert ctx.entry_price == 0.48
        assert ctx.entry_time == 1_699_999_900.0
        assert ctx.entry_side == "yes"
        assert ctx.entry_size_usdc == 5.0

    def test_multiple_markets_mixed(self):
        """Multiple markets: one with price, one without -- only priced one included."""
        t1, t2 = "token_a", "token_b"
        c1, c2 = "0xcond_a", "0xcond_b"

        tracked_markets = {
            c1: make_tracked_market(condition_id=c1, asset_ids=[t1]),
            c2: make_tracked_market(condition_id=c2, asset_ids=[t2]),
        }

        poly = self._mock_polymarket(
            yes_prices={t1: 0.55},  # t2 has no price
            no_prices={t1: 0.45},
            price_ages={t1: 100},
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        assert c1 in contexts
        assert c2 not in contexts

    def test_empty_asset_ids_skipped(self):
        """Market with empty asset_ids list is skipped."""
        cond_id = "0xcond_empty"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=[],
        )
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket()

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        assert len(contexts) == 0

    def test_strategy_config_none_propagated(self):
        """When strategy_config is None, TickContext.strategy_config is None."""
        token = "token_nocfg"
        cond_id = "0xcond_nocfg"
        tracked = make_tracked_market(condition_id=cond_id, asset_ids=[token])
        tracked_markets = {cond_id: tracked}

        poly = self._mock_polymarket(
            yes_prices={token: 0.50},
            price_ages={token: 50},
        )

        contexts = _build_tick_contexts(
            tracked_markets, poly,
            tick_price=67_000.0,
            open_price=67_000.0,
            exchange_now_ms=1_700_000_000_000,
            strategy_config=None,
        )

        assert contexts[cond_id].strategy_config is None


# ═══════════════════════════════════════════════════════════════════════════
# _handle_market_resolved()
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleMarketResolved:
    """Tests for _handle_market_resolved(): removes market and unsubscribes."""

    def _mock_polymarket(self) -> MagicMock:
        mock = MagicMock()
        mock.unsubscribe = MagicMock()
        return mock

    def test_removes_market_from_dict(self):
        """Resolved market is removed from tracked_markets dict."""
        cond_id = "0xresolved"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=["token_r1"],
        )
        tracked_markets = {cond_id: tracked}
        poly = self._mock_polymarket()

        _handle_market_resolved(cond_id, tracked_markets, poly)

        assert cond_id not in tracked_markets
        assert len(tracked_markets) == 0

    def test_unsubscribes_all_asset_ids(self):
        """All asset_ids of the resolved market are unsubscribed."""
        cond_id = "0xresolved_multi"
        tracked = make_tracked_market(
            condition_id=cond_id,
            asset_ids=["token_a", "token_b", "token_c"],
        )
        tracked_markets = {cond_id: tracked}
        poly = self._mock_polymarket()

        _handle_market_resolved(cond_id, tracked_markets, poly)

        assert poly.unsubscribe.call_count == 3
        poly.unsubscribe.assert_any_call("token_a")
        poly.unsubscribe.assert_any_call("token_b")
        poly.unsubscribe.assert_any_call("token_c")

    def test_missing_condition_id_is_noop(self):
        """When condition_id is not in dict, does nothing (no crash)."""
        other_id = "0xother"
        tracked = make_tracked_market(
            condition_id=other_id,
            asset_ids=["token_x"],
        )
        tracked_markets = {other_id: tracked}
        poly = self._mock_polymarket()

        # This condition_id is not in the dict
        _handle_market_resolved("0xnonexistent", tracked_markets, poly)

        # Dict unchanged, no unsubscribe called
        assert other_id in tracked_markets
        assert len(tracked_markets) == 1
        poly.unsubscribe.assert_not_called()

    def test_empty_dict_is_graceful(self):
        """Calling with empty tracked_markets dict does not crash."""
        tracked_markets: dict[str, TrackedMarket] = {}
        poly = self._mock_polymarket()

        _handle_market_resolved("0xghost", tracked_markets, poly)

        poly.unsubscribe.assert_not_called()

    def test_other_markets_untouched(self):
        """Only the target market is removed; others remain."""
        c1, c2 = "0xmarket_a", "0xmarket_b"
        tracked_markets = {
            c1: make_tracked_market(condition_id=c1, asset_ids=["t1"]),
            c2: make_tracked_market(condition_id=c2, asset_ids=["t2"]),
        }
        poly = self._mock_polymarket()

        _handle_market_resolved(c1, tracked_markets, poly)

        assert c1 not in tracked_markets
        assert c2 in tracked_markets
        # Only t1 was unsubscribed
        poly.unsubscribe.assert_called_once_with("t1")


# ═══════════════════════════════════════════════════════════════════════════
# TrackedMarket.has_open_position
# ═══════════════════════════════════════════════════════════════════════════


class TestTrackedMarketHasOpenPosition:
    """Tests for TrackedMarket.has_open_position property."""

    def test_entered_state_returns_true(self):
        """has_open_position is True when state is ENTERED."""
        tm = make_tracked_market(state=MarketState.ENTERED)
        assert tm.has_open_position is True

    def test_idle_state_returns_false(self):
        """has_open_position is False when state is IDLE."""
        tm = make_tracked_market(state=MarketState.IDLE)
        assert tm.has_open_position is False

    def test_entering_state_returns_false(self):
        """has_open_position is False when state is ENTERING (order in flight)."""
        tm = make_tracked_market(state=MarketState.ENTERING)
        assert tm.has_open_position is False

    def test_exiting_state_returns_false(self):
        """has_open_position is False when state is EXITING."""
        tm = make_tracked_market(state=MarketState.EXITING)
        assert tm.has_open_position is False

    def test_cooldown_state_returns_false(self):
        """has_open_position is False when state is COOLDOWN."""
        tm = make_tracked_market(state=MarketState.COOLDOWN)
        assert tm.has_open_position is False

    @pytest.mark.parametrize(
        "state",
        [s for s in MarketState if s != MarketState.ENTERED],
        ids=lambda s: s.value,
    )
    def test_all_non_entered_states_return_false(self, state: MarketState):
        """Exhaustive: every state except ENTERED returns False."""
        tm = make_tracked_market(state=state)
        assert tm.has_open_position is False
