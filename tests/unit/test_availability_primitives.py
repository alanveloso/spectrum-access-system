"""availability_constraint + events as first-class (not preemption)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primitives.availability import (
    AvailabilityChangeEvent,
    AvailabilityConstraint,
    AvailabilityEventKind,
    AvailabilityMode,
    AvailabilityScope,
    AvailabilityZoneKind,
    any_constraint_allows,
)
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing
from primitives.power import PowerDbm
from primitives.preemption import class_preempts
from primitives.access import AccessClass, OrderedAccess
from primitives.registry import MechanismAxis, builtin_mechanism_registry
from primitives.request import TransmissionFootprint
from primitives.time import TimeInterval, UtcInstant
from profiles.parse import parse_profile_document
from profiles.errors import ProfileValidationError

_T0 = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _instant(offset_s: int = 0) -> UtcInstant:
    return UtcInstant(_T0 + timedelta(seconds=offset_s))


def _interval(start_s: int, end_s: int) -> TimeInterval:
    return TimeInterval(start=_instant(start_s), end=_instant(end_s))


def _ring() -> LinearRing:
    return LinearRing.from_lon_lat([[0, 0], [2, 0], [2, 2], [0, 0]])


def _footprint(
    *,
    low: int = 1000,
    high: int = 2000,
    eirp: float = 20.0,
    location: GeoPoint | LinearRing | None = None,
) -> TransmissionFootprint:
    return TransmissionFootprint(
        frequency=FrequencyRange(low_hz=low, high_hz=high),
        power=PowerDbm(eirp),
        location=location
        or GeoPoint(latitude_deg=1.0, longitude_deg=1.0),
    )


def test_mechanism_registered_on_authorization_axis() -> None:
    reg = builtin_mechanism_registry()
    assert reg.on_axis(MechanismAxis.AUTHORIZATION, "availability_constraint")


def test_allowance_window_respects_validity_and_scope() -> None:
    constraint = AvailabilityConstraint(
        constraint_id="aw-1",
        mode=AvailabilityMode.SCHEDULED,
        validity=_interval(0, 3600),
        scope=AvailabilityScope(
            frequency=FrequencyRange(low_hz=1000, high_hz=3000),
            area=_ring(),
            max_eirp=PowerDbm(30.0),
        ),
        zone_kind=AvailabilityZoneKind.ALLOWANCE,
        source_id="incumbent-a",
    )
    fp = _footprint()
    assert constraint.allows_footprint(fp, _instant(10))
    assert not constraint.allows_footprint(fp, _instant(3600))  # half-open end
    assert not constraint.allows_footprint(_footprint(eirp=31.0), _instant(10))
    assert not constraint.allows_footprint(
        _footprint(low=5000, high=6000), _instant(10)
    )


def test_exclusion_blocks_when_in_force() -> None:
    constraint = AvailabilityConstraint(
        constraint_id="ex-1",
        mode=AvailabilityMode.ON_DEMAND,
        validity=_interval(0, 100),
        scope=AvailabilityScope(
            frequency=FrequencyRange(low_hz=1000, high_hz=2000),
            area=_ring(),
        ),
        zone_kind=AvailabilityZoneKind.EXCLUSION,
        source_id="incumbent-b",
    )
    fp = _footprint()
    assert not constraint.allows_footprint(fp, _instant(1))
    assert not constraint.allows_footprint(fp, _instant(100))


def test_network_ring_footprint_matches_identical_allowance_area() -> None:
    ring = _ring()
    constraint = AvailabilityConstraint(
        constraint_id="aw-ring",
        mode=AvailabilityMode.SCHEDULED,
        validity=_interval(0, 100),
        scope=AvailabilityScope(
            frequency=FrequencyRange(low_hz=1000, high_hz=2000),
            area=ring,
        ),
        zone_kind=AvailabilityZoneKind.ALLOWANCE,
    )
    # Identical ring: treat as covered (vertices alone sit on the boundary).
    assert constraint.allows_footprint(_footprint(location=ring), _instant(1))


def test_availability_expiry_is_not_preemption() -> None:
    constraint = AvailabilityConstraint(
        constraint_id="aw-2",
        mode=AvailabilityMode.SCHEDULED,
        validity=_interval(0, 60),
        scope=AvailabilityScope(frequency=FrequencyRange(low_hz=1, high_hz=2)),
    )
    assert constraint.expired_relative_to(True, _instant(60))
    assert not constraint.expired_relative_to(False, _instant(60))
    access = OrderedAccess(
        (
            AccessClass(class_id="high", priority=200, preemptible=False),
            AccessClass(class_id="low", priority=100, preemptible=True),
        )
    )
    # Expiry path must not require class_preempts semantics.
    assert class_preempts(access, "high", "low")
    assert constraint.expired_relative_to(True, _instant(60))


def test_change_event_and_any_constraint_fail_closed() -> None:
    event = AvailabilityChangeEvent(
        event_id="e1",
        constraint_id="aw-1",
        observed_at=_instant(0),
        kind=AvailabilityEventKind.EVACUATION,
    )
    assert event.kind is AvailabilityEventKind.EVACUATION
    fp = _footprint()
    assert any_constraint_allows((), fp, _instant(0)) is False
    with pytest.raises(ValueError):
        AvailabilityChangeEvent(
            event_id=" ",
            constraint_id="aw-1",
            observed_at=_instant(0),
            kind=AvailabilityEventKind.UPDATED,
        )


def test_profile_v2_temporal_availability_wiring() -> None:
    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "avail_probe", "version": "0.0.1", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": 2300000000, "high_hz": 2400000000}]
        },
        "authorization": {"mechanism": "fixed_window"},
        "temporal": {
            "availability": {
                "mechanism": "availability_constraint",
                "mode": "scheduled",
            }
        },
        "rf": {"required": False, "policy": "path_loss_plus_aggregate"},
    }
    parsed = parse_profile_document(doc)
    assert parsed.temporal is not None
    assert parsed.temporal.availability is not None
    assert parsed.temporal.availability.mode == "scheduled"


def test_profile_rejects_unknown_availability_mode() -> None:
    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "bad", "version": "0.0.1", "status": "custom"},
        "spectrum": {
            "ranges": [{"id": "main", "low_hz": 1, "high_hz": 2}]
        },
        "temporal": {
            "availability": {
                "mechanism": "availability_constraint",
                "mode": "heartbeat",
            }
        },
        "rf": {"required": False, "policy": "path_loss_plus_aggregate"},
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)
