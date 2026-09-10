"""IAP/BPR-side adaptation from composed RfPort to GrantRfInfo path-loss shapes.

Owned by the IAP consumer: specialized models depend on the generic RfPort
contract, not the reverse.
"""

from __future__ import annotations

from typing import Callable

from primitives.geography import GeoPoint
from rf.port import PathLossRequest, RfPort, RfUnavailableError
from services.iap.models import FrequencyChannel, GrantRfInfo, ProtectionPoint
from services.propagation.errors import PropagationUnavailableError

PathLossDbFn = Callable[[GrantRfInfo, ProtectionPoint, FrequencyChannel], float]


def path_loss_db_fn_from_rf_port(
    rf_port: RfPort,
    *,
    default_rx_height_m: float = 1.5,
) -> PathLossDbFn:
    """Adapt RfPort.path_loss to the IAP coupling path-loss signature."""

    def _fn(
        grant: GrantRfInfo,
        point: ProtectionPoint,
        channel: FrequencyChannel,
    ) -> float:
        del channel  # mid-band frequency is taken from grant edges
        freq_hz = max(int((grant.low_hz + grant.high_hz) // 2), 1)
        rx_height = (
            float(point.receiver_height_m)
            if point.receiver_height_m is not None
            else float(default_rx_height_m)
        )
        request = PathLossRequest(
            tx=GeoPoint(latitude_deg=grant.latitude, longitude_deg=grant.longitude),
            rx=GeoPoint(latitude_deg=point.latitude, longitude_deg=point.longitude),
            tx_height_m=float(grant.height_m),
            rx_height_m=rx_height,
            frequency_hz=freq_hz,
            indoor=bool(grant.indoor),
            tx_height_is_agl=bool(grant.height_is_agl),
        )
        try:
            return float(rf_port.path_loss(request).loss_db)
        except RfUnavailableError as exc:
            raise PropagationUnavailableError(str(exc)) from exc

    return _fn


def path_loss_db_between_points(
    rf_port: RfPort,
    *,
    tx_lat: float,
    tx_lon: float,
    tx_height_m: float,
    rx_lat: float,
    rx_lon: float,
    rx_height_m: float,
    frequency_hz: int,
    indoor: bool = False,
    tx_height_is_agl: bool = True,
) -> float:
    """Scalar path loss for BPR-style geometry (implementation via RfPort)."""
    request = PathLossRequest(
        tx=GeoPoint(latitude_deg=tx_lat, longitude_deg=tx_lon),
        rx=GeoPoint(latitude_deg=rx_lat, longitude_deg=rx_lon),
        tx_height_m=float(tx_height_m),
        rx_height_m=float(rx_height_m),
        frequency_hz=max(int(frequency_hz), 1),
        indoor=indoor,
        tx_height_is_agl=tx_height_is_agl,
    )
    try:
        return float(rf_port.path_loss(request).loss_db)
    except RfUnavailableError as exc:
        raise PropagationUnavailableError(str(exc)) from exc
