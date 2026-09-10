"""Admission of a spectrum request against declarative constraints.

No protocol nouns. Access-class and IAP loops stay in services.
"""

from __future__ import annotations

from primitives.access import OrderedAccess, bind_request_class
from primitives.constraint import Constraint, ConstraintKind
from primitives.geography import GeoPoint, LinearRing, PointRadius
from primitives.power import PowerDbm
from primitives.request import SpectrumRequest, TransmissionFootprint


def power_exceeds(requested: PowerDbm, limit: PowerDbm) -> bool:
    return requested.dbm > limit.dbm


def _location_allowed(footprint: TransmissionFootprint, area: PointRadius | LinearRing) -> bool:
    if isinstance(footprint.location, GeoPoint):
        return area.contains(footprint.location)
    if isinstance(footprint.location, PointRadius):
        return area.contains(footprint.location.center)
    return True


def evaluate_admission(
    request: SpectrumRequest,
    constraints: tuple[Constraint, ...],
    *,
    access: OrderedAccess | None = None,
) -> None:
    """Raise ValueError if the request violates frequency, power, area, or class."""
    bind_request_class(access, request)
    allows = tuple(
        c for c in constraints if c.kind is ConstraintKind.FREQUENCY_ALLOW and c.frequency
    )
    denies = tuple(
        c for c in constraints if c.kind is ConstraintKind.FREQUENCY_DENY and c.frequency
    )
    for footprint in request.footprints:
        if allows and not any(
            c.frequency is not None and c.frequency.contains(footprint.frequency)
            for c in allows
        ):
            raise ValueError("frequency not in allowed band")
        for deny in denies:
            if deny.frequency is not None and deny.frequency.overlaps(footprint.frequency):
                raise ValueError("frequency intersects a denied range")
        for constraint in constraints:
            if (
                constraint.kind is ConstraintKind.MAX_POWER
                and constraint.max_power is not None
                and power_exceeds(footprint.power, constraint.max_power)
            ):
                raise ValueError("requested power exceeds max_power")
            if constraint.area is not None and not _location_allowed(
                footprint, constraint.area
            ):
                raise ValueError("footprint outside constraint area")
