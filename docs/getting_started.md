# Быстрый старт

## Предварительные требования

- Docker 24+ и Docker Compose v2
- Git

## Запуск через Docker Compose (рекомендуется)

### 1. Клонирование

```bash
git clone https://github.com/y73149933-beep/uvipystock.git
cd uvipystock
```

### 2. Конфигурация

```bash
cp .env.example .env
```

При необходимости отредактируйте `.env` (пароли, секреты).

### 3. Сборка и запуск

```bash
docker compose up -d --build
```

Сборка займёт 5-10 минут (Cython компиляция + npm install).

### 4. Проверка

```bash
docker compose ps
```

Все сервисы должны быть `Up`:
- postgres (healthy)
- redis (healthy)
- backend (healthy)
- worker (running)
- frontend (healthy)
- admin (healthy)

### 5. Доступ

| Сервис | URL |
|---|---|
| Торговый терминал | http://localhost:3000 |
| Админ-панель | http://localhost:3001 |
| Swagger docs | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

## Учётные записи по умолчанию

| Логин | Пароль | Роль | Балансы |
|---|---|---|---|
| `admin` | `admin123` | Admin | — |
| `test` | `test` | Trader | 100,000 USDT + 10 BTC |
| `test2` | `test` | Trader | 100,000 USDT + 10 BTC |

API ключи создаются автоматически при первом логине.

## Локальная разработка

```bash
# Запуск PostgreSQL + Redis в Docker, остальное локально
bash infra/scripts/dev.sh
```

Этот скрипт:
1. Запускает Postgres + Redis в Docker
2. Создаёт Python venv и устанавливает зависимости
3. Компилирует Cython matching engine
4. Запускает Alembic миграции
5. Запускает: uvicorn (backend), matching worker, Vite (frontend), Vite (admin)

## Очистка данных

```bash
# Полный сброс (удалить volumes + images):
bash infra/scripts/clear_data.sh

# Сброс данных, оставить образы:
bash infra/scripts/clear_data.sh --keep-images

# Пересборка:
docker compose up -d --build
```

## Тестирование сделок

1. Откройте **Admin Panel** → http://localhost:3001
2. Войдите как `admin` / `admin123`
3. Перейдите в **Emulator** → **Random Walk**
4. Выберите символ (BTC/USDT), установите параметры
5. Нажмите **Start Random Walk**
6. Откройте **Trading Terminal** → http://localhost:3000
7. Войдите как `test` / `test`
8. График и лента сделок обновятся в реальном времени

## Размещение ордера

1. В Trading Terminal выберите тип ордера (Limit, Market, etc.)
2. Введите цену и количество
3. Нажмите **Buy** или **Sell**
4. Ордер появится в таблице "Open Orders" внизу
5. При мэтче — сделка появится в ленте "Recent Trades"

## Устранение неполадок

### Backend не запускается
```bash
docker compose logs backend
```
Частые причины:
- PostgreSQL ещё не готов → подождите 30 секунд
- Миграция не выполнена → `docker compose exec backend alembic upgrade head`

### Worker не обрабатывает ордера
```bash
docker compose logs worker
```
Проверьте, что Redis доступен и `queue:orders` не пустая:
```bash
docker compose exec redis redis-cli LLEN queue:orders
```

### WebSocket 403
Убедитесь, что в `main.py` нет `@app.middleware("http")` (ломает WS).

### График пустой
Проверьте, что endpoint свечей работает:
```bash
curl -H "X-API-Key: ..." http://localhost:8000/api/v1/trades/candles/BTC/USDT?timeframe=1m
```
Если `candles: []` — сделок ещё не было. Запустите Random Walk через админку.
