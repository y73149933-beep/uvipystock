"""CLI entry point for the spread-filler bot.

Examples::

    # Dry-run: watch the market and print plans, but don't place orders
    python -m bots.spread_filler -s BTC/USDT \\
        -l trader@exchange.local -p admin123 --dry-run

    # Live: continuously fill the spread, rebalancing every 5s
    python -m bots.spread_filler -s BTC/USDT \\
        -l trader@exchange.local -p admin123 \\
        --levels 10 --quantity 0.01 --bias 0.3

    # Aggressive bias toward last-trade direction, tight throttle
    python -m bots.spread_filler -s NEWCOIN/USDT \\
        -l trader@exchange.local -p admin123 \\
        --levels 8 --quantity 0.5 --bias 0.6 --throttle 2
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal, InvalidOperation

from .bot import SpreadFillerBot


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bots.spread_filler",
        description="Spread-filling market maker for the crypto-exchange sandbox.",
    )
    # Connection
    p.add_argument("--symbol", "-s", required=True,
                   help="Trading pair, e.g. BTC/USDT")
    p.add_argument("--login", "-l",
                   default=os.environ.get("EXCHANGE_LOGIN"),
                   help="Login email (env: EXCHANGE_LOGIN)")
    p.add_argument("--password", "-p",
                   default=os.environ.get("EXCHANGE_PASSWORD"),
                   help="Password (env: EXCHANGE_PASSWORD)")
    p.add_argument("--http-url", default="http://localhost:8000",
                   help="Base HTTP URL (default: http://localhost:8000)")
    p.add_argument("--ws-url", default="ws://localhost:8000",
                   help="Base WS URL (default: ws://localhost:8000)")

    # Strategy params
    p.add_argument("--levels", "-n", type=int, default=10,
                   help="Total levels to place, split between sides (default: 10)")
    p.add_argument("--quantity", "-q", type=str, default="0.01",
                   help="Base-asset quantity per order (default: 0.01)")
    p.add_argument("--bias", type=float, default=0.3,
                   help="Direction skew strength 0..1 (default: 0.3). "
                        "0 = always symmetric; 1 = fully one-sided.")
    p.add_argument("--tick-size", type=str, default="0.01",
                   help="Minimum price increment (default: 0.01)")
    p.add_argument("--price-precision", type=int, default=2,
                   help="Decimal places for prices (default: 2)")
    p.add_argument("--qty-precision", type=int, default=6,
                   help="Decimal places for quantities (default: 6)")
    p.add_argument("--single-side-buffer", type=float, default=1.0,
                   help="When only one side exists, estimate the other "
                        "this far away as %% of known price (default: 1.0)")

    # Rebalance behavior
    p.add_argument("--rebalance-threshold", type=float, default=0.3,
                   help="Min mid-price move (%%) to trigger rebalance "
                        "(default: 0.3)")
    p.add_argument("--throttle", type=float, default=5.0,
                   help="Min seconds between rebalances (default: 5.0)")

    # Behavior
    p.add_argument("--no-cancel-on-exit", dest="cancel_on_exit",
                   action="store_false", default=True,
                   help="Leave orders open when the bot exits.")
    p.add_argument("--dry-run", action="store_true",
                   help="Watch the market and print plans, but do NOT place orders.")

    # Logging
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging verbosity (default: INFO)")
    return p.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


async def async_main(args: argparse.Namespace) -> int:
    if not args.login or not args.password:
        print("Error: --login and --password are required "
              "(or set EXCHANGE_LOGIN / EXCHANGE_PASSWORD env vars).",
              file=sys.stderr)
        return 2

    bot = SpreadFillerBot(
        symbol=args.symbol,
        login=args.login,
        password=args.password,
        http_url=args.http_url,
        ws_url=args.ws_url,
        levels=args.levels,
        quantity=args.quantity,
        bias=args.bias,
        tick_size=args.tick_size,
        price_precision=args.price_precision,
        qty_precision=args.qty_precision,
        rebalance_threshold_pct=args.rebalance_threshold,
        throttle_seconds=args.throttle,
        single_side_buffer_pct=args.single_side_buffer,
        dry_run=args.dry_run,
        cancel_on_exit=args.cancel_on_exit,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            pass

    return await bot.run()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
