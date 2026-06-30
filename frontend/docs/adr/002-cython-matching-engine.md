# ADR-002: Cython matching engine с `with nogil:`

## Статус
Accepted

## Контекст
Matching engine обрабатывает тысячи ордеров в секунду. Python с GIL ограничивает throughput.

## Решение
**Cython `cdef class` с C-структурами и `with nogil:` блоком.**

## Обоснование
- GIL блокирует event loop FastAPI во время мэтчинга
- Cython компилируется в C, убирая overhead интерпретатора
- `with nogil:` позволяет другим потокам работать во время мэтчинга
- Pure C structs (OrderNode, PriceLevel) быстрее Python dict/list
- Pre-allocated node pool (100k) исключает malloc в горячем цикле

## Последствия
- Нужен gcc для компиляции (Docker multi-stage)
- Pre-allocated pool не растёт (fixed 100k nodes)
- Все C-level helpers объявлены `noexcept nogil`
- Bridge слой (_bridge.py) конвертирует Decimal ↔ double
