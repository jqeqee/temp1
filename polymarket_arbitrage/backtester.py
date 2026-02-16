#!/usr/bin/env python3
"""
Polymarket Arbitrage Backtester

Replays historical market data with realistic delay and slippage simulation
to estimate whether the arbitrage strategy would have been profitable.

Approach:
=========
1. Fetches ALL trades from the target account over a time range
2. For each market (conditionId), fetches ALL trades from ALL participants
   to reconstruct the actual orderbook conditions ("tape")
3. Replays the tape in chronological order with our bot's logic
4. Applies configurable latency (detection → decision → execution)
5. After adding delays, checks if prices at the delayed timestamp still work
6. Models slippage, partial fills, and competition from other bots
7. Tracks per-trade and cumulative P&L

Delay Model:
============
  detection_delay:  Time to notice the opportunity
    - WebSocket:  50-150ms (orderbook push)
    - Polling:    1000-3000ms (REST interval)

  decision_delay:   Time to compute and sign orders
    - Both modes: 5-20ms

  execution_delay:  Time for order to reach exchange and fill
    - Both modes: 80-250ms

  Total roundtrip:
    - WebSocket:  ~150-400ms
    - Polling:    ~1100-3300ms

Competition Model:
==================
  Other bots also chase the same opportunities. Modeled as:
    - fill_probability: chance our order fills before a competitor
    - decreases with smaller margin (thin arbitrage = more competition)
    - decreases with longer delay (slower bot = less chance)

Location-Aware Delay Model:
===========================
  Polymarket servers are in AWS eu-west-2 (London, UK).
  Network RTT is added on top of processing delays:
    - Co-located (Amsterdam/London):  ~4-12ms RTT  → total ~50-160ms
    - Virginia (US-East):             ~70-90ms RTT  → total ~130-250ms
    - Seoul (Korea):                  ~250-300ms RTT → total ~400-520ms

Usage:
======
    python backtester.py                                # default (1hr, Virginia WS)
    python backtester.py --hours 6                      # backtest last 6 hours
    python backtester.py --hours 6 --mode korea_ws      # simulate from Seoul
    python backtester.py --hours 6 --mode virginia_ws   # simulate from Virginia
    python backtester.py --hours 6 --mode fast_ws       # simulate co-located
    python backtester.py --compare                      # compare ALL locations
    python backtester.py --compare-locations             # compare locations only
    python backtester.py --bankroll 1000                # start with $1000
"""

import argparse
import json
import math
import random
import time
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
TARGET = "0x1d0034134e339a309700ff2d34e99fa2d48b0313"

# ────────────────────────────────────────────────────────────────
# Delay profiles (milliseconds)
# ────────────────────────────────────────────────────────────────
# Network RTT baselines (one-way) to Polymarket servers (AWS eu-west-2 London)
# These are added ON TOP of processing delays for detection and execution
NETWORK_RTT = {
    "colocated":  {"name": "Amsterdam/London", "rtt_min_ms": 2,   "rtt_max_ms": 6},
    "virginia":   {"name": "Virginia (US-East)", "rtt_min_ms": 35,  "rtt_max_ms": 45},
    "korea":      {"name": "Seoul (Korea)", "rtt_min_ms": 125, "rtt_max_ms": 150},
}

# Base processing delays (server-side + local compute, EXCLUDING network)
_BASE_WS_DETECT = (5, 30)       # WS push arrives almost instantly on server side
_BASE_WS_DECIDE = (2, 10)       # local compute to evaluate + sign
_BASE_WS_EXECUTE = (20, 60)     # server order matching + confirmation
_BASE_POLL_DETECT = (1000, 3000)  # REST polling interval (network-independent)
_BASE_POLL_DECIDE = (5, 20)
_BASE_POLL_EXECUTE = (20, 60)

def _build_profile(name: str, location: str, base_detect, base_decide, base_execute):
    """Build a delay profile by adding network RTT to base processing delays.

    Detection = base_detect + 1x RTT (data travels server → us)
    Decision  = base_decide (local only, no network)
    Execution = base_execute + 1x RTT (order travels us → server)
    Total roundtrip adds ~2x RTT on top of processing.
    """
    net = NETWORK_RTT[location]
    return {
        "name": name,
        "location": net["name"],
        "detection_min_ms":  base_detect[0] + net["rtt_min_ms"],
        "detection_max_ms":  base_detect[1] + net["rtt_max_ms"],
        "decision_min_ms":   base_decide[0],
        "decision_max_ms":   base_decide[1],
        "execution_min_ms":  base_execute[0] + net["rtt_min_ms"],
        "execution_max_ms":  base_execute[1] + net["rtt_max_ms"],
    }

DELAY_PROFILES = {
    # ── Co-located (Amsterdam/London → London) ──
    "fast_ws": _build_profile(
        "WS Co-located (Amsterdam)", "colocated",
        _BASE_WS_DETECT, _BASE_WS_DECIDE, _BASE_WS_EXECUTE,
    ),
    # ── Virginia (US-East → London, ~70-90ms RTT) ──
    "virginia_ws": _build_profile(
        "WS Virginia (US-East)", "virginia",
        _BASE_WS_DETECT, _BASE_WS_DECIDE, _BASE_WS_EXECUTE,
    ),
    "virginia_poll": _build_profile(
        "Polling Virginia (US-East)", "virginia",
        _BASE_POLL_DETECT, _BASE_POLL_DECIDE, _BASE_POLL_EXECUTE,
    ),
    # ── Korea (Seoul → London, ~250-300ms RTT) ──
    "korea_ws": _build_profile(
        "WS Seoul (Korea)", "korea",
        _BASE_WS_DETECT, _BASE_WS_DECIDE, _BASE_WS_EXECUTE,
    ),
    "korea_poll": _build_profile(
        "Polling Seoul (Korea)", "korea",
        _BASE_POLL_DETECT, _BASE_POLL_DECIDE, _BASE_POLL_EXECUTE,
    ),
    # ── Legacy aliases (kept for backwards compatibility) ──
    "ws": _build_profile(
        "WebSocket (generic)", "virginia",
        _BASE_WS_DETECT, _BASE_WS_DECIDE, _BASE_WS_EXECUTE,
    ),
    "polling": _build_profile(
        "REST Polling (generic)", "virginia",
        _BASE_POLL_DETECT, _BASE_POLL_DECIDE, _BASE_POLL_EXECUTE,
    ),
}

# ────────────────────────────────────────────────────────────────
# Simulation parameters
# ────────────────────────────────────────────────────────────────
TAKER_FEE_RATE = 0.015       # 1.5% taker fee on crypto markets
MAKER_FEE_RATE = 0.0         # 0% maker fee (+ possible rebate)
MAKER_RATIO = 0.51           # 51% of orders are maker (from analysis)
MIN_PROFIT_MARGIN = 0.01     # minimum $0.01 per pair
SLIPPAGE_BASE_BPS = 50       # base slippage 0.5%
COMPETITION_BASE_PROB = 0.85 # 85% base fill probability


@dataclass
class Trade:
    """A single trade from the market tape."""
    timestamp: int
    condition_id: str
    slug: str
    title: str
    outcome: str       # "Up" or "Down"
    outcome_index: int  # 0 or 1
    side: str          # "BUY" or "SELL"
    price: float
    size: float        # tokens
    usdc_size: float
    asset: str
    tx_hash: str
    is_target: bool    # True if from the target account


@dataclass
class MarketWindow:
    """A single market time window (e.g., BTC 5m 11:00-11:05)."""
    condition_id: str
    slug: str
    title: str
    asset_name: str     # BTC, ETH, etc.
    duration: str       # 5m, 15m
    window_start: int   # unix timestamp
    window_end: int
    trades: list[Trade] = field(default_factory=list)
    resolved_outcome: str = ""  # "Up" or "Down" (which one won)

    @property
    def up_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome == "Up" and t.side == "BUY"]

    @property
    def down_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome == "Down" and t.side == "BUY"]


@dataclass
class SimulatedOrder:
    """A simulated order in the backtest."""
    side: str           # "up" or "down"
    price: float        # intended price
    size: float         # intended tokens
    filled: bool = False
    fill_price: float = 0.0
    fill_size: float = 0.0
    fill_cost: float = 0.0
    fee: float = 0.0
    is_maker: bool = False
    slippage: float = 0.0
    latency_ms: float = 0.0


@dataclass
class ArbitrageResult:
    """Result of a single simulated arbitrage attempt."""
    market: MarketWindow
    detected_at: int       # timestamp when we detected the opportunity
    executed_at: int       # timestamp when our orders would have filled
    up_order: SimulatedOrder | None = None
    down_order: SimulatedOrder | None = None
    total_delay_ms: float = 0.0
    was_available: bool = True   # was opportunity still there after delay?
    competitor_took_it: bool = False
    skipped_reason: str = ""

    @property
    def executed(self) -> bool:
        return (self.up_order is not None and self.up_order.filled and
                self.down_order is not None and self.down_order.filled)

    @property
    def total_cost(self) -> float:
        cost = 0.0
        if self.up_order and self.up_order.filled:
            cost += self.up_order.fill_cost + self.up_order.fee
        if self.down_order and self.down_order.filled:
            cost += self.down_order.fill_cost + self.down_order.fee
        return cost

    @property
    def matched_pairs(self) -> float:
        up = self.up_order.fill_size if self.up_order and self.up_order.filled else 0
        down = self.down_order.fill_size if self.down_order and self.down_order.filled else 0
        return min(up, down)

    @property
    def gross_payout(self) -> float:
        return self.matched_pairs  # winning side pays $1.00 per token

    @property
    def net_profit(self) -> float:
        return self.gross_payout - self.total_cost


# ────────────────────────────────────────────────────────────────
# Data Fetching
# ────────────────────────────────────────────────────────────────

def fetch_target_trades(start_ts: int, end_ts: int) -> list[dict]:
    """Fetch all target account trades in the time range.
    Uses timestamp-based cursor pagination to avoid offset limits."""
    print(f"  Fetching target account trades...")
    all_trades = []
    cursor_ts = start_ts
    seen_hashes: set[str] = set()

    while cursor_ts < end_ts:
        try:
            resp = requests.get(f"{DATA_API}/activity", params={
                "user": TARGET, "limit": 500, "offset": 0,
                "type": "TRADE", "sortBy": "TIMESTAMP", "sortDirection": "ASC",
                "start": str(cursor_ts), "end": str(end_ts),
            }, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            break

        data = resp.json()
        if not data:
            break

        new_count = 0
        for item in data:
            tx = item.get("transactionHash", "")
            if tx and tx not in seen_hashes:
                seen_hashes.add(tx)
                all_trades.append(item)
                new_count += 1

        last_ts = int(data[-1].get("timestamp", cursor_ts))
        print(f"    ...fetched {len(all_trades)} trades (cursor={cursor_ts})")

        if new_count == 0 or last_ts <= cursor_ts:
            break
        cursor_ts = last_ts  # advance cursor to last seen timestamp

        if len(data) < 500:
            break
        time.sleep(0.3)

    return all_trades


def fetch_market_trades(condition_id: str, start_ts: int, end_ts: int) -> list[dict]:
    """Fetch all trades for a specific market from ALL participants."""
    all_trades = []
    offset = 0
    max_offset = 1500  # stay within API limits
    while offset < max_offset:
        try:
            resp = requests.get(f"{DATA_API}/trades", params={
                "market": condition_id, "limit": 500, "offset": offset,
                "takerOnly": "false",
            }, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            break
        data = resp.json()
        if not data:
            break
        filtered = [t for t in data
                    if start_ts <= t.get("timestamp", 0) <= end_ts]
        all_trades.extend(filtered)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return all_trades


def fetch_positions_pnl(condition_ids: list[str]) -> dict[str, dict]:
    """Fetch resolved position P&L for the target account."""
    pnl = {}
    for cid in condition_ids:
        try:
            resp = requests.get(f"{DATA_API}/positions", params={
                "user": TARGET, "market": cid, "limit": 10,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for pos in data:
                outcome = pos.get("outcome", "")
                pnl_val = float(pos.get("cashPnl", 0))
                pnl.setdefault(cid, {})[outcome] = pnl_val
        except Exception:
            pass
        time.sleep(0.1)
    return pnl


# ────────────────────────────────────────────────────────────────
# Data Processing
# ────────────────────────────────────────────────────────────────

def parse_trade(raw: dict, is_target: bool = False) -> Trade:
    return Trade(
        timestamp=int(raw.get("timestamp", 0)),
        condition_id=raw.get("conditionId", ""),
        slug=raw.get("slug", raw.get("eventSlug", "")),
        title=raw.get("title", ""),
        outcome=raw.get("outcome", ""),
        outcome_index=int(raw.get("outcomeIndex", -1)),
        side=raw.get("side", ""),
        price=float(raw.get("price", 0)),
        size=float(raw.get("size", 0)),
        usdc_size=float(raw.get("usdcSize", 0)),
        asset=raw.get("asset", ""),
        tx_hash=raw.get("transactionHash", ""),
        is_target=is_target,
    )


def build_market_windows(target_trades: list[Trade]) -> dict[str, MarketWindow]:
    """Group trades into market windows."""
    windows: dict[str, MarketWindow] = {}

    for t in target_trades:
        cid = t.condition_id
        if cid not in windows:
            slug = t.slug
            # Parse asset and duration from slug
            parts = slug.split("-")
            asset_name = parts[0].upper() if parts else "?"
            duration = "15m"
            if "5m" in slug:
                duration = "5m"

            # Parse window timestamp
            win_ts = int(parts[-1]) if parts and parts[-1].isdigit() else t.timestamp
            interval = 300 if duration == "5m" else 900

            windows[cid] = MarketWindow(
                condition_id=cid,
                slug=slug,
                title=t.title,
                asset_name=asset_name,
                duration=duration,
                window_start=win_ts,
                window_end=win_ts + interval,
            )

        windows[cid].trades.append(t)

    return windows


def determine_resolved_outcome(window: MarketWindow) -> str:
    """
    Determine which outcome won by looking at late-stage trade prices.
    Near resolution, the winning outcome's price approaches $1.00.
    """
    late_trades = [t for t in window.trades
                   if t.timestamp >= window.window_end - 30]
    if not late_trades:
        late_trades = window.trades[-5:] if window.trades else []

    up_prices = [t.price for t in late_trades if t.outcome == "Up" and t.side == "BUY"]
    down_prices = [t.price for t in late_trades if t.outcome == "Down" and t.side == "BUY"]

    avg_up = sum(up_prices) / len(up_prices) if up_prices else 0.5
    avg_down = sum(down_prices) / len(down_prices) if down_prices else 0.5

    return "Up" if avg_up > avg_down else "Down"


# ────────────────────────────────────────────────────────────────
# Simulation Engine
# ────────────────────────────────────────────────────────────────

def simulate_delay(profile: dict) -> tuple[float, float, float]:
    """Generate random delays based on profile. Returns (detect, decide, execute) in ms."""
    detect = random.uniform(profile["detection_min_ms"], profile["detection_max_ms"])
    decide = random.uniform(profile["decision_min_ms"], profile["decision_max_ms"])
    execute = random.uniform(profile["execution_min_ms"], profile["execution_max_ms"])
    return detect, decide, execute


def compute_slippage(price: float, size: float, total_market_volume: float) -> float:
    """
    Model price slippage based on order size relative to market volume.
    Larger orders relative to available liquidity get worse prices.
    """
    if total_market_volume <= 0:
        return SLIPPAGE_BASE_BPS / 10000

    size_ratio = (price * size) / max(total_market_volume, 1)
    # Slippage scales quadratically with size ratio
    slippage_bps = SLIPPAGE_BASE_BPS * (1 + size_ratio * 5)
    return min(slippage_bps / 10000, 0.05)  # cap at 5%


def compute_fill_probability(
    margin: float, total_delay_ms: float, is_btc: bool
) -> float:
    """
    Model competition: probability that our order fills before a competitor.

    Factors:
    - Wider margin = more bots chasing it = lower probability
    - Longer delay = more time for competitors = lower probability
    - BTC = more liquid = more competition
    """
    base = COMPETITION_BASE_PROB

    # Delay penalty: each 100ms delay reduces probability by ~5%
    delay_penalty = (total_delay_ms / 100) * 0.05
    base -= delay_penalty

    # Margin attractiveness: very wide margins attract more bots
    if margin > 0.10:
        base -= 0.15  # >10% margin = very competitive
    elif margin > 0.05:
        base -= 0.08

    # BTC is more competitive
    if is_btc:
        base -= 0.05

    return max(0.10, min(0.95, base))


def find_price_at_time(trades: list[Trade], outcome: str, target_ts: int) -> float | None:
    """
    Find the best available price for an outcome at a specific timestamp.
    Uses trades closest to (but not after) the target timestamp.
    """
    relevant = [t for t in trades
                if t.outcome == outcome and t.side == "BUY" and t.timestamp <= target_ts]
    if not relevant:
        return None

    # Use the most recent trade before target_ts
    relevant.sort(key=lambda t: t.timestamp, reverse=True)
    return relevant[0].price


def simulate_arbitrage(
    window: MarketWindow,
    profile: dict,
    max_bet_usd: float,
) -> ArbitrageResult | None:
    """
    Simulate our bot trying to capture arbitrage on a single market window.

    Replays the market's trade tape and finds moments when both sides
    could be bought for < $1.00, then applies realistic delays to see
    if we could have actually captured it.
    """
    trades_by_time = sorted(window.trades, key=lambda t: t.timestamp)
    if not trades_by_time:
        return None

    # Build a timeline of price snapshots
    # At each second, what was the latest known price for Up and Down?
    price_up: dict[int, list[float]] = defaultdict(list)
    price_down: dict[int, list[float]] = defaultdict(list)

    for t in trades_by_time:
        if t.side != "BUY":
            continue
        ts = t.timestamp
        if t.outcome == "Up":
            price_up[ts].append(t.price)
        elif t.outcome == "Down":
            price_down[ts].append(t.price)

    # Walk through timestamps looking for arbitrage opportunities
    all_timestamps = sorted(set(price_up.keys()) | set(price_down.keys()))
    if not all_timestamps:
        return None

    # Track running best prices (most recent seen)
    latest_up = None
    latest_down = None
    latest_up_ts = 0
    latest_down_ts = 0

    best_opportunity = None
    best_margin = 0

    for ts in all_timestamps:
        if ts in price_up:
            # Use the lowest available ask (best price to buy)
            latest_up = min(price_up[ts])
            latest_up_ts = ts
        if ts in price_down:
            latest_down = min(price_down[ts])
            latest_down_ts = ts

        if latest_up is None or latest_down is None:
            continue

        # Only consider if both prices are reasonably fresh (within 30s)
        if abs(latest_up_ts - latest_down_ts) > 30:
            continue

        combined = latest_up + latest_down
        margin = 1.0 - combined

        if margin > MIN_PROFIT_MARGIN and margin > best_margin:
            best_margin = margin
            best_opportunity = (ts, latest_up, latest_down, combined, margin)

    if best_opportunity is None:
        return None

    opp_ts, up_price, down_price, combined, margin = best_opportunity

    # Apply delay simulation
    detect_ms, decide_ms, execute_ms = simulate_delay(profile)
    total_delay_ms = detect_ms + decide_ms + execute_ms
    total_delay_s = total_delay_ms / 1000

    executed_ts = opp_ts + int(math.ceil(total_delay_s))

    result = ArbitrageResult(
        market=window,
        detected_at=opp_ts,
        executed_at=executed_ts,
        total_delay_ms=total_delay_ms,
    )

    # Check: is the opportunity still there after the delay?
    delayed_up = find_price_at_time(trades_by_time, "Up", executed_ts)
    delayed_down = find_price_at_time(trades_by_time, "Down", executed_ts)

    if delayed_up is None or delayed_down is None:
        result.was_available = False
        result.skipped_reason = "No price data at execution time"
        return result

    delayed_combined = delayed_up + delayed_down
    delayed_margin = 1.0 - delayed_combined

    if delayed_margin < MIN_PROFIT_MARGIN:
        result.was_available = False
        result.skipped_reason = (
            f"Margin evaporated: {margin:.4f} → {delayed_margin:.4f} "
            f"after {total_delay_ms:.0f}ms delay"
        )
        return result

    # Competition check
    is_btc = window.asset_name == "BTC"
    fill_prob = compute_fill_probability(delayed_margin, total_delay_ms, is_btc)

    if random.random() > fill_prob:
        result.competitor_took_it = True
        result.skipped_reason = (
            f"Competitor filled first (prob={fill_prob:.0%}, "
            f"delay={total_delay_ms:.0f}ms)"
        )
        return result

    # Calculate trade sizes
    total_market_volume = sum(t.usdc_size for t in window.trades)
    budget_per_side = max_bet_usd / 2

    up_tokens = budget_per_side / delayed_up if delayed_up > 0 else 0
    down_tokens = budget_per_side / delayed_down if delayed_down > 0 else 0
    matched = min(up_tokens, down_tokens)

    if matched < 5:  # Polymarket minimum
        result.skipped_reason = "Position too small"
        return result

    # Apply slippage
    up_slippage = compute_slippage(delayed_up, matched, total_market_volume)
    down_slippage = compute_slippage(delayed_down, matched, total_market_volume)

    actual_up_price = min(delayed_up * (1 + up_slippage), 0.99)
    actual_down_price = min(delayed_down * (1 + down_slippage), 0.99)

    # Check if still profitable after slippage
    actual_combined = actual_up_price + actual_down_price
    if actual_combined >= 1.0:
        result.was_available = False
        result.skipped_reason = (
            f"Slippage killed margin: {delayed_combined:.4f} → {actual_combined:.4f}"
        )
        return result

    # Determine maker vs taker for each order
    up_is_maker = random.random() < MAKER_RATIO
    down_is_maker = random.random() < MAKER_RATIO

    up_fee_rate = MAKER_FEE_RATE if up_is_maker else TAKER_FEE_RATE
    down_fee_rate = MAKER_FEE_RATE if down_is_maker else TAKER_FEE_RATE

    up_cost = actual_up_price * matched
    down_cost = actual_down_price * matched
    up_fee = up_cost * up_fee_rate
    down_fee = down_cost * down_fee_rate

    result.up_order = SimulatedOrder(
        side="up", price=delayed_up, size=matched, filled=True,
        fill_price=actual_up_price, fill_size=matched,
        fill_cost=up_cost, fee=up_fee,
        is_maker=up_is_maker, slippage=up_slippage,
        latency_ms=total_delay_ms,
    )
    result.down_order = SimulatedOrder(
        side="down", price=delayed_down, size=matched, filled=True,
        fill_price=actual_down_price, fill_size=matched,
        fill_cost=down_cost, fee=down_fee,
        is_maker=down_is_maker, slippage=down_slippage,
        latency_ms=total_delay_ms,
    )

    return result


# ────────────────────────────────────────────────────────────────
# Main Backtest Runner
# ────────────────────────────────────────────────────────────────

def _fetch_and_build_windows(hours: float):
    """Fetch data and build market windows. Cached to avoid re-fetching."""
    now = int(time.time())
    start_ts = now - int(hours * 3600)
    end_ts = now

    print("\n[1/4] FETCHING HISTORICAL DATA")
    print("-" * 40)

    raw_target = fetch_target_trades(start_ts, end_ts)
    target_trades = [parse_trade(t, is_target=True) for t in raw_target]
    print(f"  Target trades: {len(target_trades)}")

    if not target_trades:
        print("  No trades found in this period. Try a longer time window.")
        return None, start_ts, end_ts

    windows = build_market_windows(target_trades)
    print(f"  Markets found: {len(windows)}")

    print(f"  Fetching market-level data for {len(windows)} markets...")
    fetched = 0
    for cid, window in windows.items():
        try:
            raw_market = fetch_market_trades(
                cid, window.window_start - 60, window.window_end + 60
            )
            for t in raw_market:
                parsed = parse_trade(t, is_target=False)
                if parsed.tx_hash not in {tt.tx_hash for tt in window.trades}:
                    window.trades.append(parsed)
            fetched += 1
            if fetched % 10 == 0:
                print(f"    ...{fetched}/{len(windows)} markets")
        except Exception:
            pass
        time.sleep(0.15)

    print(f"  Market data enriched: {fetched} markets")

    for window in windows.values():
        window.resolved_outcome = determine_resolved_outcome(window)

    return windows, start_ts, end_ts


def run_backtest(
    hours: float = 1.0,
    mode: str = "ws",
    bankroll: float = 500.0,
    max_bet_usd: float = 50.0,
    seed: int | None = None,
    _cached_windows=None,
    _cached_times=None,
):
    if seed is not None:
        random.seed(seed)

    profile = DELAY_PROFILES[mode]

    if _cached_windows is not None:
        windows = _cached_windows
        start_ts, end_ts = _cached_times
    else:
        result = _fetch_and_build_windows(hours)
        if result[0] is None:
            return None
        windows, start_ts, end_ts = result

    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    print("\n" + "=" * 70)
    print("POLYMARKET ARBITRAGE BACKTESTER")
    print("=" * 70)
    print(f"Period:     {start_dt.strftime('%Y-%m-%d %H:%M')} → "
          f"{end_dt.strftime('%Y-%m-%d %H:%M')} UTC ({hours:.1f}h)")
    print(f"Mode:       {profile['name']}")
    print(f"Bankroll:   ${bankroll:,.2f}")
    print(f"Max bet:    ${max_bet_usd:,.2f}")
    print(f"Taker fee:  {TAKER_FEE_RATE*100:.1f}%")
    print(f"Maker fee:  {MAKER_FEE_RATE*100:.1f}%")
    print(f"Seed:       {seed if seed else 'random'}")
    print("=" * 70)

    # (data already fetched above or from cache)

    # ── Step 2: Run simulation ──
    print(f"\n[2/4] RUNNING SIMULATION ({profile['name']})")
    print("-" * 40)

    results: list[ArbitrageResult] = []
    current_bankroll = bankroll

    sorted_windows = sorted(windows.values(), key=lambda w: w.window_start)

    for window in sorted_windows:
        if current_bankroll < 10:
            print(f"  ⚠ Bankroll depleted (${current_bankroll:.2f})")
            break

        effective_bet = min(max_bet_usd, current_bankroll * 0.10)

        result = simulate_arbitrage(window, profile, effective_bet)
        if result is None:
            continue

        results.append(result)

        if result.executed:
            current_bankroll -= result.total_cost
            # After resolution, we get back $1.00 per matched pair
            current_bankroll += result.gross_payout

    # ── Step 3: Analyze results ──
    print(f"\n[3/4] RESULTS ANALYSIS")
    print("-" * 40)

    executed = [r for r in results if r.executed]
    margin_evaporated = [r for r in results if not r.was_available]
    competitor_took = [r for r in results if r.competitor_took_it]
    skipped = [r for r in results if r.skipped_reason and not r.executed]

    total_cost = sum(r.total_cost for r in executed)
    total_payout = sum(r.gross_payout for r in executed)
    total_fees = sum(
        (r.up_order.fee if r.up_order else 0) + (r.down_order.fee if r.down_order else 0)
        for r in executed
    )
    total_profit = sum(r.net_profit for r in executed)

    profitable = [r for r in executed if r.net_profit > 0]
    losing = [r for r in executed if r.net_profit <= 0]

    print(f"\n  Opportunities detected:     {len(results)}")
    print(f"  Successfully executed:      {len(executed)}")
    print(f"  Margin evaporated (delay):  {len(margin_evaporated)}")
    print(f"  Lost to competitors:        {len(competitor_took)}")
    print(f"  Other skipped:              {len(skipped) - len(margin_evaporated) - len(competitor_took)}")
    print(f"\n  Executed breakdown:")
    print(f"    Profitable:   {len(profitable)}")
    print(f"    Losing:       {len(losing)}")
    if executed:
        win_rate = len(profitable) / len(executed) * 100
        print(f"    Win rate:     {win_rate:.1f}%")

    print(f"\n  Financial Summary:")
    print(f"    Total invested:   ${total_cost:>10,.2f}")
    print(f"    Total payout:     ${total_payout:>10,.2f}")
    print(f"    Total fees:       ${total_fees:>10,.2f}")
    print(f"    Net profit:       ${total_profit:>10,.2f}")
    print(f"    Starting bank:    ${bankroll:>10,.2f}")
    print(f"    Ending bank:      ${current_bankroll:>10,.2f}")
    print(f"    Return:           {((current_bankroll - bankroll) / bankroll * 100):>9.2f}%")

    if executed:
        avg_profit = total_profit / len(executed)
        avg_delay = sum(r.total_delay_ms for r in executed) / len(executed)
        avg_margin = sum(
            (1.0 - (r.up_order.fill_price + r.down_order.fill_price))
            for r in executed
            if r.up_order and r.down_order
        ) / len(executed)

        print(f"\n  Per-trade averages:")
        print(f"    Avg profit/trade: ${avg_profit:>8.2f}")
        print(f"    Avg margin:       {avg_margin*100:>7.2f}%")
        print(f"    Avg delay:        {avg_delay:>7.0f}ms")
        print(f"    Avg fees/trade:   ${total_fees/len(executed):>8.2f}")

    # ── Step 4: Detailed Trade Log ──
    print(f"\n[4/4] TRADE LOG")
    print("-" * 70)
    print(f"{'#':>3} {'Market':<42} {'Margin':>7} {'Delay':>7} {'Profit':>8} {'Status'}")
    print("-" * 70)

    for i, r in enumerate(results, 1):
        title = r.market.title[:40] if r.market else "?"

        if r.executed:
            margin_pct = (1.0 - r.up_order.fill_price - r.down_order.fill_price) * 100
            status = "FILLED"
            profit_str = f"${r.net_profit:>7.2f}"
        elif r.competitor_took_it:
            margin_pct = 0
            status = "COMPETITOR"
            profit_str = "    -   "
        elif not r.was_available:
            margin_pct = 0
            status = "EVAPORATED"
            profit_str = "    -   "
        else:
            margin_pct = 0
            status = "SKIPPED"
            profit_str = "    -   "

        print(f"{i:>3} {title:<42} {margin_pct:>6.2f}% "
              f"{r.total_delay_ms:>5.0f}ms {profit_str} {status}")

    # Losses detail
    if margin_evaporated:
        print(f"\n  Margin evaporation details:")
        for r in margin_evaporated[:5]:
            print(f"    {r.market.title[:50]}: {r.skipped_reason}")

    if competitor_took:
        print(f"\n  Lost to competitor details:")
        for r in competitor_took[:5]:
            print(f"    {r.market.title[:50]}: {r.skipped_reason}")

    # ── Summary box ──
    print("\n" + "=" * 70)
    print("BACKTEST SUMMARY")
    print("=" * 70)
    print(f"  Mode:             {profile['name']}")
    print(f"  Period:           {hours:.1f} hours")
    print(f"  Markets scanned:  {len(windows)}")
    print(f"  Opportunities:    {len(results)}")
    print(f"  Executed:         {len(executed)} ({len(executed)/max(1,len(results))*100:.0f}%)")
    print(f"  Net P&L:          ${total_profit:>+.2f}")
    print(f"  Final bankroll:   ${current_bankroll:,.2f} (started ${bankroll:,.2f})")
    print(f"  ROI:              {((current_bankroll - bankroll) / bankroll * 100):>+.2f}%")

    if hours > 0:
        hourly = total_profit / hours
        daily = hourly * 24
        print(f"  Hourly rate:      ${hourly:>+.2f}/hr")
        print(f"  Projected daily:  ${daily:>+,.2f}/day")

    print("=" * 70)

    return {
        "mode": mode,
        "hours": hours,
        "bankroll_start": bankroll,
        "bankroll_end": current_bankroll,
        "total_markets": len(windows),
        "opportunities": len(results),
        "executed": len(executed),
        "profit": total_profit,
        "roi_pct": (current_bankroll - bankroll) / bankroll * 100,
    }


def run_comparison(hours: float, bankroll: float, max_bet: float, seed: int,
                   modes: list[str] | None = None):
    """Run backtest across multiple modes for comparison. Fetches data once."""
    import copy

    if modes is None:
        modes = ["korea_poll", "korea_ws", "virginia_poll", "virginia_ws", "fast_ws"]

    print("\n" + "=" * 70)
    print(f"LOCATION COMPARISON — fetching data once, simulating {len(modes)} modes")
    print("=" * 70)

    # Fetch data once
    result = _fetch_and_build_windows(hours)
    if result[0] is None:
        print("No data available for this period.")
        return
    windows, start_ts, end_ts = result

    summaries = []
    for mode in modes:
        print(f"\n{'─' * 70}")
        windows_copy = copy.deepcopy(windows)
        summary = run_backtest(
            hours=hours, mode=mode, bankroll=bankroll,
            max_bet_usd=max_bet, seed=seed,
            _cached_windows=windows_copy, _cached_times=(start_ts, end_ts),
        )
        if summary:
            summaries.append(summary)

    if summaries:
        print("\n" + "=" * 70)
        print("COMPARISON TABLE")
        print("=" * 70)
        header_fmt = f"{'Mode':<32} {'Location':<16} {'Exec':>5} {'P&L':>10} {'ROI':>8} {'$/hr':>10}"
        print(header_fmt)
        print("-" * 85)
        for s in summaries:
            profile = DELAY_PROFILES[s["mode"]]
            name = profile["name"]
            location = profile.get("location", "?")
            hourly = s["profit"] / max(s["hours"], 0.01)
            print(f"{name:<32} {location:<16} {s['executed']:>5} "
                  f"${s['profit']:>+9.2f} {s['roi_pct']:>+7.2f}% "
                  f"${hourly:>+9.2f}")
        print("=" * 85)

        # Recommendation
        best = max(summaries, key=lambda s: s["profit"])
        best_profile = DELAY_PROFILES[best["mode"]]
        print(f"\n  RECOMMENDATION: {best_profile['name']}")
        print(f"  Expected: ${best['profit']/max(best['hours'],0.01):+.2f}/hr, "
              f"{best['roi_pct']:+.2f}% ROI")

        # Korea vs Virginia comparison
        korea_results = [s for s in summaries if "korea" in s["mode"]]
        virginia_results = [s for s in summaries if "virginia" in s["mode"]]
        if korea_results and virginia_results:
            best_korea = max(korea_results, key=lambda s: s["profit"])
            best_virginia = max(virginia_results, key=lambda s: s["profit"])
            if best_virginia["profit"] > best_korea["profit"]:
                improvement = best_virginia["profit"] - best_korea["profit"]
                print(f"\n  Virginia vs Korea advantage: ${improvement:+.2f} "
                      f"({improvement/max(abs(best_korea['profit']),0.01)*100:+.0f}% more profit)")
                print(f"  → Using your Virginia server is strongly recommended.")


def main():
    all_modes = list(DELAY_PROFILES.keys())
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Backtester (Location-Aware)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Server: Polymarket runs on AWS eu-west-2 (London, UK).

Available modes:
  fast_ws        WS from Amsterdam/London (co-located, ~50-160ms)
  virginia_ws    WS from Virginia US-East (~130-250ms)
  virginia_poll  Polling from Virginia US-East (~1100-3200ms)
  korea_ws       WS from Seoul Korea (~400-520ms)
  korea_poll     Polling from Seoul Korea (~1300-3400ms)
  ws             WS generic (same as virginia_ws)
  polling        Polling generic (same as virginia_poll)

Examples:
  python backtester.py                                  Default (1hr, virginia_ws)
  python backtester.py --hours 3 --mode korea_ws        Test from Korea
  python backtester.py --hours 3 --mode virginia_ws     Test from your Virginia server
  python backtester.py --hours 3 --mode fast_ws         Test co-located
  python backtester.py --compare                        Compare ALL locations + modes
  python backtester.py --compare-locations              Compare WS across locations only
  python backtester.py --hours 6 --bankroll 1000        Start with $1000
  python backtester.py --seed 42                        Reproducible results
        """,
    )
    parser.add_argument("--hours", type=float, default=1.0,
                        help="Hours of history to backtest (default: 1)")
    parser.add_argument("--mode", choices=all_modes,
                        default="virginia_ws",
                        help="Execution mode to simulate (default: virginia_ws)")
    parser.add_argument("--bankroll", type=float, default=500.0,
                        help="Starting bankroll in USDC (default: 500)")
    parser.add_argument("--max-bet", type=float, default=50.0,
                        help="Max USD per arbitrage pair (default: 50)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible results")
    parser.add_argument("--compare", action="store_true",
                        help="Compare ALL modes (5 profiles: Korea/Virginia/Co-located)")
    parser.add_argument("--compare-locations", action="store_true",
                        help="Compare WebSocket only across 3 locations")

    args = parser.parse_args()

    if args.compare:
        seed = args.seed if args.seed else 42
        run_comparison(args.hours, args.bankroll, args.max_bet, seed)
    elif args.compare_locations:
        seed = args.seed if args.seed else 42
        run_comparison(
            args.hours, args.bankroll, args.max_bet, seed,
            modes=["korea_ws", "virginia_ws", "fast_ws"],
        )
    else:
        run_backtest(
            hours=args.hours,
            mode=args.mode,
            bankroll=args.bankroll,
            max_bet_usd=args.max_bet,
            seed=args.seed,
        )


if __name__ == "__main__":
    main()
