# Развёртывание (Docker)

## Архитектура Docker Compose

```
┌─────────────────────────────────────────────────────┐
│                   Docker Network                    │
│                                                     │
│  ┌──────────┐  ┌─────────┐  ┌──────────────────┐   │
│  │ Postgres │  │  Redis  │  │     Backend      │   │
│  │  :5432   │  │  :6379  │  │  FastAPI :8000   │   │
│  │  (vol)   │  │  (vol)  │  │  + Cython .so    │   │
│  └──────────┘  └─────────┘  └──────────────────┘   │
│                                   │                 │
│                    ┌──────────────┘                 │
│                    ▼                                │
│  ┌──────────────────┐  ┌─────────┐  ┌──────────┐   │
│  │     Worker       │  │Frontend │  │  Admin   │   │
│  │ Matching engine  │  │ nginx   │  │  nginx   │   │
│  │ (same image)     │  │ :3000   │  │  :3001   │   │
│  └──────────────────┘  └─────────┘  └──────────┘   │
│                                                     │
│  ┌──────────┐                                       │
│  │  Nginx   │  (optional, --profile full)          │
│  │  :80     │                                       │
│  └──────────┘                                       │
└─────────────────────────────────────────────────────┘
```

## Сервисы

| Сервис | Образ | Порт | Назначение |
|---|---|---|---|
| postgres | postgres:15-alpine | 5432 | База данных |
| redis | redis:7-alpine | 6379 | Кэш + брокер |
| backend | crypto-exchange-backend | 8000 | FastAPI REST + WS |
| worker | crypto-exchange-backend | — | Matching worker (same image, diff CMD) |
| frontend | crypto-exchange-frontend | 3000 | Trading terminal (nginx) |
| admin | crypto-exchange-admin | 3001 | Admin panel (nginx) |
| nginx | nginx:alpine | 80 | Reverse proxy (optional) |

## Dockerfile'ы

### backend.Dockerfile (multi-stage)
1. **Builder stage**: python:3.11-slim + gcc → install deps → compile Cython
2. **Runtime stage**: python:3.11-slim + libpq5 → copy venv + source + .so
3. CMD: `alembic upgrade head && uvicorn app.main:app`

### worker (переиспользует backend image)
```yaml
worker:
  image: crypto-exchange-backend
  build:
    dockerfile: infra/docker/backend.Dockerfile
  command: ["sh", "-c", "cd /app/backend && python -m app.matching.worker"]
```

### frontend.Dockerfile / admin.Dockerfile (multi-stage)
1. **Build stage**: node:20-alpine → npm install → vite build
2. **Runtime stage**: nginx:alpine → static files + `/api` proxy to backend

## Nginx proxy (в frontend/admin контейнерах)

```nginx
location /api {
    proxy_pass http://backend:8000;
    # WebSocket support
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
location / {
    root /usr/share/nginx/html;
    try_files $uri $uri/ /index.html;
}
```

## Команды

```bash
# Сборка и запуск
docker compose up -d --build

# Просмотр логов
docker compose logs -f backend worker

# Проверка статуса
docker compose ps

# Остановка
docker compose down

# Полная очистка (volumes + images)
bash infra/scripts/clear_data.sh

# Пересборка одного сервиса
docker compose up -d --build backend
```

## Переменные окружения

См. `.env.example`. Основные:

| Variable | Default | Описание |
|---|---|---|
| `POSTGRES_USER` | exchange | DB пользователь |
| `POSTGRES_PASSWORD` | exchange | DB пароль |
| `POSTGRES_DB` | exchange | Имя БД |
| `REDIS_URL` | redis://redis:6379/0 | Redis URL |
| `APP_SECRET` | change-me | JWT signing secret |
| `HMAC_REPLAY_WINDOW_SECONDS` | 30 | Replay protection window |
| `DEFAULT_RATE_LIMIT_PER_MIN` | 120 | Rate limit по умолчанию |

## Production рекомендации

1. **Изменить пароли** в `.env` (POSTGRES_PASSWORD, APP_SECRET)
2. **Ограничить CORS** в `main.py` (не `allow_origins=["*"]`)
3. **TLS**: добавить сертификаты в nginx proxy
4. **Backup**: настроить pg_dump cron для PostgreSQL
5. **Monitoring**: Prometheus + Grafana для метрик
6. **Logging**: структурированные логи в ELK / Loki
7. **Scaling**: несколько worker контейнеров (шардирование по символам)
