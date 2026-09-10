"""SQLAlchemy declarative base shared by all ORM models.

Kept outside ``database.py`` so model modules do not import the engine/session
layer (avoids circular imports with ``init_db`` → load models → Base).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
