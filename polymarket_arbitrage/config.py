"""Configuration for the Polymarket arbitrage bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- API Endpoints ---
CLOB_API_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# --- Wallet ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))

# --- Trading Parameters ---
MAX_BET_SIZE = float(os.getenv("MAX_BET_SIZE", "50.0"))
MIN_PROFIT_MARGIN = float(os.getenv("MIN_PROFIT_MARGIN", "0.01"))
MAX_BANKROLL_FRACTION = float(os.getenv("MAX_BANKROLL_FRACTION", "0.05"))
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "2.0"))

# --- Market Filters ---
# Actual Polymarket availability:
#   5m  → BTC only
#   15m → BTC, ETH, SOL, XRP
ASSET_DURATION_PAIRS = [
    ("btc", "5m"),
    ("btc", "15m"),
    ("eth", "15m"),
    ("sol", "15m"),
    ("xrp", "15m"),
]

# Flat lists kept for backward compatibility / .env override
ASSETS = list(dict.fromkeys(a for a, _ in ASSET_DURATION_PAIRS))
DURATIONS = list(dict.fromkeys(d for _, d in ASSET_DURATION_PAIRS))

MARKET_SLUG_PATTERNS = [f"{a}-updown-{d}" for a, d in ASSET_DURATION_PAIRS]

# --- Dry Run ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# --- Target Account (for tracking) ---
TARGET_ACCOUNT_ADDRESS = "0x1d0034134e339a309700ff2d34e99fa2d48b0313"
TARGET_ACCOUNT_PROFILE = "0x1d0034134e"

# --- Fee Configuration ---
# Polymarket taker fee on 15-min crypto markets (~1.5% at 50c)
# Maker orders earn rebates instead of paying fees
# Always prefer limit (maker) orders
PREFER_MAKER_ORDERS = True
MAKER_REBATE_RATE = 0.0  # varies, set to 0 for conservative estimates
TAKER_FEE_RATE = 0.015   # ~1.5% taker fee on crypto markets
