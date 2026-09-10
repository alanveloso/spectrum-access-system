"""Ordered access classes with configurable cardinality and priority (D10).

Classless regimes omit this type entirely. There is no separate undifferentiated
access type. Protected-entity / constraint-source roles are not classes (D11).
"""

from __future__ import annotations

from dataclasses import dataclass

from primitives.request import SpectrumRequest


@dataclass(frozen=True, slots=True)
class AccessClass:
    """One ordered class. ``class_id`` is opaque profile data, not a protocol noun."""

    class_id: str
    priority: int
    preemptible: bool

    def __post_init__(self) -> None:
        if not self.class_id.strip():
            raise ValueError("class_id is required")


@dataclass(frozen=True, slots=True)
class OrderedAccess:
    """Cardinality 1..N. Empty is invalid: omit the mechanism instead."""

    classes: tuple[AccessClass, ...]

    def __post_init__(self) -> None:
        if not self.classes:
            raise ValueError("OrderedAccess requires at least one class")
        ids = [c.class_id for c in self.classes]
        if len(ids) != len(set(ids)):
            raise ValueError("AccessClass class_id values must be unique")

    def get(self, class_id: str) -> AccessClass:
        for item in self.classes:
            if item.class_id == class_id:
                return item
        raise ValueError(f"unknown access class {class_id!r}")

    def ranks_above(self, higher_id: str, lower_id: str) -> bool:
        """True when ``higher_id`` has strictly greater numeric priority."""
        return self.get(higher_id).priority > self.get(lower_id).priority


def bind_request_class(
    access: OrderedAccess | None, request: SpectrumRequest
) -> AccessClass | None:
    """Fail closed when class presence disagrees with the access mechanism."""
    if access is None:
        if request.access_class_id is not None:
            raise ValueError("access_class_id set but ordered access is absent")
        return None
    if request.access_class_id is None:
        raise ValueError("ordered access requires access_class_id on the request")
    return access.get(request.access_class_id)
