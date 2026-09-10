"""Canadian border (Arrangement R) PFD protection for Grant.

Mirrors WINNF_FT_S_BPR_testcase logic: CBSDs in the Border Sharing Zone whose
requested EIRP would produce PFD > -80 dBm/m²/MHz at the closest border point
must be rejected with responseCode 400.

Fail-closed policy:
* When Arrangement R frequency overlap applies and the CBSD is inside the
 border sharing zone, missing RF model / ITM / terrain → **not** authorized
 (UNAVAILABLE or DENY).
* When Arrangement R applies but the required US/Canada border KMZ is missing
 or unusable, membership is indeterminate → UNAVAILABLE (deny).
* CBSDs outside the sharing zone are allowed without loading ITM/numpy.
* Free Space path loss is used only when ``sas_bpr_path_loss_model=free_space``
 is selected explicitly; it is never a silent ITM substitute.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Any, Literal

# Harness reference models (ITM, Canadian border geometry, antenna gains).
_HARNESS = Path(__file__).resolve().parents[2] / "src" / "harness"
if _HARNESS.is_dir() and str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))

# Arrangement R: grants overlapping above 3650 MHz are subject to border PFD.
ARRANGEMENT_R_LOW_HZ = 3_650_000_000
ARRANGEMENT_R_HIGH_HZ = 3_700_000_000
PFD_LIMIT_DBM_M2_MHZ = -80.0
BORDER_RX_HEIGHT_M = 1.5
ITM_FREQ_MHZ = 3625.0

BprPathLossModel = Literal["itm", "free_space"]


class BorderPfdOutcome(str, Enum):
    """Result of Arrangement R border PFD evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    UNAVAILABLE = "unavailable"  # required model/dataset missing → fail-closed


class BorderProtectionUnavailable(Exception):
    """Required BPR reference model / dataset unavailable for a required check."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _overlaps_arrangement_r(low_hz: int, high_hz: int) -> bool:
    # Same gate as BPR harness: highFrequency > 3650 MHz.
    return high_hz > ARRANGEMENT_R_LOW_HZ and low_hz < ARRANGEMENT_R_HIGH_HZ


def _configured_bpr_path_loss_model() -> BprPathLossModel:
    """Obsolete post-composition selector — migration/tests only."""
    from config import get_settings

    raw = str(getattr(get_settings(), "sas_bpr_path_loss_model", "itm") or "itm")
    normalized = raw.strip().lower()
    if normalized in ("free_space", "fs", "freespace"):
        return "free_space"
    return "itm"


def _resolve_bpr_rf_port(rf_port: object | None) -> object | None:
    if rf_port is not None:
        return rf_port
    from runtime.context import get_bound_runtime_composition

    composition = get_bound_runtime_composition()
    if composition is not None:
        return composition.rf
    return None


def _free_space_path_loss_db(
    lat_tx: float,
    lon_tx: float,
    height_tx_m: float,
    lat_rx: float,
    lon_rx: float,
    height_rx_m: float,
    *,
    freq_mhz: float = ITM_FREQ_MHZ,
) -> float:
    """FSPL (dB) for explicit free_space BPR override via external package."""
    from primitives.geography import GeoPoint
    from rf.external_propagation import path_loss_db_via_spectrum_propagation

    return path_loss_db_via_spectrum_propagation(
        tx=GeoPoint(latitude_deg=lat_tx, longitude_deg=lon_tx),
        rx=GeoPoint(latitude_deg=lat_rx, longitude_deg=lon_rx),
        tx_height_m=float(height_tx_m),
        rx_height_m=float(height_rx_m),
        frequency_hz=int(round(float(freq_mhz) * 1_000_000.0)),
        backend="free_space",
    )


def _itm_path_loss_for_bpr(
    lat_tx: float,
    lon_tx: float,
    height_tx_m: float,
    lat_rx: float,
    lon_rx: float,
    height_rx_m: float,
    *,
    indoor: bool,
    height_is_amsl: bool,
    freq_mhz: float = ITM_FREQ_MHZ,
) -> tuple[float, float]:
    """ITM median + TX horizontal incidence via external adapter (not wf_itm)."""
    from primitives.geography import GeoPoint
    from rf.external_propagation import itm_path_loss_with_geometry
    from rf.port import RfUnavailableError

    try:
        details = itm_path_loss_with_geometry(
            tx=GeoPoint(latitude_deg=lat_tx, longitude_deg=lon_tx),
            rx=GeoPoint(latitude_deg=lat_rx, longitude_deg=lon_rx),
            tx_height_m=float(height_tx_m),
            rx_height_m=float(height_rx_m),
            frequency_hz=int(round(float(freq_mhz) * 1_000_000.0)),
            indoor=indoor,
            tx_height_is_agl=not height_is_amsl,
            reliability=0.5,
        )
    except RfUnavailableError as exc:
        raise RuntimeError(str(exc)) from exc
    bearing = float(details.hor_tx_deg) if details.hor_tx_deg is not None else 0.0
    return float(details.path_loss_db), bearing


def evaluate_canadian_border_pfd(
    installation: dict[str, Any],
    max_eirp: float,
    low_hz: int,
    high_hz: int,
    *,
    rf_port: object | None = None,
    path_loss_model: BprPathLossModel | None = None,
) -> BorderPfdOutcome:
    """Evaluate Arrangement R border PFD without authorizing on model failure.

    Path-loss implementation comes from ``rf_port`` (RuntimeComposition) when
    provided/bound. Explicit ``path_loss_model`` remains a lab/test override.
    ``SAS_BPR_PATH_LOSS_MODEL`` is not consulted after composition.
    """
    if not _overlaps_arrangement_r(low_hz, high_hz):
        return BorderPfdOutcome.ALLOW

    try:
        lat = float(installation["latitude"])
        lon = float(installation["longitude"])
    except (KeyError, TypeError, ValueError):
        # Cannot prove compliance inside Arrangement R band.
        return BorderPfdOutcome.UNAVAILABLE

    ant_azi = installation.get("antennaAzimuth")
    ant_bw = installation.get("antennaBeamwidth")
    try:
        max_ant_gain = float(installation.get("antennaGain") or 0)
    except (TypeError, ValueError):
        max_ant_gain = 0.0

    # Membership uses product KMZ + stdlib (no numpy). Only CBSDs inside the
    # sharing zone require reference_models / ITM for PFD; interior sites must
    # not be fail-closed solely because RF backends are absent.
    from services.border_geometry import (
        BorderGeometryUnavailable,
        check_cbsd_in_border_sharing_zone,
    )

    try:
        in_zone, border_lat, border_lon = check_cbsd_in_border_sharing_zone(
            lat, lon, ant_azi, ant_bw
        )
    except BorderGeometryUnavailable as exc:
        raise BorderProtectionUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — geometry / dataset backends
        raise BorderProtectionUnavailable(
            f"border sharing zone check failed: {exc}"
        ) from exc

    if not in_zone or border_lat is None or border_lon is None:
        return BorderPfdOutcome.ALLOW

    height = float(installation.get("height") or 0)
    height_type = installation.get("heightType") or "AGL"
    indoor = bool(installation.get("indoorDeployment"))

    resolved_rf = None
    if rf_port is not None:
        resolved_rf = rf_port
    elif path_loss_model is None:
        resolved_rf = _resolve_bpr_rf_port(None)

    model = path_loss_model
    if resolved_rf is None and model is None:
        raise BorderProtectionUnavailable(
            "BPR requires rf_port from RuntimeComposition "
            "(or an explicit path_loss_model= test override); "
            "SAS_BPR_PATH_LOSS_MODEL is not used after composition"
        )

    try:
        if model is not None:
            # Explicit lab/test override — ignore bound composition RF.
            if model == "free_space":
                pl = _free_space_path_loss_db(
                    lat,
                    lon,
                    height,
                    float(border_lat),
                    float(border_lon),
                    BORDER_RX_HEIGHT_M,
                )
                bearing = 0.0
                try:
                    from reference_models.antenna import antenna

                    ant_gain = antenna.GetStandardAntennaGains(
                        bearing, ant_azi, ant_bw, max_ant_gain
                    )
                except Exception:  # noqa: BLE001
                    ant_gain = max_ant_gain
            else:
                # Lab/test override path_loss_model="itm" — still via external
                # adapter, never reference_models.propagation.wf_itm.
                pl, bearing = _itm_path_loss_for_bpr(
                    lat,
                    lon,
                    height,
                    float(border_lat),
                    float(border_lon),
                    BORDER_RX_HEIGHT_M,
                    indoor=indoor,
                    height_is_amsl=(height_type == "AMSL"),
                )
                try:
                    from reference_models.antenna import antenna

                    ant_gain = antenna.GetStandardAntennaGains(
                        bearing, ant_azi, ant_bw, max_ant_gain
                    )
                except Exception:  # noqa: BLE001
                    ant_gain = max_ant_gain
        else:
            from services.iap.rf_adapter import path_loss_db_between_points
            from services.propagation.errors import PropagationUnavailableError

            try:
                pl = path_loss_db_between_points(
                    resolved_rf,  # type: ignore[arg-type]
                    tx_lat=lat,
                    tx_lon=lon,
                    tx_height_m=height,
                    rx_lat=float(border_lat),
                    rx_lon=float(border_lon),
                    rx_height_m=BORDER_RX_HEIGHT_M,
                    frequency_hz=int(ITM_FREQ_MHZ * 1_000_000),
                    indoor=indoor,
                    tx_height_is_agl=(height_type != "AMSL"),
                )
            except PropagationUnavailableError:
                return BorderPfdOutcome.DENY
            bearing = 0.0
            try:
                from reference_models.antenna import antenna

                ant_gain = antenna.GetStandardAntennaGains(
                    bearing, ant_azi, ant_bw, max_ant_gain
                )
            except Exception:  # noqa: BLE001
                ant_gain = max_ant_gain
    except ImportError:
        return BorderPfdOutcome.UNAVAILABLE
    except BorderProtectionUnavailable:
        raise
    except Exception:
        # Missing terrain / ITM failure while inside sharing zone: deny.
        return BorderPfdOutcome.DENY

    # PFD = requested_eirp - maxAntGain + effectiveGain - PL + 32.6
    pfd = max_eirp - max_ant_gain + ant_gain - pl + 32.6
    return BorderPfdOutcome.DENY if pfd > PFD_LIMIT_DBM_M2_MHZ else BorderPfdOutcome.ALLOW


def violates_canadian_border_pfd(
    installation: dict[str, Any],
    max_eirp: float,
    low_hz: int,
    high_hz: int,
    *,
    rf_port: object | None = None,
    path_loss_model: BprPathLossModel | None = None,
) -> bool:
    """Return True when the grant must be rejected (responseCode 400).

    Fail-closed: ``UNAVAILABLE`` and ``DENY`` both reject. Does not authorize
    when border KMZ is missing or when a sharing-zone CBSD cannot be evaluated.
    """
    try:
        outcome = evaluate_canadian_border_pfd(
            installation,
            max_eirp,
            low_hz,
            high_hz,
            rf_port=rf_port,
            path_loss_model=path_loss_model,
        )
    except BorderProtectionUnavailable:
        return True
    return outcome in (BorderPfdOutcome.DENY, BorderPfdOutcome.UNAVAILABLE)
