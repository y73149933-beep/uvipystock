"""Orderbook Viewer bot.

Standalone async client that connects to the exchange public WebSocket
(`/api/v1/ws/orderbook/{symbol}`) and renders a live L2 orderbook in the
terminal.

Designed as the foundation for the future market-maker bot:
  * `ExchangeWSClient` is a generic reconnecting WS subscriber
    (will be reused for the private channel + HMAC REST later).
  * `LocalOrderBook` is the in-memory L2 state that the market maker
    will consult to compute fair price / spread.
  * `OrderbookViewer` is just one consumer of those two — the market
    maker will be another consumer that, instead of printing, places
    orders via the HMAC REST API.

Run with::

    python -m bots.orderbook_viewer --symbol BTC/USDT --url ws://localhost:8000
"""

__version__ = "0.1.0"
