"""User model — owners of balances, orders, and API keys."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.api_key import ApiKey
    from app.models.balance import Balance


class User(Base):
    """A trading account. Admins are flagged via `is_admin`.

    Notes
    -----
    Passwords are never stored in plaintext; `password_hash` holds a bcrypt
    digest. For paper trading with bot-friendly auth, ApiKey-based HMAC is
    preferred (see `ApiKey`).
    """

    __tablename__ = "users"

    id:            Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email:         Mapped[str]      = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str]      = mapped_column(String(255), nullable=False)
    is_active:     Mapped[bool]     = mapped_column(Boolean, default=True, nullable=False)
    is_admin:      Mapped[bool]     = mapped_column(Boolean, default=False, nullable=False)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at:    Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ─── Relationships ──────────────────────────────────────────────────────
    balances: Mapped[list["Balance"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} admin={self.is_admin}>"
