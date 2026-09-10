"""Generic RF port. No regime nouns; no numerical engines here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from primitives.geography import GeoPoint

RF_API_VERSION = "1.0.0"
RF_MODEL_PATH_LOSS = "path_loss"


class RfUnavailableError(Exception):
    """Required RF backend or supporting data is not available (fail closed)."""


@dataclass(frozen=True, slots=True)
class PathLossRequest:
    tx: GeoPoint
    rx: GeoPoint
    tx_height_m: float
    rx_height_m: float
    frequency_hz: int
    indoor: bool = False
    tx_height_is_agl: bool = True

    def __post_init__(self) -> None:
        if self.frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")


@dataclass(frozen=True, slots=True)
class PathLossResult:
    loss_db: float
    model_id: str
    provenance: str


@runtime_checkable
class RfPort(Protocol):
    """Path-loss plugin. Required RF + missing backend must raise RfUnavailableError."""

    @property
    def api_version(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    def path_loss(self, request: PathLossRequest) -> PathLossResult: ...
