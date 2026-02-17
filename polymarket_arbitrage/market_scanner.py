"""
Market Scanner: Discovers active crypto Up/Down markets on Polymarket.

These are short-duration binary markets (5min, 15min) for BTC, ETH, SOL, XRP
that resolve based on whether the price goes up or down in the time window.

Market slugs follow the pattern: {asset}-updown-{duration}-{unix_timestamp}
e.g., "btc-updown-5m-1771264500"
"""

import json
import time
import logging
import requests
from dataclasses import dataclass, field

from config import GAMMA_API_URL, DATA_API_URL, ASSETS, DURATIONS

logger = logging.getLogger(__name__)


@dataclass
class MarketOutcome:
    """A single outcome (Up or Down) in a binary market."""
    token_id: str
    outcome: str  # "Up" or "Down"
    price: float = 0.0


@dataclass
class BinaryMarket:
    """A binary crypto Up/Down market with two outcomes."""
    condition_id: str
    slug: str
    title: str
    asset: str           # btc, eth, sol, xrp
    duration: str        # 5m, 15m
    window_timestamp: int
    end_time: int        # when market resolves
    outcomes: list = field(default_factory=list)  # list of MarketOutcome
    enable_order_book: bool = True
    active: bool = True

    @property
    def up_token(self) -> MarketOutcome | None:
        for o in self.outcomes:
            if o.outcome.lower() == "up":
                return o
        return None

    @property
    def down_token(self) -> MarketOutcome | None:
        for o in self.outcomes:
            if o.outcome.lower() == "down":
                return o
        return None

    @property
    def seconds_until_close(self) -> float:
        return max(0, self.end_time - time.time())


class MarketScanner:
    """Discovers and tracks active crypto Up/Down markets."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # Only include markets closing within this window (seconds).
    # 5m/15m markets far in the future have empty orderbooks.
    MAX_CLOSE_HORIZON = 1800  # 30 minutes

    def get_active_markets(self) -> list[BinaryMarket]:
        """Fetch active crypto Up/Down markets closing soon."""
        seen_slugs = set()
        markets = []

        for asset in ASSETS:
            for duration in DURATIONS:
                found = self._search_markets(asset, duration)
                for m in found:
                    if m.slug in seen_slugs:
                        continue
                    if m.seconds_until_close > self.MAX_CLOSE_HORIZON:
                        continue
                    seen_slugs.add(m.slug)
                    markets.append(m)

        logger.info(f"Found {len(markets)} active crypto Up/Down markets (closing within {self.MAX_CLOSE_HORIZON}s)")
        return markets

    def _search_markets(self, asset: str, duration: str) -> list[BinaryMarket]:
        """Search for active markets matching asset and duration."""
        slug_prefix = f"{asset}-updown-{duration}"
        markets = []

        try:
            # Use Gamma API to search for matching markets
            resp = self.session.get(
                f"{GAMMA_API_URL}/markets",
                params={
                    "slug_contains": slug_prefix,
                    "active": "true",
                    "closed": "false",
                    "limit": 20,
                    "order": "createdAt",
                    "ascending": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data:
                market = self._parse_market(item, asset, duration)
                if market:
                    markets.append(market)

        except requests.RequestException as e:
            logger.error(f"Failed to fetch markets for {slug_prefix}: {e}")

        return markets

    def get_current_window_markets(self) -> list[BinaryMarket]:
        """
        Calculate currently active markets based on time windows.

        5-minute markets: timestamps divisible by 300
        15-minute markets: timestamps divisible by 900
        """
        now = int(time.time())
        markets = []

        for asset in ASSETS:
            for duration in DURATIONS:
                interval = 300 if duration == "5m" else 900
                window_ts = now - (now % interval)
                slug = f"{asset}-updown-{duration}-{window_ts}"

                market = self._fetch_market_by_slug(slug, asset, duration, window_ts)
                if market:
                    markets.append(market)

        return markets

    def _fetch_market_by_slug(
        self, slug: str, asset: str, duration: str, window_ts: int
    ) -> BinaryMarket | None:
        """Fetch a specific market by its slug."""
        try:
            resp = self.session.get(
                f"{GAMMA_API_URL}/markets",
                params={"slug": slug, "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data:
                return self._parse_market(data[0], asset, duration)
        except requests.RequestException as e:
            logger.debug(f"Market not found: {slug} ({e})")

        return None

    def get_upcoming_window_timestamps(self, lookahead_seconds: int = 600) -> list[dict]:
        """
        Get upcoming market window timestamps for the next N seconds.
        Useful for pre-positioning before markets open.
        """
        now = int(time.time())
        windows = []

        for asset in ASSETS:
            for duration in DURATIONS:
                interval = 300 if duration == "5m" else 900
                # Next window start
                next_ts = now - (now % interval) + interval

                while next_ts < now + lookahead_seconds:
                    windows.append({
                        "asset": asset,
                        "duration": duration,
                        "timestamp": next_ts,
                        "slug": f"{asset}-updown-{duration}-{next_ts}",
                        "opens_in": next_ts - now,
                    })
                    next_ts += interval

        return sorted(windows, key=lambda w: w["timestamp"])

    def _parse_market(
        self, data: dict, asset: str, duration: str
    ) -> BinaryMarket | None:
        """Parse Gamma API market data into a BinaryMarket."""
        try:
            condition_id = data.get("conditionId", data.get("condition_id", ""))
            slug = data.get("slug", "")
            title = data.get("question", data.get("title", ""))

            # Extract token IDs and outcomes (API returns JSON strings)
            raw_token_ids = data.get("clobTokenIds", [])
            raw_outcomes = data.get("outcomes", [])
            raw_prices = data.get("outcomePrices", [])

            clob_token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids
            outcome_names = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            outcome_prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices

            if not clob_token_ids or len(clob_token_ids) < 2:
                return None

            outcomes = []
            for i, token_id in enumerate(clob_token_ids):
                name = outcome_names[i] if i < len(outcome_names) else f"Outcome{i}"
                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
                outcomes.append(MarketOutcome(
                    token_id=token_id,
                    outcome=name,
                    price=price,
                ))

            # Parse window timestamp from slug
            parts = slug.split("-")
            window_ts = int(parts[-1]) if parts and parts[-1].isdigit() else 0

            interval = 300 if duration == "5m" else 900
            end_time = window_ts + interval

            return BinaryMarket(
                condition_id=condition_id,
                slug=slug,
                title=title,
                asset=asset,
                duration=duration,
                window_timestamp=window_ts,
                end_time=end_time,
                outcomes=outcomes,
                enable_order_book=data.get("enableOrderBook", True),
                active=data.get("active", True),
            )
        except (ValueError, KeyError, IndexError) as e:
            logger.debug(f"Failed to parse market: {e}")
            return None

    def get_market_prices(self, token_ids: list[str]) -> dict[str, dict]:
        """
        Get current prices for multiple tokens via Data API.
        Returns {token_id: {"bid": float, "ask": float, "mid": float}}
        """
        prices = {}
        try:
            token_id_str = ",".join(token_ids)
            resp = self.session.get(
                f"{DATA_API_URL}/prices",
                params={"tokens": token_id_str},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            for token_id, price_data in data.items():
                if isinstance(price_data, dict):
                    prices[token_id] = price_data
                else:
                    prices[token_id] = {"mid": float(price_data)}

        except requests.RequestException as e:
            logger.error(f"Failed to fetch prices: {e}")

        return prices
