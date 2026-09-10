"""Typed IAP / aggregate-interference domain models."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


IapAction = Literal["keep", "reduce_power", "suspend", "terminate"]
# Note: IAP engine emits keep/reduce_power/terminate only. ``suspend`` is retained
# for CPAS/lifecycle merge compatibility; R2-SGN-16 IAP does not produce suspend.


class ProtectedEntityKind(str, Enum):
    ESC = "esc"
    FSS_COCHANNEL = "fss_cochannel"
    FSS_BLOCKING = "fss_blocking"
    PPA = "ppa"
    GWPZ = "gwpz"
    GENERIC = "generic"


class FrequencyChannel(BaseModel):
    """One IAP allocation channel (typically 5 MHz)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low_hz: int = Field(..., ge=0)
    high_hz: int = Field(..., ge=0)

    @property
    def bandwidth_hz(self) -> int:
        return max(0, self.high_hz - self.low_hz)


class ProtectionPoint(BaseModel):
    """Protected entity constraint evaluated by IAP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str = Field(..., min_length=1)
    latitude: float
    longitude: float
    low_hz: int = Field(..., ge=0)
    high_hz: int = Field(..., ge=0)
    threshold_dbm: float
    entity_kind: ProtectedEntityKind = ProtectedEntityKind.GENERIC
    pre_iap_margin_db: float = Field(default=1.0, ge=0.0)
 # WINNF-TS-0112 neighborhood (km). None = no distance filter (GENERIC/tests).
    neighborhood_km: float | None = Field(default=None, ge=0.0)
 # Underlying FSS/PPA/… record id for provenance mapping (not a fixture ID).
    source_entity_id: str | None = None
 # ESC receiver antenna (WINNF computeInterferenceEsc). Optional for non-ESC.
    receiver_height_m: float | None = None
    receiver_antenna_azimuth_deg: float | None = None
    receiver_antenna_gain_pattern_dbi: tuple[float, ...] | None = None


class GrantRfInfo(BaseModel):
    """Local or peer grant inputs for aggregate / IAP (no DB coupling)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    cbsd_id: str
    latitude: float
    longitude: float
    height_m: float = 0.0
    height_is_agl: bool = True
    indoor: bool = False
    low_hz: int
    high_hz: int
    max_eirp_dbm_mhz: float
    is_managing_sas: bool = True
    grant_pk: int | None = None
 # Provenance: None/local for managing SAS; peer SAS PK (str) for FAD imports.
    source_sas_id: str | None = None
 # Frozen CBSD category ("A"/"B"). Used for ESC neighborhood (40/80 km).
    cbsd_category: str | None = None
 # Optional CBSD antenna fields for entity-specific coupling (ESC).
    antenna_azimuth_deg: float | None = None
    antenna_beamwidth_deg: float | None = None
    antenna_gain_dbi: float | None = None


class GrantChannelContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    channel: FrequencyChannel
    interference_mw: float = Field(..., ge=0.0)
    eirp_dbm_mhz: float


class ChannelAggregateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: FrequencyChannel
    aggregate_mw: float
    managing_sas_mw: float
    threshold_mw: float
    fairshare_mw: float
    within_threshold: bool


class IapGrantDecision(BaseModel):
    """Per-grant outcome after IAP at one protection point (or merged)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    cbsd_id: str
    grant_pk: int | None = None
    action: IapAction
    authorized_eirp_dbm_mhz: float
    initial_eirp_dbm_mhz: float
    explanation: str


class IapPointResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    point: ProtectionPoint
    channels: tuple[FrequencyChannel, ...]
    aggregates: tuple[ChannelAggregateResult, ...]
    decisions: tuple[IapGrantDecision, ...]


class IapRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    points: tuple[IapPointResult, ...]
    merged_decisions: tuple[IapGrantDecision, ...]
