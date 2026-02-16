"""
Trade Executor: Places orders on Polymarket to capture arbitrage.

Two execution strategies:
1. Market orders (FOK) - Immediate fill, pays taker fee
2. Limit orders (GTC) - Posts to book, earns maker rebate, may not fill

The target account primarily uses limit orders to earn maker rebates
and avoid taker fees on crypto markets.
"""

import logging
from dataclasses import dataclass

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
    PREFER_MAKER_ORDERS,
)
from orderbook_analyzer import ArbitrageOpportunity

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Result of executing a trade."""
    success: bool
    order_id: str = ""
    side: str = ""       # "up" or "down"
    token_id: str = ""
    price: float = 0.0
    size: float = 0.0
    cost: float = 0.0
    error: str = ""


@dataclass
class ArbitrageExecution:
    """Result of executing a full arbitrage (both sides)."""
    opportunity: ArbitrageOpportunity
    up_trade: TradeResult | None = None
    down_trade: TradeResult | None = None

    @property
    def success(self) -> bool:
        return (
            self.up_trade is not None
            and self.down_trade is not None
            and self.up_trade.success
            and self.down_trade.success
        )

    @property
    def total_cost(self) -> float:
        cost = 0.0
        if self.up_trade:
            cost += self.up_trade.cost
        if self.down_trade:
            cost += self.down_trade.cost
        return cost

    @property
    def expected_profit(self) -> float:
        if not self.success:
            return 0.0
        pairs = min(
            self.up_trade.size if self.up_trade else 0,
            self.down_trade.size if self.down_trade else 0,
        )
        return pairs - self.total_cost


class TradeExecutor:
    """Executes arbitrage trades on Polymarket."""

    def __init__(self, bankroll: float = 0.0):
        self.bankroll = bankroll
        self.client: ClobClient | None = None
        self._initialized = False

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
            # Derive API credentials
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._initialized = True
            logger.info("CLOB client initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            return False

    def calculate_position_size(self, opportunity: ArbitrageOpportunity) -> float:
        """
        Calculate optimal position size (number of share pairs).

        Constraints:
        - Max bet size per trade
        - Max bankroll fraction
        - Available liquidity
        """
        max_by_bet = MAX_BET_SIZE / opportunity.combined_cost
        max_by_bankroll = (self.bankroll * MAX_BANKROLL_FRACTION) / opportunity.combined_cost
        max_by_liquidity = opportunity.max_pairs

        # Use the most conservative limit
        size = min(max_by_bet, max_by_bankroll, max_by_liquidity)

        # Polymarket minimum is 5 shares
        if size < 5:
            return 0.0

        return size

    def execute_arbitrage(
        self, opportunity: ArbitrageOpportunity
    ) -> ArbitrageExecution:
        """
        Execute an arbitrage by buying both sides of the market.

        Places orders for Up and Down tokens simultaneously.
        """
        execution = ArbitrageExecution(opportunity=opportunity)

        size = self.calculate_position_size(opportunity)
        if size <= 0:
            logger.warning(
                f"Position size too small for {opportunity.market.title}"
            )
            return execution

        logger.info(
            f"Executing arbitrage: {opportunity.market.title} | "
            f"Size={size:.1f} pairs | "
            f"Expected profit=${opportunity.profit_per_pair * size:.2f}"
        )

        if DRY_RUN:
            logger.info("[DRY RUN] Would execute:")
            logger.info(
                f"  BUY {size:.1f} UP   @ {opportunity.up_price:.4f} "
                f"(${size * opportunity.up_price:.2f})"
            )
            logger.info(
                f"  BUY {size:.1f} DOWN @ {opportunity.down_price:.4f} "
                f"(${size * opportunity.down_price:.2f})"
            )
            logger.info(
                f"  Total cost: ${size * opportunity.combined_cost:.2f} | "
                f"Expected profit: ${size * opportunity.profit_per_pair:.2f}"
            )

            execution.up_trade = TradeResult(
                success=True,
                side="up",
                token_id=opportunity.up_token_id,
                price=opportunity.up_price,
                size=size,
                cost=size * opportunity.up_price,
            )
            execution.down_trade = TradeResult(
                success=True,
                side="down",
                token_id=opportunity.down_token_id,
                price=opportunity.down_price,
                size=size,
                cost=size * opportunity.down_price,
            )
            return execution

        if not self._initialized:
            logger.error("Trade executor not initialized")
            return execution

        # Execute both sides
        execution.up_trade = self._place_order(
            token_id=opportunity.up_token_id,
            price=opportunity.up_price,
            size=size,
            side="up",
        )

        execution.down_trade = self._place_order(
            token_id=opportunity.down_token_id,
            price=opportunity.down_price,
            size=size,
            side="down",
        )

        if execution.success:
            self.bankroll -= execution.total_cost
            logger.info(
                f"Arbitrage executed: cost=${execution.total_cost:.2f} | "
                f"expected profit=${execution.expected_profit:.2f}"
            )
        else:
            logger.warning(f"Partial execution for {opportunity.market.title}")
            self._handle_partial_fill(execution)

        return execution

    def _place_order(
        self, token_id: str, price: float, size: float, side: str
    ) -> TradeResult:
        """Place a single order (limit or market)."""
        try:
            if PREFER_MAKER_ORDERS:
                return self._place_limit_order(token_id, price, size, side)
            else:
                return self._place_market_order(token_id, price, size, side)
        except Exception as e:
            logger.error(f"Order failed ({side}): {e}")
            return TradeResult(success=False, side=side, error=str(e))

    def _place_limit_order(
        self, token_id: str, price: float, size: float, side: str
    ) -> TradeResult:
        """Place a GTC limit order (maker - earns rebates)."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY,
        )
        signed_order = self.client.create_order(order_args)
        resp = self.client.post_order(signed_order, OrderType.GTC)

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("id", ""))
        elif hasattr(resp, "orderID"):
            order_id = resp.orderID

        logger.info(
            f"Limit order placed ({side}): {size:.1f} @ {price:.4f} | "
            f"ID={order_id}"
        )

        return TradeResult(
            success=True,
            order_id=order_id,
            side=side,
            token_id=token_id,
            price=price,
            size=size,
            cost=price * size,
        )

    def _place_market_order(
        self, token_id: str, price: float, size: float, side: str
    ) -> TradeResult:
        """Place a FOK market order (taker - pays fees)."""
        amount = price * size  # total USDC to spend

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed_order = self.client.create_market_order(order_args)
        resp = self.client.post_order(signed_order, OrderType.FOK)

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("id", ""))
        elif hasattr(resp, "orderID"):
            order_id = resp.orderID

        logger.info(
            f"Market order placed ({side}): ${amount:.2f} | ID={order_id}"
        )

        return TradeResult(
            success=True,
            order_id=order_id,
            side=side,
            token_id=token_id,
            price=price,
            size=size,
            cost=amount,
        )

    def _handle_partial_fill(self, execution: ArbitrageExecution):
        """
        Handle the case where only one side filled.

        If only one side executes, we have directional risk.
        Try to cancel the unfilled side or accept the directional position.
        """
        if execution.up_trade and execution.up_trade.success and (
            not execution.down_trade or not execution.down_trade.success
        ):
            logger.warning(
                "Only UP side filled - exposed to directional risk. "
                "Consider canceling or hedging."
            )
        elif execution.down_trade and execution.down_trade.success and (
            not execution.up_trade or not execution.up_trade.success
        ):
            logger.warning(
                "Only DOWN side filled - exposed to directional risk. "
                "Consider canceling or hedging."
            )

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

    def cancel_all_orders(self):
        """Cancel all open orders."""
        if not self._initialized:
            return
        try:
            self.client.cancel_all()
            logger.info("All open orders cancelled")
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
