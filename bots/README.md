# Bots

Standalone async clients that talk to the exchange via its public API
(WebSocket + HMAC-signed REST). The bots package is deliberately kept
separate from `backend/` so it can be developed, versioned, and run
independently.

## Why a separate package

* The backend ships heavy deps (SQLAlchemy, asyncpg, Cython, Redis).
  A trading bot only needs a WebSocket client + an HTTP client + a few
  helpers — installing the entire backend to run a bot is overkill.
* Bots are clients of the exchange. Treating them as such forces the
  bot author to use the same API surface external users would use,
  which is the best possible integration test.
* The same `bots/` directory will host the upcoming market-maker bot,
  arbitrage scripts, perf benchmarks, etc.

## Current bots

### `orderbook_viewer`

Connects to the public WebSocket `/api/v1/ws/orderbook/{symbol}` and
renders a live L2 book in the terminal. Two modes:

```bash
pip install -r bots/requirements.txt
python -m bots.orderbook_viewer --symbol BTC/USDT
```

#### Authenticated (adds balance panel + private channel)

```bash
python -m bots.orderbook_viewer \
    --symbol BTC/USDT \
    --login trader@exchange.local \
    --password admin123
```

In authenticated mode the bot:

1. Logs in via `POST /api/v1/auth/login` to obtain an HMAC API key+secret.
2. Fetches the current balance snapshot via `GET /api/v1/balance`.
3. Opens the **public WS** for the orderbook (as in anonymous mode).
4. Opens the **private WS** (`/api/v1/ws/private`) with HMAC handshake —
   receives live `balance` and `order` events.
5. Periodically re-fetches balances via REST (default every 30s) as a
   safety net, since Redis Pub/Sub is fire-and-forget.

Credentials can also be supplied via `EXCHANGE_LOGIN` / `EXCHANGE_PASSWORD`
environment variables (recommended for shared machines — avoids leaking
the password into the shell history and `ps`).

Layout:

```
┌── BTC/USDT  orderbook  ·  trader@exchange.local ──────────────────────────┐
│       price     qty     depth-bar                                          │
│  ── asks (worst → best) ──                                                 │
│   104500.00   1.2345  ████████████░░░░░░░░                                  │
│   104400.00   3.1000  ██████████████████████               ← best ask      │
│  ── spread: 5.00 (0.0048%)   mid: 104397.50   last: 104380 ──              │
│   104395.00   2.5000  ████████████████░░░░░░               ← best bid      │
│   104380.00   1.1000  ███████░░░░░░░░░░░░░░░                                │
│  ── balances ──                                                            │
│   BTC     1.5000   1.5000   —      ████████████                            │
│   USDT  5000.00   4500.00   500.00   ██████████▒▒                           │
│  ── open orders ──                                                         │
│   42  BUY   LMT   99500.00   0.5000   0.0000  new                          │
│   43  SELL  LMT  100500.00   0.5000   0.2500  partial                      │
│   44  BUY   POST  99000.00   1.0000   0.0000  new                          │
│  ── recent trades ──                                                       │
│  SELL  104395.00   0.12   1716543200123                                    │
│  BUY   104390.00   0.05   1716543199988                                    │
│  BTC/USDT | public: connected | connects: 1 | snapshots: 1 | ...          │
│  user: trader@exchange.local (id=2) | private: auth_ok | open_orders: 3   │
│  order_updates: 5 | filled: 1 | canceled: 0                               │
└────────────────────────────────────────────────────────────────────────────┘
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--symbol`, `-s` | *(required)* | Trading pair, e.g. `BTC/USDT` |
| `--ws-url`, `-u` | `ws://localhost:8000` | Base WS URL |
| `--http-url` | `http://localhost:8000` | Base HTTP URL for REST login |
| `--depth`, `-d` | `10` | Levels per side |
| `--price-precision` | `2` | Decimal places for prices |
| `--qty-precision` | `4` | Decimal places for quantities |
| `--login` | *(env: `EXCHANGE_LOGIN`)* | Login email; enables authenticated mode |
| `--password` | *(env: `EXCHANGE_PASSWORD`)* | Password (required with `--login`) |
| `--balance-refresh` | `30` | Seconds between REST balance re-fetches |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### `grid_bot`

A simple grid market-making bot. Places a symmetric ladder of limit
orders around a center price and harvests the spread as orders fill.

```bash
# Dry-run: just print the plan, no orders placed
python -m bots.grid_bot -s BTC/USDT \
    -l trader@exchange.local -p admin123 \
    --center 100000 --levels 5 --spacing 0.5 --quantity 0.01 --dry-run

# Live: place the grid, monitor fills, replenish on fill (grid keeps its shape)
python -m bots.grid_bot -s BTC/USDT \
    -l trader@exchange.local -p admin123 \
    --center 100000 --levels 5 --spacing 0.5 --quantity 0.01 --maintain

# Auto-center from current mid-price (no --center), grid depletes as it fills
python -m bots.grid_bot -s BTC/USDT \
    -l trader@exchange.local -p admin123 \
    --levels 5 --spacing 0.5 --quantity 0.01
```

Sample output (dry-run plan):

```
======================================================================
GRID PLAN for BTC/USDT
  center:        100000
  levels/side:   5
  spacing:       0.5%
  quantity/level:0.01 BTC
  total orders:  10
  buy  capital:  4925.00 USDT (sum of price×qty)
  sell capital:  0.05 BTC (sum of qty)
----------------------------------------------------------------------
  side   lvl   id#          price          qty
  SELL   5     1        102500.00     0.010000
  SELL   4     2        102000.00     0.010000
  SELL   3     3        101500.00     0.010000
  SELL   2     4        101000.00     0.010000
  SELL   1     5        100500.00     0.010000
  BUY    1     6         99500.00     0.010000
  BUY    2     7         99000.00     0.010000
  BUY    3     8         98500.00     0.010000
  BUY    4     9         98000.00     0.010000
  BUY    5    10        97500.00     0.010000
======================================================================
```

#### How it works

1. Login via `POST /api/v1/auth/login` → obtain HMAC credentials.
2. Resolve center price: from `--center`, or from the public WS
   orderbook's mid-price (auto mode).
3. Compute the grid via `calculate_grid(...)` (pure function, unit-tested).
4. Pre-flight: fetch balances, log a warning if there's not enough
   quote currency for the buy side or base currency for the sell side.
5. Place all orders in one atomic bulk request
   (`POST /api/v1/orders/bulk`, All-or-Nothing semantics).
6. Open the private WS (`/ws/private`) to monitor `order` events.
7. On each fill:
   - If `--maintain` is set, place a new LIMIT order at the same price
     (the grid keeps its shape indefinitely).
   - Otherwise, just log the fill (the grid depletes as orders fill).
8. On Ctrl+C: cancel all remaining orders on the symbol
   (`DELETE /api/v1/orders/bulk?symbol=...&cancel_all=true`), print a
   summary, exit.

#### CLI options

| Flag | Default | Description |
|---|---|---|
| `--symbol`, `-s` | *(required)* | Trading pair, e.g. `BTC/USDT` |
| `--login`, `-l` | *(env: `EXCHANGE_LOGIN`)* | Login email |
| `--password`, `-p` | *(env: `EXCHANGE_PASSWORD`)* | Password |
| `--http-url` | `http://localhost:8000` | Base HTTP URL |
| `--ws-url` | `ws://localhost:8000` | Base WS URL |
| `--center` | *(auto from mid-price)* | Center price |
| `--levels`, `-n` | `5` | Levels per side |
| `--spacing` | `0.5` | Spacing between levels, % of center |
| `--quantity`, `-q` | `0.01` | Base-asset quantity per order |
| `--price-precision` | `2` | Decimal places for prices |
| `--qty-precision` | `6` | Decimal places for quantities |
| `--maintain` | off | Replenish orders at the same price on fill |
| `--no-cancel-on-exit` | off | Leave orders open when the bot exits |
| `--dry-run` | off | Print the plan only, don't place orders |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### `spread_filler`

A continuous spread-filling market maker for virtual-IPO scenarios.
Watches the live order book, finds best bid/ask, fills the spread
between them with limit orders. Adapts to trade direction: if the
last trade was a buy, skews the grid to pull buy orders up. If only
one side of the book exists, anchors from that side.

Unlike `grid_bot` (one-shot), this bot runs **continuously**: it
rebalances whenever the market moves significantly.

```bash
# Dry-run: watch the market and print plans, but don't place orders
python -m bots.spread_filler -s BTC/USDT \
    -l trader@exchange.local -p admin123 --dry-run

# Live: continuously fill the spread, rebalancing every 5s
python -m bots.spread_filler -s BTC/USDT \
    -l trader@exchange.local -p admin123 \
    --levels 10 --quantity 0.01 --bias 0.3

# Aggressive bias, tight throttle (for a new IPO token)
python -m bots.spread_filler -s NEWCOIN/USDT \
    -l trader@exchange.local -p admin123 \
    --levels 8 --quantity 0.5 --bias 0.6 --throttle 2
```

#### How it works

The bot handles three market states:

1. **Both sides present** — fills the spread `[best_bid, best_ask]`.
   Buys go in the lower portion, sells in the upper. The boundary
   shifts up (more room for buys) when the last trade was a buy.
2. **Only bid present** — synthesizes an ask at
   `best_bid × (1 + buffer%)` and proceeds as case 1.
3. **Only ask present** — synthesizes a bid symmetrically.

Direction bias (`--bias 0..1`):
- Last trade = **buy** → price pressure up → more buy levels,
  boundary shifts toward the ask (buys get pulled up).
- Last trade = **sell** → more sell levels, boundary shifts toward
  the bid.
- No trades yet → symmetric 50/50 split.

Rebalance triggers:
- Mid-price moved > `--rebalance-threshold` % since last placement.
- Last trade direction flipped.
- Throttled to at most once per `--throttle` seconds.
- Own order filled → immediate rebalance (market moved through us).

Feedback loop avoidance: when reading best_bid/ask from the local
book, the bot **excludes its own order prices** — otherwise it would
see its own orders as the "market" and never detect real movement.

#### CLI options

| Flag | Default | Description |
|---|---|---|
| `--symbol`, `-s` | *(required)* | Trading pair |
| `--login`, `-l` | *(env)* | Login email |
| `--password`, `-p` | *(env)* | Password |
| `--http-url` | `http://localhost:8000` | Base HTTP URL |
| `--ws-url` | `ws://localhost:8000` | Base WS URL |
| `--levels`, `-n` | `10` | Total levels (split between sides) |
| `--quantity`, `-q` | `0.01` | Base-asset quantity per order |
| `--bias` | `0.3` | Direction skew strength 0..1 |
| `--tick-size` | `0.01` | Minimum price increment |
| `--price-precision` | `2` | Decimal places for prices |
| `--qty-precision` | `6` | Decimal places for quantities |
| `--single-side-buffer` | `1.0` | % to estimate missing side |
| `--rebalance-threshold` | `0.3` | Min mid-price move % to trigger |
| `--throttle` | `5.0` | Min seconds between rebalances |
| `--no-cancel-on-exit` | off | Leave orders open on exit |
| `--dry-run` | off | Watch + print, don't place orders |
| `--log-level` | `INFO` | Logging verbosity |

## Architecture (foundation for the market maker)

```
bots/
├── orderbook_viewer/      # Live L2 viewer + balances + open orders
│   ├── __init__.py
│   ├── __main__.py            # python -m bots.orderbook_viewer
│   ├── cli.py                 # argparse + asyncio.run + signal handling
│   ├── client.py              # ExchangeWSClient: public WS (reconnecting)
│   ├── private_client.py      # PrivateChannelClient: private WS + HMAC handshake
│   ├── hmac_rest_client.py    # ExchangeRESTClient: login + HMAC-signed REST
│   ├── book.py                # LocalOrderBook: snapshot + delta state
│   ├── balances.py            # BalanceTracker: REST snapshot + WS live updates
│   ├── orders.py              # OrderTracker: REST snapshot + WS lifecycle events
│   ├── display.py             # Rich live terminal panel (book + balances + tape)
│   ├── viewer.py              # OrderbookViewer: public-only mode
│   └── auth_viewer.py         # AuthenticatedOrderbookViewer: + login + private WS
└── grid_bot/              # Grid market-making strategy
    ├── __init__.py
    ├── __main__.py            # python -m bots.grid_bot
    ├── cli.py                 # argparse + asyncio main
    ├── strategy.py            # Pure grid-math: calculate_grid, GridLevel, GridPlan
    └── bot.py                 # GridBot: login → plan → bulk-place → monitor → maintain

bots/spread_filler/        # Continuous spread-filling market maker
├── __init__.py
├── __main__.py            # python -m bots.spread_filler
├── cli.py                 # argparse + asyncio main
├── strategy.py            # Pure spread-math: calculate_spread_grid + bias logic
└── bot.py                 # SpreadFillerBot: monitor → rebalance → cancel+replace loop
```

The two WS clients (`ExchangeWSClient` and `PrivateChannelClient`) and the
REST client (`ExchangeRESTClient`) are shared infrastructure. Both
`grid_bot` and `spread_filler` reuse them — the only strategy-specific
code is `strategy.py` (pure math) + `bot.py` (orchestration). Future
strategies (Avellaneda-Stoikov, inventory-aware MM, …) will follow the
same pattern.

### HMAC signature

REST requests are signed with HMAC-SHA256 over the payload
`"{METHOD}\n{PATH}\n{TIMESTAMP}\n{BODY}"` using the raw `api_secret` as the
key. The private WS handshake uses a separate signature over
`"WS_HANDSHAKE\n{TIMESTAMP}\n{API_KEY}"`. Both match `app.core.security` on
the server side.

## Roadmap

1. ~~`orderbook_viewer` (public)~~ ✓
2. ~~HMAC REST client + private WS + balance panel~~ ✓
3. ~~`grid_bot` — symmetric grid around a center price~~ ✓
4. ~~`spread_filler` — continuous spread-filling with direction bias~~ ✓
5. `market_maker` — Avellaneda-Stoikov-style quoting with inventory skew
   and risk limits; reuses `ExchangeWSClient` + `PrivateChannelClient`
   + `ExchangeRESTClient` + `LocalOrderBook` + `BalanceTracker`
6. Admin UI integration — start/stop/parameterise bots from the admin
   panel via a small control API
