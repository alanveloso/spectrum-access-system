"""Propagation / antenna Admin query (WInnForum PAT) — / .

Mirrors harness ``computePropagationAntennaModel`` / ``computePropagationDpa``
without fixture hardcodes. Numerical engines are injectable so unit tests do not
require the compiled ITM extension; production loads WInnForum reference models
from the sibling harness checkout when available.

Type-3 (DPA) uses Rel1Ext composition: ITM + P.2108 clutter + activity/loading
(see ``services.propagation.rel1ext_dpa``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping, Protocol

from services.propagation.errors import (
    PropagationRequestError,
    PropagationUnavailableError,
)
from services.propagation.rel1ext_dpa import (
    ACTIVITY_LOSS_FACTOR_DB,
    calc_p2108_clutter_db,
    compose_dpa_pathloss_db,
)

FREQ_MHZ_DEFAULT = 3625.0
PPA_RX_HEIGHT_M = 1.5
PPA_GRID_ARCSEC = 1
ACTIVITY_LOSS_FACTOR_DEFAULT = ACTIVITY_LOSS_FACTOR_DB


class _Incidence(Protocol):
    hor_cbsd: float
    hor_rx: float
    ver_rx: float


class _PathLoss(Protocol):
    db_loss: float
    incidence_angles: _Incidence


@dataclass(frozen=True)
class PropagationEngines:
    """Injectable numerical backends (ITM / hybrid / P.2108 / antenna / geo)."""

    calc_itm: Callable[..., _PathLoss]
    calc_hybrid: Callable[..., _PathLoss]
    calc_p2108: Callable[..., float]
    activity_loss_factor: float
    antenna_standard_gains: Callable[..., float]
    antenna_fss_gains: Callable[..., float]
    grid_polygon: Callable[..., list[tuple[float, float]]]
    region_nlcd_vote: Callable[..., str]
    terrain_elevation_m: Callable[[float, float], float]
    # ESC azimuth pattern (WINNF GetAntennaPatternGains). Optional for older mocks.
    antenna_pattern_gains: Callable[..., float] | None = None


def _require_mapping(value: Any, *, name: str) -> MutableMapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PropagationRequestError(f"{name} must be an object")
    return dict(value)


def _require_float(container: Mapping[str, Any], key: str) -> float:
    if key not in container:
        raise PropagationRequestError(f"missing {key}")
    try:
        return float(container[key])
    except (TypeError, ValueError) as exc:
        raise PropagationRequestError(f"{key} must be a number") from exc


def _require_bool(container: Mapping[str, Any], key: str) -> bool:
    if key not in container:
        raise PropagationRequestError(f"missing {key}")
    value = container[key]
    if not isinstance(value, bool):
        raise PropagationRequestError(f"{key} must be a boolean")
    return value


def _cbsd_fields(cbsd: Mapping[str, Any]) -> dict[str, Any]:
    height_type = cbsd.get("heightType") or "AGL"
    if height_type not in {"AGL", "AMSL"}:
        raise PropagationRequestError("cbsd.heightType must be AGL or AMSL")
    return {
        "latitude": _require_float(cbsd, "latitude"),
        "longitude": _require_float(cbsd, "longitude"),
        "height": _require_float(cbsd, "height"),
        "heightType": height_type,
        "indoorDeployment": _require_bool(cbsd, "indoorDeployment"),
        "antennaAzimuth": cbsd.get("antennaAzimuth"),
        "antennaBeamwidth": cbsd.get("antennaBeamwidth"),
        "antennaGain": cbsd.get("antennaGain"),
    }


def _engine_call(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run an engine callable; map ValueError → 400, other failures → 503."""
    try:
        return fn(*args, **kwargs)
    except PropagationRequestError:
        raise
    except ValueError as exc:
        raise PropagationRequestError(f"{label}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — numerical / IO backends
        raise PropagationUnavailableError(f"{label} failed: {exc}") from exc


def compute_propagation_and_antenna_model(
    request: Mapping[str, Any],
    *,
    engines: PropagationEngines,
) -> dict[str, Any]:
    """Compute PAT Admin response fields for modelType 1/2/3.

 Raises:
 PropagationRequestError: invalid request (HTTP 400).
 PropagationUnavailableError: engines cannot complete (HTTP 503).
 """
    if not isinstance(request, Mapping):
        raise PropagationRequestError("request must be a JSON object")

    if "dpaPoint" in request:
        return _compute_dpa(request, engines=engines)

    try:
        reliability_level = float(request["reliabilityLevel"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PropagationRequestError("reliabilityLevel missing or invalid") from exc
    if reliability_level not in {-1.0, 0.05, 0.95}:
        raise PropagationRequestError(
            "reliabilityLevel not in [-1, 0.05, 0.95]"
        )

    if "cbsd" not in request:
        raise PropagationRequestError("missing cbsd")
    tx = _cbsd_fields(_require_mapping(request["cbsd"], name="cbsd"))

    if ("fss" in request) and ("ppa" in request):
        raise PropagationRequestError("fss and ppa in request")
    if "ppa" in request:
        return _compute_ppa(request, tx=tx, engines=engines)
    if "fss" in request:
        return _compute_fss(
            request, tx=tx, reliability_level=reliability_level, engines=engines
        )
    raise PropagationRequestError("Neither fss nor ppa in request")


def _compute_dpa(
    request: Mapping[str, Any], *, engines: PropagationEngines
) -> dict[str, Any]:
    model_type = str(request.get("modelType", ""))
    if model_type != "3":
        raise PropagationRequestError("modelType is not 3")
    try:
        reliability_level = float(request["reliabilityLevel"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PropagationRequestError("reliabilityLevel missing or invalid") from exc
    if reliability_level != 0.5:
        raise PropagationRequestError("reliabilityLevel is not 0.5")

    tx = _cbsd_fields(_require_mapping(request["cbsd"], name="cbsd"))
    rx = dict(_require_mapping(request["dpaPoint"], name="dpaPoint"))
    try:
        rx_lat = float(rx["latitude"])
        rx_lon = float(rx["longitude"])
        rx_height = float(rx["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PropagationRequestError("dpaPoint coordinates/height invalid") from exc
    height_type = rx.get("heightType") or "AGL"
    if height_type not in {"AGL", "AMSL"}:
        raise PropagationRequestError("dpaPoint.heightType must be AGL or AMSL")
    if height_type == "AMSL":
        altitude_rx = _engine_call(
            "terrain elevation", engines.terrain_elevation_m, rx_lat, rx_lon
        )
        rx_height = rx_height - float(altitude_rx)

    path_loss = _engine_call(
        "ITM path-loss",
        engines.calc_itm,
        tx["latitude"],
        tx["longitude"],
        tx["height"],
        rx_lat,
        rx_lon,
        rx_height,
        cbsd_indoor=tx["indoorDeployment"],
        reliability=reliability_level,
        freq_mhz=FREQ_MHZ_DEFAULT,
        is_height_cbsd_amsl=(tx["heightType"] == "AMSL"),
    )
    # Rel1Ext Type-3 clutter is owned by the UUT (single formula). Harness
    # ``engines.calc_p2108`` remains available for other callers / diagnostics
    # but must not diverge from this path for Admin PAT.2.
    clutter_loss = _engine_call(
        "P.2108 clutter",
        calc_p2108_clutter_db,
        tx["latitude"],
        tx["longitude"],
        tx["height"],
        rx_lat,
        rx_lon,
        is_height_cbsd_amsl=(tx["heightType"] == "AMSL"),
        terrain_elevation_m=engines.terrain_elevation_m,
    )

    # Rel1Ext Type-3: no antenna e/f/g — path loss only (PAT.2).
    return {
        "pathlossDb": compose_dpa_pathloss_db(
            float(path_loss.db_loss),
            float(clutter_loss),
            activity_loss_db=float(engines.activity_loss_factor),
        )
    }


def _compute_fss(
    request: Mapping[str, Any],
    *,
    tx: Mapping[str, Any],
    reliability_level: float,
    engines: PropagationEngines,
) -> dict[str, Any]:
    rx = _require_mapping(request["fss"], name="fss")
    try:
        rx_lat = float(rx["latitude"])
        rx_lon = float(rx["longitude"])
        rx_height = float(rx["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PropagationRequestError("fss coordinates/height invalid") from exc

    path_loss = _engine_call(
        "ITM path-loss",
        engines.calc_itm,
        tx["latitude"],
        tx["longitude"],
        tx["height"],
        rx_lat,
        rx_lon,
        rx_height,
        cbsd_indoor=tx["indoorDeployment"],
        reliability=reliability_level,
        freq_mhz=FREQ_MHZ_DEFAULT,
        is_height_cbsd_amsl=(tx["heightType"] == "AMSL"),
    )
    gain_tx = _engine_call(
        "TX antenna gain",
        engines.antenna_standard_gains,
        path_loss.incidence_angles.hor_cbsd,
        ant_azimuth=tx.get("antennaAzimuth"),
        ant_beamwidth=tx.get("antennaBeamwidth"),
        ant_gain=tx.get("antennaGain"),
    )

    result: dict[str, Any] = {
        "pathlossDb": float(path_loss.db_loss),
        "txAntennaGainDbi": float(gain_tx),
    }
    # Harness: key presence (not truthiness) gates RX gain.
    if "rxAntennaGainRequired" in rx:
        for key in ("antennaAzimuth", "antennaElevation", "antennaGain"):
            if key not in rx:
                raise PropagationRequestError(f"fss missing {key} for RX gain")
        gain_rx = _engine_call(
            "FSS RX antenna gain",
            engines.antenna_fss_gains,
            path_loss.incidence_angles.hor_rx,
            path_loss.incidence_angles.ver_rx,
            rx["antennaAzimuth"],
            rx["antennaElevation"],
            rx["antennaGain"],
        )
        result["rxAntennaGainDbi"] = float(gain_rx)
    return result


def _compute_ppa(
    request: Mapping[str, Any],
    *,
    tx: Mapping[str, Any],
    engines: PropagationEngines,
) -> dict[str, Any]:
    ppa = _require_mapping(request["ppa"], name="ppa")
    if "geometry" not in ppa:
        raise PropagationRequestError("ppa.geometry missing")
    ppa_points = _engine_call(
        "ppa grid", engines.grid_polygon, ppa["geometry"], PPA_GRID_ARCSEC
    )
    if len(ppa_points) == 0:
        raise PropagationRequestError("ppa boundary contains no protection point")
    if len(ppa_points) != 1:
        raise PropagationRequestError(
            "ppa boundary contains more than a single protection point"
        )
    rx_lon, rx_lat = float(ppa_points[0][0]), float(ppa_points[0][1])
    region_val = _engine_call(
        "NLCD region vote", engines.region_nlcd_vote, [[rx_lat, rx_lon]]
    )
    path_loss = _engine_call(
        "hybrid path-loss",
        engines.calc_hybrid,
        tx["latitude"],
        tx["longitude"],
        tx["height"],
        rx_lat,
        rx_lon,
        PPA_RX_HEIGHT_M,
        cbsd_indoor=tx["indoorDeployment"],
        reliability=-1,
        freq_mhz=FREQ_MHZ_DEFAULT,
        region=region_val,
        is_height_cbsd_amsl=(tx["heightType"] == "AMSL"),
    )
    gain_tx = _engine_call(
        "TX antenna gain",
        engines.antenna_standard_gains,
        path_loss.incidence_angles.hor_cbsd,
        ant_azimuth=tx.get("antennaAzimuth"),
        ant_beamwidth=tx.get("antennaBeamwidth"),
        ant_gain=tx.get("antennaGain"),
    )

    return {
        "pathlossDb": float(path_loss.db_loss),
        "txAntennaGainDbi": float(gain_tx),
    }
