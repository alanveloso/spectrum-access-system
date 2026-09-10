"""Build IAP ``ProtectionPoint`` lists from admin-injected entities.

Thresholds default to WINNF-TS-0061 Table 8.4-2 values used by the harness
``reference_models.iap.iap`` module (normative RBW figures). Official harness
tolerances / fixture geometry remain ENV-bound for PASS_OFFICIAL.

Neighborhood distances follow WINNF-TS-0112 / interference.py constants.
No harness fixture IDs or coordinates are hard-coded.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from services.data_injection_service import (
    KIND_FSS,
    KIND_WISP,
    KIND_ZONE,
    load_injected,
)
from services.iap.models import ProtectedEntityKind, ProtectionPoint

logger = logging.getLogger(__name__)

# Normative ESC passband (WINNF interference.ESC_LOW/HIGH_FREQ_HZ).
ESC_PASSBAND_LOW_HZ = 3_550_000_000
ESC_PASSBAND_HIGH_HZ = 3_680_000_000

# WINNF-TS-0112 / reference_models.interference (Hz).
FSS_COEXIST_LOW_HZ = 3_600_000_000
FSS_TTC_LOW_HZ = 3_700_000_000
FSS_TTC_HIGH_HZ = 4_200_000_000

# Neighborhood distances (km) — WINNF-TS-0112.
NEIGHBORHOOD_GWPZ_KM = 40.0
NEIGHBORHOOD_PPA_KM = 40.0
NEIGHBORHOOD_FSS_COCHANNEL_KM = 150.0
NEIGHBORHOOD_FSS_BLOCKING_KM = 40.0
# ESC neighborhood (km) — WINNF-TS-0112 / interference.py:
# ESC_NEIGHBORHOOD_DIST_A = 40, ESC_NEIGHBORHOOD_DIST_B = 80.
# Per-grant filter in ``grants_in_neighborhood`` applies A/B; the point stores
# the Cat-B envelope so distance filtering is never capped below 80 km.
NEIGHBORHOOD_ESC_KM_A = 40.0
NEIGHBORHOOD_ESC_KM_B = 80.0
NEIGHBORHOOD_ESC_KM = NEIGHBORHOOD_ESC_KM_B

# Current IAP implementation requires aggregate-linear-power protection semantics.
_IAP_REQUIRED_PROTECTION_MECHANISM = "aggregate_linear_power"


def esc_neighborhood_km_for_category(category: str | None) -> float:
    """Delegate to IAP engine helper (single source for ESC A/B distances)."""
    from services.iap.engine import esc_neighborhood_km_for_category as _impl

    return _impl(category)

KIND_PAL = "pal"
KIND_GWBL = "gwbl"


class ProtectionEntityError(ValueError):
    """Invalid / incomplete protection entity input (domain validation)."""


def cbrs_band_hz() -> tuple[int, int]:
    """Active Profile operating-band edges for IAP clipping / FSS gates.

 Current IAP implementation requires aggregate-linear-power protection
 semantics. Edges come from ``primary_spectrum_range``, not assignment
 channelization.
 """
    from profiles.context import get_active_profile_document, primary_spectrum_range
    from profiles.errors import (
        ProfileNotFoundError,
        ProfilePathError,
        ProfileValidationError,
    )

    try:
        document = get_active_profile_document()
    except (ProfileNotFoundError, ProfilePathError, ProfileValidationError) as exc:
        raise ProtectionEntityError(
            f"IAP operating band unavailable: {exc}"
        ) from exc

    if (
        document.protection is None
        or _IAP_REQUIRED_PROTECTION_MECHANISM not in document.protection.mechanisms
    ):
        raise ProtectionEntityError(
            "Current IAP implementation requires aggregate-linear-power "
            "protection semantics"
        )

    try:
        primary = primary_spectrum_range(document)
    except ProfileValidationError as exc:
        raise ProtectionEntityError(
            f"IAP operating band unavailable: {exc}"
        ) from exc

    return int(primary.low_hz), int(primary.high_hz)


@dataclass(frozen=True)
class IapThresholdProfile:
    """Protection thresholds (dBm) and pre-IAP margin (dB).

 Classification:
 * ``esc_dbm`` / ``fss_cochannel_dbm`` / ``fss_blocking_dbm`` / ``ppa_dbm`` /
 ``gwpz_dbm`` / ``pre_iap_margin_db`` — **A** normative defaults aligned to
 WINNF-TS-0061 Table 8.4-2 / SSC higher-tier Mg=1 dB (not fixture copies).
 ESC threshold: ``THRESH_ESC_DBM_PER_RBW = -109`` in harness
 ``reference_models.iap.iap`` (same table).
 """

    esc_dbm: float = -109.0
    fss_cochannel_dbm: float = -129.0
    fss_blocking_dbm: float = -60.0
    ppa_dbm: float = -80.0
    gwpz_dbm: float = -80.0
    pre_iap_margin_db: float = 1.0


def clip_frequency_to_cbrs(
    low_hz: int,
    high_hz: int,
    *,
    band_low: int | None = None,
    band_high: int | None = None,
) -> tuple[int, int] | None:
    if band_low is None or band_high is None:
        default_low, default_high = cbrs_band_hz()
        band_low = default_low if band_low is None else band_low
        band_high = default_high if band_high is None else band_high
    low = max(int(low_hz), int(band_low))
    high = min(int(high_hz), int(band_high))
    if low >= high:
        return None
    return low, high


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _freq_pair(fr: Any) -> tuple[int, int] | None:
    if not isinstance(fr, dict):
        return None
    try:
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
    except (KeyError, TypeError, ValueError):
        return None
    if low >= high:
        return None
    return low, high


def _geometry_from_zone_blob(zone: Any) -> dict[str, Any] | None:
    if not isinstance(zone, dict):
        return None
    ztype = zone.get("type")
    if ztype in {"Polygon", "MultiPolygon"}:
        return zone
    if ztype == "Feature":
        geom = zone.get("geometry")
        return geom if isinstance(geom, dict) else None
    if ztype == "FeatureCollection":
        features = zone.get("features") or []
        if isinstance(features, list) and features:
            first = features[0]
            if isinstance(first, dict):
                geom = first.get("geometry")
                return geom if isinstance(geom, dict) else None
    return None


def _point_from_installation(inst: Any) -> tuple[float, float] | None:
    if not isinstance(inst, dict):
        return None
    lat = _as_float(inst.get("latitude"))
    lon = _as_float(inst.get("longitude"))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def parse_fss_ttc(payload: dict[str, Any]) -> bool | None:
    """Return True/False when ``ttc`` is present; None when missing."""
    if "ttc" not in payload:
        return None
    raw = payload.get("ttc")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    raise ProtectionEntityError(f"invalid FSS ttc value: {raw!r}")


def _fss_record_and_loc_freq(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], tuple[float, float], tuple[int, int]] | None:
    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    if not isinstance(record, dict):
        return None
    deps = record.get("deploymentParam")
    if not isinstance(deps, list) or not deps:
        return None
    first = deps[0] if isinstance(deps[0], dict) else None
    if first is None:
        return None
    loc = _point_from_installation(first.get("installationParam"))
    fr_blob = (first.get("operationParam") or {}).get("operationFrequencyRange")
    if isinstance(fr_blob, dict) and (
        "lowFrequency" in fr_blob or "highFrequency" in fr_blob
    ):
        fr = _freq_pair(fr_blob)
        if fr is None:
            raise ProtectionEntityError("FSS operationFrequencyRange invalid")
    else:
        fr = None
    if loc is None or fr is None:
        return None
    return record, loc, fr


def protection_points_from_fss_payload(
    payload: dict[str, Any],
    *,
    profile: IapThresholdProfile = IapThresholdProfile(),
) -> list[ProtectionPoint]:
    """Build FSS_COCHANNEL and/or FSS_BLOCKING points (0–2).

 Semantics aligned with harness ``performIapForFssCochannel`` /
 ``performIapForFssBlocking`` + TTC gate (not fixture geometry).
 """
    parsed = _fss_record_and_loc_freq(payload)
    if parsed is None:
        return []
    record, loc, (fss_low, fss_high) = parsed
    if fss_low <= 0 or fss_high <= fss_low:
        raise ProtectionEntityError("FSS operationFrequencyRange invalid")

    ttc = parse_fss_ttc(payload) if isinstance(payload, dict) else None
    point_id = str(record.get("id") or "fss").strip() or "fss"
    band_low, band_high = cbrs_band_hz()
    out: list[ProtectionPoint] = []

    # Co-channel: FSS passband overlaps CBRS and extends to/above CBRS high
    # (harness requires fss_high >= CBRS_HIGH for co-channel IAP).
    if fss_high >= band_high and fss_low < band_high:
        clipped = clip_frequency_to_cbrs(max(fss_low, band_low), band_high)
        if clipped is not None:
            out.append(
                ProtectionPoint(
                    point_id=f"fss-cc:{point_id}",
                    latitude=loc[0],
                    longitude=loc[1],
                    low_hz=clipped[0],
                    high_hz=clipped[1],
                    threshold_dbm=profile.fss_cochannel_dbm,
                    entity_kind=ProtectedEntityKind.FSS_COCHANNEL,
                    pre_iap_margin_db=profile.pre_iap_margin_db,
                    neighborhood_km=NEIGHBORHOOD_FSS_COCHANNEL_KM,
                    source_entity_id=point_id,
                )
            )

    # Blocking: protect CBRS emissions below FSS low edge, unless FSS is
    # entirely in 3700–4200 with TTC explicitly false.
    entirely_ttc_band = fss_low >= FSS_TTC_LOW_HZ and fss_high <= FSS_TTC_HIGH_HZ
    if entirely_ttc_band and ttc is False:
        return out
    if entirely_ttc_band and ttc is None:
        raise ProtectionEntityError(
            "FSS in 3700–4200 MHz requires explicit ttc for blocking applicability"
        )
    if fss_low > band_low:
        block_high = min(fss_low, band_high)
        if block_high > band_low:
            out.append(
                ProtectionPoint(
                    point_id=f"fss-bl:{point_id}",
                    latitude=loc[0],
                    longitude=loc[1],
                    low_hz=band_low,
                    high_hz=block_high,
                    threshold_dbm=profile.fss_blocking_dbm,
                    entity_kind=ProtectedEntityKind.FSS_BLOCKING,
                    pre_iap_margin_db=profile.pre_iap_margin_db,
                    neighborhood_km=NEIGHBORHOOD_FSS_BLOCKING_KM,
                    source_entity_id=point_id,
                )
            )
    return out


def protection_point_from_fss_payload(
    payload: dict[str, Any],
    *,
    profile: IapThresholdProfile = IapThresholdProfile(),
) -> ProtectionPoint | None:
    """Back-compat: first co-channel point if any, else first blocking point."""
    points = protection_points_from_fss_payload(payload, profile=profile)
    for pt in points:
        if pt.entity_kind is ProtectedEntityKind.FSS_COCHANNEL:
            return pt
    return points[0] if points else None


def protection_point_from_wisp_payload(
    payload: dict[str, Any],
    *,
    profile: IapThresholdProfile = IapThresholdProfile(),
) -> ProtectionPoint | None:
    from services.dpa_protection import polygon_representative_point

    record = payload.get("record")
    zone = payload.get("zone")
    if not isinstance(record, dict):
        return None
    geom = _geometry_from_zone_blob(zone)
    loc = polygon_representative_point(geom)
    if loc is None:
        return None
    deps = record.get("deploymentParam")
    fr = None
    if isinstance(deps, list) and deps and isinstance(deps[0], dict):
        fr = _freq_pair((deps[0].get("operationParam") or {}).get("operationFrequencyRange"))
    if fr is None:
        # Record omitted frequency: protect GWPZ-typical upper CBRS segment
        # from spectrum profile (3650–3700 when band is standard CBRS).
        band_low, band_high = cbrs_band_hz()
        fr = (max(band_low, 3_650_000_000), band_high)
    clipped = clip_frequency_to_cbrs(fr[0], fr[1])
    if clipped is None:
        return None
    point_id = str(record.get("id") or "gwpz").strip() or "gwpz"
    return ProtectionPoint(
        point_id=f"gwpz:{point_id}",
        latitude=loc[0],
        longitude=loc[1],
        low_hz=clipped[0],
        high_hz=clipped[1],
        threshold_dbm=profile.gwpz_dbm,
        entity_kind=ProtectedEntityKind.GWPZ,
        pre_iap_margin_db=profile.pre_iap_margin_db,
        neighborhood_km=NEIGHBORHOOD_GWPZ_KM,
    )


def _pal_freq_from_record(pal: dict[str, Any]) -> tuple[int, int] | None:
    """Resolve PAL protected band from common WINNF / marketplace shapes."""
    channel = pal.get("channelAssignment") or {}
    if isinstance(channel, dict):
        primary = channel.get("primaryAssignment")
        if isinstance(primary, dict):
            fr = _freq_pair(primary.get("operationFrequencyRange")) or _freq_pair(primary)
            if fr is not None:
                return fr
        fr = _freq_pair(channel.get("operationFrequencyRange"))
        if fr is not None:
            return fr
    block = pal.get("palBlock")
    if isinstance(block, dict):
        fr = _freq_pair(block)
        if fr is not None:
            return fr
    return _freq_pair(pal.get("operationFrequencyRange"))


def protection_point_from_zone_payload(
    payload: dict[str, Any],
    *,
    profile: IapThresholdProfile = IapThresholdProfile(),
    pal_by_id: dict[str, dict[str, Any]] | None = None,
) -> ProtectionPoint | None:
    from services.dpa_protection import polygon_representative_point

    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    if not isinstance(record, dict):
        return None
    if record.get("terminated") is True:
        return None
    if record.get("usage") != "PPA" and "ppaInfo" not in record:
        return None
    geom = _geometry_from_zone_blob(record.get("zone"))
    loc = polygon_representative_point(geom)
    if loc is None:
        return None

    pals = pal_by_id or {}
    ppa_info = record.get("ppaInfo") or {}
    pal_ids = ppa_info.get("palId") or []
    ranges: list[tuple[int, int]] = []
    if isinstance(pal_ids, list):
        for pal_id in pal_ids:
            pal = pals.get(str(pal_id))
            if not isinstance(pal, dict):
                continue
            pf = _pal_freq_from_record(pal)
            if pf is not None:
                ranges.append(pf)
    if ranges:
        low = min(r[0] for r in ranges)
        high = max(r[1] for r in ranges)
        clipped = clip_frequency_to_cbrs(low, high)
    else:
        # No usable PAL binding: cannot invent a protected band silently.
        # Fail closed at builder → omit point (caller may treat missing PPA
        # PAL as incomplete when PPA requires protection).
        logger.info(
            "PPA %s has no resolvable PAL frequency; skipping IAP point",
            record.get("id"),
        )
        return None
    if clipped is None:
        return None
    point_id = str(record.get("id") or "ppa").strip() or "ppa"
    return ProtectionPoint(
        point_id=f"ppa:{point_id}",
        latitude=loc[0],
        longitude=loc[1],
        low_hz=clipped[0],
        high_hz=clipped[1],
        threshold_dbm=profile.ppa_dbm,
        entity_kind=ProtectedEntityKind.PPA,
        pre_iap_margin_db=profile.pre_iap_margin_db,
        neighborhood_km=NEIGHBORHOOD_PPA_KM,
    )


def _esc_gain_pattern_from_install(install: dict[str, Any]) -> tuple[float, ...]:
    """Parse azimuthRadiationPattern into a 360-entry dBi tuple (angle index)."""
    raw = install.get("azimuthRadiationPattern")
    if not isinstance(raw, list) or not raw:
        raise ProtectionEntityError("ESC azimuthRadiationPattern missing or empty")
    gains = [float("nan")] * 360
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProtectionEntityError("ESC azimuthRadiationPattern entry invalid")
        try:
            angle = int(entry["angle"])
            gain = float(entry["gain"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtectionEntityError(
                "ESC azimuthRadiationPattern angle/gain invalid"
            ) from exc
        if angle < 0 or angle > 359:
            raise ProtectionEntityError("ESC azimuthRadiationPattern angle out of range")
        gains[angle] = gain
    if any(math.isnan(g) for g in gains):
        raise ProtectionEntityError("ESC azimuthRadiationPattern incomplete (need 0..359)")
    return tuple(gains)


def protection_point_from_esc_sensor_record(
    record: dict[str, Any],
    *,
    record_id: str,
    profile: IapThresholdProfile = IapThresholdProfile(),
) -> ProtectionPoint | None:
    if not isinstance(record, dict):
        raise ProtectionEntityError("ESC sensor record must be an object")
    install = record.get("installationParam")
    if not isinstance(install, dict):
        deps = record.get("deploymentParam")
        if isinstance(deps, list) and deps and isinstance(deps[0], dict):
            install = deps[0].get("installationParam")
    if not isinstance(install, dict):
        # Incomplete sensor without coordinates: skip (not fail-open protection).
        return None
    loc = _point_from_installation(install)
    if loc is None:
        return None

    height = _as_float(install.get("height"))
    azimuth = _as_float(install.get("antennaAzimuth"))
    if height is None:
        raise ProtectionEntityError(f"ESC {record_id} installation height missing")
    if azimuth is None:
        raise ProtectionEntityError(f"ESC {record_id} antennaAzimuth missing")
    pattern = _esc_gain_pattern_from_install(install)

    fr = None
    saw_freq_blob = False
    for key in ("protectionFrequencyRange", "operationFrequencyRange"):
        blob = record.get(key)
        if isinstance(blob, dict) and (
            "lowFrequency" in blob or "highFrequency" in blob
        ):
            saw_freq_blob = True
            fr = _freq_pair(blob)
            if fr is None:
                raise ProtectionEntityError(
                    f"ESC {record_id} frequency range invalid"
                )
            break
    if fr is None:
        if saw_freq_blob:
            raise ProtectionEntityError(f"ESC {record_id} frequency range invalid")
        # Normative ESC passband (WINNF interference.ESC_LOW/HIGH_FREQ_HZ).
        fr = (ESC_PASSBAND_LOW_HZ, ESC_PASSBAND_HIGH_HZ)
    clipped = clip_frequency_to_cbrs(fr[0], fr[1])
    if clipped is None:
        return None
    rid = str(record_id or record.get("id") or "esc").strip() or "esc"
    return ProtectionPoint(
        point_id=f"esc:{rid}",
        latitude=loc[0],
        longitude=loc[1],
        low_hz=clipped[0],
        high_hz=clipped[1],
        threshold_dbm=profile.esc_dbm,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=profile.pre_iap_margin_db,
        neighborhood_km=NEIGHBORHOOD_ESC_KM,
        receiver_height_m=float(height),
        receiver_antenna_azimuth_deg=float(azimuth),
        receiver_antenna_gain_pattern_dbi=pattern,
    )


def _index_pals(pal_payloads: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pal in pal_payloads or []:
        if not isinstance(pal, dict):
            continue
        pid = pal.get("palId") or (pal.get("record") or {}).get("palId")
        if pid:
            out[str(pid)] = pal
    return out


def build_protection_points_from_payloads(
    *,
    fss_payloads: list[dict[str, Any]] | None = None,
    wisp_payloads: list[dict[str, Any]] | None = None,
    zone_payloads: list[dict[str, Any]] | None = None,
    esc_records: list[tuple[str, dict[str, Any]]] | None = None,
    pal_payloads: list[dict[str, Any]] | None = None,
    profile: IapThresholdProfile | None = None,
) -> list[ProtectionPoint]:
    """Build IAP points from already-loaded payloads (frozen or live)."""
    thr = profile or IapThresholdProfile()
    points: list[ProtectionPoint] = []
    pal_by_id = _index_pals(pal_payloads)

    for payload in fss_payloads or []:
        points.extend(protection_points_from_fss_payload(payload, profile=thr))

    for payload in wisp_payloads or []:
        pt = protection_point_from_wisp_payload(payload, profile=thr)
        if pt is not None:
            points.append(pt)

    for payload in zone_payloads or []:
        pt = protection_point_from_zone_payload(
            payload, profile=thr, pal_by_id=pal_by_id
        )
        if pt is not None:
            points.append(pt)

    for record_id, data in esc_records or []:
        pt = protection_point_from_esc_sensor_record(
            data, record_id=record_id, profile=thr
        )
        if pt is not None:
            points.append(pt)

    points.sort(key=lambda p: (p.entity_kind.value, p.point_id, p.low_hz, p.high_hz))
    return points


def _assert_frozen_esc_connectivity(
    protection_records: tuple[tuple[str, str, str], ...],
) -> None:
    """Fail-closed when frozen ESC state is invalid (not missing)."""
    from services.esc_admin_service import (
        KIND_ESC_STATE,
        EscConnectivityError,
        EscConnectivityState,
        parse_frozen_esc_connectivity,
    )

    for kind, _rid, data_json in protection_records:
        if kind != KIND_ESC_STATE:
            continue
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError as exc:
            raise ProtectionEntityError("esc_state JSON invalid") from exc
        try:
            state = parse_frozen_esc_connectivity(data)
        except EscConnectivityError as exc:
            raise ProtectionEntityError(str(exc)) from exc
        if state in {EscConnectivityState.INVALID, EscConnectivityState.UNKNOWN}:
            raise ProtectionEntityError(
                f"ESC connectivity state {state.value} cannot authorize RF"
            )
        # CONNECTED / DISCONNECTED / ABSENT: keep ESC IAP points active.
        # Disconnect/absent strengthen DPA/IPR paths; they do not drop EPR IAP.
        return


def protection_points_from_peer_esc_records(
    peer_esc_records: list[dict[str, Any]],
    *,
    profile: IapThresholdProfile | None = None,
) -> list[ProtectionPoint]:
    """Build ESC ProtectionPoints from frozen peer FAD esc_sensor payloads."""
    thr = profile or IapThresholdProfile()
    out: list[ProtectionPoint] = []
    for idx, record in enumerate(peer_esc_records):
        if not isinstance(record, dict):
            continue
        rid = str(record.get("id") or f"peer-esc:{idx}")
        pt = protection_point_from_esc_sensor_record(
            record, record_id=f"peer:{rid}", profile=thr
        )
        if pt is not None:
            out.append(pt)
    return out


def build_protection_points_from_frozen(
    protection_records: tuple[tuple[str, str, str], ...],
    *,
    profile: IapThresholdProfile | None = None,
    peer_esc_records: list[dict[str, Any]] | None = None,
) -> list[ProtectionPoint]:
    """Build IAP points from ``CpasSnapshot.protection_records`` generation N."""
    _assert_frozen_esc_connectivity(protection_records)
    fss: list[dict[str, Any]] = []
    wisp: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    pals: list[dict[str, Any]] = []
    esc: list[tuple[str, dict[str, Any]]] = []
    for kind, record_id, data_json in protection_records:
        try:
            data = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if kind == KIND_FSS:
            fss.append(data)
        elif kind == KIND_WISP:
            wisp.append(data)
        elif kind == KIND_ZONE:
            zones.append(data)
        elif kind == KIND_PAL:
            pals.append(data)
        elif kind == "esc_sensor":
            esc.append((record_id, data))
        # gwbl / exclusion_zone / ntia / esc_state: pre-IAP or connectivity only.
    points = build_protection_points_from_payloads(
        fss_payloads=fss,
        wisp_payloads=wisp,
        zone_payloads=zones,
        esc_records=esc,
        pal_payloads=pals,
        profile=profile,
    )
    if peer_esc_records:
        points.extend(
            protection_points_from_peer_esc_records(
                peer_esc_records, profile=profile
            )
        )
        points.sort(
            key=lambda p: (p.entity_kind.value, p.point_id, p.low_hz, p.high_hz)
        )
    return points


def build_protection_points_from_db(
    db: Session,
    *,
    profile: IapThresholdProfile | None = None,
) -> list[ProtectionPoint]:
    """Collect IAP points from injected FSS / WISP / PPA zones and ESC sensors."""
    from models.models import EscSensor

    esc_records: list[tuple[str, dict[str, Any]]] = []
    for row in db.query(EscSensor).order_by(EscSensor.id).all():
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            esc_records.append((row.record_id, data))

    from services.pal_service import load_pal_records

    points = build_protection_points_from_payloads(
        fss_payloads=load_injected(db, KIND_FSS),
        wisp_payloads=load_injected(db, KIND_WISP),
        zone_payloads=load_injected(db, KIND_ZONE),
        esc_records=esc_records,
        pal_payloads=load_pal_records(db),
        profile=profile,
    )
    logger.debug("Built %d IAP protection points from injections", len(points))
    return points


def capture_protection_records_for_freeze(db: Session) -> tuple[tuple[str, str, str], ...]:
    """Snapshot protection entity payloads for CPAS generation N.

 Includes FSS/WISP/ZONE/ESC/PAL/GWBL plus EXZ/NTIA and ESC connectivity
 so EXZ/EPR evaluate without live N+1 regulatory reads.
 """
    from models.models import EscSensor
    from services.esc_admin_service import capture_esc_connectivity_for_freeze
    from services.exclusion_zone_service import capture_exclusion_zone_records_for_freeze
    from services.pal_service import load_pal_records

    rows: list[tuple[str, str, str]] = []
    for kind in (KIND_FSS, KIND_WISP, KIND_ZONE, KIND_GWBL):
        for idx, payload in enumerate(load_injected(db, kind)):
            record_id = ""
            if isinstance(payload, dict):
                record = (
                    payload.get("record")
                    if isinstance(payload.get("record"), dict)
                    else payload
                )
                if isinstance(record, dict):
                    record_id = str(
                        record.get("id")
                        or record.get("palId")
                        or payload.get("palId")
                        or ""
                    )
            if not record_id:
                record_id = f"{kind}:{idx}"
            rows.append(
                (kind, record_id, json.dumps(payload, sort_keys=True, default=str))
            )

    for idx, pal in enumerate(load_pal_records(db)):
        pal_id = str(pal.get("palId") or f"pal:{idx}")
        rows.append(
            (KIND_PAL, pal_id, json.dumps(pal, sort_keys=True, default=str))
        )

    rows.extend(capture_exclusion_zone_records_for_freeze(db))
    rows.append(capture_esc_connectivity_for_freeze(db))

    for idx, payload in enumerate(load_injected(db, "scheduled_dpa")):
        rows.append(
            (
                "scheduled_dpa",
                f"scheduled_dpa:{idx}",
                json.dumps(payload, sort_keys=True, default=str),
            )
        )
    from services.meas_report import FLAG_DPA_ACTIVE

    for idx, payload in enumerate(load_injected(db, FLAG_DPA_ACTIVE)):
        dpa_id = ""
        if isinstance(payload, dict):
            dpa_id = str(payload.get("dpaId") or "")
        rows.append(
            (
                "dpa_activation",
                dpa_id or f"dpa_activation:{idx}",
                json.dumps(payload, sort_keys=True, default=str),
            )
        )

    for row in db.query(EscSensor).order_by(EscSensor.id).all():
        rows.append(("esc_sensor", str(row.record_id), row.data_json or "{}"))

    rows.sort(key=lambda t: (t[0], t[1]))
    return tuple(rows)
