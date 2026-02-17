"""
Trade Executor: Places orders on Polymarket to capture arbitrage.

STRATEGY BASED ON ACCOUNT ANALYSIS (0x1d0034134e):
═══════════════════════════════════════════════════

The target account uses a HYBRID maker+taker strategy:
  - 51% of trades are MAKER (limit orders → earn rebates, avoid fees)
  - 49% of trades are TAKER (market orders → immediate fill, pay ~1.5% fee)

Execution pattern:
  - Up to 19 trades per second in bursts
  - 59% of trades happen in the same second
  - 93% of trades within 5 seconds of each other
  - Average 20 tokens ($8 USD) per individual trade
  - Many small trades rather than few large ones (liquidity sweeping)

Position sizing (from analysis):
  - Median trade: 20 tokens (~$8)
  - Mean trade: 34 tokens (~$17)
  - Max single trade: 308 tokens (~$210)
  - Trades at extreme prices ($0.01-0.20 and $0.80-0.99) are smaller
  - Trades near equilibrium ($0.40-0.60) are larger

Order types by strategy:
  1. MAKER: Post limit orders at desired prices on BOTH sides
     → Slower fill but zero fees + rebates
     → Used when time to expiry is sufficient (>60s)

  2. TAKER: Sweep available asks immediately
     → Instant fill but pays ~1.5% taker fee
     → Used when mispricing is large enough to absorb fees
     → Or when time to expiry is short (<60s)

  3. HYBRID: Post limit on one side, sweep the other
     → Balances speed vs cost
"""

import time
import logging
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from config import (
    CLOB_API_URL,
    PRIVATE_KEY,
    WALLET_ADDRESS,
    CHAIN_ID,
    SIGNATURE_TYPE,
    MAX_BET_SIZE,
    MAX_BANKROLL_FRACTION,
    DRY_RUN,
    TAKER_FEE_RATE,
)
from orderbook_analyzer import ArbitrageOpportunity, OrderLevel

logger = logging.getLogger(__name__)


# --- Sizing constants derived from account analysis ---
# The target account's observed trade size distribution:
#   $0.01-0.10: avg 27 tokens   $0.40-0.50: avg 40 tokens
#   $0.10-0.20: avg 33 tokens   $0.50-0.60: avg 41 tokens
#   $0.20-0.30: avg 21 tokens   $0.60-0.70: avg 38 tokens
#   $0.30-0.40: avg 28 tokens   $0.70-0.80: avg 34 tokens
SIZE_BY_PRICE_BUCKET = {
    0.05: 27,  0.15: 33,  0.25: 21,  0.35: 28,  0.45: 40,
    0.55: 41,  0.65: 38,  0.75: 34,  0.85: 27,  0.95: 32,
}

# Time thresholds for strategy selection
MAKER_ONLY_THRESHOLD_SECONDS = 120   # >120s → pure maker (limit orders)
HYBRID_THRESHOLD_SECONDS = 60        # 60-120s → hybrid
TAKER_ONLY_THRESHOLD_SECONDS = 30    # <30s → pure taker (market orders)


@dataclass
class TradeResult:
    """Result of executing a single order."""
    success: bool
    order_id: str = ""
    side: str = ""           # "up" or "down"
    token_id: str = ""
    price: float = 0.0
    size: float = 0.0
    cost: float = 0.0
    order_type: str = ""     # "GTC" (maker) or "FOK" (taker)
    error: str = ""
    latency_ms: float = 0.0  # time to place the order


@dataclass
class ArbitrageExecution:
    """Result of executing a full arbitrage (both sides)."""
    opportunity: ArbitrageOpportunity
    up_trades: list[TradeResult] = field(default_factory=list)
    down_trades: list[TradeResult] = field(default_factory=list)
    strategy_used: str = ""  # "maker", "taker", "hybrid"
    total_latency_ms: float = 0.0

    @property
    def success(self) -> bool:
        return bool(self.up_trades) and bool(self.down_trades) and \
               any(t.success for t in self.up_trades) and \
               any(t.success for t in self.down_trades)

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.up_trades + self.down_trades if t.success)

    @property
    def total_up_tokens(self) -> float:
        return sum(t.size for t in self.up_trades if t.success)

    @property
    def total_down_tokens(self) -> float:
        return sum(t.size for t in self.down_trades if t.success)

    @property
    def matched_pairs(self) -> float:
        return min(self.total_up_tokens, self.total_down_tokens)

    @property
    def expected_profit(self) -> float:
        if not self.success:
            return 0.0
        return self.matched_pairs - self.total_cost


class TradeExecutor:
    """
    Executes arbitrage trades using the hybrid maker+taker strategy.

    Key optimizations for speed:
    1. ThreadPoolExecutor for concurrent order placement
    2. Batch order submission (up to 15 orders per call)
    3. Pre-signed orders prepared before submission
    4. Liquidity sweeping: multiple small orders across price levels
    """

    def __init__(self, bankroll: float = 0.0):
        self.bankroll = bankroll
        self.client: ClobClient | None = None
        self._initialized = False
        # Thread pool for concurrent order execution
        self._executor = ThreadPoolExecutor(max_workers=4)
        # Track open orders for cleanup
        self._open_order_ids: list[str] = []
        # Performance stats
        self.stats = {
            "orders_placed": 0,
            "orders_filled": 0,
            "orders_failed": 0,
            "total_latency_ms": 0.0,
            "maker_orders": 0,
            "taker_orders": 0,
        }

    def initialize(self) -> bool:
        """Initialize the CLOB client with credentials."""
        if not PRIVATE_KEY or not WALLET_ADDRESS:
            logger.error("Missing PRIVATE_KEY or WALLET_ADDRESS in config")
            return False

        try:
            self.client = ClobClient(
                CLOB_API_URL,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE,
                funder=WALLET_ADDRESS,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._initialized = True
            logger.info("CLOB client initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            return False

    def select_strategy(self, opportunity: ArbitrageOpportunity) -> str:
        """
        Select execution strategy based on time remaining and margin size.

        The target account adapts its strategy:
        - With ample time: posts limit orders (maker) to avoid fees
        - With little time: sweeps the book (taker) to guarantee fill
        - Wide margins: taker is fine (fees don't eat all profit)
        - Thin margins: must use maker to preserve profit
        """
        seconds_left = opportunity.market.seconds_until_close
        margin = opportunity.profit_per_pair

        # Wide margin (>5%) → taker is safe even with fees
        can_absorb_fees = margin > (opportunity.combined_cost * TAKER_FEE_RATE * 2)

        if seconds_left > MAKER_ONLY_THRESHOLD_SECONDS:
            return "maker"
        elif seconds_left > HYBRID_THRESHOLD_SECONDS:
            return "hybrid" if can_absorb_fees else "maker"
        elif seconds_left > TAKER_ONLY_THRESHOLD_SECONDS:
            return "taker" if can_absorb_fees else "hybrid"
        else:
            # Very close to expiry — must use taker for guaranteed fill
            return "taker"

    def calculate_position_sizes(
        self, opportunity: ArbitrageOpportunity, strategy: str
    ) -> list[tuple[str, str, float, float]]:
        """
        Calculate order sizes split across multiple price levels.

        Returns: [(side, token_id, price, size), ...]

        Mimics the target account's approach of many small orders
        sweeping through available liquidity levels.
        """
        orders = []

        max_total_cost = min(
            MAX_BET_SIZE,
            self.bankroll * MAX_BANKROLL_FRACTION,
        )

        if max_total_cost <= 0:
            return orders

        # For each side, split into multiple orders matching the
        # target account's observed pattern (~20 tokens per order)
        for side, token_id, ask_levels in [
            ("up", opportunity.up_token_id, self._get_ask_levels(opportunity, "up")),
            ("down", opportunity.down_token_id, self._get_ask_levels(opportunity, "down")),
        ]:
            side_budget = max_total_cost / 2  # split evenly
            spent = 0.0

            for level in ask_levels:
                if spent >= side_budget:
                    break

                remaining_budget = side_budget - spent
                max_tokens_by_budget = remaining_budget / level.price if level.price > 0 else 0

                # Target size based on account's observed pattern
                target_size = self._get_target_size_for_price(level.price)
                size = min(target_size, level.size, max_tokens_by_budget)

                if size < 1:  # minimum viable trade
                    continue

                if strategy == "maker":
                    # Post at the bid side (slightly below the ask)
                    maker_price = max(0.01, level.price - 0.01)
                    orders.append((side, token_id, maker_price, size))
                else:
                    # Take the ask
                    orders.append((side, token_id, level.price, size))

                spent += size * level.price

        return orders

    def _get_target_size_for_price(self, price: float) -> float:
        """Get target order size based on observed account patterns."""
        # Find nearest price bucket
        bucket = round(price * 10) / 10
        bucket_center = max(0.05, min(0.95, bucket + 0.05))
        return SIZE_BY_PRICE_BUCKET.get(bucket_center, 30)

    def _get_ask_levels(
        self, opportunity: ArbitrageOpportunity, side: str
    ) -> list[OrderLevel]:
        """
        Get ask levels for a side. Falls back to single level from opportunity.
        """
        # If we have detailed orderbook data, use it
        # Otherwise, create a single level from the opportunity's price
        if side == "up":
            return [OrderLevel(price=opportunity.up_price, size=opportunity.max_pairs)]
        else:
            return [OrderLevel(price=opportunity.down_price, size=opportunity.max_pairs)]

    def execute_arbitrage(
        self, opportunity: ArbitrageOpportunity
    ) -> ArbitrageExecution:
        """
        Execute an arbitrage using the hybrid maker+taker strategy.

        Key improvement: places BOTH sides CONCURRENTLY using ThreadPoolExecutor,
        matching the target account's pattern of near-simultaneous execution.
        """
        strategy = self.select_strategy(opportunity)
        execution = ArbitrageExecution(
            opportunity=opportunity,
            strategy_used=strategy,
        )

        orders = self.calculate_position_sizes(opportunity, strategy)
        if not orders:
            logger.warning(f"No viable orders for {opportunity.market.title}")
            return execution

        up_orders = [(s, tid, p, sz) for s, tid, p, sz in orders if s == "up"]
        down_orders = [(s, tid, p, sz) for s, tid, p, sz in orders if s == "down"]

        logger.info(
            f"Executing [{strategy}]: {opportunity.market.title} | "
            f"{len(up_orders)} up + {len(down_orders)} down orders | "
            f"Margin={opportunity.profit_per_pair:.4f}"
        )

        if DRY_RUN:
            return self._dry_run_execute(execution, up_orders, down_orders, strategy)

        if not self._initialized:
            logger.error("Trade executor not initialized")
            return execution

        start = time.monotonic()

        # CRITICAL: Execute both sides CONCURRENTLY
        # This matches the observed pattern of near-simultaneous execution
        order_type = OrderType.GTC if strategy == "maker" else OrderType.FOK
        label = "GTC" if strategy == "maker" else "FOK"

        futures = {}

        # Submit all up orders
        for side, token_id, price, size in up_orders:
            future = self._executor.submit(
                self._place_order, token_id, price, size, "up", order_type, label
            )
            futures[future] = "up"

        # Submit all down orders
        for side, token_id, price, size in down_orders:
            future = self._executor.submit(
                self._place_order, token_id, price, size, "down", order_type, label
            )
            futures[future] = "down"

        # Collect results
        for future in as_completed(futures):
            side = futures[future]
            try:
                result = future.result(timeout=10)
                if side == "up":
                    execution.up_trades.append(result)
                else:
                    execution.down_trades.append(result)
            except Exception as e:
                logger.error(f"Order future failed ({side}): {e}")

        execution.total_latency_ms = (time.monotonic() - start) * 1000

        if execution.success:
            self.bankroll -= execution.total_cost
            logger.info(
                f"Arbitrage executed: {execution.matched_pairs:.0f} pairs | "
                f"cost=${execution.total_cost:.2f} | "
                f"profit=${execution.expected_profit:.2f} | "
                f"latency={execution.total_latency_ms:.0f}ms"
            )
        else:
            logger.warning(
                f"Partial execution for {opportunity.market.title} | "
                f"up={execution.total_up_tokens:.0f} down={execution.total_down_tokens:.0f}"
            )
            self._handle_partial_fill(execution)

        return execution

    def execute_arbitrage_with_orderbooks(
        self,
        opportunity: ArbitrageOpportunity,
        up_asks: list[OrderLevel],
        down_asks: list[OrderLevel],
    ) -> ArbitrageExecution:
        """
        Execute with detailed orderbook data for optimal liquidity sweeping.

        Instead of a single order at the best ask, this splits across multiple
        price levels — matching how the target account sweeps liquidity.
        """
        strategy = self.select_strategy(opportunity)
        execution = ArbitrageExecution(
            opportunity=opportunity,
            strategy_used=strategy,
        )

        max_total_cost = min(
            MAX_BET_SIZE,
            self.bankroll * MAX_BANKROLL_FRACTION,
        )

        if max_total_cost <= 0:
            return execution

        # Build order lists from detailed orderbook levels
        up_order_list = self._build_sweep_orders(
            "up", opportunity.up_token_id, up_asks, max_total_cost / 2, strategy
        )
        down_order_list = self._build_sweep_orders(
            "down", opportunity.down_token_id, down_asks, max_total_cost / 2, strategy
        )

        if DRY_RUN:
            return self._dry_run_execute(execution, up_order_list, down_order_list, strategy)

        if not self._initialized:
            return execution

        start = time.monotonic()

        # Try batch order submission first (faster than individual orders)
        all_results = self._execute_batch(up_order_list + down_order_list, strategy)

        for result in all_results:
            if result.side == "up":
                execution.up_trades.append(result)
            else:
                execution.down_trades.append(result)

        execution.total_latency_ms = (time.monotonic() - start) * 1000

        if execution.success:
            self.bankroll -= execution.total_cost

        return execution

    def _build_sweep_orders(
        self,
        side: str,
        token_id: str,
        asks: list[OrderLevel],
        budget: float,
        strategy: str,
    ) -> list[tuple[str, str, float, float]]:
        """Build a list of orders to sweep through ask levels."""
        orders = []
        spent = 0.0

        for ask in asks:
            if spent >= budget:
                break

            remaining = budget - spent
            max_by_budget = remaining / ask.price if ask.price > 0 else 0
            target = self._get_target_size_for_price(ask.price)
            size = min(target, ask.size, max_by_budget)

            if size < 1:
                continue

            if strategy == "maker":
                price = max(0.01, ask.price - 0.01)
            else:
                price = ask.price

            orders.append((side, token_id, price, size))
            spent += size * ask.price

        return orders

    def _execute_batch(
        self, orders: list[tuple[str, str, float, float]], strategy: str
    ) -> list[TradeResult]:
        """
        Execute orders using batch submission when possible.

        Polymarket supports up to 15 orders per batch call.
        """
        order_type = OrderType.GTC if strategy == "maker" else OrderType.FOK
        label = "GTC" if strategy == "maker" else "FOK"
        results = []

        # Split into batches of 15 (Polymarket limit)
        batch_size = 15
        batches = [orders[i:i + batch_size] for i in range(0, len(orders), batch_size)]

        for batch in batches:
            # Use concurrent individual orders (batch API not always available)
            futures = {}
            for side, token_id, price, size in batch:
                future = self._executor.submit(
                    self._place_order, token_id, price, size, side, order_type, label
                )
                futures[future] = side

            for future in as_completed(futures):
                try:
                    result = future.result(timeout=10)
                    results.append(result)
                except Exception as e:
                    side = futures[future]
                    results.append(TradeResult(success=False, side=side, error=str(e)))

        return results

    def _place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: OrderType,
        label: str,
    ) -> TradeResult:
        """Place a single order with latency tracking."""
        start = time.monotonic()
        try:
            if order_type == OrderType.GTC:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,
                )
                signed_order = self.client.create_order(order_args)
                resp = self.client.post_order(signed_order, OrderType.GTC)
                self.stats["maker_orders"] += 1
            else:
                amount = price * size
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=amount,
                    side=BUY,
                    order_type=OrderType.FOK,
                )
                signed_order = self.client.create_market_order(order_args)
                resp = self.client.post_order(signed_order, OrderType.FOK)
                self.stats["taker_orders"] += 1

            latency = (time.monotonic() - start) * 1000
            self.stats["orders_placed"] += 1

            order_id = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("id", ""))
                if order_type == OrderType.GTC:
                    self._open_order_ids.append(order_id)
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID

            self.stats["total_latency_ms"] += latency

            return TradeResult(
                success=True,
                order_id=order_id,
                side=side,
                token_id=token_id,
                price=price,
                size=size,
                cost=price * size,
                order_type=label,
                latency_ms=latency,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            self.stats["orders_failed"] += 1
            logger.error(f"Order failed ({side} {label}): {e} [{latency:.0f}ms]")
            return TradeResult(
                success=False, side=side, error=str(e),
                latency_ms=latency, order_type=label,
            )

    def _dry_run_execute(
        self,
        execution: ArbitrageExecution,
        up_orders: list[tuple],
        down_orders: list[tuple],
        strategy: str,
    ) -> ArbitrageExecution:
        """Simulate execution in dry-run mode."""
        label = "GTC" if strategy == "maker" else "FOK"
        logger.info(f"[DRY RUN] Strategy: {strategy} ({label})")

        for side, token_id, price, size in up_orders:
            logger.info(f"  BUY UP   {size:>7.1f} @ ${price:.4f} = ${size*price:>8.2f}")
            execution.up_trades.append(TradeResult(
                success=True, side="up", token_id=token_id,
                price=price, size=size, cost=price * size, order_type=label,
            ))

        for side, token_id, price, size in down_orders:
            logger.info(f"  BUY DOWN {size:>7.1f} @ ${price:.4f} = ${size*price:>8.2f}")
            execution.down_trades.append(TradeResult(
                success=True, side="down", token_id=token_id,
                price=price, size=size, cost=price * size, order_type=label,
            ))

        logger.info(
            f"  Total: ${execution.total_cost:.2f} | "
            f"Pairs: {execution.matched_pairs:.0f} | "
            f"Profit: ${execution.expected_profit:.2f}"
        )
        return execution

    def _handle_partial_fill(self, execution: ArbitrageExecution):
        """Handle partial fills — cancel unfilled maker orders."""
        up_filled = execution.total_up_tokens
        down_filled = execution.total_down_tokens

        if abs(up_filled - down_filled) < 5:
            return  # close enough

        if up_filled > down_filled * 1.5 or down_filled > up_filled * 1.5:
            logger.warning(
                f"Significant imbalance: up={up_filled:.0f} down={down_filled:.0f}. "
                f"Consider hedging the excess {abs(up_filled - down_filled):.0f} tokens."
            )

    def cleanup_open_orders(self):
        """Cancel all open GTC orders from this session."""
        if not self._initialized or not self._open_order_ids:
            return

        logger.info(f"Cleaning up {len(self._open_order_ids)} open orders...")
        for order_id in self._open_order_ids:
            try:
                self.client.cancel(order_id)
            except Exception as e:
                logger.debug(f"Failed to cancel {order_id}: {e}")
        self._open_order_ids.clear()

    def cancel_all_orders(self):
        """Cancel all open orders."""
        if not self._initialized:
            return
        try:
            self.client.cancel_all()
            self._open_order_ids.clear()
            logger.info("All open orders cancelled")
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")

    def get_open_orders(self) -> list:
        """Get all open orders."""
        if not self._initialized:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            return self.client.get_orders(OpenOrderParams())
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def print_stats(self):
        """Print executor performance stats."""
        s = self.stats
        total = s["orders_placed"] + s["orders_failed"]
        avg_latency = s["total_latency_ms"] / s["orders_placed"] if s["orders_placed"] > 0 else 0

        print(f"\nTrade Executor Stats:")
        print(f"  Orders placed: {s['orders_placed']} / {total} attempted")
        print(f"  Maker orders:  {s['maker_orders']}")
        print(f"  Taker orders:  {s['taker_orders']}")
        print(f"  Failed orders: {s['orders_failed']}")
        print(f"  Avg latency:   {avg_latency:.0f}ms")
