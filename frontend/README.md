# Crypto Exchange Sandbox (Paper Trading)

Educational crypto exchange sandbox with async matching engine, Cython core, Redis-backed order book, and real-time WebSocket streaming.

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.11, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| **Matching Engine** | Cython (`with nogil:`), pure-C structs, Price-Time Priority |
| **Broker/Cache** | Redis (Sorted Sets, Lists, Pub/Sub) |
| **Database** | PostgreSQL 15+ |
| **Frontend** | React 18, TypeScript, Vite, TailwindCSS, Zustand, TradingView LWC |
| **Admin** | React + TypeScript (custom) |

## Quick Start

### Option 1: Docker Compose (recommended)

```bash
# 1. Clone and configure
cp .env.example .env

# 2. Build and start all services
docker compose up -d --build

# 3. Check status
docker compose ps
```

**Access points:**
- 🖥️ **Trading Terminal**: http://localhost:3000
- 🛠️ **Admin Panel**: http://localhost:3001
- 📖 **API Docs (Swagger)**: http://localhost:8000/docs
- 🗄️ **PostgreSQL**: localhost:5432
- ⚡ **Redis**: localhost:6379

**Default credentials:**
- Admin: `admin@exchange.local` / `admin123`
- Trader: `trader@exchange.local` / `admin123`

API keys are auto-generated on first login (no need to pre-create them).

### Option 2: Local Development

```bash
# Start PostgreSQL + Redis via Docker, then run backend/frontend locally
bash infra/scripts/dev.sh
```

This script:
1. Starts Postgres + Redis in Docker
2. Creates a Python venv and installs backend deps
3. Compiles the Cython matching engine
4. Runs Alembic migrations
5. Starts: uvicorn (backend), matching worker, Vite (frontend), Vite (admin)

## Project Structure

```
crypto-exchange-sandbox/
├── backend/                 # FastAPI + Cython + matching worker
│   ├── alembic/             # Database migrations
│   ├── app/
│   │   ├── api/             # REST routers (v1 + admin)
│   │   ├── core/            # Security, exceptions, rate limiting
│   │   ├── db/              # Async SQLAlchemy session
│   │   ├── matching/        # ★ Cython engine.pyx + worker.py
│   │   ├── models/          # SQLAlchemy 2.0 models
│   │   ├── redis_client/    # Orderbook, queues, pubsub, stops
│   │   ├── repositories/    # Data access layer
│   │   ├── schemas/         # Pydantic v2 DTOs
│   │   ├── services/        # Business logic (balance, order, trade, stop)
│   │   ├── ws/              # WebSocket handlers
│   │   └── main.py          # FastAPI entry point
│   ├── tests/               # Unit + integration tests
│   └── scripts/             # Seed, benchmark
├── frontend/                # Trading terminal (React SPA)
├── admin/                   # Admin panel (React SPA)
├── infra/
│   ├── docker/              # Dockerfiles + postgres-init.sql
│   ├── nginx/               # Reverse proxy config
│   └── scripts/             # dev.sh, build_cython.sh
├── docs/                    # Architecture docs + ADRs
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Key Features

### Trading
- **Order types**: Market, Limit, Stop-Market, Stop-Limit, Post-Only, IOC, FOK, Trailing Stop, Iceberg
- **Price-Time Priority** matching via Cython (1.2M orders/sec, p50 latency 0.24μs)
- **GIL-free** matching engine (`with nogil:`) — doesn't block the event loop
- **Cancel-Replace** order modification (atomic, preserves queue position)
- **Bulk operations**: All-or-Nothing batch placement, batch cancel, cancel-all
- **SL/TP**: Optional stop-loss/take-profit children attached to market/limit orders

### Balances
- **Three-way split**: `total = locked + available`
- **Optimistic locking** (version-based) for API layer
- **Pessimistic locking** (`SELECT FOR UPDATE`) for matching worker
- **Invariant enforcement** on every mutation

### Real-time
- **Public WS**: L2 orderbook snapshots + deltas, trade prints
- **Private WS**: Per-user order/balance events (HMAC-authenticated)
- **Auto-reconnect** with exponential backoff

### Admin
- User management (create, block/activate)
- Manual balance adjustments (credit/debit)
- Trading pair CRUD (precision, lot sizes, tick size, fees)
- **Market Emulator**: Random Walk generator + manual trade injection

## API Authentication

### REST (trading bots)
```
X-API-Key:     <public_key>
X-Timestamp:   <unix_seconds>
X-Signature:   HMAC-SHA256(secret_hash, "{METHOD}\n{PATH}\n{TS}\n{BODY}")
```

### WebSocket (private channel)
Send within 5 seconds of connection:
```json
{
  "action": "auth",
  "api_key": "<public_key>",
  "timestamp": 1690000000,
  "signature": "<ws_handshake_signature>"
}
```

### Admin (JWT)
```
Authorization: Bearer <jwt_token>
```

## Development

### Running Tests

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://exchange:exchange@localhost:5432/exchange \
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

**Test coverage**: 107 unit tests
- 27 Cython matching engine
- 51 Redis client
- 17 Balance service
- 12 API endpoints

### Building Cython

```bash
# Locally (for development)
bash infra/scripts/build_cython.sh

# In Docker (automatic during build)
docker compose build backend
```

### Database Migrations

```bash
# Apply migrations
docker compose exec backend cd /app/backend && alembic upgrade head

# Create a new migration
docker compose exec backend cd /app/backend && alembic revision --autogenerate -m "description"

# Rollback
docker compose exec backend cd /app/backend && alembic downgrade -1
```

### Performance Benchmark

```bash
cd backend
PYTHONPATH=. python scripts/benchmark_matching.py
```

Expected output:
```
Throughput: 1,200,000 orders/sec
Latency p50: 0.24μs  p99: 0.40μs
GIL released: ✓ confirmed
```

## Architecture

See `docs/architecture.md` for the full design document including:
- ASCII diagram of Client ↔ FastAPI ↔ Redis ↔ Cython ↔ PostgreSQL
- Database schema (6 tables, 14 indexes, 14 FKs)
- Redis key layout (ZSETs, Lists, Hashes, Pub/Sub channels)
- Cython matching engine specification
- REST + WebSocket API contracts

## License

Educational project. Use at your own risk.
