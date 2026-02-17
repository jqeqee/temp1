"""
Account Tracker: Monitors the target account's trading activity.

Tracks what the target account is trading in real-time, which markets
they enter, at what prices, and whether we can follow their strategy.

This serves two purposes:
1. Validate our arbitrage detection matches their actual trades
2. Discover new market patterns or strategies they employ
"""

import time
import logging
from dataclasses import dataclass, field

import requests

from config import DATA_API_URL, TARGET_ACCOUNT_ADDRESS

logger = logging.getLogger(__name__)


@dataclass
class AccountTrade:
    """A single trade from the tracked account."""
    side: str              # BUY or SELL
    title: str             # market title
    outcome: str           # Up, Down, Yes, No, etc.
    price: float
    size: float            # number of tokens
    cash: float            # USD value
    timestamp: int
    condition_id: str = ""
    asset: str = ""        # token_id
    transaction_hash: str = ""
    slug: str = ""


@dataclass
class AccountPosition:
    """A position held by the tracked account."""
    title: str
    outcome: str
    size: float
    avg_price: float
    current_value: float
    initial_value: float
    cash_pnl: float
    percent_pnl: float
    condition_id: str = ""


@dataclass
class TradePair:
    """A matched pair of trades on the same market (Up + Down)."""
    condition_id: str
    title: str
    up_trade: AccountTrade | None = None
    down_trade: AccountTrade | None = None

    @property
    def is_arbitrage(self) -> bool:
        if not self.up_trade or not self.down_trade:
            return False
        return self.up_trade.price + self.down_trade.price < 1.0

    @property
    def combined_cost(self) -> float:
        up_cost = self.up_trade.price if self.up_trade else 0
        down_cost = self.down_trade.price if self.down_trade else 0
        return up_cost + down_cost

    @property
    def profit_per_pair(self) -> float:
        return 1.0 - self.combined_cost if self.is_arbitrage else 0


class AccountTracker:
    """Tracks and analyzes the target account's trading activity."""

    def __init__(self, address: str = TARGET_ACCOUNT_ADDRESS):
        self.address = address
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.last_seen_timestamp: int = 0

    def get_recent_trades(
        self, limit: int = 100, since_timestamp: int = 0
    ) -> list[AccountTrade]:
        """Fetch recent trades from the target account."""
        trades = []
        try:
            params = {
                "user": self.address,
                "limit": limit,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
                "type": "TRADE",
            }
            if since_timestamp > 0:
                params["start"] = str(since_timestamp)

            resp = self.session.get(
                f"{DATA_API_URL}/activity",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                for item in data:
                    trade = self._parse_trade(item)
                    if trade:
                        trades.append(trade)

        except requests.RequestException as e:
            logger.error(f"Failed to fetch trades for {self.address}: {e}")

        return trades

    def get_new_trades(self) -> list[AccountTrade]:
        """Fetch only trades newer than our last seen timestamp."""
        trades = self.get_recent_trades(
            limit=50, since_timestamp=self.last_seen_timestamp
        )

        if trades:
            self.last_seen_timestamp = max(t.timestamp for t in trades)

        return trades

    def get_positions(
        self, sort_by: str = "CASHPNL", limit: int = 100
    ) -> list[AccountPosition]:
        """Fetch current positions of the target account."""
        positions = []
        try:
            resp = self.session.get(
                f"{DATA_API_URL}/positions",
                params={
                    "user": self.address,
                    "limit": limit,
                    "sortBy": sort_by,
                    "sortDirection": "DESC",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                for item in data:
                    pos = self._parse_position(item)
                    if pos:
                        positions.append(pos)

        except requests.RequestException as e:
            logger.error(f"Failed to fetch positions: {e}")

        return positions

    def find_arbitrage_pairs(
        self, trades: list[AccountTrade]
    ) -> list[TradePair]:
        """
        Match trades into arbitrage pairs (same market, opposite outcomes).

        Groups BUY trades by condition_id and checks if both Up and Down
        outcomes were purchased for the same market.
        """
        # Group trades by condition_id
        by_market: dict[str, list[AccountTrade]] = {}
        for trade in trades:
            if trade.side == "BUY":
                key = trade.condition_id or trade.title
                if key not in by_market:
                    by_market[key] = []
                by_market[key].append(trade)

        pairs = []
        for condition_id, market_trades in by_market.items():
            pair = TradePair(
                condition_id=condition_id,
                title=market_trades[0].title if market_trades else "",
            )

            for trade in market_trades:
                outcome_lower = trade.outcome.lower()
                if outcome_lower in ("up", "yes"):
                    if pair.up_trade is None or trade.size > pair.up_trade.size:
                        pair.up_trade = trade
                elif outcome_lower in ("down", "no"):
                    if pair.down_trade is None or trade.size > pair.down_trade.size:
                        pair.down_trade = trade

            if pair.up_trade or pair.down_trade:
                pairs.append(pair)

        return pairs

    def analyze_strategy(self, num_trades: int = 500) -> dict:
        """
        Analyze the target account's overall strategy.

        Returns statistics about their trading patterns.
        """
        trades = self.get_recent_trades(limit=num_trades)
        pairs = self.find_arbitrage_pairs(trades)

        arb_pairs = [p for p in pairs if p.is_arbitrage]
        directional = [p for p in pairs if not p.is_arbitrage]

        total_volume = sum(t.cash for t in trades)
        buy_trades = [t for t in trades if t.side == "BUY"]
        sell_trades = [t for t in trades if t.side == "SELL"]

        # Price distribution
        buy_prices = [t.price for t in buy_trades]
        avg_buy_price = sum(buy_prices) / len(buy_prices) if buy_prices else 0

        # Market type distribution
        market_types: dict[str, int] = {}
        for trade in trades:
            slug = trade.slug or trade.title
            # Extract market type from slug (e.g., "btc-updown-5m")
            parts = slug.split("-")
            if len(parts) >= 3:
                mtype = "-".join(parts[:3])
            else:
                mtype = slug
            market_types[mtype] = market_types.get(mtype, 0) + 1

        # Arbitrage profit analysis
        arb_profits = [p.profit_per_pair for p in arb_pairs]
        avg_arb_profit = sum(arb_profits) / len(arb_profits) if arb_profits else 0

        return {
            "total_trades": len(trades),
            "buy_trades": len(buy_trades),
            "sell_trades": len(sell_trades),
            "total_volume_usd": total_volume,
            "avg_buy_price": avg_buy_price,
            "total_market_pairs": len(pairs),
            "arbitrage_pairs": len(arb_pairs),
            "directional_pairs": len(directional),
            "avg_arbitrage_margin": avg_arb_profit,
            "market_types": market_types,
            "arb_pair_details": [
                {
                    "title": p.title,
                    "combined_cost": p.combined_cost,
                    "profit_per_pair": p.profit_per_pair,
                    "up_price": p.up_trade.price if p.up_trade else None,
                    "down_price": p.down_trade.price if p.down_trade else None,
                }
                for p in arb_pairs[:20]  # top 20
            ],
        }

    def _parse_trade(self, data: dict) -> AccountTrade | None:
        """Parse a trade from the Data API response."""
        try:
            return AccountTrade(
                side=data.get("side", ""),
                title=data.get("title", ""),
                outcome=data.get("outcome", ""),
                price=float(data.get("price", 0)),
                size=float(data.get("size", 0)),
                cash=float(data.get("cash", data.get("usdcSize", 0))),
                timestamp=int(data.get("timestamp", 0)),
                condition_id=data.get("conditionId", ""),
                asset=data.get("asset", ""),
                transaction_hash=data.get("transactionHash", ""),
                slug=data.get("slug", data.get("eventSlug", "")),
            )
        except (ValueError, KeyError) as e:
            logger.debug(f"Failed to parse trade: {e}")
            return None

    def _parse_position(self, data: dict) -> AccountPosition | None:
        """Parse a position from the Data API response."""
        try:
            return AccountPosition(
                title=data.get("title", ""),
                outcome=data.get("outcome", ""),
                size=float(data.get("size", 0)),
                avg_price=float(data.get("avgPrice", 0)),
                current_value=float(data.get("currentValue", 0)),
                initial_value=float(data.get("initialValue", 0)),
                cash_pnl=float(data.get("cashPnl", 0)),
                percent_pnl=float(data.get("percentPnl", 0)),
                condition_id=data.get("conditionId", ""),
            )
        except (ValueError, KeyError) as e:
            logger.debug(f"Failed to parse position: {e}")
            return None

    def print_strategy_report(self):
        """Print a human-readable strategy analysis report."""
        analysis = self.analyze_strategy()

        print("=" * 70)
        print(f"TARGET ACCOUNT ANALYSIS: {self.address}")
        print("=" * 70)
        print(f"Total trades analyzed: {analysis['total_trades']}")
        print(f"  BUY:  {analysis['buy_trades']}")
        print(f"  SELL: {analysis['sell_trades']}")
        print(f"Total volume: ${analysis['total_volume_usd']:,.2f}")
        print(f"Average buy price: ${analysis['avg_buy_price']:.4f}")
        print()
        print(f"Market pairs found: {analysis['total_market_pairs']}")
        print(f"  Arbitrage pairs: {analysis['arbitrage_pairs']}")
        print(f"  Directional:     {analysis['directional_pairs']}")
        print(f"  Avg arb margin:  ${analysis['avg_arbitrage_margin']:.4f}")
        print()
        print("Market types:")
        for mtype, count in sorted(
            analysis["market_types"].items(), key=lambda x: x[1], reverse=True
        ):
            print(f"  {mtype}: {count} trades")
        print()

        if analysis["arb_pair_details"]:
            print("Top arbitrage pairs:")
            for pair in analysis["arb_pair_details"][:10]:
                print(
                    f"  {pair['title']}: "
                    f"Up={pair['up_price']:.4f} Down={pair['down_price']:.4f} "
                    f"Cost={pair['combined_cost']:.4f} "
                    f"Profit={pair['profit_per_pair']:.4f}"
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tracker = AccountTracker()
    tracker.print_strategy_report()
