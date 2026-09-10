"""SQLAlchemy column types shared across ORM models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """UTC timestamps stored as timezone-aware datetimes.

 PostgreSQL: ``TIMESTAMP WITH TIME ZONE``. SQLite: ISO text with offset.
 Bind accepts naive (treated as UTC) or aware values. Results are always
 timezone-aware UTC.
 """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect
    ) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UtcDateTime expected datetime, got {type(value)!r}")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: datetime | None, dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
