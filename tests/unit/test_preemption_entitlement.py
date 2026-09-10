"""class-vs-class preemption and protection entitlement (D12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primitives.access import AccessClass, OrderedAccess
from primitives.entitlement import ProtectionEntitlement
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius
from primitives.power import PowerDbm
from primitives.preemption import class_preempts
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant


def _access() -> OrderedAccess:
    return OrderedAccess(
        classes=(
            AccessClass(class_id="high", priority=300, preemptible=False),
            AccessClass(class_id="mid", priority=200, preemptible=True),
            AccessClass(class_id="low", priority=100, preemptible=True),
            AccessClass(class_id="tied", priority=100, preemptible=True),
        )
    )


def test_higher_class_preempts_preemptible_lower_only():
    access = _access()
    assert class_preempts(access, "high", "low") is True
    assert class_preempts(access, "high", "mid") is True
    assert class_preempts(access, "low", "high") is False
    assert class_preempts(access, "mid", "high") is False
    assert class_preempts(access, "low", "low") is False


def test_equal_priority_and_unknown_class_do_not_invent_preemption():
    access = _access()
    assert class_preempts(access, "low", "tied") is False
    assert class_preempts(access, "tied", "low") is False
    with pytest.raises(ValueError):
        class_preempts(access, "high", "missing")


def test_entitlement_covers_holder_subset_scope():
    right = ProtectionEntitlement(
        entitlement_id="e1",
        holder_id="h1",
        frequency=FrequencyRange(1000, 3000),
        area=PointRadius(center=GeoPoint(0.0, 0.0), radius_m=200.0),
    )
    inside = TransmissionFootprint(
        frequency=FrequencyRange(1200, 1800),
        power=PowerDbm(10.0),
        location=GeoPoint(0.0, 0.001),
    )
    assert right.covers(holder_id="h1", footprint=inside) is True
    assert right.covers(holder_id="other", footprint=inside) is False
    wide = TransmissionFootprint(
        frequency=FrequencyRange(1000, 4000),
        power=PowerDbm(10.0),
        location=GeoPoint(0.0, 0.0),
    )
    assert right.covers(holder_id="h1", footprint=wide) is False
    far = TransmissionFootprint(
        frequency=FrequencyRange(1200, 1800),
        power=PowerDbm(10.0),
        location=GeoPoint(10.0, 10.0),
    )
    assert right.covers(holder_id="h1", footprint=far) is False


def test_entitlement_validity_and_untestable_geometry_fail_closed():
    start = datetime(2026, 8, 15, tzinfo=timezone.utc)
    window = TimeInterval.from_datetimes(start, start + timedelta(hours=1))
    right = ProtectionEntitlement(
        entitlement_id="e2",
        holder_id="h1",
        frequency=FrequencyRange(1000, 2000),
        validity=window,
    )
    fp = TransmissionFootprint(
        frequency=FrequencyRange(1000, 1500),
        power=PowerDbm(1.0),
        location=GeoPoint(0.0, 0.0),
    )
    assert right.covers(holder_id="h1", footprint=fp) is False
    assert right.covers(holder_id="h1", footprint=fp, at=UtcInstant(start)) is True
    geo_right = ProtectionEntitlement(
        entitlement_id="e3",
        holder_id="h1",
        frequency=FrequencyRange(1000, 2000),
        area=PointRadius(center=GeoPoint(0.0, 0.0), radius_m=100.0),
    )
    ring_fp = TransmissionFootprint(
        frequency=FrequencyRange(1000, 1500),
        power=PowerDbm(1.0),
        location=LinearRing.from_lon_lat([[0, 0], [1, 0], [1, 1], [0, 0]]),
    )
    assert geo_right.covers(holder_id="h1", footprint=ring_fp) is False
    with pytest.raises(ValueError):
        ProtectionEntitlement(
            entitlement_id=" ",
            holder_id="h1",
            frequency=FrequencyRange(1, 2),
        )
