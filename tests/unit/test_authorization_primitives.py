"""lease, fixed window, authorized area, exclusion zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primitives.authorization import (
    AuthorizedArea,
    ExclusionZone,
    FixedWindow,
    Lease,
    LeaseState,
)
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing
from primitives.power import PowerDbm
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant

_START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _interval(hours: float = 1.0) -> TimeInterval:
    return TimeInterval.from_datetimes(_START, _START + timedelta(hours=hours))


def _fp(**kwargs: object) -> TransmissionFootprint:
    defaults = dict(
        frequency=FrequencyRange(1000, 2000),
        power=PowerDbm(10.0),
        location=GeoPoint(1.0, 1.0),
    )
    defaults.update(kwargs)
    return TransmissionFootprint(**defaults)  # type: ignore[arg-type]


def _lease(**kwargs: object) -> Lease:
    defaults = dict(
        lease_id="l1",
        holder_id="h1",
        footprints=(_fp(),),
        validity=_interval(),
    )
    defaults.update(kwargs)
    return Lease(**defaults)  # type: ignore[arg-type]


def test_lease_usable_and_state_machine():
    lease = _lease()
    now = UtcInstant(_START)
    assert lease.is_usable_at(now) is True
    suspended = lease.transition(LeaseState.SUSPENDED)
    assert suspended.is_usable_at(now) is False
    active = suspended.transition(LeaseState.ACTIVE)
    ended = active.transition(LeaseState.TERMINATED)
    with pytest.raises(ValueError):
        ended.transition(LeaseState.ACTIVE)
    past = UtcInstant(_START + timedelta(hours=2))
    assert _lease().expire_if_due(past).state == LeaseState.EXPIRED
    assert _lease().expire_if_due(now).state == LeaseState.ACTIVE


def test_fixed_window_caps_lease_validity():
    outer = FixedWindow(window=_interval(hours=2))
    assert outer.allows_validity(_interval(hours=1)) is True
    inner_late = TimeInterval.from_datetimes(
        _START + timedelta(hours=1), _START + timedelta(hours=3)
    )
    assert outer.allows_validity(inner_late) is False


def test_authorized_area_and_exclusion_zone():
    ring = LinearRing.from_lon_lat([[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]])
    area = AuthorizedArea(area_id="a1", ring=ring)
    assert area.allows(GeoPoint(1.0, 1.0)) is True
    assert area.allows(GeoPoint(10.0, 10.0)) is False
    assert area.allows(ring) is False
    zone = ExclusionZone(
        zone_id="z1",
        frequency=FrequencyRange(1500, 2500),
        area=ring,
    )
    assert zone.excludes(_fp(location=GeoPoint(1.0, 1.0))) is True
    assert zone.excludes(_fp(frequency=FrequencyRange(3000, 4000))) is False
    ring_fp = _fp(location=ring)
    assert zone.excludes(ring_fp) is True
    with pytest.raises(ValueError):
        Lease(lease_id="l", holder_id="h", footprints=(), validity=_interval())
