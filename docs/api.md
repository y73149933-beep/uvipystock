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
Для запросов без тела (GET/DELETE) в подписи участвует пустая строка `{BODY}`.

### Admin (JWT)
```
Authorization: Bearer ***
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
  "symbol": "BTC/USD",
  "side": "buy",
  "type": "limit",
  "price": "42150.50",
  "quantity": "0.123",
  "time_in_force": "GTC",
  "post_only": false,
  "sl": { "type": "stop_market", "stop_price": "41000" },
  "tp": { "type": "limit", "price": "45000" }
}
```

Поля по типам:

| Поле | Требуется для |
|---|---|
| `price` | limit / post_only / ioc / fok / iceberg / stop_limit |
| `stop_price` | stop_market / stop_limit / trailing_stop |
| `trailing_delta` | trailing_stop |
| `iceberg_visible_quantity`, `iceberg_hidden_quantity` | iceberg (сумма = `quantity`) |

Внимание: поле типа называется **`type`**, а не `order_type` (иначе 422).

**Response 201:**
```json
{
  "id": 12345,
  "symbol": "BTC/USD",
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

Status-machine: `pending` (условные стопы до триггера) → `new` →
`partially_filled` → `filled`; терминальные также `canceled`, `rejected`,
`expired`. Маркет-ордера отвечают `new` синхронно — исполнение (или expire)
делает асинхронный worker; при отказе по ликвидности guard отвечает 400 сразу
(см. Error codes).

#### `POST /api/v1/orders/bulk`
Массовое создание (All-or-Nothing). Guard-ы ликвидности маркет-ордеров
действуют и в bulk-пути.

**Request:**
```json
{
  "bulk_id": "uuid-optional",
  "orders": [
    { "symbol": "BTC/USD", "side": "buy", "type": "limit", "price": "42100", "quantity": "0.1" },
    { "symbol": "BTC/USD", "side": "buy", "type": "limit", "price": "42000", "quantity": "0.2" }
  ]
}
```

#### `PUT /api/v1/orders/{order_id}`
Cancel-Replace: старый ордер отменяется, новый создаётся в одной транзакции
(`replaced_by_id` / `replaces_id`). **Новый id** — это новый ордер, он
попадает в конец очереди своего ценового уровня (Price-Time Priority
обнуляется). Поддерживается только для `limit` / `post_only` / `iceberg`;
для стопов — 409, перемещать стоп нужно через cancel + создание нового.

**Request:**
```json
{ "price": "42200.00", "quantity": "0.15" }
```

**Response 200:** новый ордер (форма как у `POST /api/v1/orders`).

#### `DELETE /api/v1/orders/{order_id}`
Отмена одного ордера (работает и для `pending` стопов).

#### `DELETE /api/v1/orders/bulk`
Массовая отмена.

**Request (по IDs):**
```json
{ "order_ids": [100, 101, 102] }
```

**Request (cancel-all по символу):**
```json
{ "symbol": "BTC/USD", "cancel_all": true }
```

#### `GET /api/v1/orders`
Список своих ордеров.

**Query:** `symbol`, `status` (comma-separated, напр. `new,partially_filled,pending`), `offset`, `limit`

---

### Стоп-ордера (stop_market / stop_limit / trailing_stop)

- При создании ордер получает status **`pending`**, в стакане его нет;
  монитор крутит Redis ZSET очередей, PG — источник истины (rescan
  восстанавливает пропавшие регистрации).
- Условие срабатывания: sell-стоп триггерится когда `last_price <= stop_price`,
  buy-стоп — когда `last_price >= stop_price`. Направленного guard-а при
  создании нет: sell-стоп выше рынка сработает немедленно.
- При триггере родитель становится `FILLED` (он «выполнил свою работу»), а
  торговать начинает **дочерний** ордер: `stop_market → market`,
  `stop_limit → limit` с ценой из `price` родителя. Дочерний не наследует
  `stop_price`; SL/TP-цепочки связаны через `parent_order_id`.
- Баланс блокируется при триггере (не при создании): sell — base qty,
  buy-limit — `price*qty`, buy-market — worst-case проход по asks (+0.1%
  буфер). Поэтому market-buy стоп не сработает при пустом/тонком ask-стороне
  (монитор пишет лог, стоп остаётся `pending`).
- `trailing_stop`: триггер дрейфует за экстремумом на `trailing_delta`;
  статичной цены нет, cancel-replace к нему не применяется.

### Self-Trade Prevention (STP)
Тейкер никогда не исполняется против собственного же resting-мейкера: своя
заявка пропускается на этапе матчинга (не удаляясь из книги), тейкер идёт
дальше. Практическое следствие для тестов: нужны **два разных аккаунта**,
чтобы состоялась сделка. Guard-ы ликвидности маркет-ордеров тоже исключают
собственные встречные заявки — `MARKET SELL` против стакана, где стоят только
ваши bid-ы, получит 400, а не иллюзию исполнения.

---

### Позиции

#### `GET /api/v1/positions`
Позиция как итог торговли (не баланс: стартовые эмиссии стенда позицией не
считаются). Считается сворачиванием полной истории сделок пользователя
(скользящая средневзвешенная цена; sell фиксирует realized PnL против
средневзвешенной ДО продажи; полное закрытие обнуляет базу).

**Response 200:**
```json
{
  "positions": [
    {
      "symbol": "BTC/USD",
      "base": "BTC",
      "quantity": 1.639,
      "balance": 2000.4,
      "locked": 0.0,
      "avg_cost": 97.694,
      "last_price": 97.0,
      "unrealized_pnl": -1.138,
      "realized_pnl": -4.846,
      "position_value": 158.983
    }
  ]
}
```
`last_price` — ближайший доступный close 1m kline-кэша, fallback — последняя
собственная цена сделки. `balance`/`locked` — справочно: позиция ≠ баланс.

---

### Баланс

#### `GET /api/v1/balance`
Балансы пользователя.

**Response 200:**
```json
{
  "balances": [
    { "asset": "BTC", "total": "10", "locked": "0.3", "available": "9.7" },
    { "asset": "USD", "total": "50000", "locked": "4215", "available": "45785" }
  ]
}
```

---

### Сделки

#### `GET /api/v1/trades`
История сделок пользователя (as taker or maker).

**Query:** `symbol`, `side`, `start`, `end`, `offset`, `limit` (≤500)

**Response 200:**
```json
{
  "trades": [
    {
      "id": 42,
      "symbol": "BTC/USD",
      "side": "buy",
      "price": "97.500000000000000000",
      "quantity": "0.100000000000000000",
      "quote_quantity": "9.750000000000000000",
      "role": "taker",
      "fee": "0.009750000000000000",
      "order_id": 12345,
      "executed_at": "2026-11-11T18:01:00Z"
    }
  ],
  "pagination": { "offset": 0, "limit": 50, "count": 1 }
}
```
Важно: `side` — это сторона **тейкера**. Для `role == "maker"` ваша
собственная сторона — обратная.

#### `GET /api/v1/trades/public/{symbol}`
Последние публичные сделки по символу (все пользователи). Символ содержит
слэш — URL вида `/trades/public/BTC/USD`.

**Response 200:** сделки в **хронологическом порядке (ASC, старые первыми)**;
`ts` — epoch **миллисекунды** (та же единица, что и WS trade prints). Клиенты,
показывающие ленту «новые сверху», обязаны сами реверснуть массив.

```json
{
  "trades": [
    { "trade_id": 42, "symbol": "BTC/USD", "price": 97.5, "quantity": 0.123, "side": "buy", "ts": 1690000000123 }
  ]
}
```

#### `GET /api/v1/trades/candles/{symbol}`
Исторические OHLCV свечи (Redis kline-кэш; PG — источник истины, кэш
доматериализуется лениво).

**Query:** `timeframe` (1m, 5m, 15m, 1h, 4h, 1d), `limit` (≤1000),
`end` (epoch seconds — правая граница окна, по умолчанию «сейчас»)

**Response 200:**
```json
{
  "candles": [
    { "time": 1690000000, "open": 42150, "high": 42155, "low": 42148, "close": 42152, "volume": 0.123 }
  ],
  "symbol": "BTC/USD",
  "timeframe": "1m",
  "has_more": true,
  "next_end": 1689999700
}
```
Пагинация назад по истории: следующий запрос — `end=next_end`. Курсор всегда
серверный (`next_end` = левый край окна) — даже если страница пустая.
Пустые окна дополняются **синтетическими плоскими барами** (`volume = 0`,
O=H=L=C=предыдущий close) — их нет в PG, любые сверки с таблицей `trades`
должны фильтровать `volume == 0`. До первой сделки история не выдумывается.

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
{ "action": "subscribe", "channel": "orderbook", "symbol": "BTC/USD", "depth": 20 }
```

**Server → Client messages:**

Snapshot (первое сообщение):
```json
{
  "event": "orderbook_snapshot",
  "symbol": "BTC/USD",
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
  "symbol": "BTC/USD",
  "changes": [{"side": "bid", "price": 42150.50, "qty": 0.6}],
  "ts": 1690000000456
}
```

Trade print:
```json
{
  "event": "trade",
  "symbol": "BTC/USD",
  "trade_id": 456,
  "price": 42150.50,
  "quantity": 0.123,
  "side": "buy",
  "ts": 1690000000789
}
```

После переподключения клиент сам догрушает пропущенное через REST
(`/trades/public` для ленты + `/trades/candles` для свежих окон) — сервер не
хранит «недоставленные» сообщения.

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
`status_event` ∈ `placed | partially_filled | filled | canceled | modified | rejected | triggered`
(событие жизненного цикла; в `status` — текущий статус ордера из status-machine).

Balance update:
```json
{
  "event": "balance",
  "asset": "USD",
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
| `market` | Исполняется немедленно | Buy: worst-case quote (walk asks); Sell: base |
| `limit` | Resting в стакане | ✓ сразу |
| `stop_market` | Триггер → market order (дочерний) | ✗ при trigger |
| `stop_limit` | Триггер → limit order (дочерний) | ✗ при trigger |
| `post_only` | Только maker, reject при cross | ✓ сразу |
| `ioc` | Immediate-Or-Cancel | ✓ сразу |
| `fok` | Fill-Or-Kill | ✓ сразу |
| `trailing_stop` | Stop-drift за экстремумом | ✗ при trigger |
| `iceberg` | Скрытый объём | ✓ полный (visible+hidden) |

## Error codes

| Code | HTTP | Описание |
|---|---|---|
| `insufficient_balance` | 402 | Недостаточно средств |
| `order_not_found` | 404 | Ордер не найден |
| `order_not_cancelable` | 409 | Ордер нельзя отменить/изменить (в т.ч. `PUT` на стоп-тип) |
| `order_validation_error` | 400 | Невалидные параметры, вкл. guard-ы ликвидности: `Cannot place MARKET BUY on {sym}: no asks available` / `... MARKET SELL ...: no bids available` / `insufficient ask|bid volume` |
| `post_only_cross` | 409 | Post-Only пересекает спред |
| `rate_limit_exceeded` | 429 | Превышен rate limit |
| `unauthorized` | 401 | Неверная подпись / нет ключа |
| `invalid_signature` | 401 | HMAC не совпал (пустое тело подписывается как `""`) |
| `insufficient_permissions` | 403 | Нет нужного permission |
