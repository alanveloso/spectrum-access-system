"""ordered access classes with arbitrary cardinality (D10)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primitives.access import AccessClass, OrderedAccess, bind_request_class
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint
from primitives.power import PowerDbm
from primitives.request import SpectrumRequest, TransmissionFootprint
from primitives.time import UtcInstant


def _request(*, access_class_id: str | None = None) -> SpectrumRequest:
    fp = TransmissionFootprint(
        frequency=FrequencyRange(1000, 2000),
        power=PowerDbm(20.0),
        location=GeoPoint(0.0, 0.0),
    )
    return SpectrumRequest(
        request_id="r1",
        holder_id="h1",
        footprints=(fp,),
        requested_at=UtcInstant(datetime(2026, 8, 15, tzinfo=timezone.utc)),
        access_class_id=access_class_id,
    )


def test_ordered_access_allows_one_or_many_classes():
    one = OrderedAccess(
        classes=(AccessClass(class_id="only", priority=1, preemptible=False),)
    )
    assert len(one.classes) == 1
    many = OrderedAccess(
        classes=(
            AccessClass(class_id="critical", priority=300, preemptible=False),
            AccessClass(class_id="local", priority=200, preemptible=True),
            AccessClass(class_id="opportunistic", priority=100, preemptible=True),
        )
    )
    assert many.ranks_above("critical", "opportunistic") is True
    assert many.ranks_above("opportunistic", "critical") is False
    assert many.ranks_above("local", "local") is False


def test_empty_ordered_access_is_invalid():
    with pytest.raises(ValueError):
        OrderedAccess(classes=())
    with pytest.raises(ValueError):
        AccessClass(class_id=" ", priority=1, preemptible=True)
    with pytest.raises(ValueError):
        OrderedAccess(
            classes=(
                AccessClass(class_id="a", priority=1, preemptible=True),
                AccessClass(class_id="a", priority=2, preemptible=True),
            )
        )


def test_classless_regime_omits_ordered_access():
    req = _request(access_class_id=None)
    assert bind_request_class(None, req) is None
    with pytest.raises(ValueError):
        bind_request_class(None, _request(access_class_id="local"))


def test_ordered_access_requires_known_class_on_request():
    access = OrderedAccess(
        classes=(
            AccessClass(class_id="local", priority=200, preemptible=True),
            AccessClass(class_id="critical", priority=300, preemptible=False),
        )
    )
    bound = bind_request_class(access, _request(access_class_id="local"))
    assert bound is not None
    assert bound.preemptible is True
    with pytest.raises(ValueError):
        bind_request_class(access, _request(access_class_id=None))
    with pytest.raises(ValueError):
        bind_request_class(access, _request(access_class_id="unknown"))
    with pytest.raises(ValueError):
        _request(access_class_id=" ")


def test_no_undifferentiated_access_type():
    import primitives.access as mod

    assert not hasattr(mod, "FlatAccess")
    assert not hasattr(mod, "flat_access")
