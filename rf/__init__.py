"""Generic RF contract and discovery.

Concrete backends (CBRS reference composition, external propagation) live in
their own modules and are reached through discovery / DeploymentConfig, never by
importing them from this package root.
"""

from rf.discovery import RfModelDiscovery
from rf.port import (
    RF_API_VERSION,
    RF_MODEL_PATH_LOSS,
    PathLossRequest,
    PathLossResult,
    RfPort,
    RfUnavailableError,
)

__all__ = [
    "RF_API_VERSION",
    "RF_MODEL_PATH_LOSS",
    "PathLossRequest",
    "PathLossResult",
    "RfModelDiscovery",
    "RfPort",
    "RfUnavailableError",
]
