"""CLI entry point for the grid bot.

Examples::

    # Dry-run: just print the plan, don't place anything
    python -m bots.grid_bot -s BTC/USDT -l trader@exchange.local -p admin123 \\
        --center 100000 --levels 5 --spacing 0.5 --quantity 0.01 --dry-run

    # Live: place the grid, monitor fills, replenish on fill
    python -m bots.grid_bot -s BTC/USDT -l trader@exchange.local -p admin123 \\
        --center 100000 --levels 5 --spacing 0.5 --quantity 0.01 --maintain

    # Auto-center from current mid-price, no maintain (grid depletes as it fills)
    python -m bots.grid_bot -s BTC/USDT -l trader@exchange.local -p admin123 \\
        --levels 5 --spacing 0.5 --quantity 0.01

Credentials can also come from EXCHANGE_LOGIN / EXCHANGE_PASSWORD env vars.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal, InvalidOperation

from .bot import GridBot


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bots.grid_bot",
        description="Grid market-making bot for the crypto-exchange sandbox.",
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

    # Grid params
    p.add_argument("--center", type=str, default=None,
                   help="Center price. If omitted, uses current mid-price "
                        "from the public orderbook WS.")
    p.add_argument("--levels", "-n", type=int, default=5,
                   help="Levels per side (default: 5)")
    p.add_argument("--spacing", type=float, default=0.5,
                   help="Spacing between levels as %% of center (default: 0.5)")
    p.add_argument("--quantity", "-q", type=str, default="0.01",
                   help="Base-asset quantity per order (default: 0.01)")
    p.add_argument("--price-precision", type=int, default=2,
                   help="Decimal places for prices (default: 2)")
    p.add_argument("--qty-precision", type=int, default=6,
                   help="Decimal places for quantities (default: 6)")

    # Behavior
    p.add_argument("--maintain", action="store_true",
                   help="Replenish orders at the same price when they fill "
                        "(grid keeps its shape). Without this, the grid "
                        "depletes as orders fill.")
    p.add_argument("--no-cancel-on-exit", dest="cancel_on_exit",
                   action="store_false", default=True,
                   help="Leave orders open when the bot exits (default: "
                        "cancel all on exit).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print the plan, but do NOT place orders.")

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

    # Parse center price (if provided) into Decimal
    center: Decimal | None = None
    if args.center is not None:
        try:
            center = Decimal(args.center)
        except InvalidOperation:
            print(f"Error: invalid --center value: {args.center!r}", file=sys.stderr)
            return 2

    bot = GridBot(
        symbol=args.symbol,
        login=args.login,
        password=args.password,
        http_url=args.http_url,
        ws_url=args.ws_url,
        center=center,
        levels=args.levels,
        spacing_pct=args.spacing,
        quantity=args.quantity,
        maintain=args.maintain,
        cancel_on_exit=args.cancel_on_exit,
        dry_run=args.dry_run,
        price_precision=args.price_precision,
        qty_precision=args.qty_precision,
    )

    # Graceful Ctrl+C
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            # Windows
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
