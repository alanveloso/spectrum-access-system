"""Pydantic schemas for CBSD Grant (batch format)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.common import ChannelType, FrequencyRange, MeasReport, ResponseObject
from services.error_handlers import MAXIMUM_BATCH_SIZE


class OperationParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maxEirp: float | None = Field(default=None, ge=-137, le=37)
    operationFrequencyRange: FrequencyRange | None = None


class GrantRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cbsdId: str | None = None
    operationParam: OperationParam | None = None
    measuringCapabilities: list[str] | None = None
    measReport: MeasReport | None = None

    @field_validator("measuringCapabilities")
    @classmethod
    def _non_empty_meas(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) == 0:
            raise ValueError("measuringCapabilities must not be empty when present")
        return value


class GrantBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantRequest: list[GrantRequestItem] = Field(..., max_length=MAXIMUM_BATCH_SIZE)


class GrantResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    grantId: str | None = None
    grantExpireTime: str | None = None
    transmitExpireTime: str | None = None
    heartbeatInterval: int | None = None
    channelType: ChannelType | None = None
    response: ResponseObject


class GrantBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantResponse: list[GrantResponseItem]
