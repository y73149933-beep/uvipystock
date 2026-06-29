# ADR-004: Dedicated Redis connections для blocking операций

## Статус
Accepted

## Контекст
BRPOP и PubSub `listen()` — blocking операции, которые держат connection длительное время.

## Решение
**Использовать отдельные Redis connections (не из пула) для BRPOP и PubSub.**

## Обоснование
- Shared pool с `health_check_interval=30` отправляет PING, который прерывает blocking read → `CancelledError`
- `retry_on_timeout=True` может вызвать double-pop (обработка ордера дважды)
- Pool starvation: blocking команда держит connection из пула

## Последствия
- Worker создаёт dedicated connection для BRPOP с `socket_timeout=60`, `retry_on_timeout=False`, `health_check_interval=0`
- PubSub `subscribe()` создаёт dedicated connection с `socket_timeout=None`, `health_check_interval=0`
- При обрыве связи — retry с пересозданием connection
