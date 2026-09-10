"""Synthetic PPA RF contour engines for unit tests (no ITM / NED / shapely)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from services.ppa_rf_contour import PpaRfEngines, THRESHOLD_PER_10MHZ_DBM
from services.terrain.vincenty import geodesic_distance_km


@dataclass
class _Loss:
    db_loss: float


def _fspl_db(dist_km: float, freq_mhz: float = 3625.0) -> float:
    d = max(dist_km, 1e-3)
    return float(20.0 * math.log10(d) + 20.0 * math.log10(freq_mhz) + 32.44)


def fake_ppa_rf_engines(
    *,
    region: str = "SUBURBAN",
    extra_loss_db: float = 35.0,
) -> PpaRfEngines:
    """Hybrid stub: free-space-like loss so coverage scales with EIRP.

    ``extra_loss_db`` defaults high enough that Cat-A contours stay within
    typical unit-test service-area squares (~0.02°) without requiring NED/ITM.
    Not a production Free-Space fallback — only via ``body['_rfEngines']``.
    """

    def calc_hybrid(
        lat_cbsd: float,
        lon_cbsd: float,
        height_cbsd: float,
        lat_rx: float,
        lon_rx: float,
        height_rx: float,
        **kwargs: Any,
    ) -> _Loss:
        dist = geodesic_distance_km(lat_cbsd, lon_cbsd, lat_rx, lon_rx)
        # Mild height term so tests can observe height sensitivity.
        height_term = max(0.0, 10.0 - float(height_cbsd)) * 0.15
        return _Loss(db_loss=_fspl_db(dist) + extra_loss_db + height_term)

    def antenna_standard_gains(
        azimuths: Sequence[float] | float,
        antenna_azimuth: float | None,
        antenna_beamwidth: float | None,
        antenna_gain: float,
    ) -> list[float] | float:
        gain = float(antenna_gain)
        if isinstance(azimuths, (int, float)):
            return _directional_gain(
                float(azimuths), antenna_azimuth, antenna_beamwidth, gain
            )
        return [
            _directional_gain(float(az), antenna_azimuth, antenna_beamwidth, gain)
            for az in azimuths
        ]

    def region_type(lat: float, lon: float) -> str:
        return region

    return PpaRfEngines(
        calc_hybrid=calc_hybrid,
        antenna_standard_gains=antenna_standard_gains,
        region_type=region_type,
    )


def _directional_gain(
    az: float,
    antenna_azimuth: float | None,
    antenna_beamwidth: float | None,
    peak_gain: float,
) -> float:
    if antenna_azimuth is None or antenna_beamwidth is None:
        return peak_gain
    bw = float(antenna_beamwidth)
    if bw >= 360.0:
        return peak_gain
    # Simple sector: full gain inside beamwidth, −20 dB outside.
    delta = abs((az - float(antenna_azimuth) + 180.0) % 360.0 - 180.0)
    if delta <= bw / 2.0:
        return peak_gain
    return peak_gain - 20.0


def coverage_radius_km_for_eirp(
    eirp_dbm_10mhz: float, *, peak_gain: float = 0.0, freq_mhz: float = 3625.0
) -> float:
    """Approximate isotropic free-space radius where received = −96 dBm/10 MHz."""
    # eirp - peak + peak - fspl >= -96  =>  eirp - fspl >= -96
    # fspl <= eirp + 96
    # 20log10(d) + 20log10(f) + 32.44 <= eirp + 96
    budget = eirp_dbm_10mhz + abs(THRESHOLD_PER_10MHZ_DBM) - (
        20.0 * math.log10(freq_mhz) + 32.44
    )
    if budget <= 0:
        return 0.2
    return 10 ** (budget / 20.0)
