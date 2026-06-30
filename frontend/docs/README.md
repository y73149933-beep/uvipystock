# Документация

Полная документация криптобиржи-песочницы (paper trading).

## Документы

| Документ | Описание |
|---|---|
| [getting_started.md](getting_started.md) | Быстрый старт: установка, запуск,第一批 шаги |
| [architecture.md](architecture.md) | Архитектурная схема, принципы проектирования, производительность |
| [api.md](api.md) | REST + WebSocket API контракты, типы ордеров, error codes |
| [database.md](database.md) | Схема БД, ER-диаграмма, индексы, инварианты |
| [matching_engine.md](matching_engine.md) | Cython matching engine: C-структуры, алгоритм, performance |
| [deployment.md](deployment.md) | Docker Compose, Dockerfiles, Nginx, production рекомендации |
| [development.md](development.md) | Руководство разработчика: структура, тесты, code style |

## Architecture Decision Records (ADR)

| ADR | Решение |
|---|---|
| [ADR-001](adr/001-postgres-as-source-of-truth.md) | PostgreSQL как источник истины для балансов |
| [ADR-002](adr/002-cython-matching-engine.md) | Cython matching engine с `with nogil:` |
| [ADR-003](adr/003-cancel-replace-pattern.md) | Cancel-Replace паттерн для модификации ордеров |
| [ADR-004](adr/004-dedicated-redis-connections.md) | Dedicated Redis connections для blocking операций |

## Краткая сводка

- **Backend**: Python 3.11, FastAPI, SQLAlchemy 2.0, Cython, Redis, PostgreSQL
- **Frontend**: React 18, TypeScript, Vite, TailwindCSS, Zustand, TradingView LWC
- **Performance**: 1.2M ордеров/сек, p50=0.24μs, GIL-free matching
- **Tests**: 107 unit tests (matching, redis, balance, API)
- **Docker**: 7 сервисов (postgres, redis, backend, worker, frontend, admin, nginx)
- **Размер**: ~190 файлов, ~20,000 строк кода
