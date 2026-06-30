"""User repository."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email."""
        stmt = select(User).where(User.email == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self) -> Sequence[User]:
        """All active users."""
        stmt = select(User).where(User.is_active == True).order_by(User.id)  # noqa: E712
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_admins(self) -> Sequence[User]:
        """All admin users."""
        stmt = select(User).where(User.is_admin == True).order_by(User.id)  # noqa: E712
        result = await self.session.execute(stmt)
        return result.scalars().all()
