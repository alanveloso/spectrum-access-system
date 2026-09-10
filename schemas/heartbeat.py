"""Pydantic schemas for CBSD Heartbeat (batch format)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.common import MeasReport, OperationState, ResponseObject, parse_rfc3339
from services.error_handlers import MAXIMUM_BATCH_SIZE


class HeartbeatRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cbsdId: str | None = None
    grantId: str | None = None
    # Strict enum — unknown strings are INVALID_VALUE (103), not silently accepted.
    operationState: OperationState | None = None
    grantRenew: bool | None = None
    measReport: MeasReport | None = None
    transmitExpireTime: str | None = None

    @field_validator("transmitExpireTime")
    @classmethod
    def _transmit_expire_rfc3339(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_rfc3339(value)
        return value


class HeartbeatBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heartbeatRequest: list[HeartbeatRequestItem] = Field(
        ..., max_length=MAXIMUM_BATCH_SIZE
    )


class HeartbeatResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    grantId: str | None = None
    transmitExpireTime: str
    grantExpireTime: str | None = None
    heartbeatInterval: int | None = None
    measReportConfig: list[str] | None = None
    response: ResponseObject


class HeartbeatBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heartbeatResponse: list[HeartbeatResponseItem]
