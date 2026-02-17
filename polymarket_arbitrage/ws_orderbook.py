"""
WebSocket-based real-time orderbook monitor.

Instead of polling the REST API every ~2 seconds, we subscribe to orderbook
updates via WebSocket and get sub-100ms updates. This is critical for
capturing arbitrage opportunities before other bots.

Market channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
No authentication required for market data.

Event types:
  - book: Full orderbook snapshot (bids + asks)
  - price_change: Incremental update when orders placed/cancelled
  - last_trade_price: Trade execution event
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from collections import defaultdict

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_ASSETS_PER_CONNECTION = 450  # Polymarket limit ~500, leave some headroom


@dataclass
class OrderLevel:
    price: float
    size: float


@dataclass
class LiveOrderbook:
    """Real-time orderbook maintained via WebSocket updates."""
    asset_id: str
    market: str  # condition_id
    bids: list[OrderLevel] = field(default_factory=list)
    asks: list[OrderLevel] = field(default_factory=list)
    last_update: float = 0.0
    hash: str = ""

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else float("inf")

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def age_ms(self) -> float:
        return (time.time() - self.last_update) * 1000


class OrderbookManager:
    """
    Manages real-time orderbooks for multiple assets via WebSocket.

    Provides instant access to current orderbook state and emits
    callbacks when arbitrage conditions may exist.
    """

    def __init__(self):
        # asset_id -> LiveOrderbook
        self.orderbooks: dict[str, LiveOrderbook] = {}
        # condition_id -> list of asset_ids (Up token, Down token)
        self.market_tokens: dict[str, list[str]] = {}
        # Callback for arbitrage detection
        self._on_update_callbacks: list = []
        self._ws_connections: list = []
        self._running = False
        self._subscribed_assets: set[str] = set()
        # Stats
        self.stats = {
            "messages_received": 0,
            "book_updates": 0,
            "price_changes": 0,
            "errors": 0,
        }

    def register_market(self, condition_id: str, up_token_id: str, down_token_id: str):
        """Register a binary market's token pair for tracking."""
        self.market_tokens[condition_id] = [up_token_id, down_token_id]

        for token_id in [up_token_id, down_token_id]:
            if token_id not in self.orderbooks:
                self.orderbooks[token_id] = LiveOrderbook(
                    asset_id=token_id, market=condition_id
                )

    def on_update(self, callback):
        """Register callback: called with (condition_id, up_book, down_book) on updates."""
        self._on_update_callbacks.append(callback)

    def get_orderbook(self, asset_id: str) -> LiveOrderbook | None:
        return self.orderbooks.get(asset_id)

    def get_market_books(self, condition_id: str) -> tuple[LiveOrderbook | None, LiveOrderbook | None]:
        """Get both orderbooks for a binary market."""
        tokens = self.market_tokens.get(condition_id, [])
        if len(tokens) != 2:
            return None, None
        return self.orderbooks.get(tokens[0]), self.orderbooks.get(tokens[1])

    async def start(self):
        """Start WebSocket connections for all registered assets."""
        self._running = True
        asset_ids = list(self.orderbooks.keys())

        if not asset_ids:
            logger.warning("No assets registered for WebSocket monitoring")
            return

        # Split into chunks for multiple connections if needed
        chunks = []
        for i in range(0, len(asset_ids), MAX_ASSETS_PER_CONNECTION):
            chunks.append(asset_ids[i : i + MAX_ASSETS_PER_CONNECTION])

        logger.info(
            f"Starting {len(chunks)} WebSocket connection(s) "
            f"for {len(asset_ids)} assets"
        )

        tasks = [self._run_connection(chunk) for chunk in chunks]
        await asyncio.gather(*tasks)

    async def stop(self):
        """Stop all WebSocket connections."""
        self._running = False
        for ws in self._ws_connections:
            await ws.close()
        self._ws_connections.clear()

    async def _run_connection(self, asset_ids: list[str]):
        """Manage a single WebSocket connection with auto-reconnection."""
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,  # 10MB max message
                ) as ws:
                    self._ws_connections.append(ws)
                    logger.info(f"WebSocket connected, subscribing to {len(asset_ids)} assets")

                    # Subscribe
                    sub_msg = json.dumps({
                        "assets_ids": asset_ids,
                        "type": "market",
                    })
                    await ws.send(sub_msg)
                    self._subscribed_assets.update(asset_ids)

                    # Process messages
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            self._process_message(raw_msg)
                        except Exception as e:
                            logger.debug(f"Error processing message: {e}")
                            self.stats["errors"] += 1

            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed, reconnecting in 1s...")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"WebSocket error: {e}, reconnecting in 2s...")
                self.stats["errors"] += 1
                await asyncio.sleep(2)

    async def subscribe_new_assets(self, asset_ids: list[str]):
        """Dynamically subscribe to new assets on existing connections."""
        new_ids = [a for a in asset_ids if a not in self._subscribed_assets]
        if not new_ids or not self._ws_connections:
            return

        ws = self._ws_connections[0]  # Use first connection
        try:
            sub_msg = json.dumps({
                "assets_ids": new_ids,
                "type": "market",
                "operation": "subscribe",
            })
            await ws.send(sub_msg)
            self._subscribed_assets.update(new_ids)
            logger.info(f"Subscribed to {len(new_ids)} new assets")
        except Exception as e:
            logger.error(f"Failed to subscribe new assets: {e}")

    def _process_message(self, raw_msg: str):
        """Process a WebSocket message and update orderbooks."""
        self.stats["messages_received"] += 1

        # Messages can be arrays
        msgs = json.loads(raw_msg)
        if not isinstance(msgs, list):
            msgs = [msgs]

        for msg in msgs:
            event_type = msg.get("event_type", "")

            if event_type == "book":
                self._handle_book_event(msg)
            elif event_type == "price_change":
                self._handle_price_change(msg)
            elif event_type == "last_trade_price":
                self._handle_last_trade(msg)

    def _handle_book_event(self, msg: dict):
        """Handle full orderbook snapshot."""
        self.stats["book_updates"] += 1
        asset_id = msg.get("asset_id", "")

        book = self.orderbooks.get(asset_id)
        if not book:
            return

        # Parse bids (sorted descending by price)
        bids = []
        for level in msg.get("bids", msg.get("buys", [])):
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if size > 0:
                bids.append(OrderLevel(price, size))
        bids.sort(key=lambda x: x.price, reverse=True)

        # Parse asks (sorted ascending by price)
        asks = []
        for level in msg.get("asks", msg.get("sells", [])):
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if size > 0:
                asks.append(OrderLevel(price, size))
        asks.sort(key=lambda x: x.price)

        book.bids = bids
        book.asks = asks
        book.last_update = time.time()
        book.hash = msg.get("hash", "")

        self._notify_update(book)

    def _handle_price_change(self, msg: dict):
        """Handle incremental price change (order placed/cancelled)."""
        self.stats["price_changes"] += 1
        # price_change events have the same structure as book events
        # but represent incremental changes
        self._handle_book_event(msg)

    def _handle_last_trade(self, msg: dict):
        """Handle trade execution event."""
        # Useful for monitoring but not critical for arbitrage detection
        pass

    def _notify_update(self, updated_book: LiveOrderbook):
        """Notify callbacks when an orderbook updates."""
        condition_id = updated_book.market
        tokens = self.market_tokens.get(condition_id, [])

        if len(tokens) != 2:
            return

        up_book = self.orderbooks.get(tokens[0])
        down_book = self.orderbooks.get(tokens[1])

        if not up_book or not down_book:
            return

        for callback in self._on_update_callbacks:
            try:
                callback(condition_id, up_book, down_book)
            except Exception as e:
                logger.error(f"Callback error: {e}")
