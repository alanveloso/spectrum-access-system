"""Pydantic schemas for CBSD Spectrum Inquiry (batch format)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import (
    ChannelType,
    FrequencyRange,
    MeasReport,
    ResponseObject,
)
from services.error_handlers import MAXIMUM_BATCH_SIZE


class SpectrumInquiryRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cbsdId: str | None = None
    inquiredSpectrum: list[FrequencyRange] | None = None
    measReport: MeasReport | None = None

    @model_validator(mode="after")
    def _require_inquired_spectrum(self) -> SpectrumInquiryRequestItem:
        if self.inquiredSpectrum is not None and len(self.inquiredSpectrum) == 0:
            raise ValueError("inquiredSpectrum must not be an empty list")
        return self


class SpectrumInquiryBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spectrumInquiryRequest: list[SpectrumInquiryRequestItem] = Field(
        ..., max_length=MAXIMUM_BATCH_SIZE
    )


class AvailableChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequencyRange: FrequencyRange
    channelType: ChannelType
    ruleApplied: str = "FCC_PART_96"


class SpectrumInquiryResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    availableChannel: list[AvailableChannel] | None = None
    response: ResponseObject


class SpectrumInquiryBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spectrumInquiryResponse: list[SpectrumInquiryResponseItem]
