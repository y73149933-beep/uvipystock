# Руководство разработчика

## Структура проекта

```
crypto-exchange-sandbox/
├── backend/                  # Python backend
│   ├── alembic/              # Миграции БД
│   ├── app/
│   │   ├── api/              # REST routers
│   │   │   ├── v1/           # Trading API (orders, balance, trades, auth, ws)
│   │   │   └── admin/        # Admin API (users, balances, market, emulator)
│   │   ├── core/             # Security, exceptions, rate_limit, logging
│   │   ├── db/               # SQLAlchemy session, base, seed
│   │   ├── matching/         # ★ Cython engine.pyx/.pxd + worker.py + bridge
│   │   ├── models/           # SQLAlchemy 2.0 models (6 таблиц)
│   │   ├── redis_client/     # Orderbook, stops, queues, pubsub, rate_limit
│   │   ├── repositories/     # Data access layer (CRUD + specialized queries)
│   │   ├── schemas/          # Pydantic v2 DTOs
│   │   ├── services/         # Business logic (balance, order, trade, stop, admin)
│   │   ├── ws/               # WebSocket handlers (public + private)
│   │   └── main.py           # FastAPI app + lifespan
│   ├── tests/                # Unit + integration tests (107 tests)
│   └── scripts/              # seed.py, benchmark_matching.py
├── frontend/                 # Trading terminal (React SPA)
│   └── src/
│       ├── api/              # HMAC fetch wrapper
│       ├── ws/               # WebSocket clients
│       ├── store/            # Zustand stores (6)
│       ├── hooks/            # useOrderbook, usePrivateFeed
│       ├── components/       # UI components
│       └── pages/            # TradingPage, LoginPage, RegisterPage
├── admin/                    # Admin panel (React SPA)
├── infra/                    # Docker, Nginx, scripts
└── docs/                     # Документация
```

## Разработка backend

### Установка зависимостей
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn sqlalchemy[asyncio] asyncpg alembic pydantic pydantic-settings redis cython bcrypt PyJWT
```

### Компиляция Cython
```bash
cd backend
PYTHONPATH=. python app/matching/setup.py build_ext --inplace
```

### Миграции
```bash
cd backend
alembic upgrade head           # применить
alembic revision --autogenerate -m "add column"  # создать
alembic downgrade -1           # откатить
```

### Запуск backend
```bash
cd backend
DATABASE_URL=postgresql+asyncpg://exchange:exchange@localhost:5432/exchange \
REDIS_URL=redis://localhost:6379/0 \
PYTHONPATH=. uvicorn app.main:app --reload --port 8000
```

### Запуск worker
```bash
cd backend
DATABASE_URL=postgresql+asyncpg://exchange:exchange@localhost:5432/exchange \
REDIS_URL=redis://localhost:6379/0 \
PYTHONPATH=. python -m app.matching.worker
```

## Разработка frontend

```bash
cd frontend
npm install
npm run dev    # → http://localhost:3000
npm run build  # production build
```

## Разработка admin

```bash
cd admin
npm install
npm run dev    # → http://localhost:3001
npm run build
```

## Тесты

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://exchange:exchange@localhost:5432/exchange \
PYTHONPATH=. python -m pytest tests/ -v
```

Покрытие:
- 27 tests: Cython matching engine (limit, FOK, IOC, Post-Only, Iceberg)
- 51 tests: Redis client (orderbook, stops, queues, pubsub, rate_limit)
- 17 tests: BalanceService (lock/unlock/settle/credit, optimistic locking)
- 12 tests: API endpoints (HMAC auth, orders, admin)

## Бенчмарк

```bash
cd backend
PYTHONPATH=. python scripts/benchmark_matching.py
```

Ожидаемый результат:
```
Throughput: 1,200,000 orders/sec
Latency p50: 0.24μs  p99: 0.40μs
GIL released: ✓
```

## Добавление нового типа ордера

1. **`app/models/enums.py`** — добавить в `OrderType`
2. **`app/matching/_constants.py`** — добавить C-константу
3. **`app/matching/_bridge.py`** — добавить в `_TYPE_MAP`
4. **`app/matching/engine.pyx`** — реализовать логику в `_match_internal`
5. **`app/matching/engine.pxd`** — обновить если нужен новый enum
6. **`app/services/order_service.py`** — добавить lock логику
7. **`app/schemas/order.py`** — обновить валидацию
8. **`frontend/src/types/order.ts`** — добавить тип
9. **`frontend/src/components/trade-form/OrderTypeSelect.tsx`** — добавить в dropdown
10. Перекомпилировать Cython: `python app/matching/setup.py build_ext --inplace`

## Code style

- Python: black (line-length=100), ruff, mypy strict
- TypeScript: strict mode, no unused locals
- Naming: snake_case (Python), camelCase (TypeScript)
- Imports: isort (Python), alphabetical (TypeScript)
