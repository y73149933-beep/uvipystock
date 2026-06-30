# REST & WebSocket API

## Аутентификация

### REST (торговые боты)
Все endpoints (кроме `/auth/login` и `/auth/register`) требуют HMAC-SHA256 заголовки:

| Header | Описание |
|---|---|
| `X-API-Key` | Публичный ключ (32-char hex) |
| `X-Timestamp` | Unix секунды; отклоняется если `|now - ts| > 30s` |
| `X-Signature` | `HMAC-SHA256(secret_hash, "{METHOD}\n{PATH}\n{TS}\n{BODY}")` → hex |

HMAC ключ = `sha256(raw_secret)` (хранится в БД как `secret_hash`).

### Admin (JWT)
```
Authorization: Bearer <jwt_token>
```
JWT выдаётся при `POST /api/admin/login` (только для is_admin=True пользователей).

## REST Endpoints

### Аутентификация

#### `POST /api/v1/auth/login`
Login + password → API key + secret для HMAC.

**Request:**
```json
{ "login": "test", "password": "test" }
```

**Response 200:**
```json
{
  "user_id": 2,
  "login": "test",
  "is_admin": false,
  "api_key": "a1b2c3d4e5f6...",
  "api_secret": "f6e5d4c3b2a1...",
  "permissions": ["trade", "read", "ws"]
}
```

#### `POST /api/v1/auth/register`
Регистрация нового пользователя.

**Request:**
```json
{ "login": "newuser", "password": "pass1234" }
```

**Response 201:**
```json
{ "user_id": 5, "login": "newuser", "message": "Account created. Please login." }
```

#### `GET /api/v1/auth/me`
Текущий пользователь (требует HMAC auth).

**Response 200:**
```json
{ "user_id": 2, "login": "test", "is_admin": false }
```

---

### Ордера

#### `POST /api/v1/orders`
Создание ордера (включая вложенные SL/TP).

**Request:**
```json
{
  "symbol": "BTC/USDT",
  "side": "buy",
  "type": "limit",
  "price": "42150.50",
  "quantity": "0.123",
  "sl": { "type": "stop_market", "stop_price": "41000" },
  "tp": { "type": "limit", "price": "45000" }
}
```

**Response 201:**
```json
{
  "id": 12345,
  "symbol": "BTC/USDT",
  "side": "buy",
  "type": "limit",
  "status": "new",
  "price": "42150.50",
  "quantity": "0.123",
  "filled_quantity": "0",
  "remaining_quantity": "0.123",
  "sl_order_id": 12346,
  "tp_order_id": 12347,
  "created_at": "2026-06-29T10:00:00Z"
}
```

#### `POST /api/v1/orders/bulk`
Массовое создание (All-or-Nothing).

**Request:**
```json
{
  "bulk_id": "uuid-optional",
  "orders": [
    { "symbol": "BTC/USDT", "side": "buy", "type": "limit", "price": "42100", "quantity": "0.1" },
    { "symbol": "BTC/USDT", "side": "buy", "type": "limit", "price": "42000", "quantity": "0.2" }
  ]
}
```

#### `PUT /api/v1/orders/{order_id}`
Cancel-Replace редактирование.

**Request:**
```json
{ "price": "42200.00", "quantity": "0.15" }
```

#### `DELETE /api/v1/orders/{order_id}`
Отмена одного ордера.

#### `DELETE /api/v1/orders/bulk`
Массовая отмена.

**Request (по IDs):**
```json
{ "order_ids": [100, 101, 102] }
```

**Request (cancel-all по символу):**
```json
{ "symbol": "BTC/USDT", "cancel_all": true }
```

#### `GET /api/v1/orders`
Список своих ордеров.

**Query:** `symbol`, `status` (comma-separated), `offset`, `limit`

---

### Баланс

#### `GET /api/v1/balance`
Балансы пользователя.

**Response 200:**
```json
{
  "balances": [
    { "asset": "BTC", "total": "10", "locked": "0.3", "available": "9.7" },
    { "asset": "USDT", "total": "50000", "locked": "4215", "available": "45785" }
  ]
}
```

---

### Сделки

#### `GET /api/v1/trades`
История сделок пользователя (as taker or maker).

#### `GET /api/v1/trades/public/{symbol}`
Последние публичные сделки по символу (все пользователи).

**Response 200:**
```json
{
  "trades": [
    { "trade_id": 42, "price": 42150.5, "quantity": 0.123, "side": "buy", "ts": 1690000000123 }
  ]
}
```

#### `GET /api/v1/trades/candles/{symbol}`
Исторические OHLCV свечи для графика.

**Query:** `timeframe` (1m, 5m, 15m, 1h, 4h, 1d), `limit` (max 1000)

**Response 200:**
```json
{
  "candles": [
    { "time": 1690000000, "open": 42150, "high": 42155, "low": 42148, "close": 42152, "volume": 0.123 }
  ],
  "symbol": "BTC/USDT",
  "timeframe": "1m"
}
```

---

### Admin Endpoints (JWT auth)

#### `POST /api/admin/login`
Admin login → JWT.

#### `POST /api/admin/users` / `GET /api/admin/users` / `PATCH /api/admin/users/{id}/active`
Управление пользователями.

#### `POST /api/admin/balances/adjust` / `GET /api/admin/balances/{user_id}`
Управление балансами.

#### `POST /api/admin/market/pairs` / `GET /api/admin/market/pairs`
Управление торговыми парами.

#### `POST /api/admin/api-keys` / `DELETE /api/admin/api-keys/{id}`
Управление API ключами.

#### `POST /api/admin/emulator/random-walk` / `POST /api/admin/emulator/trade-inject`
Эмулятор рынка.

---

## WebSocket API

### Публичный канал: `ws://api/v1/ws/orderbook/{symbol}`

**Subscribe message (клиент → сервер):**
```json
{ "action": "subscribe", "channel": "orderbook", "symbol": "BTC/USDT", "depth": 20 }
```

**Server → Client messages:**

Snapshot (первое сообщение):
```json
{
  "event": "orderbook_snapshot",
  "symbol": "BTC/USDT",
  "bids": [["42150.50", "0.5"], ["42150.00", "1.2"]],
  "asks": [["42155.00", "0.3"], ["42160.00", "0.8"]],
  "last_trade_price": 42152.00,
  "ts": 1690000000123
}
```

Delta:
```json
{
  "event": "orderbook_update",
  "symbol": "BTC/USDT",
  "changes": [{"side": "bid", "price": 42150.50, "qty": 0.6}],
  "ts": 1690000000456
}
```

Trade print:
```json
{
  "event": "trade",
  "symbol": "BTC/USDT",
  "trade_id": 456,
  "price": 42150.50,
  "quantity": 0.123,
  "side": "buy",
  "ts": 1690000000789
}
```

---

### Приватный канал: `ws://api/v1/ws/private`

**Auth message (клиент → сервер, в течение 5 секунд):**
```json
{
  "action": "auth",
  "api_key": "...",
  "timestamp": 1690000000,
  "signature": "..."
}
```

**Server → Client messages:**

Order update:
```json
{
  "event": "order",
  "order_id": 12345,
  "status_event": "partially_filled",
  "status": "partially_filled",
  "filled_quantity": 0.05,
  "remaining_quantity": 0.073,
  "avg_fill_price": 42150.50,
  "ts": 1690000000123
}
```

Balance update:
```json
{
  "event": "balance",
  "asset": "USDT",
  "total": 50000,
  "locked": 4215,
  "available": 45785,
  "change": -100.5,
  "reason": "order_placed",
  "ts": 1690000000123
}
```

Bulk result:
```json
{
  "event": "bulk_result",
  "bulk_id": "uuid",
  "action": "place",
  "total": 10,
  "succeeded": 8,
  "failed": [{"index": 3, "code": "insufficient_balance", "message": "..."}],
  "ts": 1690000000123
}
```

---

## Типы ордеров

| Тип | Описание | Lock при placement |
|---|---|---|
| `market` | Исполняется немедленно | Buy: worst-case quote; Sell: base |
| `limit` | Resting в стакане | ✓ сразу |
| `stop_market` | Триггер → market order | ✗ после trigger |
| `stop_limit` | Триггер → limit order | ✗ после trigger |
| `post_only` | Только maker, reject при cross | ✓ сразу |
| `ioc` | Immediate-Or-Cancel | ✓ сразу |
| `fok` | Fill-Or-Kill | ✓ сразу |
| `trailing_stop` | Trailing stop | ✗ после trigger |
| `iceberg` | Скрытый объём | ✓ полный hidden volume |

## Error codes

| Code | HTTP | Описание |
|---|---|---|
| `insufficient_balance` | 402 | Недостаточно средств |
| `order_not_found` | 404 | Ордер не найден |
| `order_not_cancelable` | 409 | Ордер нельзя отменить/изменить |
| `order_validation_error` | 400 | Невалидные параметры |
| `post_only_cross` | 409 | Post-Only пересекает спред |
| `rate_limit_exceeded` | 429 | Превышен rate limit |
| `unauthorized` | 401 | Неверная подпись / нет ключа |
| `insufficient_permissions` | 403 | Нет нужного permission |
