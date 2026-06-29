# Matching Engine (Cython)

## Обзор

Matching engine — ядро биржи. Реализован на Cython с чистыми C-структурами и `with nogil:` для неблокирующего мэтчинга.

## Производительность

| Метрика | Значение |
|---|---|
| Throughput | 1,200,000 ордеров/сек |
| Latency p50 | 0.24 μs |
| Latency p99 | 0.40 μs |
| GIL release | ✓ подтверждено |
| Node pool | 100,000 pre-allocated |
| Allocations в горячем цикле | 0 |

## C-структуры

### OrderNode
```cython
cdef struct OrderNode:
    int64_t     order_id
    double      remaining_qty     # остаток к исполнению
    double      visible_qty       # видимая часть (для iceberg)
    int         is_iceberg        # 0 / 1
    OrderNode*  next              # FIFO within price level
```

### PriceLevel
```cython
cdef struct PriceLevel:
    double       price
    OrderNode*   head             # старейший ордер
    OrderNode*   tail             # новейший ордер (O(1) append)
    double       total_volume     # сумма remaining_qty
    int          order_count
```

### OrderBookSide
```cython
cdef struct OrderBookSide:
    PriceLevel*  levels           # contiguous array, sorted by price
    int          size             # текущее количество уровней
    int          capacity         # выделенная ёмкость
    int          is_descending    # 1 = bids (highest first), 0 = asks (lowest first)
```

### COrder
```cython
cdef struct COrder:
    int64_t  order_id
    int      side                 # C_BUY=0, C_SELL=1
    int      type                 # C_MARKET=0, C_LIMIT=1, ...
    double   price                # NaN для market
    double   quantity
    double   remaining_qty
    int      is_iceberg
    double   visible_qty
    double   hidden_qty
```

### TradeResult
```cython
cdef struct TradeResult:
    int64_t  taker_order_id
    int64_t  maker_order_id
    double   price
    double   quantity
    int      taker_side
```

## Python-Cython интерфейс

### PyCOrder
Python-visible обёртка вокруг `COrder`:
```cython
cdef class PyCOrder:
    cdef COrder c_val
    cpdef COrder get_c(self)
    cpdef void _set(self, ...)  # заполнение полей из bridge
    # Read-only properties: order_id, side, type, price, quantity, ...
```

### CMatchingEngine
```cython
cdef class CMatchingEngine:
    cdef:
        OrderBookSide  _bids
        OrderBookSide  _asks
        OrderNode*     _node_pool      # pre-allocated, 100k nodes
        TradeResult*   _trades_buf     # pre-allocated results
        int            _trades_count

    cpdef bint add_passive_order(...)
    cpdef tuple match_active_order(PyCOrder incoming)
    cpdef bint cancel_order(...)
    cpdef tuple snapshot(int side, int depth)
    cpdef void reset()
```

## Алгоритм мэтчинга

### Price-Time Priority
1. Bids отсортированы по убыванию цены (лучшая цена первой)
2. Asks отсортированы по возрастанию (лучшая цена первой)
3. Внутри уровня — FIFO (head = старейший ордер)

### Match flow (`_match_internal`)

```
1. Post-Only guard: если пересекает спред → C_POST_ONLY_CROSS
2. FOK precheck: пройти opposite side, проверить достаточность объёма
3. Walk opposite side:
   for each price level (best first):
     if price check fails (market除外): break
     for each order at level (FIFO):
       compute fill_qty = min(incoming.remaining, maker.visible)
       update remaining quantities
       emit TradeResult
       if maker exhausted: unlink from level
       if iceberg: refill visible from hidden
   if level empty: remove + compact
4. Return outcome + trades + remaining
```

### Iceberg refill
Когда видимая часть iceberg исчерпана:
- `visible_qty` пополняется из `hidden_qty`
- Order остаётся в стакане
- Мэтчер продолжает с того же maker (не переходит к next)

### FOK atomicity
Перед любым мэтчем FOK проходит opposite side и суммирует доступный объём. Если `< quantity` → `C_FOK_REJECTED`, никаких сделок.

## Pre-allocated pool

Node pool выделен один раз при construction (100,000 нод) и НЕ растёт через `realloc` — это критично, потому что `realloc` мог бы инвалидировать все `OrderNode*` указатели в linked-list.

При переполнении pool выдаёт `MemoryError` (caller должен `reset()` или увеличить `INITIAL_NODE_POOL_CAP`).

## Ограничения внутри `with nogil:`

ЗАПРЕЩЕНО:
- Вызовы Python-функций (`print`, `len`, `dict.get`)
- Создание Python-объектов (`list`, `dict`, `Decimal`)
- Доступ к атрибутам `cdef class` через Python-имя
- Вызовы в Redis / PostgreSQL
- Броски исключений Python (`raise`)

РАЗРЕШЕНО:
- C-struct операции
- `malloc`/`realloc`/`free`
- Арифметика на `double` / `int64_t`
- Указатели (`OrderNode*`)

## Сборка

```bash
cd backend
PYTHONPATH=. python app/matching/setup.py build_ext --inplace
# → app/matching/engine.cpython-311-x86_64-linux-gnu.so
```

Компилятор: `gcc -O3 -ffast-math -Wall`
