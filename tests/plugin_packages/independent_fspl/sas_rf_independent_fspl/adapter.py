"""Independent Free-Space RfPort plugin (not the WInnForum wrapper)."""

from __future__ import annotations

import math

from primitives.geography import haversine_m
from rf.port import (
    RF_API_VERSION,
    RF_MODEL_PATH_LOSS,
    PathLossRequest,
    PathLossResult,
    RfUnavailableError,
)

PROVENANCE = "independent-fspl:v1"


class IndependentFsplRfAdapter:
    """RfPort using 2D great-circle FSPL (identity distinct from SAS free_space)."""

    api_version = RF_API_VERSION
    model_id = RF_MODEL_PATH_LOSS

    @property
    def provenance(self) -> str:
        return PROVENANCE

    def path_loss(self, request: PathLossRequest) -> PathLossResult:
        if request.frequency_hz <= 0:
            raise RfUnavailableError("frequency_hz must be positive")
        # Deliberately ignore height slant: algorithmic identity ≠ SAS free_space.
        dist_m = haversine_m(request.tx, request.rx)
        dist_km = max(dist_m / 1000.0, 1e-6)
        freq_mhz = request.frequency_hz / 1_000_000.0
        loss_db = float(20.0 * math.log10(dist_km) + 20.0 * math.log10(freq_mhz) + 32.44)
        return PathLossResult(
            loss_db=loss_db,
            model_id=self.model_id,
            provenance=self.provenance,
        )


def independent_fspl_rf_adapter() -> IndependentFsplRfAdapter:
    """Zero-arg entry-point factory."""
    return IndependentFsplRfAdapter()
