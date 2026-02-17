# Polymarket Arbitrage Strategy Analysis

## Target Account: Canine-Commandment (0x1d0034134e)

| Metric | Value |
|--------|-------|
| Address | `0x1d0034134e339a309700ff2d34e99fa2d48b0313` |
| Created | January 26, 2026 |
| Total Trades | 6,861 markets |
| Volume | $24,185,783 |
| Profit | $227,450 (~0.94% of volume) |
| Daily Profit | ~$12,000/day |
| Markets | Crypto Up/Down (5min, 15min) |

## Identified Strategy: Direction-Neutral Binary Arbitrage

### Core Concept

On Polymarket's crypto "Up or Down" binary markets, exactly **one outcome resolves to $1.00** and the other to $0.00. In a perfectly efficient market:

```
price_up + price_down = $1.00
```

However, real markets have temporary mispricings where:

```
price_up + price_down < $1.00
```

When this happens, buying **both sides** guarantees a risk-free profit:

```
Profit per share pair = $1.00 - price_up - price_down
```

## Deep Trade Analysis (from live data)

### Maker vs Taker Breakdown

The target account uses a **HYBRID** strategy (not pure maker or pure taker):

| Order Type | Count | Percentage |
|-----------|-------|------------|
| **TAKER** (market orders) | ~49% | Crosses the spread for immediate fill |
| **MAKER** (limit orders) | ~51% | Posts to book for rebates |

- **Maker avg price**: $0.4522 (buys at slightly cheaper prices)
- **Taker avg price**: $0.4974 (pays the ask price)

### Execution Speed

| Metric | Value |
|--------|-------|
| Same-second trades | **59.1%** of all trades |
| Within 5 seconds | **93.7%** |
| Within 10 seconds | **98.4%** |
| Max burst | **19 trades in 1 second** |
| Avg burst | 2.4 trades per timestamp |
| Mean interval | 1.41 seconds |

The bot fires **bursts of 4-19 orders simultaneously** across multiple markets, then pauses, then fires again.

### Position Sizing Logic

| Price Range | Avg Tokens/Trade | # Trades | Behavior |
|-------------|-----------------|----------|----------|
| $0.00-0.10 | 27 tokens | 91 | Small bets on extreme odds |
| $0.10-0.20 | 33 tokens | 113 | |
| $0.20-0.30 | 21 tokens | 191 | Smaller near low confidence |
| $0.30-0.40 | 28 tokens | 236 | |
| **$0.40-0.50** | **39 tokens** | **265** | **Largest near equilibrium** |
| **$0.50-0.60** | **41 tokens** | **303** | **Most active zone** |
| $0.60-0.70 | 38 tokens | 314 | High activity |
| $0.70-0.80 | 34 tokens | 229 | |
| $0.80-0.90 | 27 tokens | 141 | |
| $0.90-1.00 | 33-41 tokens | 117 | Near-certain outcomes |

Key insight: **Trades are largest at $0.40-$0.60** (equilibrium zone) where liquidity is deepest, and smaller at the extremes.

**Per-trade statistics:**
- Median: 20 tokens (~$8 USD)
- Mean: 34 tokens (~$17 USD)
- Max: 308 tokens (~$210 USD)

### Arbitrage Pair Performance

From 2,000 recent trades across 25 markets:

| Metric | Value |
|--------|-------|
| Arbitrage pairs (cost < $1.00) | **15 / 25 (60%)** |
| Loss pairs (cost >= $1.00) | 10 / 25 (40%) |
| Total estimated profit | **$833** (from this sample) |

**Top profitable pairs:**
| Market | Up Price | Down Price | Combined | Margin | Profit |
|--------|---------|------------|----------|--------|--------|
| BTC 12:45-12:50 5m | $0.1788 | $0.7078 | $0.8867 | 11.3% | $151 |
| BTC 1:00-1:15 15m | $0.7458 | $0.1713 | $0.9171 | 8.3% | $150 |
| BTC 12:45-1:00 15m | $0.1972 | $0.7601 | $0.9573 | 4.3% | $128 |
| BTC 12:55-1:00 5m | $0.5418 | $0.4345 | $0.9763 | 2.4% | $110 |
| BTC 1:05-1:10 5m | $0.7105 | $0.2320 | $0.9425 | 5.8% | $99 |

**Loss pairs (lessons learned):**
| Market | Combined | Loss |
|--------|----------|------|
| XRP 12:30-12:45 | $1.1316 | -13.2% |
| XRP 1:00-1:15 | $1.0571 | -5.7% |

Losses occur when the bot can't fill both sides at good enough prices — especially on less liquid assets like XRP.

### Asset Distribution

| Asset | Trades | Volume | % of Volume |
|-------|--------|--------|-------------|
| **BTC** | 1,412 | $29,588 | **89.6%** |
| XRP | 224 | $977 | 3.0% |
| ETH | 198 | $1,638 | 5.0% |
| SOL | 166 | $827 | 2.5% |

**BTC dominates** because it has the deepest liquidity and widest spreads.

## Bot Architecture (v2 — Speed Optimized)

```
main.py                     ← Orchestrator (polling + WebSocket modes)
├── ws_orderbook.py         ← NEW: Real-time orderbook via WebSocket (~50ms)
├── market_scanner.py       ← Discovers active crypto Up/Down markets
├── orderbook_analyzer.py   ← Analyzes orderbooks for arbitrage
├── trade_executor.py       ← UPDATED: Hybrid maker+taker, concurrent execution
├── account_tracker.py      ← Monitors target account's trades
├── analyze_account.py      ← NEW: Deep trade analysis script
└── config.py               ← Configuration & parameters
```

### Speed Optimizations

| Optimization | Before | After | Impact |
|-------------|--------|-------|--------|
| **Orderbook monitoring** | REST polling (2s) | WebSocket (50ms) | **40x faster detection** |
| **Order execution** | Sequential | Concurrent (ThreadPool) | **Both sides simultaneously** |
| **Position sizing** | Single large order | Multiple small orders | **Matches observed 20-token pattern** |
| **Strategy selection** | Always limit | Hybrid maker+taker | **Adapts to time remaining** |
| **Market discovery** | On each scan | Pre-registered + periodic refresh | **No discovery latency** |

### Latency Comparison

```
                    REST Polling Mode          WebSocket Mode
Market detection:   ~500ms API call            ~0ms (pre-registered)
Orderbook fetch:    ~200ms per token           ~50ms (push-based)
Arbitrage check:    ~1ms computation           ~1ms computation
Order placement:    ~100ms per order           ~100ms per order (concurrent)
─────────────────────────────────────────────────────────────
Total latency:      ~1000ms                    ~150ms
```

## Setup & Usage

### Prerequisites

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your wallet credentials
```

### Commands

```bash
# Analyze the target account's strategy
python main.py analyze

# Deep analysis with maker/taker + sizing breakdown
python main.py analyze --deep

# Scan for current arbitrage opportunities
python main.py scan

# Run in dry-run mode (REST polling, ~2s cycle)
python main.py run --dry-run

# Run in dry-run mode (WebSocket, ~50ms cycle) — RECOMMENDED
python main.py run --dry-run --ws

# Run in live mode with WebSocket (fastest)
python main.py run --live --ws
```

### Configuration

Key parameters in `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DRY_RUN` | `true` | Simulate trades without executing |
| `MAX_BET_SIZE` | `50.0` | Max USDC per arbitrage pair |
| `MIN_PROFIT_MARGIN` | `0.01` | Min profit per share pair ($) |
| `MAX_BANKROLL_FRACTION` | `0.05` | Max % of bankroll per trade |
| `SCAN_INTERVAL` | `2.0` | Seconds between scans (polling mode) |
| `ASSETS` | `btc,eth,sol,xrp` | Crypto assets to trade |
| `DURATIONS` | `5m,15m` | Market timeframes |

## Strategy Selection Logic

The bot adapts its order type based on time remaining and margin:

```
Time to expiry > 120s  →  MAKER only (limit orders, earn rebates)
Time to expiry 60-120s →  HYBRID (maker if thin margin, taker if wide)
Time to expiry 30-60s  →  TAKER preferred (speed over cost)
Time to expiry < 30s   →  TAKER only (must fill immediately)
```

"Wide margin" = profit > combined_cost × 1.5% × 2 (enough to absorb taker fees on both sides)

## Risk Factors

1. **Execution risk**: If only one side fills, you have directional exposure
2. **Latency**: Other bots compete for the same opportunities — **WebSocket mode is essential**
3. **Fee changes**: Polymarket may adjust taker/maker fee structure
4. **Liquidity**: Thin markets (XRP, SOL) have higher loss rates — **focus on BTC**
5. **Smart contract risk**: Underlying Polymarket contracts on Polygon
6. **Regulatory risk**: Prediction market regulations may change
7. **Adverse selection**: When margins are too wide, it may indicate stale orderbooks

## Recommendations

1. **Start with WebSocket dry-run** (`python main.py run --dry-run --ws`) to observe opportunities
2. **Focus on BTC markets** (89.6% of target's volume, best liquidity)
3. **Start small**: $10-50 per pair, increase as you validate
4. **Prefer maker orders** when time permits — saves 1.5% per side
5. **Monitor XRP carefully** — it showed the most loss pairs
6. **Run close to Polymarket's servers** for lowest latency
7. **Track actual vs expected P&L** to calibrate the strategy
