"""frequency, power, time, geography value objects (CBRS-independent)."""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius, haversine_m
from primitives.power import PowerDbm, dbm_to_mw, mw_to_dbm
from primitives.time import TimeInterval, UtcInstant
from services.iap.aggregate import dbm_to_mw as iap_dbm_to_mw
from services.iap.aggregate import mw_to_dbm as iap_mw_to_dbm

_BANNED = (
    "cbsd",
    "pal",
    "gaa",
    "incumbent",
    "dpa",
    "esc",
    "ppa",
    "cpas",
    "fss",
    "grant",
    "winnforum",
    "fcc",
    "brasil",
    "brazil",
    "canada",
)


def test_frequency_half_open_overlap_and_intersection():
    a = FrequencyRange(low_hz=3_550_000_000, high_hz=3_560_000_000)
    b = FrequencyRange(low_hz=3_560_000_000, high_hz=3_570_000_000)
    c = FrequencyRange(low_hz=3_555_000_000, high_hz=3_565_000_000)
    assert a.width_hz == 10_000_000
    assert a.overlaps(b) is False
    assert a.overlaps(c) is True
    assert a.contains_hz(3_550_000_000) is True
    assert a.contains_hz(3_560_000_000) is False
    inter = a.intersection(c)
    assert inter == FrequencyRange(low_hz=3_555_000_000, high_hz=3_560_000_000)
    assert a.intersection(b) is None


def test_frequency_rejects_empty_or_negative():
    with pytest.raises(ValueError):
        FrequencyRange(low_hz=10, high_hz=10)
    with pytest.raises(ValueError):
        FrequencyRange(low_hz=-1, high_hz=1)


def test_power_matches_iap_conversion_identity():
    for dbm in (30.0, -10.0, 0.0, -137.0):
        assert dbm_to_mw(dbm) == pytest.approx(iap_dbm_to_mw(dbm))
        assert mw_to_dbm(dbm_to_mw(dbm)) == pytest.approx(iap_mw_to_dbm(iap_dbm_to_mw(dbm)))
    assert mw_to_dbm(0.0) == float("-inf")
    assert mw_to_dbm(-1.0) == float("-inf")
    assert PowerDbm(30.0).to_mw().mw == pytest.approx(1000.0)


def test_time_interval_half_open_and_rejects_naive():
    start = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=3600)
    window = TimeInterval.from_datetimes(start, end)
    assert window.duration_seconds == 3600.0
    assert window.contains(UtcInstant(start)) is True
    assert window.contains(UtcInstant(end)) is False
    other = TimeInterval.from_datetimes(end, end + timedelta(seconds=60))
    assert window.overlaps(other) is False
    with pytest.raises(ValueError):
        UtcInstant(datetime(2026, 8, 15, 12, 0))
    with pytest.raises(ValueError):
        TimeInterval.from_datetimes(end, start)


def test_geography_point_radius_and_ring():
    origin = GeoPoint(0.0, 0.0)
    nearby = GeoPoint(0.0, 0.001)
    far = GeoPoint(10.0, 10.0)
    ball = PointRadius(center=origin, radius_m=200.0)
    assert ball.contains(nearby) is True
    assert ball.contains(far) is False
    assert haversine_m(origin, origin) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        GeoPoint(91.0, 0.0)
    ring = LinearRing.from_lon_lat([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
    assert ring.contains(GeoPoint(1.0, 1.0)) is True
    assert ring.contains(GeoPoint(5.0, 5.0)) is False


def test_primitives_package_has_no_regime_nouns_or_service_imports():
    root = Path(__file__).resolve().parents[2] / "primitives"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for token in _BANNED:
            assert token not in lowered, f"{path.name} contains banned token {token!r}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("services")
                    assert not alias.name.startswith("models")
                    assert not alias.name.startswith("routes")
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("services")
                assert not node.module.startswith("models")
                assert not node.module.startswith("routes")


def test_haversine_agrees_with_services_geometry_on_sample():
    from services.geometry import haversine_m as svc_haversine

    a, b = GeoPoint(39.0, -77.0), GeoPoint(38.9, -77.1)
    assert haversine_m(a, b) == pytest.approx(
        svc_haversine(a.latitude_deg, a.longitude_deg, b.latitude_deg, b.longitude_deg)
    )
    assert math.isfinite(haversine_m(a, b))
