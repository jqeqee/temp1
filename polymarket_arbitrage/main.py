#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot

Detects and executes risk-free arbitrage on Polymarket's crypto Up/Down markets.

Strategy Overview:
On a binary market (e.g., "Will BTC go Up or Down in the next 5 minutes?"),
exactly one outcome resolves to $1.00 and the other to $0.00.

If we can buy shares of BOTH outcomes for a combined cost < $1.00, we
guarantee a profit regardless of outcome:

    Profit = $1.00 - price_up - price_down  (per matched share pair)

This bot continuously scans all active crypto Up/Down markets (5min, 15min)
for these mispricing opportunities and executes trades when found.

Usage:
    # Analyze the target account's strategy
    python main.py analyze

    # Scan for arbitrage opportunities (no trading)
    python main.py scan

    # Run the bot in dry-run mode (simulate trades)
    python main.py run --dry-run

    # Run the bot in live mode
    python main.py run --live
"""

import sys
import time
import json
import signal
import logging
import argparse
from datetime import datetime, timezone

from config import (
    SCAN_INTERVAL,
    DRY_RUN,
    WALLET_ADDRESS,
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

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
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
        }

    def run(self):
        """Main bot loop."""
        self.running = True
        self.stats["start_time"] = datetime.now(timezone.utc)

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("=" * 60)
        logger.info("POLYMARKET ARBITRAGE BOT")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        logger.info(f"Assets: {', '.join(ASSETS)}")
        logger.info(f"Durations: {', '.join(DURATIONS)}")
        logger.info(f"Min profit margin: ${MIN_PROFIT_MARGIN}")
        logger.info(f"Max bet size: ${MAX_BET_SIZE}")
        logger.info(f"Scan interval: {SCAN_INTERVAL}s")
        logger.info("=" * 60)

        if not self.dry_run:
            if not self.executor.initialize():
                logger.error("Failed to initialize trading client. Exiting.")
                return

        while self.running:
            try:
                self._scan_and_execute()
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(SCAN_INTERVAL * 2)

        self._print_session_stats()

    def _scan_and_execute(self):
        """Single scan cycle: find markets, detect arbitrage, execute."""
        self.stats["scans"] += 1
        now = datetime.now(timezone.utc)

        # Get active markets (both from API and calculated from time windows)
        markets = self.scanner.get_active_markets()
        window_markets = self.scanner.get_current_window_markets()

        # Merge and deduplicate
        seen_slugs = {m.slug for m in markets}
        for wm in window_markets:
            if wm.slug not in seen_slugs:
                markets.append(wm)

        if not markets:
            return

        # Scan orderbooks for arbitrage
        opportunities = self.analyzer.scan_all_markets(markets)

        if not opportunities:
            if self.stats["scans"] % 30 == 0:  # Log every ~60s at 2s interval
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

            # Execute the arbitrage
            execution = self.executor.execute_arbitrage(opp)

            if execution.success:
                self.stats["trades_executed"] += 1
                self.stats["total_invested"] += execution.total_cost
                self.stats["total_profit"] += execution.expected_profit

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

        # Also show upcoming windows
        windows = self.scanner.get_upcoming_window_timestamps(lookahead_seconds=300)
        if windows:
            logger.info(f"\nUpcoming market windows (next 5 min):")
            for w in windows[:10]:
                logger.info(f"  {w['slug']} opens in {w['opens_in']}s")

    def analyze_target(self):
        """Analyze the target account's strategy."""
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
  python main.py analyze          Analyze target account strategy
  python main.py scan             Scan for current arbitrage opportunities
  python main.py run --dry-run    Run bot in simulation mode
  python main.py run --live       Run bot with live trading
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Analyze command
    subparsers.add_parser("analyze", help="Analyze target account strategy")

    # Scan command
    subparsers.add_parser("scan", help="Scan for arbitrage opportunities")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the arbitrage bot")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate trades without executing (default)",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real trades (requires wallet config)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.command:
        parser.print_help()
        return

    if args.command == "analyze":
        bot = ArbitrageBot()
        bot.analyze_target()

    elif args.command == "scan":
        bot = ArbitrageBot()
        bot.scan_only()

    elif args.command == "run":
        dry_run = not getattr(args, "live", False)
        bot = ArbitrageBot(dry_run=dry_run)
        bot.run()


if __name__ == "__main__":
    main()
