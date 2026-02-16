#!/usr/bin/env python3
"""
Deep analysis of target account's trading patterns.
Determines maker vs taker behavior, position sizing logic, and timing patterns.
"""

import json
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

TARGET = "0x1d0034134e339a309700ff2d34e99fa2d48b0313"
DATA_API = "https://data-api.polymarket.com"


def fetch_all_trades(taker_only: bool, max_records: int = 2000) -> list[dict]:
    """Fetch trades with pagination."""
    all_trades = []
    offset = 0
    while offset < max_records:
        resp = requests.get(
            f"{DATA_API}/trades",
            params={
                "user": TARGET,
                "limit": 500,
                "offset": offset,
                "takerOnly": str(taker_only).lower(),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_trades.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.5)
    return all_trades


def fetch_activity(max_records: int = 2000) -> list[dict]:
    """Fetch activity data with pagination."""
    all_activity = []
    offset = 0
    while offset < max_records:
        resp = requests.get(
            f"{DATA_API}/activity",
            params={
                "user": TARGET,
                "limit": 500,
                "offset": offset,
                "type": "TRADE",
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_activity.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.5)
    return all_activity


def analyze():
    print("=" * 70)
    print("DEEP ANALYSIS: Canine-Commandment (0x1d0034134e)")
    print("=" * 70)

    # --- 1. Maker vs Taker Analysis ---
    print("\n[1] MAKER vs TAKER ANALYSIS")
    print("-" * 50)

    print("Fetching taker-only trades...")
    taker_trades = fetch_all_trades(taker_only=True)
    print(f"  Taker trades: {len(taker_trades)}")

    print("Fetching all trades (maker + taker)...")
    all_trades = fetch_all_trades(taker_only=False)
    print(f"  All trades: {len(all_trades)}")

    # Build set of taker trade identifiers (timestamp + asset + size)
    taker_keys = set()
    for t in taker_trades:
        key = (t.get("timestamp"), t.get("asset"), str(t.get("size")), str(t.get("price")))
        taker_keys.add(key)

    # Identify maker trades
    maker_trades = []
    for t in all_trades:
        key = (t.get("timestamp"), t.get("asset"), str(t.get("size")), str(t.get("price")))
        if key not in taker_keys:
            maker_trades.append(t)

    maker_count = len(maker_trades)
    taker_count = len(taker_trades)
    total = len(all_trades)

    print(f"\n  TAKER trades: {taker_count} ({taker_count/total*100:.1f}%)")
    print(f"  MAKER trades: {maker_count} ({maker_count/total*100:.1f}%)")
    print(f"  Total:        {total}")

    # Analyze price distribution for maker vs taker
    taker_prices = [float(t.get("price", 0)) for t in taker_trades]
    maker_prices = [float(t.get("price", 0)) for t in maker_trades]

    if taker_prices:
        print(f"\n  Taker price distribution:")
        print(f"    Mean:   ${sum(taker_prices)/len(taker_prices):.4f}")
        print(f"    Min:    ${min(taker_prices):.4f}")
        print(f"    Max:    ${max(taker_prices):.4f}")

    if maker_prices:
        print(f"\n  Maker price distribution:")
        print(f"    Mean:   ${sum(maker_prices)/len(maker_prices):.4f}")
        print(f"    Min:    ${min(maker_prices):.4f}")
        print(f"    Max:    ${max(maker_prices):.4f}")

    # --- 2. Timing Analysis ---
    print("\n\n[2] TIMING ANALYSIS")
    print("-" * 50)

    # Sort all trades by timestamp
    all_sorted = sorted(all_trades, key=lambda t: t.get("timestamp", 0))

    if len(all_sorted) >= 2:
        intervals = []
        for i in range(1, len(all_sorted)):
            dt = all_sorted[i].get("timestamp", 0) - all_sorted[i-1].get("timestamp", 0)
            intervals.append(dt)

        print(f"  Time between consecutive trades:")
        print(f"    Mean:    {sum(intervals)/len(intervals):.2f}s")
        print(f"    Median:  {sorted(intervals)[len(intervals)//2]:.2f}s")
        print(f"    Min:     {min(intervals)}s")
        print(f"    Max:     {max(intervals)}s")

        # Count trades with 0-second gap (same second)
        same_second = sum(1 for i in intervals if i == 0)
        within_5s = sum(1 for i in intervals if i <= 5)
        within_10s = sum(1 for i in intervals if i <= 10)
        print(f"\n  Trades in same second:     {same_second} ({same_second/len(intervals)*100:.1f}%)")
        print(f"  Trades within 5 seconds:   {within_5s} ({within_5s/len(intervals)*100:.1f}%)")
        print(f"  Trades within 10 seconds:  {within_10s} ({within_10s/len(intervals)*100:.1f}%)")

    # --- 3. Trades per timestamp (burst analysis) ---
    print("\n\n[3] BURST PATTERN ANALYSIS")
    print("-" * 50)

    trades_per_ts = defaultdict(list)
    for t in all_sorted:
        ts = t.get("timestamp", 0)
        trades_per_ts[ts].append(t)

    burst_sizes = [len(trades) for trades in trades_per_ts.values()]
    print(f"  Unique timestamps: {len(trades_per_ts)}")
    print(f"  Trades per timestamp:")
    print(f"    Mean:  {sum(burst_sizes)/len(burst_sizes):.1f}")
    print(f"    Max:   {max(burst_sizes)}")

    # Show distribution
    burst_dist = defaultdict(int)
    for size in burst_sizes:
        bucket = min(size, 20)
        burst_dist[bucket] += 1
    print(f"\n  Burst size distribution:")
    for size in sorted(burst_dist.keys()):
        label = f"{size}+" if size == 20 else str(size)
        print(f"    {label} trades/sec: {burst_dist[size]} occurrences")

    # Show sample bursts
    big_bursts = sorted(trades_per_ts.items(), key=lambda x: len(x[1]), reverse=True)[:3]
    for ts, trades in big_bursts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        print(f"\n  Burst at {dt.strftime('%H:%M:%S')} UTC ({len(trades)} trades):")
        for t in trades[:8]:
            print(f"    {t.get('side','?'):4s} | {t.get('outcome','?'):5s} | "
                  f"${float(t.get('price',0)):.4f} | "
                  f"{float(t.get('size',0)):>10.2f} tokens | "
                  f"{t.get('title','')[:50]}")

    # --- 4. Position Sizing Analysis ---
    print("\n\n[4] POSITION SIZING ANALYSIS")
    print("-" * 50)

    sizes = [float(t.get("size", 0)) for t in all_trades]
    cash_values = []
    for t in all_trades:
        price = float(t.get("price", 0))
        size = float(t.get("size", 0))
        cash_values.append(price * size)

    print(f"  Token size per trade:")
    print(f"    Mean:    {sum(sizes)/len(sizes):.2f}")
    print(f"    Median:  {sorted(sizes)[len(sizes)//2]:.2f}")
    print(f"    Min:     {min(sizes):.2f}")
    print(f"    Max:     {max(sizes):.2f}")

    print(f"\n  USD value per trade:")
    print(f"    Mean:    ${sum(cash_values)/len(cash_values):.2f}")
    print(f"    Median:  ${sorted(cash_values)[len(cash_values)//2]:.2f}")
    print(f"    Min:     ${min(cash_values):.2f}")
    print(f"    Max:     ${max(cash_values):.2f}")

    # Size distribution by price bucket
    print(f"\n  Avg trade size by price bucket:")
    price_buckets = defaultdict(list)
    for t in all_trades:
        p = float(t.get("price", 0))
        s = float(t.get("size", 0))
        bucket = round(p * 10) / 10  # round to nearest 0.1
        price_buckets[bucket].append(s)

    for bucket in sorted(price_buckets.keys()):
        sizes_in_bucket = price_buckets[bucket]
        avg_size = sum(sizes_in_bucket) / len(sizes_in_bucket)
        total_usd = sum(s * bucket for s in sizes_in_bucket)
        print(f"    ${bucket:.1f}: avg {avg_size:>8.1f} tokens | "
              f"{len(sizes_in_bucket):>4d} trades | "
              f"total ${total_usd:>10.2f}")

    # --- 5. Market Pair Analysis (both sides) ---
    print("\n\n[5] ARBITRAGE PAIR MATCHING")
    print("-" * 50)

    # Group by conditionId and timestamp window (5 sec)
    by_market = defaultdict(list)
    for t in all_trades:
        cid = t.get("conditionId", "")
        if cid:
            by_market[cid].append(t)

    arb_pairs = 0
    total_arb_profit = 0.0
    single_side = 0
    pair_details = []

    for cid, trades in by_market.items():
        ups = [t for t in trades if t.get("outcome", "").lower() == "up" and t.get("side") == "BUY"]
        downs = [t for t in trades if t.get("outcome", "").lower() == "down" and t.get("side") == "BUY"]

        if ups and downs:
            # Weighted average prices
            up_total_tokens = sum(float(t.get("size", 0)) for t in ups)
            up_total_cost = sum(float(t.get("size", 0)) * float(t.get("price", 0)) for t in ups)
            down_total_tokens = sum(float(t.get("size", 0)) for t in downs)
            down_total_cost = sum(float(t.get("size", 0)) * float(t.get("price", 0)) for t in downs)

            avg_up = up_total_cost / up_total_tokens if up_total_tokens else 0
            avg_down = down_total_cost / down_total_tokens if down_total_tokens else 0

            combined = avg_up + avg_down
            matched_pairs = min(up_total_tokens, down_total_tokens)
            profit = (1.0 - combined) * matched_pairs if combined < 1.0 else 0

            pair_details.append({
                "title": trades[0].get("title", ""),
                "avg_up": avg_up,
                "avg_down": avg_down,
                "combined": combined,
                "up_tokens": up_total_tokens,
                "down_tokens": down_total_tokens,
                "matched": matched_pairs,
                "profit": profit,
                "is_arb": combined < 1.0,
                "num_up_trades": len(ups),
                "num_down_trades": len(downs),
            })

            if combined < 1.0:
                arb_pairs += 1
                total_arb_profit += profit
        elif ups or downs:
            single_side += 1

    print(f"  Markets with both sides: {len(pair_details)}")
    print(f"  Markets with single side: {single_side}")
    print(f"  Arbitrage pairs (combined < $1.00): {arb_pairs}")
    print(f"  Total estimated arb profit: ${total_arb_profit:.2f}")

    # Show top pairs
    pair_details.sort(key=lambda p: p["profit"], reverse=True)
    print(f"\n  Top 15 arbitrage pairs:")
    for p in pair_details[:15]:
        status = "ARB" if p["is_arb"] else "LOSS"
        print(f"    [{status}] {p['title'][:52]}")
        print(f"         Up=${p['avg_up']:.4f}({p['num_up_trades']}tx,{p['up_tokens']:.0f}tok) "
              f"Down=${p['avg_down']:.4f}({p['num_down_trades']}tx,{p['down_tokens']:.0f}tok) "
              f"Comb=${p['combined']:.4f} Profit=${p['profit']:.2f}")

    # Show loss pairs too
    loss_pairs = [p for p in pair_details if not p["is_arb"]]
    if loss_pairs:
        print(f"\n  Top 5 loss pairs (combined >= $1.00):")
        loss_pairs.sort(key=lambda p: p["combined"], reverse=True)
        for p in loss_pairs[:5]:
            print(f"    {p['title'][:52]}")
            print(f"         Up=${p['avg_up']:.4f} Down=${p['avg_down']:.4f} "
                  f"Comb=${p['combined']:.4f}")

    # --- 6. Asset Distribution ---
    print("\n\n[6] ASSET DISTRIBUTION")
    print("-" * 50)

    asset_trades = defaultdict(lambda: {"count": 0, "volume": 0.0})
    for t in all_trades:
        title = t.get("title", "")
        if "bitcoin" in title.lower() or "btc" in title.lower():
            asset = "BTC"
        elif "ethereum" in title.lower() or "eth" in title.lower():
            asset = "ETH"
        elif "solana" in title.lower() or "sol" in title.lower():
            asset = "SOL"
        elif "xrp" in title.lower():
            asset = "XRP"
        else:
            asset = "OTHER"
        asset_trades[asset]["count"] += 1
        asset_trades[asset]["volume"] += float(t.get("size", 0)) * float(t.get("price", 0))

    for asset in sorted(asset_trades.keys()):
        data = asset_trades[asset]
        print(f"  {asset:5s}: {data['count']:>5d} trades | ${data['volume']:>12,.2f} volume")

    # --- Save raw data for further analysis ---
    with open("trade_analysis_raw.json", "w") as f:
        json.dump({
            "all_trades_count": len(all_trades),
            "taker_trades_count": len(taker_trades),
            "maker_trades_count": maker_count,
            "pair_details": pair_details[:50],
        }, f, indent=2, default=str)
    print("\nRaw data saved to trade_analysis_raw.json")


if __name__ == "__main__":
    analyze()
