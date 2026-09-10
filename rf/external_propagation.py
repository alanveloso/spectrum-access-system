"""SAS RfPort adapter backed by the external ``spectrum_propagation`` package.

Maps SAS ``PathLossRequest`` ↔ generic ``PropagationRequest``. Domain services
must not import ``spectrum_propagation`` directly.

Specialized regulatory paths that need ITM median *and* incidence geometry
(e.g. BPR antenna bearing) use ``itm_path_loss_with_geometry`` — still adapter
scoped, never ``wf_itm`` / ``CalcItmPropagationLoss`` in services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from primitives.geography import GeoPoint
from rf.port import (
    RF_API_VERSION,
    RF_MODEL_PATH_LOSS,
    PathLossRequest,
    PathLossResult,
    RfUnavailableError,
)
from spectrum_propagation import PropagationRequest
from spectrum_propagation.errors import (
    InvalidPropagationRequest,
    ModelUnavailableError,
    PropagationError,
    TerrainDataError,
)
from spectrum_propagation.models.free_space import free_space_path_loss

BackendName = Literal["free_space", "itm"]


def _to_propagation_request(
    request: PathLossRequest,
    *,
    reliability: float | None = None,
) -> PropagationRequest:
    return PropagationRequest(
        tx_lat_deg=float(request.tx.latitude_deg),
        tx_lon_deg=float(request.tx.longitude_deg),
        rx_lat_deg=float(request.rx.latitude_deg),
        rx_lon_deg=float(request.rx.longitude_deg),
        tx_height_m=float(request.tx_height_m),
        rx_height_m=float(request.rx_height_m),
        frequency_hz=int(request.frequency_hz),
        indoor=bool(request.indoor),
        tx_height_is_agl=bool(request.tx_height_is_agl),
        reliability=reliability,
    )


def _translate_error(exc: Exception) -> RfUnavailableError:
    return RfUnavailableError(str(exc))


@dataclass(frozen=True, slots=True)
class ItmGeometryResult:
    """ITM median path loss plus optional incidence geometry metadata.

    Field names are technical (no CBRS/ESC/DPA vocabulary).
    """

    path_loss_db: float
    provenance: str
    hor_tx_deg: float | None = None
    hor_rx_deg: float | None = None
    ver_rx_deg: float | None = None


class SpectrumPropagationRfAdapter:
    """RfPort implementation selecting a ``spectrum_propagation`` model."""

    api_version = RF_API_VERSION
    model_id = RF_MODEL_PATH_LOSS

    def __init__(self, *, backend: BackendName = "free_space") -> None:
        if backend not in ("free_space", "itm"):
            raise ValueError(f"unsupported RF backend: {backend}")
        self._backend: BackendName = backend

    @property
    def provenance(self) -> str:
        return f"spectrum-propagation:{self._backend}"

    def path_loss(self, request: PathLossRequest) -> PathLossResult:
        prop_req = _to_propagation_request(request)
        try:
            if self._backend == "free_space":
                result = free_space_path_loss(prop_req)
            else:
                from spectrum_propagation.integrations.winnforum import itm_path_loss

                result = itm_path_loss(prop_req)
        except (PropagationError, InvalidPropagationRequest, ModelUnavailableError, TerrainDataError) as exc:
            raise _translate_error(exc) from exc
        except ValueError as exc:
            raise _translate_error(exc) from exc
        return PathLossResult(
            loss_db=float(result.path_loss_db),
            model_id=self.model_id,
            provenance=self.provenance,
        )


def free_space_rf_adapter() -> SpectrumPropagationRfAdapter:
    """Entry-point factory: Free Space via external ``spectrum_propagation``."""
    return SpectrumPropagationRfAdapter(backend="free_space")


def itm_rf_adapter() -> SpectrumPropagationRfAdapter:
    """Entry-point factory: ITM via external WInnForum bridge."""
    return SpectrumPropagationRfAdapter(backend="itm")


def path_loss_db_via_spectrum_propagation(
    *,
    tx: GeoPoint,
    rx: GeoPoint,
    tx_height_m: float,
    rx_height_m: float,
    frequency_hz: int,
    indoor: bool = False,
    tx_height_is_agl: bool = True,
    backend: BackendName = "free_space",
) -> float:
    """Helper for thin SAS wrappers (IAP coupling) — still adapter-scoped."""
    adapter = SpectrumPropagationRfAdapter(backend=backend)
    result = adapter.path_loss(
        PathLossRequest(
            tx=tx,
            rx=rx,
            tx_height_m=tx_height_m,
            rx_height_m=rx_height_m,
            frequency_hz=frequency_hz,
            indoor=indoor,
            tx_height_is_agl=tx_height_is_agl,
        )
    )
    return float(result.loss_db)


def itm_path_loss_with_geometry(
    *,
    tx: GeoPoint,
    rx: GeoPoint,
    tx_height_m: float,
    rx_height_m: float,
    frequency_hz: int,
    indoor: bool = False,
    tx_height_is_agl: bool = True,
    reliability: float = 0.5,
) -> ItmGeometryResult:
    """ITM median + incidence geometry via external package (adapter boundary).

    Used by specialized regulatory orchestration (e.g. BPR antenna bearing).
    Does not import ``wf_itm`` in the caller.
    """
    from spectrum_propagation.integrations.winnforum import itm_path_loss

    prop_req = _to_propagation_request(
        PathLossRequest(
            tx=tx,
            rx=rx,
            tx_height_m=tx_height_m,
            rx_height_m=rx_height_m,
            frequency_hz=frequency_hz,
            indoor=indoor,
            tx_height_is_agl=tx_height_is_agl,
        ),
        reliability=reliability,
    )
    try:
        result = itm_path_loss(prop_req, reliability=reliability)
    except (PropagationError, InvalidPropagationRequest, ModelUnavailableError, TerrainDataError) as exc:
        raise _translate_error(exc) from exc
    except ValueError as exc:
        raise _translate_error(exc) from exc

    meta = result.metadata or {}
    def _opt_float(key: str) -> float | None:
        raw = meta.get(key)
        if raw is None:
            return None
        return float(raw)

    return ItmGeometryResult(
        path_loss_db=float(result.path_loss_db),
        provenance=str(result.provenance),
        hor_tx_deg=_opt_float("hor_cbsd"),
        hor_rx_deg=_opt_float("hor_rx"),
        ver_rx_deg=_opt_float("ver_rx"),
    )
