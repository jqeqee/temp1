"""
Orderbook Analyzer & Arbitrage Detector.

Core logic:
On a binary market (Up/Down), exactly one outcome resolves to $1.00.
If we can buy shares of BOTH outcomes for a combined cost < $1.00,
we are guaranteed a profit regardless of outcome.

Profit per share pair = 1.00 - price_up - price_down

The bot scans orderbooks for both outcomes and finds the maximum number
of share pairs we can assemble below our profit threshold.
"""

import logging
from dataclasses import dataclass, field

import requests
from py_clob_client.client import ClobClient

from config import CLOB_API_URL, MIN_PROFIT_MARGIN, TAKER_FEE_RATE, PREFER_MAKER_ORDERS
from market_scanner import BinaryMarket

logger = logging.getLogger(__name__)


@dataclass
class OrderLevel:
    """A single price level in the orderbook."""
    price: float
    size: float  # number of shares available


@dataclass
class Orderbook:
    """Orderbook for a single outcome token."""
    token_id: str
    bids: list[OrderLevel] = field(default_factory=list)  # sorted descending by price
    asks: list[OrderLevel] = field(default_factory=list)  # sorted ascending by price

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def total_ask_liquidity(self) -> float:
        return sum(level.size for level in self.asks)


@dataclass
class ArbitrageOpportunity:
    """A detected arbitrage opportunity."""
    market: BinaryMarket
    up_price: float         # cost of Up shares
    down_price: float       # cost of Down shares
    combined_cost: float    # up_price + down_price
    profit_per_pair: float  # 1.00 - combined_cost
    max_pairs: float        # max share pairs we can buy (limited by liquidity)
    max_profit: float       # profit_per_pair * max_pairs
    up_token_id: str
    down_token_id: str
    fee_adjusted_profit: float  # profit after fees

    @property
    def profit_margin_pct(self) -> float:
        return (self.profit_per_pair / self.combined_cost) * 100 if self.combined_cost > 0 else 0


class OrderbookAnalyzer:
    """Analyzes orderbooks to find arbitrage opportunities."""

    def __init__(self, clob_client: ClobClient | None = None):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.clob_client = clob_client

    def get_orderbook(self, token_id: str) -> Orderbook:
        """Fetch the orderbook for a token from CLOB API."""
        try:
            if self.clob_client:
                raw = self.clob_client.get_order_book(token_id)
                return self._parse_clob_orderbook(token_id, raw)

            resp = self.session.get(
                f"{CLOB_API_URL}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_orderbook(token_id, data)

        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return Orderbook(token_id=token_id)

    def _parse_orderbook(self, token_id: str, data: dict) -> Orderbook:
        """Parse raw orderbook data into Orderbook."""
        bids = []
        asks = []

        for bid in data.get("bids", []):
            bids.append(OrderLevel(
                price=float(bid.get("price", 0)),
                size=float(bid.get("size", 0)),
            ))

        for ask in data.get("asks", []):
            asks.append(OrderLevel(
                price=float(ask.get("price", 0)),
                size=float(ask.get("size", 0)),
            ))

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return Orderbook(token_id=token_id, bids=bids, asks=asks)

    def _parse_clob_orderbook(self, token_id: str, raw) -> Orderbook:
        """Parse py-clob-client orderbook response."""
        bids = []
        asks = []

        if hasattr(raw, "bids"):
            for bid in raw.bids:
                bids.append(OrderLevel(
                    price=float(bid.price),
                    size=float(bid.size),
                ))
        elif isinstance(raw, dict):
            for bid in raw.get("bids", []):
                bids.append(OrderLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0)),
                ))

        if hasattr(raw, "asks"):
            for ask in raw.asks:
                asks.append(OrderLevel(
                    price=float(ask.price),
                    size=float(ask.size),
                ))
        elif isinstance(raw, dict):
            for ask in raw.get("asks", []):
                asks.append(OrderLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0)),
                ))

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return Orderbook(token_id=token_id, bids=bids, asks=asks)

    def find_arbitrage(self, market: BinaryMarket) -> ArbitrageOpportunity | None:
        """
        Check if a binary market has an arbitrage opportunity.

        Strategy:
        1. Get orderbooks for both Up and Down tokens
        2. Walk through ask levels of both orderbooks
        3. Find the maximum number of share pairs where
           ask_up + ask_down < 1.00 (minus fees)
        """
        up = market.up_token
        down = market.down_token

        if not up or not down:
            return None

        book_up = self.get_orderbook(up.token_id)
        book_down = self.get_orderbook(down.token_id)

        if not book_up.asks or not book_down.asks:
            return None

        # Quick check: if best asks already sum to >= 1.00, no arbitrage
        best_ask_up = book_up.best_ask
        best_ask_down = book_down.best_ask

        if best_ask_up is None or best_ask_down is None:
            return None

        fee_rate = 0.0 if PREFER_MAKER_ORDERS else TAKER_FEE_RATE
        effective_cost = best_ask_up + best_ask_down
        fee_cost = effective_cost * fee_rate
        net_profit = 1.0 - effective_cost - fee_cost

        if net_profit < MIN_PROFIT_MARGIN:
            return None

        # Walk orderbooks to find max pairs at profitable levels
        max_pairs, weighted_up, weighted_down = self._walk_orderbooks(
            book_up.asks, book_down.asks, fee_rate
        )

        if max_pairs <= 0:
            return None

        avg_up_price = weighted_up / max_pairs
        avg_down_price = weighted_down / max_pairs
        avg_combined = avg_up_price + avg_down_price
        avg_fee = avg_combined * fee_rate
        avg_profit = 1.0 - avg_combined - avg_fee

        return ArbitrageOpportunity(
            market=market,
            up_price=avg_up_price,
            down_price=avg_down_price,
            combined_cost=avg_combined,
            profit_per_pair=1.0 - avg_combined,
            max_pairs=max_pairs,
            max_profit=avg_profit * max_pairs,
            up_token_id=up.token_id,
            down_token_id=down.token_id,
            fee_adjusted_profit=avg_profit * max_pairs,
        )

    def _walk_orderbooks(
        self,
        asks_up: list[OrderLevel],
        asks_down: list[OrderLevel],
        fee_rate: float,
    ) -> tuple[float, float, float]:
        """
        Walk through both ask sides to find maximum profitable pairs.

        Returns (max_pairs, weighted_cost_up, weighted_cost_down).

        The algorithm matches the cheapest available shares from each side,
        consuming liquidity level by level.
        """
        total_pairs = 0.0
        weighted_up = 0.0
        weighted_down = 0.0

        # Copy ask levels so we can consume them
        up_levels = [OrderLevel(a.price, a.size) for a in asks_up]
        down_levels = [OrderLevel(a.price, a.size) for a in asks_down]

        up_idx = 0
        down_idx = 0

        while up_idx < len(up_levels) and down_idx < len(down_levels):
            up_ask = up_levels[up_idx]
            down_ask = down_levels[down_idx]

            combined = up_ask.price + down_ask.price
            fee = combined * fee_rate
            net = 1.0 - combined - fee

            if net < MIN_PROFIT_MARGIN:
                # Try next level on the cheaper side
                if up_ask.price <= down_ask.price:
                    up_idx += 1
                else:
                    down_idx += 1
                continue

            # Number of pairs at this level = min of available shares
            pairs = min(up_ask.size, down_ask.size)

            total_pairs += pairs
            weighted_up += pairs * up_ask.price
            weighted_down += pairs * down_ask.price

            # Consume liquidity
            up_ask.size -= pairs
            down_ask.size -= pairs

            if up_ask.size <= 0:
                up_idx += 1
            if down_ask.size <= 0:
                down_idx += 1

        return total_pairs, weighted_up, weighted_down

    def scan_all_markets(
        self, markets: list[BinaryMarket]
    ) -> list[ArbitrageOpportunity]:
        """Scan all markets for arbitrage opportunities."""
        opportunities = []

        for market in markets:
            if not market.active or not market.enable_order_book:
                continue

            opp = self.find_arbitrage(market)
            if opp:
                logger.info(
                    f"ARBITRAGE FOUND: {market.title} | "
                    f"Up={opp.up_price:.4f} Down={opp.down_price:.4f} | "
                    f"Combined={opp.combined_cost:.4f} | "
                    f"Profit/pair=${opp.profit_per_pair:.4f} | "
                    f"Max pairs={opp.max_pairs:.1f} | "
                    f"Max profit=${opp.fee_adjusted_profit:.2f}"
                )
                opportunities.append(opp)

        opportunities.sort(key=lambda o: o.fee_adjusted_profit, reverse=True)
        return opportunities
