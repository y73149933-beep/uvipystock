# Backend Dockerfile: FastAPI app + Cython matching engine
# Multi-stage: builder compiles Cython, runtime is slim

# ─── Stage 1: Builder ────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Install build deps for Cython + asyncpg + bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first (better layer caching)
COPY pyproject.toml alembic.ini ./
COPY backend/ ./backend/

# Install dependencies to a virtualenv we can copy later
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    "fastapi>=0.110,<0.120" \
    "uvicorn[standard]>=0.27" \
    "sqlalchemy[asyncio]>=2.0.25,<2.1" \
    "asyncpg>=0.29" \
    "alembic>=1.13" \
    "pydantic>=2.6,<3" \
    "pydantic-settings>=2.1" \
    "redis>=5.0,<6" \
    "cython>=3.0" \
    "bcrypt>=4.0,<5.0" \
    "hiredis>=2.3" \
    "PyJWT>=2.8" \
    "setuptools>=68" \
    "wheel"

# Compile the Cython matching engine
WORKDIR /build/backend
RUN PYTHONPATH=/build/backend python app/matching/setup.py build_ext --inplace

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Install only runtime libs (no gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy backend source + compiled .so
COPY --from=builder /build/backend/ ./backend/
COPY --from=builder /build/pyproject.toml /build/alembic.ini ./

# Expose FastAPI port
EXPOSE 8000

# Healthcheck: hit the /openapi.json endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/openapi.json', timeout=5)" || exit 1

# Default: run migrations (from /app where alembic.ini lives) then start uvicorn
# alembic.ini is at /app/alembic.ini with script_location = backend/alembic
# uvicorn needs to run from /app/backend/ so `app.main:app` resolves
WORKDIR /app
CMD ["sh", "-c", "alembic upgrade head && cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
