"""Tests for updown.pnl.gamma_client — Gamma API resolution checker."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from updown.pnl.gamma_client import (
    _find_market,
    _parse_resolution,
    check_resolution,
)

# Reuse the conftest factory for Gamma responses.
from updown.tests.conftest import mock_gamma_response


# ═══════════════════════════════════════════════════════════════════════════
# _find_market
# ═══════════════════════════════════════════════════════════════════════════


class TestFindMarket:
    """Tests for _find_market() matching logic."""

    def test_exact_match_by_conditionId(self):
        records = [
            {"conditionId": "0xabc123", "question": "Q1"},
            {"conditionId": "0xdef456", "question": "Q2"},
        ]
        result = _find_market(records, "0xabc123")
        assert result is not None
        assert result["question"] == "Q1"

    def test_case_insensitive_match(self):
        records = [{"conditionId": "0xABC123"}]
        assert _find_market(records, "0xabc123") is not None

    def test_match_using_condition_id_field(self):
        """Gamma may return snake_case field name."""
        records = [{"condition_id": "0xabc123"}]
        assert _find_market(records, "0xabc123") is not None

    def test_no_match_returns_single_record_fallback(self):
        """When there's only one record and no conditionId match, trust the API."""
        records = [{"conditionId": "0xother", "question": "only one"}]
        result = _find_market(records, "0xabc123")
        assert result is not None
        assert result["question"] == "only one"

    def test_no_match_multiple_records_returns_none(self):
        records = [
            {"conditionId": "0xother1"},
            {"conditionId": "0xother2"},
        ]
        assert _find_market(records, "0xabc123") is None

    def test_empty_records(self):
        # Edge case: single-element fallback doesn't apply to empty list
        assert _find_market([], "0xabc123") is None


# ═══════════════════════════════════════════════════════════════════════════
# _parse_resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestParseResolution:
    """Tests for _parse_resolution() with settled and unsettled prices."""

    def test_resolved_yes(self):
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
            outcome_prices=["1", "0"],
        )
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": True, "outcome": "Yes"}

    def test_resolved_no(self):
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
            outcome_prices=["0", "1"],
        )
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": True, "outcome": "No"}

    def test_unresolved_open_market(self):
        market = mock_gamma_response(closed=False, accepting_orders=True)
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}

    def test_closed_but_still_accepting_orders(self):
        """Edge: closed=True but acceptingOrders=True -> unresolved."""
        market = mock_gamma_response(
            closed=True, accepting_orders=True,
            outcome_prices=["1", "0"],
        )
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}

    def test_closed_but_prices_not_settled(self):
        """Prices haven't fully settled to 0/1 -> unresolved."""
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
            outcome_prices=["0.8", "0.2"],
        )
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}

    def test_outcome_prices_as_json_string(self):
        """outcomePrices may be a JSON-encoded string."""
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
        )
        market["outcomePrices"] = '["1", "0"]'
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": True, "outcome": "Yes"}

    def test_invalid_outcome_prices_string(self):
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
        )
        market["outcomePrices"] = "not-json"
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}

    def test_missing_outcome_prices(self):
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
        )
        market["outcomePrices"] = []
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}

    def test_non_numeric_prices(self):
        market = mock_gamma_response(
            closed=True, accepting_orders=False,
            outcome_prices=["abc", "def"],
        )
        result = _parse_resolution(market, "0xtest")
        assert result == {"resolved": False, "outcome": None}


# ═══════════════════════════════════════════════════════════════════════════
# check_resolution — integration with HTTP mocking
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckResolution:
    """Tests for check_resolution() with mocked requests.get."""

    def _mock_response(self, *, status_code=200, json_data=None, text="", raise_for_status=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text or json.dumps(json_data or [])
        if json_data is not None:
            resp.json.return_value = json_data
        else:
            resp.json.side_effect = ValueError("No JSON")
        return resp

    @patch("updown.pnl.gamma_client.requests.get")
    def test_resolved_yes_market(self, mock_get):
        record = mock_gamma_response(
            condition_id="0xabc",
            closed=True,
            accepting_orders=False,
            outcome_prices=["1", "0"],
        )
        mock_get.return_value = self._mock_response(json_data=[record])

        result = check_resolution("0xabc")
        assert result == {"resolved": True, "outcome": "Yes"}

    @patch("updown.pnl.gamma_client.requests.get")
    def test_resolved_no_market(self, mock_get):
        record = mock_gamma_response(
            condition_id="0xabc",
            closed=True,
            accepting_orders=False,
            outcome_prices=["0", "1"],
        )
        mock_get.return_value = self._mock_response(json_data=[record])

        result = check_resolution("0xabc")
        assert result == {"resolved": True, "outcome": "No"}

    @patch("updown.pnl.gamma_client.requests.get")
    def test_unresolved_market(self, mock_get):
        record = mock_gamma_response(
            condition_id="0xabc",
            closed=False,
            accepting_orders=True,
        )
        mock_get.return_value = self._mock_response(json_data=[record])

        result = check_resolution("0xabc")
        assert result is not None
        assert result["resolved"] is False

    @patch("updown.pnl.gamma_client.requests.get")
    def test_http_error_returns_none(self, mock_get):
        mock_get.return_value = self._mock_response(
            status_code=500, text="Internal Server Error"
        )
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_connection_error_returns_none(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_timeout_returns_none(self, mock_get):
        mock_get.side_effect = requests.Timeout("timed out")
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_malformed_json_returns_none(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        resp.text = "not json at all"
        mock_get.return_value = resp
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_empty_array_returns_none(self, mock_get):
        mock_get.return_value = self._mock_response(json_data=[])
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_request_exception_returns_none(self, mock_get):
        mock_get.side_effect = requests.RequestException("generic failure")
        result = check_resolution("0xabc")
        assert result is None

    @patch("updown.pnl.gamma_client.requests.get")
    def test_unexpected_exception_returns_none(self, mock_get):
        """Any unexpected exception is caught by the outer try/except."""
        mock_get.side_effect = RuntimeError("unexpected")
        result = check_resolution("0xabc")
        assert result is None
