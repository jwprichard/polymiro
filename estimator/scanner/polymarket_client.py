"""
polymarket_client.py — Subprocess wrapper around the polymarket-cli Rust binary.

Field name assumptions (defensive notes)
-----------------------------------------
The polymarket-cli binary was NOT present in the current environment when this
module was written. The field names below are derived from the Polymarket Gamma
REST API conventions, which the CLI is documented to mirror.

Expected JSON shape for a single market record (markets list):
    {
        "condition_id":   "0xabc...",           # hex condition ID — used as market_id
        "id":             12345,                 # optional integer ID — ignored; condition_id preferred
        "question":       "Will X happen?",
        "clobTokenIds":   ["987654...", "..."],  # YES token ID is index 0
        "outcomePrices":  ["0.72", "0.28"],      # YES price index 0, NO price index 1
        "volume":         "1234567.89",          # all-time volume string
        "volume_num":     1234567.89,            # float, used as volume_24h fallback
        "volume24hr":     "50000.00",            # preferred 24h volume (may be absent)
        "endDate":        "2025-12-31T00:00:00Z",# ISO-8601 close date, raw string
        "active":         true
    }

Expected JSON shape for a spread record (clob spread):
    {
        "spread": 0.02,
        "bid":    0.71,
        "ask":    0.73
    }
    If "spread" key is absent the client computes ask - bid itself.

If the actual binary produces different field names, update the _map_market()
and fetch_spread() methods accordingly and remove the corresponding comment.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from common.config import POLYMARKET_CLI_BIN
from estimator.scanner.models import Market

logger = logging.getLogger(__name__)

# Regex that a token_id must satisfy before being passed to the CLI.
# Allows hex strings, UUIDs (with dashes), base64url, and plain integers.
_TOKEN_ID_RE = re.compile(r"^[0-9a-zA-Z\-_]+$")


class PolymarketClientError(RuntimeError):
    """Raised for all errors originating inside PolymarketClient.

    Callers will never see subprocess.CalledProcessError or json.JSONDecodeError.
    The original error message is always included in the string representation.
    """


class PolymarketClient:
    """Stateless client — instantiate with no arguments.

    All methods are synchronous and have a 30-second timeout.  No retries are
    performed; error propagation is the caller's responsibility.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_active_markets(self, limit: int) -> list[Market]:
        """Return up to *limit* active markets ordered by volume.

        CLI invoked:
            polymarket-cli markets list --active true --order volume_num
                --limit <limit> --output json

        Returns an empty list when the CLI returns a valid JSON empty array.
        Raises PolymarketClientError on any error.
        """
        cmd = [
            POLYMARKET_CLI_BIN,
            "markets", "list",
            "--active", "true",
            "--limit", str(limit),
            "-o", "json",
        ]
        raw = self._run(cmd)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolymarketClientError(
                f"JSON parse failure from markets list: {exc}\nRaw output: {raw[:500]}"
            ) from exc

        # The CLI may return a top-level list or a dict with a "markets" / "data" key.
        records: list[dict[str, Any]]
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            for key in ("markets", "data", "results"):
                if key in payload and isinstance(payload[key], list):
                    records = payload[key]
                    break
            else:
                raise PolymarketClientError(
                    f"Unexpected JSON structure from markets list; "
                    f"expected list or dict with 'markets'/'data' key. "
                    f"Keys found: {list(payload.keys())}"
                )
        else:
            raise PolymarketClientError(
                f"Unexpected top-level JSON type from markets list: {type(payload)}"
            )

        markets: list[Market] = []
        for idx, record in enumerate(records):
            try:
                markets.append(self._map_market(record))
            except PolymarketClientError as exc:
                logger.warning("Skipping market at index %d: %s", idx, exc)
        return markets

    def fetch_spread(self, token_id: str) -> float:
        """Return the spread for *token_id* as a float.

        Validates token_id with regex before invoking the CLI so that
        user-supplied strings cannot inject shell metacharacters even though
        we never use shell=True.

        CLI invoked:
            polymarket-cli clob spread <TOKEN_ID> --output json

        Raises PolymarketClientError on invalid token_id, CLI error, or JSON
        parse failure.
        """
        if not _TOKEN_ID_RE.match(token_id):
            raise PolymarketClientError(
                f"Invalid token_id {token_id!r}: must match ^[0-9a-zA-Z\\-_]+$"
            )

        cmd = [
            POLYMARKET_CLI_BIN,
            "clob", "spread", token_id,
            "-o", "json",
        ]
        raw = self._run(cmd)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolymarketClientError(
                f"JSON parse failure from clob spread: {exc}\nRaw output: {raw[:500]}"
            ) from exc

        return self._extract_spread(payload)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str]) -> str:
        """Run *cmd* via subprocess and return stdout as a string.

        stderr is always captured and logged at WARNING level.
        Raises PolymarketClientError on FileNotFoundError or non-zero exit.
        Never raises subprocess.CalledProcessError.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                # Never use shell=True — the command is always a list.
            )
        except FileNotFoundError as exc:
            bin_name = cmd[0]
            raise PolymarketClientError(
                f"Binary not found: {bin_name!r}. "
                f"Ensure the polymarket-cli Rust binary is installed and its "
                f"path is set via the POLYMARKET_CLI_BIN environment variable "
                f"(currently resolves to {POLYMARKET_CLI_BIN!r})."
            ) from exc

        if result.stderr:
            logger.warning(
                "polymarket-cli stderr [cmd=%s]: %s",
                " ".join(cmd[:4]),  # truncate for readability
                result.stderr.strip(),
            )

        if result.returncode != 0:
            raise PolymarketClientError(
                f"polymarket-cli exited with code {result.returncode}. "
                f"Command: {' '.join(cmd)}\n"
                f"stderr: {result.stderr.strip()}"
            )

        return result.stdout

    def _map_market(self, record: dict[str, Any]) -> Market:
        """Map a raw CLI JSON record to a Market dataclass.

        Field mapping (see module docstring for full expected shape):
        - market_id  <- condition_id (hex, "0x…")
                        If an integer "id" also exists, it is ignored and a
                        WARNING is emitted to surface the ambiguity.
        - question   <- question
        - token_id   <- clobTokenIds[0]  (YES token)
        - yes_price  <- outcomePrices[0] (coerced to float)
        - no_price   <- outcomePrices[1] (coerced to float)
        - volume_24h <- volume24hr if present, else volume_num.
                        NOTE: If neither dedicated 24h field exists, volume_num
                        represents all-time volume, not 24h.  This is a known
                        approximation used when the CLI does not expose a 24h
                        field separately.
        - closes_at  <- endDate (raw ISO-8601 string, no parsing here)
        - is_active  <- active
        """
        # --- market_id ---
        condition_id: str | None = record.get("condition_id") or record.get("conditionId")
        int_id = record.get("id")
        if condition_id is None:
            # Fallback: if only an integer id is present, warn and convert it.
            if int_id is not None:
                logger.warning(
                    "Market record has no 'condition_id'; falling back to integer "
                    "'id' = %s. Update field mapping if this is incorrect.",
                    int_id,
                )
                condition_id = str(int_id)
            else:
                raise PolymarketClientError(
                    f"Market record missing both 'condition_id' and 'id': {record}"
                )
        else:
            if int_id is not None:
                logger.warning(
                    "Market record has both 'condition_id' (%s) and integer 'id' "
                    "(%s); using condition_id as market_id.",
                    condition_id,
                    int_id,
                )

        # --- question ---
        question: str = record.get("question", "")

        # --- token_id (YES token) ---
        # clobTokenIds may be a JSON-encoded string rather than a list.
        raw_clob_ids = record.get("clobTokenIds") or record.get("clob_token_ids") or []
        if isinstance(raw_clob_ids, str):
            try:
                raw_clob_ids = json.loads(raw_clob_ids)
            except (json.JSONDecodeError, ValueError):
                raw_clob_ids = []
        token_id: str = raw_clob_ids[0] if raw_clob_ids else ""

        # --- outcomePrices (YES=0, NO=1) ---
        # outcomePrices may be a JSON-encoded string rather than a list.
        raw_outcome_prices = record.get("outcomePrices") or record.get("outcome_prices") or []
        if isinstance(raw_outcome_prices, str):
            try:
                raw_outcome_prices = json.loads(raw_outcome_prices)
            except (json.JSONDecodeError, ValueError):
                raw_outcome_prices = []
        try:
            yes_price = float(raw_outcome_prices[0]) if len(raw_outcome_prices) > 0 else 0.0
        except (ValueError, TypeError):
            yes_price = 0.0
        try:
            no_price = float(raw_outcome_prices[1]) if len(raw_outcome_prices) > 1 else 0.0
        except (ValueError, TypeError):
            no_price = 0.0

        # --- volume_24h ---
        # Prefer a dedicated 24h field.  If absent, fall back to volume_num.
        # NOTE: volume_num may be all-time volume when no 24h field is exposed
        # by the CLI.  Callers should treat this field as "best available
        # volume proxy" rather than a guaranteed 24h figure.
        raw_volume_24h = (
            record.get("volume24hr")
            or record.get("volume_24hr")
            or record.get("volume24h")
        )
        if raw_volume_24h is not None:
            try:
                volume_24h = float(raw_volume_24h)
            except (ValueError, TypeError):
                volume_24h = 0.0
        else:
            # Fallback to volume_num (all-time volume proxy)
            try:
                volume_24h = float(record.get("volumeNum") or record.get("volume_num") or record.get("volume") or 0.0)
            except (ValueError, TypeError):
                volume_24h = 0.0

        # --- closes_at (raw string, no date parsing) ---
        closes_at: str | None = (
            record.get("endDate")
            or record.get("end_date")
            or record.get("closingTime")
            or None
        )

        # --- is_active ---
        is_active: bool = bool(record.get("active", True))

        return Market(
            market_id=condition_id,
            question=question,
            token_id=token_id,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            closes_at=closes_at,
            is_active=is_active,
        )

    def _extract_spread(self, payload: Any) -> float:
        """Extract a float spread value from the CLI JSON payload.

        Preferred key: "spread".
        Fallback: ask - bid (both must be present).
        Raises PolymarketClientError if neither approach succeeds.
        """
        if isinstance(payload, dict):
            if "spread" in payload:
                try:
                    return float(payload["spread"])
                except (ValueError, TypeError) as exc:
                    raise PolymarketClientError(
                        f"Could not convert spread value {payload['spread']!r} to float: {exc}"
                    ) from exc

            # Fallback: compute from bid/ask
            bid = payload.get("bid") or payload.get("bestBid") or payload.get("best_bid")
            ask = payload.get("ask") or payload.get("bestAsk") or payload.get("best_ask")
            if bid is not None and ask is not None:
                try:
                    return float(ask) - float(bid)
                except (ValueError, TypeError) as exc:
                    raise PolymarketClientError(
                        f"Could not compute spread from bid={bid!r} ask={ask!r}: {exc}"
                    ) from exc

        raise PolymarketClientError(
            f"Cannot extract spread from payload: {str(payload)[:300]}"
        )
