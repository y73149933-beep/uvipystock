# ADR-003: Cancel-Replace паттерн для модификации ордеров

## Статус
Accepted

## Контекст
При модификации ордера (изменение цены/количества) нужно сохранить Price-Time Priority.

## Решение
**Cancel-Replace: старый ордер отменяется, создаётся новый с новым `created_at`.**

## Обоснование
- Если оставить старый timestamp, модифицированный ордер "обгонит" более новые
- Создание нового order_id + связь через `replaces_id` / `replaced_by_id` сохраняет audit trail
- Одна PG транзакция: cancel old → unlock → lock new → insert new

## Последствия
- `PUT /orders/{id}` возвращает новый order_id (не старый)
- Цепочка модификаций: `replaces_id` → предыдущий, `replaced_by_id` → следующий
- `replace_count` отслеживает количество модификаций
- Старый ордер получает статус CANCELED
