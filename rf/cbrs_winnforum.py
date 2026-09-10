"""CBRS / WInnForum RF adapter for Rel1Ext regulatory composition only.

Generic Free Space / ITM ``RfPort`` backends live in ``rf.external_propagation``.
This module must not depend on ``services.iap.coupling`` (inverted dependency).
"""

from __future__ import annotations

from collections.abc import Callable

from primitives.geography import GeoPoint
from rf.external_propagation import (
    SpectrumPropagationRfAdapter,
    free_space_rf_adapter,
    itm_rf_adapter,
    path_loss_db_via_spectrum_propagation,
)
from rf.port import (
    RF_API_VERSION,
    RF_MODEL_PATH_LOSS,
    PathLossRequest,
    PathLossResult,
    RfUnavailableError,
)
from services.propagation.errors import PropagationUnavailableError
from services.propagation.rel1ext_dpa import calc_p2108_clutter_db, compose_dpa_pathloss_db

_BACKENDS = frozenset({"free_space", "itm", "rel1ext"})
ItmFn = Callable[[float, float, float, float, float, float, float, float], float]
# (tx_lat, tx_lon, tx_h, rx_lat, rx_lon, rx_h, freq_mhz, indoor_as_float) — tests inject simpler forms


class CbrsWinnForumRfAdapter:
    """Rel1Ext composition (ITM median + P.2108 + activity) behind RfPort."""

    api_version = RF_API_VERSION
    model_id = RF_MODEL_PATH_LOSS

    def __init__(
        self,
        *,
        backend: str,
        itm_fn: Callable[..., float] | None = None,
        terrain_elevation_m: Callable[[float, float], float] | None = None,
    ) -> None:
        if backend not in _BACKENDS:
            raise ValueError(f"unsupported RF backend: {backend}")
        self._backend = backend
        self._itm_fn = itm_fn
        self._terrain_elevation_m = terrain_elevation_m
        self._generic: SpectrumPropagationRfAdapter | None = None
        if backend in ("free_space", "itm"):
            self._generic = SpectrumPropagationRfAdapter(backend=backend)  # type: ignore[arg-type]

    @property
    def provenance(self) -> str:
        if self._generic is not None:
            return self._generic.provenance
        return f"cbrs-winnforum:{self._backend}"

    def path_loss(self, request: PathLossRequest) -> PathLossResult:
        if self._generic is not None:
            return self._generic.path_loss(request)
        try:
            loss = self._compute_rel1ext(request)
        except PropagationUnavailableError as exc:
            raise RfUnavailableError(str(exc)) from exc
        except ValueError as exc:
            raise RfUnavailableError(str(exc)) from exc
        return PathLossResult(
            loss_db=loss,
            model_id=self.model_id,
            provenance=self.provenance,
        )

    def _compute_rel1ext(self, request: PathLossRequest) -> float:
        freq_mhz = request.frequency_hz / 1_000_000.0
        itm = self._itm_median(request, freq_mhz)
        clutter = calc_p2108_clutter_db(
            request.tx.latitude_deg,
            request.tx.longitude_deg,
            request.tx_height_m,
            request.rx.latitude_deg,
            request.rx.longitude_deg,
            is_height_cbsd_amsl=not request.tx_height_is_agl,
            terrain_elevation_m=self._terrain_elevation_m,
        )
        return float(compose_dpa_pathloss_db(itm, clutter))

    def _itm_median(self, request: PathLossRequest, freq_mhz: float) -> float:
        if self._itm_fn is not None:
            # Test injectables historically used (grant, point, rx_h, freq) or similar.
            try:
                from services.iap.models import GrantRfInfo, ProtectedEntityKind, ProtectionPoint

                grant = GrantRfInfo(
                    grant_id="rf-port",
                    cbsd_id="rf-port",
                    latitude=request.tx.latitude_deg,
                    longitude=request.tx.longitude_deg,
                    height_m=request.tx_height_m,
                    height_is_agl=request.tx_height_is_agl,
                    indoor=request.indoor,
                    low_hz=request.frequency_hz,
                    high_hz=request.frequency_hz + 1,
                    max_eirp_dbm_mhz=0.0,
                )
                point = ProtectionPoint(
                    point_id="rx",
                    latitude=request.rx.latitude_deg,
                    longitude=request.rx.longitude_deg,
                    low_hz=request.frequency_hz,
                    high_hz=request.frequency_hz + 1,
                    threshold_dbm=0.0,
                    entity_kind=ProtectedEntityKind.GENERIC,
                )
                return float(self._itm_fn(grant, point, request.rx_height_m, freq_mhz))
            except TypeError:
                return float(self._itm_fn(request, freq_mhz))
        try:
            return path_loss_db_via_spectrum_propagation(
                tx=GeoPoint(
                    latitude_deg=request.tx.latitude_deg,
                    longitude_deg=request.tx.longitude_deg,
                ),
                rx=GeoPoint(
                    latitude_deg=request.rx.latitude_deg,
                    longitude_deg=request.rx.longitude_deg,
                ),
                tx_height_m=float(request.tx_height_m),
                rx_height_m=float(request.rx_height_m),
                frequency_hz=int(request.frequency_hz),
                indoor=bool(request.indoor),
                tx_height_is_agl=bool(request.tx_height_is_agl),
                backend="itm",
            )
        except RfUnavailableError as exc:
            raise PropagationUnavailableError(str(exc)) from exc


__all__ = [
    "CbrsWinnForumRfAdapter",
    "free_space_rf_adapter",
    "itm_rf_adapter",
]
