# Архитектура

## Обзор

Криптобиржа-песочница (paper trading) — асинхронная модульная система с разделением слоёв API и бизнес-логики. Построена на Python 3.11 + FastAPI с Cython matching engine и Redis в качестве брокера сообщений и кэша стакана.

## Технологический стек

| Слой | Технология | Назначение |
|---|---|---|
| **Backend** | Python 3.11, FastAPI, Pydantic v2 | REST API, WebSocket, валидация |
| **ORM** | SQLAlchemy 2.0 (async), Alembic | Модели, миграции |
| **Matching Engine** | Cython 3.0 (`with nogil:`) | Price-Time Priority мэтчинг |
| **Брокер/Кэш** | Redis 7 (Sorted Sets, Lists, Pub/Sub) | Стакан, очереди, события |
| **База данных** | PostgreSQL 15 | Источник истины для балансов |
| **Frontend** | React 18, TypeScript, Vite, Tailwind | Торговый терминал |
| **Admin** | React + TypeScript | Админ-панель |
| **Графики** | TradingView Lightweight Charts | Candlestick визуализация |
| **State** | Zustand | Управление состоянием frontend |

## Архитектурная схема

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT LAYER                                    │
│                                                                              │
│   ┌────────────────────────────┐         ┌────────────────────────────┐      │
│   │  Trading Terminal (React)  │         │  Admin Panel (React+TS)    │      │
│   │  Zustand + TradingView LWC │         │  Users/Balances/Market/    │      │
│   │  WS: public + private      │         │  Emulator                  │      │
│   └────────────┬───────────────┘         └──────────────┬─────────────┘      │
└────────────────┼────────────────────────────────────────┼────────────────────┘
                 │ HTTPS REST (HMAC-SHA256)                │ HTTPS (JWT)
                 │ WSS public/private                      │
                 ▼                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      API GATEWAY — FastAPI (async)                           │
│                                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │ REST Router      │  │ WS Router        │  │ Middleware               │   │
│  │ • POST /orders   │  │ /ws/orderbook/   │  │ • HMAC signature verify  │   │
│  │ • POST /orders/  │  │   {symbol:path}  │  │ • Rate limiter (Redis)   │   │
│  │   bulk           │  │ /ws/private      │  │ • CORS                   │   │
│  │ • PUT /orders/   │  │                  │  │ • Exception handlers     │   │
│  │   {id}           │  │ Auth: HMAC       │  │                          │   │
│  │ • DELETE /orders │  │ handshake        │  │                          │   │
│  │ • GET /orders    │  │                  │  │                          │   │
│  │ • GET /balance   │  │                  │  │                          │   │
│  │ • GET /trades    │  │                  │  │                          │   │
│  │ • GET /trades/   │  │                  │  │                          │   │
│  │   candles        │  │                  │  │                          │   │
│  │ • POST /auth/    │  │                  │  │                          │   │
│  │   login          │  │                  │  │                          │   │
│  │ • POST /auth/    │  │                  │  │                          │   │
│  │   register       │  │                  │  │                          │   │
│  └────────┬─────────┘  └────────┬─────────┘  └──────────────────────────┘   │
└───────────┼─────────────────────┼──────────────────────────────────────────┘
            │                      │
            │ ① Validate params     │ Subscribe via Redis pubsub
            │ ② Lock balance (PG)   │ (dedicated connection)
            │ ③ INSERT order        │
            │ ④ LPUSH queue:orders  │
            │ ⑤ RESPOND 201         │
            ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       REDIS (Cache + Message Broker)                         │
│                                                                              │
│  LISTS (FIFO queues):                                                       │
│   • queue:orders      ──► consumed by Matching Worker                       │
│   • queue:trades      ──► consumed by Persistence Worker                    │
│   • queue:events.deadletter                                                  │
│                                                                              │
│  SORTED SETS (order book + stop queue):                                      │
│   • ob:{symbol}:bids    score=price*1e4*1e6+seq   member=order_id          │
│   • ob:{symbol}:asks    score=price*1e4*1e6+seq   member=order_id          │
│   • stops:{symbol}      score=stop_price*1e4*1e6  member=order_id          │
│   • trailing:{symbol}   score=current_trigger     member=order_id           │
│                                                                              │
│  HASHES (order metadata, trailing state):                                    │
│   • order:{order_id}                  fields: side,price,qty,type,...       │
│   • trailing_state:{order_id}         fields: delta,extreme,trigger         │
│   • ratelimit:{api_key}               ZSET: sliding window timestamps       │
│                                                                              │
│  SETS (user order index):                                                    │
│   • user:{uid}:open_orders             all active orders of user            │
│   • user:{uid}:open_orders:{symbol}    per-symbol (Cancel-All)              │
│   • symbol:{symbol}:open_orders        all active orders on symbol           │
│                                                                              │
│  PUB/SUB CHANNELS:                                                           │
│   • pub:orderbook:{symbol}   L2 deltas/snapshots (public)                   │
│   • pub:trades:{symbol}      trade prints (public + stop monitor)           │
│   • pub:orders:{user_id}     private order status updates                   │
│   • pub:balances:{user_id}   private balance updates                        │
│   • pub:bulk:{user_id}       bulk operation results                         │
└─────────────────────────────────────────────────────────────────────────────┘
             ▲                                         ▲
             │ BRPOP queue:orders (dedicated conn)     │
             │                                         │
             ▼                                         │
┌─────────────────────────────────────────────────────────────────────────────┐
│              MATCHING WORKER (asyncio + Cython)                              │
│                                                                              │
│  loop:                                                                       │
│    1. BRPOP queue:orders (dedicated Redis connection)                       │
│    2. Load opposite-side book from Redis (ZSET + HASHes)                    │
│    3. Build PyCOrder via _bridge.build_corder()                             │
│    4. CMatchingEngine.match_active_order(corder)                            │
│       ┌─────────────────────────────────────────────────────┐                │
│       │  with nogil:           ◄── GIL RELEASED ────►       │                │
│       │      # Pure C struct operations                      │                │
│       │      # Price-Time Priority walk                      │                │
│       │      # FOK/IOC/Post-Only/Iceberg checks              │                │
│       │  return trades                                        │                │
│       └─────────────────────────────────────────────────────┘                │
│    5. Apply results to Redis (ZREM/ZADD/HSET)                               │
│    6. PG transaction: UPDATE balances + INSERT trades + UPDATE orders       │
│    7. PUBLISH events to pub:orderbook, pub:trades, pub:orders, pub:balances │
│                                                                              │
│  Stop Monitor (background task per symbol):                                  │
│    • Subscribes to pub:trades:{symbol}                                       │
│    • On each print: evaluate stop triggers + update trailing extremes       │
│    • Triggered stops → re-enqueue as MARKET/LIMIT into queue:orders         │
└─────────────────────────────────────────────────────────────────────────────┘
             │                                         │
             │ SQLAlchemy 2.0 async (asyncpg)          │
             ▼                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                POSTGRESQL 15 (Source of Truth)                               │
│                                                                              │
│  users          — id, email(login), password_hash, is_admin                 │
│  api_keys       — api_key, secret_hash, permissions, rate_limit             │
│  balances       — user_id, asset, total/locked/available, version           │
│  trading_pairs  — symbol, base/quote, precision, lot_size, tick_size, fees  │
│  orders         — user_id, symbol, side, type, status, price, qty,          │
│                   filled_qty, SL/TP linkage, Cancel-Replace chain, bulk_id  │
│  trades         — taker/maker order_id + user_id, price, qty, fees          │
│                                                                              │
│  Alembic migrations versioned.                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Ключевые точки снятия GIL и блокировки средств

| Точка | Что происходит | GIL | Блокировка средств |
|---|---|---|---|
| FastAPI REST handler | Валидация, HMAC-проверка | удерживается | — |
| PG-транзакция в API handler | `SELECT FOR UPDATE` на балансе → `UPDATE` locked/available | удерживается | PG: пессимистичная блокировка строки |
| Push в `queue:orders` | `LPUSH` через aioredis | удерживается | — |
| Matching Worker — приём | `BRPOP` (dedicated connection) | удерживается | — |
| **Cython `with nogil:`** | Обход стакана, Price-Time Priority, FOK/IOC checks | **снят** | — |
| Matching Worker — применение | `ZADD`/`ZREM`/`HSET` pipeline | удерживается | — |
| **PG-транзакция в Worker** | UPDATE balances, INSERT trades, UPDATE orders | удерживается | PG: optimistic lock via `version` |
| PUBLISH событий | aioredis pubsub | удерживается | — |

## Принципы проектирования

### 1. PostgreSQL — единственный источник истины для балансов
Redis — только кэш стакана и шина событий. Потеря Redis = потеря производительности, но не денег. При рестарте Worker-а состояние восстанавливается из PG.

### 2. Cython-мэтчер stateless per-call
Каждый `BRPOP` создаёт свой `CMatchingEngine`, загружает снапшот, выполняет матч, возвращает trades. Никакого разделяемого состояния между вызовами.

### 3. Cancel-Replace = новый `order_id` + старый отменяется
Сохраняет строгую Price-Time Priority (старый timestamp не "воровался" бы новым ордером).

### 4. SL/TP — отдельные ордера со `parent_order_id`
Упрощает каскадную отмену и не требует специальной логики в матчителе.

### 5. Dedicated connections для blocking operations
BRPOP и PubSub `listen()` используют отдельные Redis-соединения (не из пула), чтобы избежать:
- Pool starvation (blocking команды держат connection)
- Health check PING конфликтов с blocking read
- `retry_on_timeout` double-pop risk

## Производительность

| Метрика | Значение |
|---|---|
| Throughput | 1.2M ордеров/сек |
| Latency p50 | 0.24 μs |
| Latency p99 | 0.40 μs |
| GIL release | ✓ подтверждено |
| Node pool | 100,000 pre-allocated |

## Масштабирование

- **Вертикальное**: увеличить `matching_worker_concurrency` (несколько worker процессов)
- **Шардирование**: `queue:orders:{symbol}` — один worker на символ
- **Read replicas**: PostgreSQL read replicas для GET endpoints (orders, trades, balance)
- **Redis Cluster**: шардирование стакана по символам
