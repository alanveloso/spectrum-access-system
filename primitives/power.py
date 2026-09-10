"""Power quantities in base units (dBm and milliwatts). Density is dBm/MHz as data."""

from __future__ import annotations

import math
from dataclasses import dataclass


def dbm_to_mw(dbm: float) -> float:
    """Linear milliwatts from dBm (10 ** (dbm / 10))."""
    return float(10.0 ** (dbm / 10.0))


def mw_to_dbm(mw: float) -> float:
    """dBm from milliwatts. Non-positive milliwatts map to −inf."""
    if mw <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(mw)


@dataclass(frozen=True, slots=True)
class PowerDbm:
    """Absolute or density power expressed in dBm (or dBm/MHz as a numeric value)."""

    dbm: float

    def to_mw(self) -> PowerMw:
        return PowerMw(mw=dbm_to_mw(self.dbm))


@dataclass(frozen=True, slots=True)
class PowerMw:
    mw: float

    def to_dbm(self) -> PowerDbm:
        return PowerDbm(dbm=mw_to_dbm(self.mw))
