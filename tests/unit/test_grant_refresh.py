"""periodic refresh and exclusive-end validity match CBRS heartbeat clocks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primitives.refresh import PeriodicRefresh, open_until
from primitives.time import UtcInstant
from services.grant_service import DEFAULT_GRANT_REFRESH, HEARTBEAT_INTERVAL_SEC
from services.lifecycle import _grant_expired


def test_heartbeat_interval_is_periodic_refresh_default():
    assert HEARTBEAT_INTERVAL_SEC == 60
    assert DEFAULT_GRANT_REFRESH.interval_seconds == 60
    assert DEFAULT_GRANT_REFRESH.advertised(None) == 60
    assert DEFAULT_GRANT_REFRESH.advertised(0) == 60
    assert DEFAULT_GRANT_REFRESH.advertised(90) == 90
    later = DEFAULT_GRANT_REFRESH.next_after(
        UtcInstant(datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    assert later.value == datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        PeriodicRefresh(interval_seconds=0)


def test_open_until_matches_truncated_expire_comparison():
    end = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    same = UtcInstant(end)
    before = UtcInstant(end - timedelta(seconds=1))
    after = UtcInstant(end + timedelta(seconds=1))
    assert open_until(UtcInstant(end), before) is True
    assert open_until(UtcInstant(end), same) is False
    assert open_until(UtcInstant(end), after) is False
    # Identity with previous lifecycle rule: expire <= wall (second precision).
    wall = end.replace(microsecond=0)
    assert (end.replace(microsecond=0) <= wall) is (not open_until(UtcInstant(end), UtcInstant(wall)))


class _Grant:
    def __init__(self, expire: datetime | None) -> None:
        self.grant_expire_time = expire


def test_grant_expired_uses_open_until():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    assert _grant_expired(_Grant(now + timedelta(seconds=1)), now=now) is False
    assert _grant_expired(_Grant(now), now=now) is True
    assert _grant_expired(_Grant(now - timedelta(seconds=1)), now=now) is True
    assert _grant_expired(_Grant(None), now=now) is False
