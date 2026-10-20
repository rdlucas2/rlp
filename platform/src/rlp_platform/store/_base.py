"""Shared helpers for the store modules."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy.orm import Session

from rlp_platform.models import Base


class NotFound(LookupError):
    """No record with that key."""


class UnknownField(ValueError):
    """An update named a field the record doesn't let callers change."""


def get[T: Base](session: Session, model: type[T], record_id: int) -> T:
    record = session.get(model, record_id)
    if record is None:
        raise NotFound(f"{model.__tablename__} {record_id} not found")
    return record


def add[T: Base](session: Session, record: T) -> T:
    session.add(record)
    session.flush()
    return record


def update[T: Base](session: Session, record: T, allowed: Collection[str], fields: dict[str, Any]) -> T:
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        raise UnknownField(f"{type(record).__tablename__} has no updatable field {', '.join(unknown)}")
    for name, value in fields.items():
        setattr(record, name, value)
    session.flush()
    return record
