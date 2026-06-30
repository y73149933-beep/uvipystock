"""Generic async repository base class.

Provides common CRUD helpers that work for any SQLAlchemy 2.0 model.
Subclasses define the `model` attribute and inherit `get`, `get_multi`,
`create`, `update`, `delete`.
"""
from __future__ import annotations

from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import select, update as sa_update, delete as sa_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Async CRUD base for a single model type."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id_: int) -> ModelT | None:
        """Fetch one row by primary key."""
        return await self.session.get(self.model, id_)

    async def get_multi(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        order_by: str | None = None,
        order_desc: bool = False,
    ) -> Sequence[ModelT]:
        """Fetch a page of rows."""
        stmt = select(self.model).offset(offset).limit(limit)
        if order_by:
            col = getattr(self.model, order_by, None)
            if col is not None:
                stmt = stmt.order_by(col.desc() if order_desc else col.asc())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count(self) -> int:
        """Total row count for this model."""
        result = await self.session.execute(select(func.count()).select_from(self.model))
        return int(result.scalar_one())

    async def create(self, **kwargs: Any) -> ModelT:
        """Insert a new row. Caller must `session.commit()`."""
        obj = self.model(**kwargs)
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, id_: int, **kwargs: Any) -> ModelT | None:
        """Update a row by primary key. Returns the updated object or None."""
        obj = await self.get(id_)
        if obj is None:
            return None
        for k, v in kwargs.items():
            setattr(obj, k, v)
        await self.session.flush()
        return obj

    async def delete(self, id_: int) -> bool:
        """Delete a row by primary key. Returns True if deleted."""
        obj = await self.get(id_)
        if obj is None:
            return False
        await self.session.delete(obj)
        await self.session.flush()
        return True
