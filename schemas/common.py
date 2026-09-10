"""Shared CBSD-SAS schema primitives (WINNF-TS-0016 aligned, profile-agnostic)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# CBRS Part 96 band edges in Hz (generic constants — not fixture coordinates).
CBRS_LOW_HZ = 3_550_000_000
CBRS_HIGH_HZ = 3_700_000_000

CbsdCategory = Literal["A", "B"]
ChannelType = Literal["PAL", "GAA"]
OperationState = Literal["GRANTED", "AUTHORIZED"]
HeightType = Literal["AGL", "AMSL"]
MeasCapability = Literal[
    "RECEIVED_POWER_WITHOUT_GRANT",
    "RECEIVED_POWER_WITH_GRANT",
]
RadioTechnology = Literal["E_UTRA", "NR"]


class ResponseObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    responseCode: int
    responseMessage: str | None = None
    responseData: Any | None = None


class FrequencyRange(BaseModel):
    """Frequency interval in Hz; low must be strictly below high."""

    model_config = ConfigDict(extra="forbid")

    lowFrequency: int = Field(..., ge=1)
    highFrequency: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _ordered_and_plausible(self) -> FrequencyRange:
        if self.lowFrequency >= self.highFrequency:
            raise ValueError("lowFrequency must be < highFrequency")
        # Reject absurd magnitudes while remaining band-plan agnostic.
        if self.highFrequency > 100_000_000_000:
            raise ValueError("highFrequency out of plausible RF range")
        return self


class InstallationParam(BaseModel):
    """Common installationParam fields.

    ``extra=allow``: WINNF permits additional optional antenna/location fields;
    known fields are range-checked, unknowns are preserved for domain services.
    """

    model_config = ConfigDict(extra="allow")

    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    height: float | None = Field(default=None, ge=0)
    heightType: HeightType | None = None
    indoorDeployment: bool | None = None
    antennaAzimuth: float | None = Field(default=None, ge=0, le=360)
    antennaDowntilt: float | None = Field(default=None, ge=-90, le=90)
    antennaGain: float | None = None
    antennaBeamwidth: float | None = Field(default=None, ge=0, le=360)
    antennaModel: str | None = None
    eirpCapability: float | None = None
    horizontalAccuracy: float | None = Field(default=None, ge=0)
    verticalAccuracy: float | None = Field(default=None, ge=0)


class AirInterface(BaseModel):
    """radioTechnology is constrained; other airInterface keys are preserved."""

    model_config = ConfigDict(extra="allow")

    radioTechnology: RadioTechnology | None = None


class CpiSignatureData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protectedHeader: str
    encodedCpiSignedData: str
    digitalSignature: str


class GroupingParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groupId: str | None = None
    groupType: str | None = None


def parse_rfc3339(value: str) -> datetime:
    """Parse WINNF-style timestamps (RFC3339 / ISO-8601 with Z)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class MeasReport(BaseModel):
    """Minimal measReport envelope; nested RCs validated loosely as numbers."""

    model_config = ConfigDict(extra="allow")

    rcvdPowerMeasReports: list[dict[str, Any]] | None = None


def winnf_code_from_validation_errors(errors: list[Any]) -> int:
    """Map Pydantic error types to WINNF 102/103."""
    from services.error_handlers import INVALID_VALUE, MISSING_PARAM

    missing_types = {
        "missing",
        "missing_argument",
        "missing_positional_only_argument",
        "missing_keyword_only_argument",
    }
    for err in errors:
        etype = err.get("type") if isinstance(err, dict) else getattr(err, "type", "")
        if etype in missing_types:
            return MISSING_PARAM
    return INVALID_VALUE
