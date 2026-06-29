"""Application settings loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application configuration.

    All values can be overridden via environment variables (case-insensitive).
    A `.env` file at the project root is loaded automatically in development.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Runtime ─────────────────────────────────────────────────────────────
    app_env: str = Field(default="development", description="development|staging|production")
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret: str = Field(default="change-me-in-production", min_length=8)
    app_log_level: str = "INFO"

    # ─── Database ────────────────────────────────────────────────────────────
    postgres_user: str = "exchange"
    postgres_password: str = "exchange"
    postgres_db: str = "exchange"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Full DSN overrides the individual fields above if set
    database_url: PostgresDsn | None = None

    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_echo: bool = False

    # ─── Redis ───────────────────────────────────────────────────────────────
    redis_url: RedisDsn = "redis://localhost:6379/0"
    redis_pool_size: int = 50

    # ─── Auth ────────────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 1440  # 24h
    hmac_replay_window_seconds: int = 30

    # ─── Rate Limiting ───────────────────────────────────────────────────────
    default_rate_limit_per_min: int = 120

    # ─── Matching Engine ─────────────────────────────────────────────────────
    matching_worker_concurrency: int = 4
    matching_queue_block_timeout: int = 1  # BRPOP timeout — short for fast reconnect detection

    # ─── Convenience properties ─────────────────────────────────────────────

    @property
    def effective_database_url(self) -> str:
        """Resolve the actual database URL — explicit override wins."""
        if self.database_url is not None:
            return str(self.database_url)
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor — avoids re-parsing environment on every request."""
    return Settings()
