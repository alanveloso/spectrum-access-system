"""Canonical consumer semantics derived from composed DeviceAdapter output."""

from __future__ import annotations

import json
from typing import Any, Mapping

from adapters.device import ConsumerAdapter, ConsumerView
from models.models import Cbsd
from primitives.geography import GeoPoint


def primary_geo_point(view: ConsumerView) -> GeoPoint:
    if not view.footprints:
        raise ValueError("consumer view has no footprints")
    location = view.footprints[0].location
    if not isinstance(location, GeoPoint):
        raise ValueError("device geolocation requires a GeoPoint footprint")
    return location


def geo_coordinates(view: ConsumerView) -> tuple[float, float]:
    point = primary_geo_point(view)
    return point.latitude_deg, point.longitude_deg


def consumer_view_from_payload(
    payload: Mapping[str, object],
    adapter: ConsumerAdapter,
) -> ConsumerView:
    return adapter.to_consumer(payload)


def consumer_view_from_cbsd(cbsd: Cbsd, adapter: ConsumerAdapter) -> ConsumerView:
    try:
        reg: dict[str, Any] = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        reg = {}
    if cbsd.cbsd_id and not reg.get("cbsdId"):
        reg = {**reg, "cbsdId": cbsd.cbsd_id}
    return adapter.to_consumer(reg)


def cbsd_geo_coordinates(cbsd: Cbsd, adapter: ConsumerAdapter) -> tuple[float, float]:
    return geo_coordinates(consumer_view_from_cbsd(cbsd, adapter))
