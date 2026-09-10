"""Canonical GWPZ (injected WISP zone) geometry + frequency protection.

Shared by grant-time INTERFERENCE checks and CPAS/pre-IAP ``gwpz_exclusion``.
Geometry uses ``within_geojson_buffer_m`` (buffer 0 m) — the same predicate as
pre-IAP. Frequency and zone are read from the injected record; no fixture
constants.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from sqlalchemy.orm import Session

from services.data_injection_service import KIND_WISP, load_injected
from services.geometry import geojson_geometry_usable, within_geojson_buffer_m

# Pre-IAP / grant-time canonical: inside zone or on boundary (distance 0).
GWPZ_BUFFER_M = 0.0


class GwpzProtectionError(ValueError):
    """Malformed GWPZ data preventing a required protection determination."""


def freq_overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def parse_gwpz_zone_and_freq(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int, int] | None:
    """Extract GeoJSON zone and protected frequency from a WISP/GWPZ payload.

 Returns ``None`` when the record is incomplete or frequencies are invalid.
 """
    if not isinstance(payload, dict):
        return None
    zone = payload.get("zone")
    if not isinstance(zone, dict):
        return None
    record = payload.get("record")
    if not isinstance(record, dict):
        return None
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps or not isinstance(deps[0], dict):
        return None
    fr = (deps[0].get("operationParam") or {}).get("operationFrequencyRange") or {}
    if not isinstance(fr, dict):
        return None
    try:
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return None
    if low >= high:
        return None
    return zone, low, high


def gwpz_blocks(
    lat: float,
    lon: float,
    low_hz: int,
    high_hz: int,
    gwpz_record: dict[str, Any],
    *,
    buffer_m: float = GWPZ_BUFFER_M,
) -> bool | None:
    """Whether a location/frequency is blocked by one GWPZ record.

 Returns:
 ``True`` — inside/intersects zone (per buffer) and frequency overlaps.
 ``False`` — record is evaluable and does not block.
 ``None`` — record incomplete or geometry unusable (caller policy).
 """
    parsed = parse_gwpz_zone_and_freq(gwpz_record)
    if parsed is None:
        return None
    zone, z_low, z_high = parsed
    if not geojson_geometry_usable(zone):
        return None
    if not freq_overlaps(low_hz, high_hz, z_low, z_high):
        return False
    return within_geojson_buffer_m(float(lat), float(lon), zone, float(buffer_m))


def gwpz_blocks_any(
    lat: float,
    lon: float,
    low_hz: int,
    high_hz: int,
    gwpz_records: Sequence[dict[str, Any]],
    *,
    buffer_m: float = GWPZ_BUFFER_M,
    fail_closed_on_indeterminate: bool = False,
) -> bool:
    """True if any GWPZ record blocks the location/frequency.

 When ``fail_closed_on_indeterminate`` is True, indeterminate records raise
 ``GwpzProtectionError``. When False (pre-IAP), indeterminate records are
 skipped — preserving historical pre-IAP skip behavior.
 """
    for payload in gwpz_records:
        if not isinstance(payload, dict):
            if fail_closed_on_indeterminate:
                raise GwpzProtectionError("non-object GWPZ/WISP payload")
            continue
        result = gwpz_blocks(
            lat, lon, low_hz, high_hz, payload, buffer_m=buffer_m
        )
        if result is None:
            if fail_closed_on_indeterminate:
                raise GwpzProtectionError(
                    "malformed or indeterminate GWPZ/WISP protection data"
                )
            continue
        if result:
            return True
    return False


def grant_blocked_by_gwpz(
    db: Session, lat: float, lon: float, low_hz: int, high_hz: int
) -> bool:
    """Grant-time: True when an injected WISP/GWPZ blocks the proposed grant.

 Fail-closed: malformed KIND_WISP rows that cannot be evaluated raise
 ``GwpzProtectionError`` (caller maps to INTERFERENCE 400).
 """
    return gwpz_blocks_any(
        lat,
        lon,
        low_hz,
        high_hz,
        load_injected(db, KIND_WISP),
        buffer_m=GWPZ_BUFFER_M,
        fail_closed_on_indeterminate=True,
    )


def wisps_from_protection_records(
    protection_records: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Decode frozen CPAS protection rows of kind ``wisp``."""
    out: list[dict[str, Any]] = []
    for kind, _rid, data_json in protection_records:
        if kind != KIND_WISP:
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out
