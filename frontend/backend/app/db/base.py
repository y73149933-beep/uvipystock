"""DeclarativeBase for all ORM models.

All models inherit from `Base`. The `metadata` attribute is what Alembic
autogenerate compares against when generating migrations.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base."""
    pass
