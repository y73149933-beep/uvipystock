# Схема базы данных

## ER-диаграмма (текстовая)

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│     users       │     │    api_keys     │     │    balances     │
├─────────────────┤     ├─────────────────┤     ├─────────────────┤
│ id (PK)         │◄──┐ │ id (PK)         │     │ id (PK)         │
│ email           │   │ │ user_id (FK)    │     │ user_id (FK)    │──► users
│ password_hash   │   └─┤ api_key (UQ)    │     │ asset           │
│ is_active       │     │ secret_hash     │     │ total_balance   │
│ is_admin        │     │ permissions[]   │     │ locked_balance  │
│ created_at      │     │ rate_limit      │     │ available_balance│
│ updated_at      │     │ is_revoked      │     │ version         │
└─────────────────┘     │ created_at      │     │ updated_at      │
                        │ last_used_at    │     └─────────────────┘
                        └─────────────────┘     UNIQUE(user_id, asset)

┌─────────────────┐     ┌─────────────────────────────────────┐
│ trading_pairs   │     │               orders                │
├─────────────────┤     ├─────────────────────────────────────┤
│ id (PK)         │     │ id (PK)                             │
│ symbol (UQ)     │◄──┐ │ user_id (FK)                        │──► users
│ base_asset      │   │ │ symbol (FK)                         │──► trading_pairs
│ quote_asset     │   └─┤ side (buy/sell)                     │
│ price_precision │     │ type (market/limit/stop/...)        │
│ qty_precision   │     │ status (new/partial/filled/...)     │
│ min_lot_size    │     │ price                               │
│ max_lot_size    │     │ stop_price                          │
│ tick_size       │     │ trailing_delta                      │
│ maker_fee_bps   │     │ quantity                            │
│ taker_fee_bps   │     │ filled_quantity                     │
│ is_active       │     │ filled_quote_qty                    │
│ created_at      │     │ visible_quantity (iceberg)          │
└─────────────────┘     │ hidden_quantity (iceberg)           │
                        │ replace_count                       │
                        │ replaces_id (self-FK)               │
                        │ replaced_by_id (self-FK)            │
                        │ parent_order_id (self-FK)           │
                        │ sl_order_id (self-FK)               │
                        │ tp_order_id (self-FK)               │
                        │ bulk_id                             │
                        │ created_at / updated_at / version   │
                        └─────────────────────────────────────┘

┌─────────────────────────────────────┐
│               trades                │
├─────────────────────────────────────┤
│ id (PK)                             │
│ taker_order_id (FK → orders)        │
│ maker_order_id (FK → orders)        │
│ symbol (FK → trading_pairs)         │
│ price                               │
│ quantity                            │
│ quote_quantity                      │
│ side (taker side: buy/sell)         │
│ taker_user_id (FK → users)          │
│ maker_user_id (FK → users)          │
│ taker_fee                           │
│ maker_fee                           │
│ executed_at                         │
└─────────────────────────────────────┘
```

## Инварианты

### Balance
```
total_balance == locked_balance + available_balance
```
Поддерживается на уровне сервисного слоя (`BalanceService`). Каждая мутация через `check_invariant()`.

### Order
- `filled_quantity <= quantity`
- `remaining_quantity = quantity - filled_quantity`
- SL/TP children: `parent_order_id` → parent; parent: `sl_order_id` / `tp_order_id` → children
- Cancel-Replace: `replaces_id` → old order; old: `replaced_by_id` → new

## Индексы

| Таблица | Индекс | Назначение |
|---|---|---|
| users | `ix_users_email` (unique) | Поиск по email/login |
| api_keys | `ix_api_keys_api_key` (unique) | Auth lookup |
| api_keys | `ix_api_keys_user_id` | Список ключей пользователя |
| balances | `uq_balances_user_asset` (unique) | Один баланс на (user, asset) |
| balances | `ix_balances_user_asset` | Быстрый lookup |
| trading_pairs | `uq_trading_pairs_symbol` (unique) | Поиск по символу |
| orders | `ix_orders_user_symbol_status` | GET /orders с фильтрами |
| orders | `ix_orders_status_symbol` | Worker: reconstruction |
| orders | `ix_orders_parent_id` | Каскадная отмена SL/TP |
| orders | `ix_orders_bulk_id` | Трассировка bulk операций |
| orders | `ix_orders_replaces_id` | Cancel-Replace audit |
| trades | `ix_trades_symbol_executed_at` | График свечей, лента |
| trades | `ix_trades_taker_user_id` | История сделок user |
| trades | `ix_trades_maker_user_id` | История сделок user |
| trades | `ix_trades_taker_order_id` | Связь с ордером |

## Миграции (Alembic)

```bash
# Применить миграции
cd backend && alembic upgrade head

# Откатить последнюю
alembic downgrade -1

# Создать новую
alembic revision --autogenerate -m "description"
```

Начальная миграция: `0001_initial.py` — создаёт 6 таблиц + 3 PG ENUM типа + 14 индексов + 14 FK.
