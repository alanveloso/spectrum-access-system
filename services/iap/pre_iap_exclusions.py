"""Pre-IAP exclusion-zone style terminations (WINNF CPAS zone purge subset).

Runs before aggregate IAP. Local grants only; peers never mutated.

Covered:
* Injected / NTIA Exclusion Zones (geometry + frequency) → terminate
* GWPZ geometry + frequency overlap → terminate
* FSS with neighboring GWBL + grant on 3650–3700 MHz within 150 km → terminate
* Quiet-zone / FCC / Table Mountain / configurable QPR areas → terminate

TT&C FSS purge-list (full R2-SGN-29 algorithm) remains approximate here:
when ``ttc=True`` and FSS high > 3700 MHz, grants inside the FSS blocking
neighborhood on the CBRS band are terminated (conservative product rule).
Full Monte-Carlo purge parity stays ENV/harness-bound.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from sqlalchemy.orm import Session

from services.exclusion_zone_service import (
    EXZ_BUFFER_M,
    KIND_EXCLUSION_ZONE,
    KIND_NTIA_ZONES,
    ExclusionZoneError,
    point_hits_exclusion_records,
)
from services.geometry import haversine_m
from services.gwpz_protection import (
    GWPZ_BUFFER_M,
    gwpz_blocks,
    wisps_from_protection_records,
)
from services.iap.protection_points import (
    FSS_TTC_LOW_HZ,
    KIND_FSS,
    KIND_GWBL,
    ProtectionEntityError,
    parse_fss_ttc,
)
from services.quiet_zone_service import QuietZoneUnavailable, quiet_zone_blocks_location

FSS_GWBL_LOW_HZ = 3_650_000_000
FSS_GWBL_HIGH_HZ = 3_700_000_000
FSS_GWBL_KM = 150.0
FSS_TTC_PURGE_KM = 40.0


def _freq_overlap(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _payloads(
    protection_records: Sequence[tuple[str, str, str]], kind: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, _rid, data_json in protection_records:
        if k != kind:
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _frozen_exz_records(
    protection_records: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    return _payloads(protection_records, KIND_EXCLUSION_ZONE) + _payloads(
        protection_records, KIND_NTIA_ZONES
    )


def _fss_lat_lon_freq(
    payload: dict[str, Any],
) -> tuple[float, float, int, int] | None:
    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    if not isinstance(record, dict):
        return None
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps or not isinstance(deps[0], dict):
        return None
    inst = deps[0].get("installationParam") or {}
    fr = (deps[0].get("operationParam") or {}).get("operationFrequencyRange") or {}
    try:
        lat = float(inst["latitude"])
        lon = float(inst["longitude"])
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return None
    return lat, lon, low, high


def _gwbl_points(payloads: list[dict[str, Any]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for rec in payloads:
        try:
            out.append((float(rec["latitude"]), float(rec["longitude"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def evaluate_pre_iap_exclusions(
    local_grants: Sequence[Any],
    protection_records: Sequence[tuple[str, str, str]],
    *,
    db: Session | None = None,
) -> list[tuple[Any, str]]:
    """Return ``(frozen_grant, reason)`` pairs that must terminate before IAP."""
    wisps = wisps_from_protection_records(protection_records)
    fsses = _payloads(protection_records, KIND_FSS)
    gwbles = _gwbl_points(_payloads(protection_records, KIND_GWBL))
    exz_records = _frozen_exz_records(protection_records)
    hits: list[tuple[Any, str]] = []

    for frozen in local_grants:
        if getattr(frozen, "terminated", False):
            continue
        lat = getattr(frozen, "latitude", None)
        lon = getattr(frozen, "longitude", None)
        if lat is None or lon is None:
            continue
        low = int(frozen.low_hz)
        high = int(frozen.high_hz)
        reason: str | None = None

        try:
            if exz_records and point_hits_exclusion_records(
                exz_records,
                float(lat),
                float(lon),
                low,
                high,
                buffer_m=EXZ_BUFFER_M,
                strict_geometry=True,
            ):
                reason = "exz_exclusion"
        except ExclusionZoneError as exc:
            raise ProtectionEntityError(f"EXZ evaluation failed: {exc}") from exc

        if reason is None:
            cat = str(getattr(frozen, "cbsd_category", "") or "A")
            bw_mhz = (high - low) / 1_000_000.0 if high > low else None
            try:
                q_reason = quiet_zone_blocks_location(
                    float(lat),
                    float(lon),
                    cbsd_category=cat or "A",
                    bandwidth_mhz=bw_mhz,
                    db=db,
                )
            except QuietZoneUnavailable as exc:
                raise ProtectionEntityError(f"quiet-zone evaluation failed: {exc}") from exc
            if q_reason is not None:
                reason = f"quiet_zone_{q_reason}"

        if reason is None:
            for wisp in wisps:
 # Indeterminate WISP → skip (historical pre-IAP); only True blocks.
                if (
                    gwpz_blocks(
                        float(lat),
                        float(lon),
                        low,
                        high,
                        wisp,
                        buffer_m=GWPZ_BUFFER_M,
                    )
                    is True
                ):
                    reason = "gwpz_exclusion"
                    break

        if reason is None and gwbles and _freq_overlap(
            low, high, FSS_GWBL_LOW_HZ, FSS_GWBL_HIGH_HZ
        ):
            for fss in fsses:
                coords = _fss_lat_lon_freq(fss)
                if coords is None:
                    continue
                f_lat, f_lon, _fl, _fh = coords
                if haversine_m(float(lat), float(lon), f_lat, f_lon) > FSS_GWBL_KM * 1000.0:
                    continue
                if any(
                    haversine_m(f_lat, f_lon, g_lat, g_lon) <= FSS_GWBL_KM * 1000.0
                    for g_lat, g_lon in gwbles
                ):
                    reason = "fss_gwbl_exclusion"
                    break

        if reason is None:
            for fss in fsses:
                try:
                    ttc = parse_fss_ttc(fss)
                except ProtectionEntityError:
                    raise
                coords = _fss_lat_lon_freq(fss)
                if coords is None or ttc is not True:
                    continue
                f_lat, f_lon, _fl, f_high = coords
                if f_high <= FSS_TTC_LOW_HZ:
                    continue
                if (
                    haversine_m(float(lat), float(lon), f_lat, f_lon)
                    <= FSS_TTC_PURGE_KM * 1000.0
                ):
                    reason = "fss_ttc_purge"
                    break

        if reason is not None:
            hits.append((frozen, reason))

    seen: set[int] = set()
    unique: list[tuple[Any, str]] = []
    for frozen, reason in hits:
        pk = int(frozen.grant_pk)
        if pk in seen:
            continue
        seen.add(pk)
        unique.append((frozen, reason))
    return unique
