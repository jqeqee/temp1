#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot

Detects and executes risk-free arbitrage on Polymarket's crypto Up/Down markets.

Two execution modes:
  1. POLLING (default): REST API scan every ~2 seconds. Simpler, works anywhere.
  2. WEBSOCKET (--ws): Real-time orderbook via WebSocket. Sub-100ms latency.
     Required for competitive execution against other bots.

Usage:
    python main.py analyze              Analyze target account strategy
    python main.py analyze --deep       Deep analysis with maker/taker breakdown
    python main.py scan                 One-shot scan for current opportunities
    python main.py run --dry-run        Run bot in simulation (polling mode)
    python main.py run --dry-run --ws   Run bot in simulation (WebSocket mode)
    python main.py run --live           Live trading (polling mode)
    python main.py run --live --ws      Live trading (WebSocket mode, fastest)
"""

import sys
import time
import asyncio
import signal
import logging
import argparse
from datetime import datetime, timezone

from config import (
    SCAN_INTERVAL,
    DRY_RUN,
    MAX_BET_SIZE,
    MIN_PROFIT_MARGIN,
    ASSETS,
    DURATIONS,
)
from market_scanner import MarketScanner
from orderbook_analyzer import OrderbookAnalyzer
from trade_executor import TradeExecutor
from account_tracker import AccountTracker

logger = logging.getLogger("polymarket_arb")


class ArbitrageBot:
    """Main orchestrator for the arbitrage bot."""

    def __init__(self, dry_run: bool = True, use_websocket: bool = False):
        self.dry_run = dry_run
        self.use_websocket = use_websocket
        self.scanner = MarketScanner()
        self.analyzer = OrderbookAnalyzer()
        self.executor = TradeExecutor()
        self.tracker = AccountTracker()

        self.running = False
        self.stats = {
            "scans": 0,
            "opportunities_found": 0,
            "trades_executed": 0,
            "total_invested": 0.0,
            "total_profit": 0.0,
            "start_time": None,
            "ws_updates": 0,
            "arb_checks": 0,
        }

    def run(self):
        """Main entry point — selects polling or WebSocket mode."""
        if self.use_websocket:
            asyncio.run(self._run_websocket())
        else:
            self._run_polling()

    # ──────────────────────────────────────────────────────────────
    # MODE 1: REST API Polling (simpler, ~2s latency)
    # ──────────────────────────────────────────────────────────────

    def _run_polling(self):
        """Polling-based main loop."""
        self._setup()

        while self.running:
            try:
                self._scan_and_execute()
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(SCAN_INTERVAL * 2)

        self._shutdown()

    def _scan_and_execute(self):
        """Single scan cycle: find markets, detect arbitrage, execute."""
        self.stats["scans"] += 1
        now = datetime.now(timezone.utc)

        markets = self.scanner.get_active_markets()
        window_markets = self.scanner.get_current_window_markets()

        seen_slugs = {m.slug for m in markets}
        for wm in window_markets:
            if wm.slug not in seen_slugs:
                markets.append(wm)

        if not markets:
            return

        opportunities = self.analyzer.scan_all_markets(markets)

        if not opportunities:
            if self.stats["scans"] % 30 == 0:
                logger.info(
                    f"[{now.strftime('%H:%M:%S')}] Scan #{self.stats['scans']}: "
                    f"{len(markets)} markets, no arbitrage found"
                )
            return

        self.stats["opportunities_found"] += len(opportunities)

        for opp in opportunities:
            logger.info(
                f"[{now.strftime('%H:%M:%S')}] OPPORTUNITY: {opp.market.title}\n"
                f"  Up={opp.up_price:.4f} Down={opp.down_price:.4f} "
                f"Combined={opp.combined_cost:.4f}\n"
                f"  Profit/pair=${opp.profit_per_pair:.4f} "
                f"({opp.profit_margin_pct:.1f}%)\n"
                f"  Max pairs={opp.max_pairs:.0f} "
                f"Max profit=${opp.fee_adjusted_profit:.2f}\n"
                f"  Time to close: {opp.market.seconds_until_close:.0f}s"
            )

            execution = self.executor.execute_arbitrage(opp)
            if execution.success:
                self.stats["trades_executed"] += 1
                self.stats["total_invested"] += execution.total_cost
                self.stats["total_profit"] += execution.expected_profit

    # ──────────────────────────────────────────────────────────────
    # MODE 2: WebSocket Real-Time (fast, ~50ms latency)
    # ──────────────────────────────────────────────────────────────

    async def _run_websocket(self):
        """WebSocket-based main loop for minimum latency."""
        from ws_orderbook import OrderbookManager

        self._setup()

        ws_manager = OrderbookManager()

        # Discover current markets and register them
        markets = self.scanner.get_active_markets()
        window_markets = self.scanner.get_current_window_markets()
        seen_slugs = {m.slug for m in markets}
        for wm in window_markets:
            if wm.slug not in seen_slugs:
                markets.append(wm)

        market_map = {}  # condition_id -> BinaryMarket
        for market in markets:
            up = market.up_token
            down = market.down_token
            if up and down:
                ws_manager.register_market(market.condition_id, up.token_id, down.token_id)
                market_map[market.condition_id] = market

        logger.info(f"Registered {len(market_map)} markets for WebSocket monitoring")

        # Register arbitrage check callback
        ws_manager.on_update(
            lambda cid, up_book, down_book: self._on_orderbook_update(
                cid, up_book, down_book, market_map
            )
        )

        # Start WebSocket + periodic market refresh in parallel
        ws_task = asyncio.create_task(ws_manager.start())
        refresh_task = asyncio.create_task(
            self._periodic_market_refresh(ws_manager, market_map)
        )

        try:
            await asyncio.gather(ws_task, refresh_task)
        except asyncio.CancelledError:
            pass
        finally:
            await ws_manager.stop()
            self._shutdown()

    def _on_orderbook_update(self, condition_id, up_book, down_book, market_map):
        """
        Called on EVERY orderbook update from WebSocket.

        This is the hot path — must be fast.
        Checks if the two books create an arbitrage opportunity.
        """
        self.stats["ws_updates"] += 1
        self.stats["arb_checks"] += 1

        market = market_map.get(condition_id)
        if not market:
            return

        # Quick check: do best asks sum to less than $1.00?
        best_ask_up = up_book.best_ask
        best_ask_down = down_book.best_ask

        if best_ask_up >= float("inf") or best_ask_down >= float("inf"):
            return

        combined = best_ask_up + best_ask_down
        profit = 1.0 - combined

        if profit < MIN_PROFIT_MARGIN:
            return

        # Found opportunity — build full ArbitrageOpportunity and execute
        from orderbook_analyzer import ArbitrageOpportunity, OrderLevel

        # Walk both ask sides to calculate max pairs
        up_asks = [OrderLevel(l.price, l.size) for l in up_book.asks]
        down_asks = [OrderLevel(l.price, l.size) for l in down_book.asks]
        max_pairs, weighted_up, weighted_down = self._walk_asks(up_asks, down_asks)

        if max_pairs <= 0:
            return

        avg_up = weighted_up / max_pairs
        avg_down = weighted_down / max_pairs

        opp = ArbitrageOpportunity(
            market=market,
            up_price=avg_up,
            down_price=avg_down,
            combined_cost=avg_up + avg_down,
            profit_per_pair=1.0 - avg_up - avg_down,
            max_pairs=max_pairs,
            max_profit=(1.0 - avg_up - avg_down) * max_pairs,
            up_token_id=up_book.asset_id,
            down_token_id=down_book.asset_id,
            fee_adjusted_profit=(1.0 - avg_up - avg_down) * max_pairs,
        )

        now = datetime.now(timezone.utc)
        logger.info(
            f"[{now.strftime('%H:%M:%S.%f')[:12]}] WS OPPORTUNITY: {market.title} | "
            f"Up={avg_up:.4f} Down={avg_down:.4f} Combined={avg_up+avg_down:.4f} | "
            f"Profit=${opp.profit_per_pair:.4f} ({opp.profit_margin_pct:.1f}%) | "
            f"Pairs={max_pairs:.0f} | Latency: up={up_book.age_ms:.0f}ms down={down_book.age_ms:.0f}ms"
        )

        self.stats["opportunities_found"] += 1

        # Execute with full orderbook data for optimal liquidity sweeping
        execution = self.executor.execute_arbitrage_with_orderbooks(opp, up_asks, down_asks)

        if execution.success:
            self.stats["trades_executed"] += 1
            self.stats["total_invested"] += execution.total_cost
            self.stats["total_profit"] += execution.expected_profit

    def _walk_asks(self, up_asks, down_asks):
        """Fast orderbook walk for WebSocket callback (must be fast)."""
        total_pairs = 0.0
        weighted_up = 0.0
        weighted_down = 0.0

        up_idx = 0
        down_idx = 0

        # Make copies to consume
        up_levels = [(a.price, a.size) for a in up_asks]
        down_levels = [(a.price, a.size) for a in down_asks]
        up_remaining = [s for _, s in up_levels]
        down_remaining = [s for _, s in down_levels]

        while up_idx < len(up_levels) and down_idx < len(down_levels):
            up_price = up_levels[up_idx][0]
            down_price = down_levels[down_idx][0]

            if up_price + down_price >= 1.0 - MIN_PROFIT_MARGIN:
                if up_price <= down_price:
                    up_idx += 1
                else:
                    down_idx += 1
                continue

            pairs = min(up_remaining[up_idx], down_remaining[down_idx])
            total_pairs += pairs
            weighted_up += pairs * up_price
            weighted_down += pairs * down_price

            up_remaining[up_idx] -= pairs
            down_remaining[down_idx] -= pairs

            if up_remaining[up_idx] <= 0:
                up_idx += 1
            if down_remaining[down_idx] <= 0:
                down_idx += 1

        return total_pairs, weighted_up, weighted_down

    async def _periodic_market_refresh(self, ws_manager, market_map):
        """Periodically discover new markets and subscribe to them."""
        while self.running:
            await asyncio.sleep(30)  # refresh every 30s

            try:
                markets = self.scanner.get_active_markets()
                new_assets = []

                for market in markets:
                    if market.condition_id not in market_map:
                        up = market.up_token
                        down = market.down_token
                        if up and down:
                            ws_manager.register_market(
                                market.condition_id, up.token_id, down.token_id
                            )
                            market_map[market.condition_id] = market
                            new_assets.extend([up.token_id, down.token_id])

                if new_assets:
                    await ws_manager.subscribe_new_assets(new_assets)
                    logger.info(f"Subscribed to {len(new_assets)//2} new markets")

            except Exception as e:
                logger.error(f"Market refresh error: {e}")

    # ──────────────────────────────────────────────────────────────
    # Shared setup / shutdown / utilities
    # ──────────────────────────────────────────────────────────────

    def _setup(self):
        """Common setup for both modes."""
        self.running = True
        self.stats["start_time"] = datetime.now(timezone.utc)

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("=" * 60)
        logger.info("POLYMARKET ARBITRAGE BOT")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        logger.info(f"Engine: {'WebSocket (real-time)' if self.use_websocket else 'REST Polling'}")
        logger.info(f"Assets: {', '.join(ASSETS)}")
        logger.info(f"Durations: {', '.join(DURATIONS)}")
        logger.info(f"Min profit margin: ${MIN_PROFIT_MARGIN}")
        logger.info(f"Max bet size: ${MAX_BET_SIZE}")
        if not self.use_websocket:
            logger.info(f"Scan interval: {SCAN_INTERVAL}s")
        logger.info("=" * 60)

        if not self.dry_run:
            if not self.executor.initialize():
                logger.error("Failed to initialize trading client. Exiting.")
                self.running = False

    def _shutdown(self):
        """Common shutdown for both modes."""
        self.executor.cleanup_open_orders()
        self.executor.print_stats()
        self._print_session_stats()

    def scan_only(self):
        """Scan for opportunities without executing trades."""
        logger.info("Scanning for arbitrage opportunities...")
        markets = self.scanner.get_active_markets()
        window_markets = self.scanner.get_current_window_markets()

        seen_slugs = {m.slug for m in markets}
        for wm in window_markets:
            if wm.slug not in seen_slugs:
                markets.append(wm)

        logger.info(f"Found {len(markets)} active markets")

        for m in markets:
            logger.info(
                f"  {m.slug}: {m.title} "
                f"(closes in {m.seconds_until_close:.0f}s)"
            )

        logger.info("\nScanning orderbooks for arbitrage...")
        opportunities = self.analyzer.scan_all_markets(markets)

        if not opportunities:
            logger.info("No arbitrage opportunities found at this time.")
            logger.info(
                "Tip: Opportunities are transient and may last only seconds. "
                "Run the full bot for continuous monitoring."
            )
        else:
            logger.info(f"\nFound {len(opportunities)} opportunities:")
            for opp in opportunities:
                print(
                    f"\n  Market: {opp.market.title}\n"
                    f"  Up price:    ${opp.up_price:.4f}\n"
                    f"  Down price:  ${opp.down_price:.4f}\n"
                    f"  Combined:    ${opp.combined_cost:.4f}\n"
                    f"  Profit/pair: ${opp.profit_per_pair:.4f} "
                    f"({opp.profit_margin_pct:.1f}%)\n"
                    f"  Max pairs:   {opp.max_pairs:.0f}\n"
                    f"  Max profit:  ${opp.fee_adjusted_profit:.2f}\n"
                    f"  Closes in:   {opp.market.seconds_until_close:.0f}s"
                )

        windows = self.scanner.get_upcoming_window_timestamps(lookahead_seconds=300)
        if windows:
            logger.info(f"\nUpcoming market windows (next 5 min):")
            for w in windows[:10]:
                logger.info(f"  {w['slug']} opens in {w['opens_in']}s")

    def analyze_target(self, deep: bool = False):
        """Analyze the target account's strategy."""
        if deep:
            logger.info("Running deep analysis (maker/taker + sizing patterns)...")
            import analyze_account
            analyze_account.analyze()
        else:
            logger.info("Analyzing target account strategy...")
            self.tracker.print_strategy_report()

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown."""
        logger.info("\nShutting down...")
        self.running = False

    def _print_session_stats(self):
        """Print end-of-session statistics."""
        elapsed = (
            datetime.now(timezone.utc) - self.stats["start_time"]
        ).total_seconds() if self.stats["start_time"] else 0

        print("\n" + "=" * 60)
        print("SESSION SUMMARY")
        print("=" * 60)
        print(f"Duration: {elapsed:.0f}s")
        if self.use_websocket:
            print(f"WebSocket updates: {self.stats['ws_updates']}")
            print(f"Arbitrage checks: {self.stats['arb_checks']}")
        else:
            print(f"Total scans: {self.stats['scans']}")
        print(f"Opportunities found: {self.stats['opportunities_found']}")
        print(f"Trades executed: {self.stats['trades_executed']}")
        print(f"Total invested: ${self.stats['total_invested']:.2f}")
        print(f"Total profit: ${self.stats['total_profit']:.2f}")
        if self.stats["total_invested"] > 0:
            roi = (self.stats["total_profit"] / self.stats["total_invested"]) * 100
            print(f"ROI: {roi:.2f}%")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py analyze              Analyze target account strategy
  python main.py analyze --deep       Deep analysis (maker/taker breakdown)
  python main.py scan                 Scan for current arbitrage opportunities
  python main.py run --dry-run        Run bot in simulation (polling, ~2s)
  python main.py run --dry-run --ws   Run bot in simulation (websocket, ~50ms)
  python main.py run --live --ws      Run bot live with WebSocket (fastest)
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze target account strategy")
    analyze_parser.add_argument(
        "--deep", action="store_true",
        help="Run deep analysis with maker/taker and sizing breakdown",
    )

    # Scan command
    subparsers.add_parser("scan", help="Scan for arbitrage opportunities")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the arbitrage bot")
    run_parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Simulate trades without executing (default)",
    )
    run_parser.add_argument(
        "--live", action="store_true",
        help="Execute real trades (requires wallet config)",
    )
    run_parser.add_argument(
        "--ws", action="store_true",
        help="Use WebSocket for real-time orderbook updates (faster)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.command:
        parser.print_help()
        return

    if args.command == "analyze":
        bot = ArbitrageBot()
        bot.analyze_target(deep=getattr(args, "deep", False))

    elif args.command == "scan":
        bot = ArbitrageBot()
        bot.scan_only()

    elif args.command == "run":
        dry_run = not getattr(args, "live", False)
        use_ws = getattr(args, "ws", False)
        bot = ArbitrageBot(dry_run=dry_run, use_websocket=use_ws)
        bot.run()


if __name__ == "__main__":
    main()
