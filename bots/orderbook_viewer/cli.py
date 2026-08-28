"""CLI entry point for the orderbook viewer bot.

Usage (anonymous, public-only)::

    python -m bots.orderbook_viewer --symbol BTC/USDT

Usage (authenticated, with balances + private channel)::

    python -m bots.orderbook_viewer --symbol BTC/USDT \\
        --login trader@exchange.local --password admin123

Credentials can also be supplied via environment variables
``EXCHANGE_LOGIN`` / ``EXCHANGE_PASSWORD`` to avoid putting them on the
command line (which leaks them into the shell history and `ps`).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .viewer import OrderbookViewer
from .auth_viewer import AuthenticatedOrderbookViewer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bots.orderbook_viewer",
        description="Live L2 orderbook viewer for the crypto-exchange sandbox.",
    )
    p.add_argument(
        "--symbol", "-s",
        required=True,
        help="Trading pair symbol, e.g. BTC/USDT",
    )
    p.add_argument(
        "--ws-url", "--url", "-u",
        dest="ws_url",
        default="ws://localhost:8000",
        help="Base exchange WS URL (default: ws://localhost:8000)",
    )
    p.add_argument(
        "--http-url",
        default="http://localhost:8000",
        help="Base exchange HTTP URL for REST login (default: http://localhost:8000)",
    )
    p.add_argument(
        "--depth", "-d",
        type=int, default=10,
        help="How many levels to show on each side (default: 10)",
    )
    p.add_argument(
        "--price-precision",
        type=int, default=2,
        help="Decimal places for prices (default: 2)",
    )
    p.add_argument(
        "--qty-precision",
        type=int, default=4,
        help="Decimal places for quantities (default: 4)",
    )
    p.add_argument(
        "--login",
        default=os.environ.get("EXCHANGE_LOGIN"),
        help="Login (email). If set, enables authenticated mode with balance "
             "panel + private WS. Can also be set via EXCHANGE_LOGIN env var.",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("EXCHANGE_PASSWORD"),
        help="Password. Can also be set via EXCHANGE_PASSWORD env var.",
    )
    p.add_argument(
        "--balance-refresh",
        type=float, default=30.0,
        help="Interval (seconds) for periodic REST balance re-fetch (default: 30)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return p.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,  # stdout would corrupt the Rich live display
    )


async def async_main(args: argparse.Namespace) -> int:
    # Pick the right viewer based on whether --login was supplied
    if args.login:
        if not args.password:
            print("Error: --password (or EXCHANGE_PASSWORD env var) is required "
                  "when --login is set.", file=sys.stderr)
            return 2
        viewer = AuthenticatedOrderbookViewer(
            url=args.ws_url,
            http_url=args.http_url,
            symbol=args.symbol,
            login=args.login,
            password=args.password,
            depth=args.depth,
            price_precision=args.price_precision,
            qty_precision=args.qty_precision,
            balance_refresh_interval=args.balance_refresh,
        )
    else:
        viewer = OrderbookViewer(
            url=args.ws_url,
            symbol=args.symbol,
            depth=args.depth,
            price_precision=args.price_precision,
            qty_precision=args.qty_precision,
        )

    # Graceful Ctrl+C
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        viewer.stop()
        # AuthenticatedOrderbookViewer uses tasks internally — also signal stop
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows
            pass

    # For the authenticated viewer, .run() handles its own task management;
    # we just need to wait for it to finish or for the stop signal.
    viewer_task = asyncio.create_task(viewer.run())
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        {viewer_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc and not isinstance(exc, asyncio.CancelledError):
            raise exc

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
