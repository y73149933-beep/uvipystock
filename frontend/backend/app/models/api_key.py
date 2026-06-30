"""ApiKey model — HMAC-signed credentials for trading bots."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class ApiKey(Base):
    """API keypair used by trading bots for HMAC-SHA256 authentication.

    The public `api_key` is sent in the `X-API-Key` header; the secret
    is stored as a bcrypt hash and used to verify `X-Signature`.

    Permissions
    -----------
    Stored as a Postgres TEXT[]:
      * `"trade"` — POST/PUT/DELETE /orders, /orders/bulk
      * `"read"`  — GET /orders, /balance, /trades
      * `"ws"`    — WebSocket subscriptions
    """

    __tablename__ = "api_keys"

    id:                 Mapped[int]               = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id:            Mapped[int]               = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    api_key:            Mapped[str]               = mapped_column(String(64), unique=True, nullable=False, index=True)
    secret_hash:        Mapped[str]               = mapped_column(String(255), nullable=False)
    label:              Mapped[str | None]        = mapped_column(String(100), nullable=True)
    permissions:        Mapped[list[str]]         = mapped_column(
        ARRAY(String), default=lambda: ["trade", "read", "ws"], nullable=False
    )
    rate_limit_per_min: Mapped[int]               = mapped_column(Integer, default=120, nullable=False)
    is_revoked:         Mapped[bool]              = mapped_column(Boolean, default=False, nullable=False)
    created_at:         Mapped[datetime]          = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at:       Mapped[datetime | None]   = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:         Mapped[datetime | None]   = mapped_column(DateTime(timezone=True), nullable=True)

    # ─── Relationships ──────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(back_populates="api_keys")

    def has_permission(self, perm: str) -> bool:
        """Check if this key grants `perm` ('trade'/'read'/'ws')."""
        return perm in (self.permissions or [])

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} key={self.api_key[:8]}... user_id={self.user_id}>"
