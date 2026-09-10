"""configurable admission primitives (band, power, class, geography)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primitives.access import AccessClass, OrderedAccess
from primitives.admission import evaluate_admission, power_exceeds
from primitives.constraint import Constraint, ConstraintKind
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, PointRadius
from primitives.power import PowerDbm
from primitives.request import SpectrumRequest, TransmissionFootprint
from primitives.time import UtcInstant
from services.grant_service import (
    CBRS_HIGH_HZ,
    CBRS_LOW_HZ,
    INVALID_PARAM,
    UNSUPPORTED_SPECTRUM,
    _parse_freq,
)


def _now() -> UtcInstant:
    return UtcInstant(datetime(2026, 1, 1, tzinfo=timezone.utc))


def _request(
    *,
    low: int,
    high: int,
    power_dbm: float = 10.0,
    lat: float = 39.0,
    lon: float = -77.0,
    access_class_id: str | None = None,
) -> SpectrumRequest:
    return SpectrumRequest(
        request_id="r1",
        holder_id="h1",
        footprints=(
            TransmissionFootprint(
                frequency=FrequencyRange(low_hz=low, high_hz=high),
                power=PowerDbm(power_dbm),
                location=GeoPoint(latitude_deg=lat, longitude_deg=lon),
            ),
        ),
        requested_at=_now(),
        access_class_id=access_class_id,
    )


def test_power_exceeds_strict_greater_than():
    assert power_exceeds(PowerDbm(21.0), PowerDbm(20.0)) is True
    assert power_exceeds(PowerDbm(20.0), PowerDbm(20.0)) is False


def test_frequency_allow_and_deny():
    allow = Constraint(
        kind=ConstraintKind.FREQUENCY_ALLOW,
        frequency=FrequencyRange(low_hz=3_550_000_000, high_hz=3_700_000_000),
    )
    deny = Constraint(
        kind=ConstraintKind.FREQUENCY_DENY,
        frequency=FrequencyRange(low_hz=3_650_000_000, high_hz=3_660_000_000),
    )
    evaluate_admission(_request(low=3_550_000_000, high=3_560_000_000), (allow,))
    with pytest.raises(ValueError, match="allowed band"):
        evaluate_admission(_request(low=3_400_000_000, high=3_410_000_000), (allow,))
    with pytest.raises(ValueError, match="denied"):
        evaluate_admission(
            _request(low=3_655_000_000, high=3_658_000_000), (allow, deny)
        )


def test_max_power_and_geography():
    area = PointRadius(
        center=GeoPoint(latitude_deg=39.0, longitude_deg=-77.0),
        radius_m=1000.0,
    )
    constraints = (
        Constraint(kind=ConstraintKind.MAX_POWER, max_power=PowerDbm(20.0), area=area),
    )
    evaluate_admission(_request(low=1, high=2, power_dbm=20.0), constraints)
    with pytest.raises(ValueError, match="max_power"):
        evaluate_admission(_request(low=1, high=2, power_dbm=20.1), constraints)
    with pytest.raises(ValueError, match="outside"):
        evaluate_admission(
            _request(low=1, high=2, power_dbm=10.0, lat=40.0, lon=-77.0),
            constraints,
        )


def test_ordered_access_binds_class():
    access = OrderedAccess(
        classes=(
            AccessClass(class_id="primary", priority=2, preemptible=False),
            AccessClass(class_id="secondary", priority=1, preemptible=True),
        )
    )
    evaluate_admission(
        _request(low=1, high=2, access_class_id="secondary"),
        (),
        access=access,
    )
    with pytest.raises(ValueError, match="access_class_id"):
        evaluate_admission(_request(low=1, high=2), (), access=access)
    with pytest.raises(ValueError, match="unknown"):
        evaluate_admission(
            _request(low=1, high=2, access_class_id="other"),
            (),
            access=access,
        )


def test_grant_parse_freq_uses_band_contains():
    ok = {
        "operationFrequencyRange": {
            "lowFrequency": CBRS_LOW_HZ,
            "highFrequency": CBRS_HIGH_HZ,
        }
    }
    err, low, high = _parse_freq(ok)
    assert err is None and low == CBRS_LOW_HZ and high == CBRS_HIGH_HZ
    outside = {
        "operationFrequencyRange": {
            "lowFrequency": CBRS_LOW_HZ,
            "highFrequency": CBRS_HIGH_HZ + 1,
        }
    }
    err, _, _ = _parse_freq(outside)
    assert err == UNSUPPORTED_SPECTRUM
    inverted = {
        "operationFrequencyRange": {
            "lowFrequency": CBRS_HIGH_HZ,
            "highFrequency": CBRS_LOW_HZ,
        }
    }
    err, _, _ = _parse_freq(inverted)
    assert err == INVALID_PARAM
