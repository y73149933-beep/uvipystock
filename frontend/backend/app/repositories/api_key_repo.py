"""API key repository."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.repositories.base import BaseRepository


class ApiKeyRepository(BaseRepository[ApiKey]):
    model = ApiKey

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_api_key(self, api_key: str) -> ApiKey | None:
        """Fetch an API key record by the public key string."""
        stmt = select(ApiKey).where(ApiKey.api_key == api_key)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(self, user_id: int) -> Sequence[ApiKey]:
        """All API keys for a user."""
        stmt = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_active_by_user(self, user_id: int) -> Sequence[ApiKey]:
        """All non-revoked API keys for a user."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.user_id == user_id, ApiKey.is_revoked == False)  # noqa: E712
            .order_by(ApiKey.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
