"""Spread-filling market maker for virtual IPO scenarios.

Watches the live order book, finds the best bid/ask, and fills the
spread between them with limit orders. Adapts to trade direction:
if the last trade was a buy, skews the grid to pull buy orders up
(momentum-following). If only one side of the book exists, anchors
from that side.

Unlike `grid_bot` (one-shot placement), this bot runs continuously:
it rebalances whenever the market moves significantly.
"""

__version__ = "0.1.0"
