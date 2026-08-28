"""Grid market-making bot.

Places a ladder of limit orders on both sides of a center price and
harvests the spread as orders fill. The first concrete strategy built
on top of the shared `orderbook_viewer` infrastructure
(`ExchangeRESTClient`, `PrivateChannelClient`, `LocalOrderBook`).

Run with::

    python -m bots.grid_bot \\
        --symbol BTC/USDT \\
        --login trader@exchange.local \\
        --password admin123 \\
        --center 100000 \\
        --levels 5 \\
        --spacing 0.5 \\
        --quantity 0.01
"""

__version__ = "0.1.0"
