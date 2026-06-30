#!/usr/bin/env bash
# Local development orchestration script.
# Starts PostgreSQL + Redis via docker-compose, then runs the backend + worker + frontends locally.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Color codes for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[dev]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[error]${NC} $*" >&2; }

# ─── Pre-flight checks ──────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { err "docker not found"; exit 1; }
command -v python3 >/dev/null 2>&1 || { err "python3 not found"; exit 1; }
command -v npm >/dev/null 2>&1 || { err "npm not found"; exit 1; }

# ─── Create .env if missing ─────────────────────────────────────────────────
if [ ! -f .env ]; then
    log "Creating .env from .env.example"
    cp .env.example .env
fi

# ─── Start infrastructure (Postgres + Redis) ────────────────────────────────
log "Starting PostgreSQL + Redis via docker-compose..."
docker compose up -d postgres redis

# Wait for Postgres to be ready
log "Waiting for PostgreSQL..."
for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U exchange >/dev/null 2>&1; then
        log "PostgreSQL is ready"
        break
    fi
    sleep 1
done

# Wait for Redis
log "Waiting for Redis..."
for i in $(seq 1 15); do
    if docker compose exec -T redis redis-cli ping >/dev/null 2>&1; then
        log "Redis is ready"
        break
    fi
    sleep 1
done

# ─── Backend: create venv, install deps, run migrations ─────────────────────
if [ ! -d backend/.venv ]; then
    log "Creating Python venv for backend..."
    python3 -m venv backend/.venv
fi

log "Installing backend dependencies..."
backend/.venv/bin/pip install --quiet --upgrade pip
backend/.venv/bin/pip install --quiet \
    "fastapi>=0.110,<0.120" \
    "uvicorn[standard]>=0.27" \
    "sqlalchemy[asyncio]>=2.0.25,<2.1" \
    "asyncpg>=0.29" \
    "alembic>=1.13" \
    "pydantic>=2.6,<3" \
    "pydantic-settings>=2.1" \
    "redis>=5.0,<6" \
    "cython>=3.0" \
    "passlib[bcrypt]>=1.7" \
    "hiredis>=2.3" \
    "PyJWT>=2.8" \
    "setuptools>=68" \
    "wheel"

# Build Cython
log "Building Cython matching engine..."
cd backend
PYTHONPATH=. .venv/bin/python app/matching/setup.py build_ext --inplace
cd ..

# Run migrations
log "Running database migrations..."
cd backend
DATABASE_URL="postgresql+asyncpg://exchange:exchange@localhost:5432/exchange" \
    PYTHONPATH=. .venv/bin/alembic upgrade head
cd ..

# ─── Start backend (uvicorn) in background ──────────────────────────────────
log "Starting backend (uvicorn) on :8000..."
cd backend
DATABASE_URL="postgresql+asyncpg://exchange:exchange@localhost:5432/exchange" \
    PYTHONPATH=. .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
cd ..
log "Backend PID: $BACKEND_PID"

# ─── Start matching worker in background ────────────────────────────────────
log "Starting matching worker..."
cd backend
DATABASE_URL="postgresql+asyncpg://exchange:exchange@localhost:5432/exchange" \
    PYTHONPATH=. .venv/bin/python -m app.matching.worker &
WORKER_PID=$!
cd ..
log "Worker PID: $WORKER_PID"

# ─── Start frontend (Vite dev server) ───────────────────────────────────────
if [ ! -d frontend/node_modules ]; then
    log "Installing frontend dependencies..."
    cd frontend && npm install && cd ..
fi

log "Starting frontend (Vite) on :3000..."
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..
log "Frontend PID: $FRONTEND_PID"

# ─── Start admin panel ──────────────────────────────────────────────────────
if [ ! -d admin/node_modules ]; then
    log "Installing admin panel dependencies..."
    cd admin && npm install && cd ..
fi

log "Starting admin panel (Vite) on :3001..."
cd admin
npm run dev &
ADMIN_PID=$!
cd ..
log "Admin PID: $ADMIN_PID"

# ─── Trap exit: kill all background processes ───────────────────────────────
cleanup() {
    log "Shutting down..."
    kill $BACKEND_PID $WORKER_PID $FRONTEND_PID $ADMIN_PID 2>/dev/null || true
    docker compose stop postgres redis 2>/dev/null || true
    log "Done"
}
trap cleanup EXIT INT TERM

log ""
log "========================================"
log "  Development environment is running!"
log "========================================"
log "  Frontend:       http://localhost:3000"
log "  Admin panel:    http://localhost:3001"
log "  Backend API:    http://localhost:8000"
log "  Swagger docs:   http://localhost:8000/docs"
log "  PostgreSQL:     localhost:5432"
log "  Redis:          localhost:6379"
log "========================================"
log ""
log "Press Ctrl+C to stop all services."

wait
