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

### How the Target Account Operates

1. **Monitors all crypto Up/Down markets** (BTC, ETH, SOL, XRP) across 5-minute and 15-minute timeframes
2. **Scans orderbooks** for both outcomes simultaneously
3. **When best_ask(Up) + best_ask(Down) < $1.00**, buys matched pairs of both sides
4. **Uses limit (maker) orders** to avoid taker fees (~1.5%) and earn maker rebates
5. **Executes within seconds** — all trades in a batch happen within 2-10 seconds
6. **Waits for resolution** → guaranteed $1.00 payout per matched pair

### Observed Trade Patterns

From the account's trading data:

- **Bitcoin 5m markets**: Up at $0.37-$0.73, Down at $0.20-$0.62
  - Combined minimum: ~$0.57-$0.70 (30-43% margin)
- **Bitcoin 15m markets**: Up at $0.07-$0.34, Down at $0.70-$0.82
  - Combined minimum: ~$0.77-$0.89 (11-23% margin)
- **Solana 15m markets**: Up at $0.18-$0.48, Down at $0.59-$0.85
  - Combined minimum: ~$0.77-$0.85 (15-23% margin)

### Why This Works

1. **Short-duration markets** (5min, 15min) have higher volatility in pricing
2. **New markets open every 5/15 minutes**, creating constant fresh opportunities
3. **Thin liquidity** in newly opened markets → wider spreads → more mispricing
4. **Emotional traders** overweight one side, pushing prices out of equilibrium
5. **Capital recycling**: Markets resolve quickly, freeing capital for next opportunity

### Profit Math

```
Per trade example:
  Buy 1000 shares of Up @ $0.40    = $400
  Buy 1000 shares of Down @ $0.50  = $500
  Total cost                        = $900
  Guaranteed payout (one wins)      = $1,000
  Profit                            = $100 (11.1% ROI)

At scale (daily):
  ~360 markets/day × $3,500 avg per market = $1.26M daily volume
  × 0.94% avg margin = ~$12,000 daily profit
```

### Fee Considerations

- **Taker fee**: ~1.5% on crypto markets → eats into margins
- **Maker rebates**: Variable, distributed daily in USDC
- **Strategy**: Always use **limit orders** (maker) to avoid fees and earn rebates
- Minimum viable margin after fees: ~$0.02-0.03 per share pair

## Bot Architecture

```
main.py                  ← Entry point & orchestrator
├── market_scanner.py    ← Discovers active crypto Up/Down markets
├── orderbook_analyzer.py← Analyzes orderbooks for arbitrage
├── trade_executor.py    ← Executes trades via CLOB Client
├── account_tracker.py   ← Monitors target account's trades
└── config.py            ← Configuration & parameters
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
# 1. Analyze the target account's strategy
python main.py analyze

# 2. Scan for current arbitrage opportunities
python main.py scan

# 3. Run in dry-run mode (simulation, no real trades)
python main.py run --dry-run

# 4. Run in live mode (executes real trades)
python main.py run --live
```

### Configuration

Key parameters in `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DRY_RUN` | `true` | Simulate trades without executing |
| `MAX_BET_SIZE` | `50.0` | Max USDC per arbitrage pair |
| `MIN_PROFIT_MARGIN` | `0.01` | Min profit per share pair ($) |
| `MAX_BANKROLL_FRACTION` | `0.05` | Max % of bankroll per trade |
| `SCAN_INTERVAL` | `2.0` | Seconds between scans |
| `ASSETS` | `btc,eth,sol,xrp` | Crypto assets to trade |
| `DURATIONS` | `5m,15m` | Market timeframes |

## Risk Factors

1. **Execution risk**: If only one side fills, you have directional exposure
2. **Latency**: Other bots compete for the same opportunities
3. **Fee changes**: Polymarket may adjust taker/maker fee structure
4. **Liquidity**: Thin markets may not have enough shares for profitable execution
5. **Smart contract risk**: Underlying Polymarket contracts on Polygon
6. **Regulatory risk**: Prediction market regulations may change

## Scaling Considerations

- Start with small positions ($10-50 per arbitrage pair)
- Monitor fill rates — if orders aren't filling, adjust to market orders
- Track actual vs. expected P&L to validate the strategy
- Gradually increase position sizes as you confirm profitability
- Consider running multiple instances for different asset pairs
