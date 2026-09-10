"""Rel1Ext Type-3 DPA path loss (WINNF-TS-4010 / REL1Ext-R2-SGN-02) — .

Composition matches harness ``computePropagationDpa``:

 pathlossDb = ITM(db) + ITU-R P.2108 §3.2 clutter (Tx AGL ≤ 6 m) + 8 dB
 network-loading / TDD activity factor

Antenna pattern gains (e/f/g) are intentionally omitted for modelType 3.
Official PAT.2 harness tolerance: UUT ``pathlossDb`` < reference + 1.0 dB.
"""

from __future__ import annotations

import math
from typing import Callable

from services.terrain.vincenty import geodesic_distance_km

# REL1Ext-R2-SGN-02 / WINNF-TS-1020 — same constants as harness ``p2108``.
F_2108_GHZ = 3.6
P2108_LOSS_2KM_DB = 30.50030179
CLUTTER_TX_AGL_MAX_M = 6.0
CLUTTER_DISTANCE_MIN_KM = 0.25
CLUTTER_DISTANCE_CAP_KM = 2.0
ACTIVITY_LOSS_FACTOR_DB = 8.0
# Harness PAT.2: sas_response['pathlossDb'] < ref_response['pathlossDb'] + 1
PAT2_PATHLOSS_TOLERANCE_DB = 1.0


def compose_dpa_pathloss_db(
    itm_db: float,
    clutter_db: float,
    *,
    activity_loss_db: float = ACTIVITY_LOSS_FACTOR_DB,
) -> float:
    """Return total Type-3 path loss toward a DPA protection point."""
    return float(itm_db) + float(clutter_db) + float(activity_loss_db)


def pathloss_within_pat2_tolerance(
    uut_pathloss_db: float,
    reference_pathloss_db: float,
    *,
    margin_db: float = PAT2_PATHLOSS_TOLERANCE_DB,
) -> bool:
    """True when UUT satisfies the official PAT.2 inequality vs a reference."""
    return float(uut_pathloss_db) < float(reference_pathloss_db) + float(margin_db)


def calc_p2108_clutter_db(
    lat_cbsd: float,
    lon_cbsd: float,
    height_cbsd: float,
    lat_rx: float,
    lon_rx: float,
    *,
    is_height_cbsd_amsl: bool = False,
    terrain_elevation_m: Callable[[float, float], float] | None = None,
) -> float:
    """ITU-R P.2108 §3.2.2 clutter for Rel1Ext DPA (portable harness mirror).

 Returns 0 when Tx AGL > 6 m or link distance < 0.25 km; caps at 2 km.
 """
    height_agl = float(height_cbsd)
    if is_height_cbsd_amsl:
        if terrain_elevation_m is None:
            raise ValueError("terrain_elevation_m required when height is AMSL")
        height_agl = height_agl - float(terrain_elevation_m(lat_cbsd, lon_cbsd))

    if height_agl > CLUTTER_TX_AGL_MAX_M:
        return 0.0

    distance_km = geodesic_distance_km(lat_cbsd, lon_cbsd, lat_rx, lon_rx)
    if distance_km < CLUTTER_DISTANCE_MIN_KM:
        return 0.0
    if distance_km >= CLUTTER_DISTANCE_CAP_KM:
        return float(P2108_LOSS_2KM_DB)

    # P.2108 (4a), (5a), (3a) with Q_inv = 0 (WINNF Rel1Ext simplification).
    l_l = -2.0 * math.log10(
        10.0 ** (-5.0 * math.log10(F_2108_GHZ) - 12.5) + 10.0 ** (-16.5)
    )
    l_s = 32.98 + 23.9 * math.log10(distance_km) + 3.0 * math.log10(F_2108_GHZ)
    return float(-5.0 * math.log10(10.0 ** (-0.2 * l_l) + 10.0 ** (-0.2 * l_s)))
