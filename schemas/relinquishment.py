"""Pydantic schemas for CBSD Relinquishment (batch format)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from schemas.common import ResponseObject
from services.error_handlers import MAXIMUM_BATCH_SIZE


class RelinquishmentRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cbsdId: str | None = None
    grantId: str | None = None


class RelinquishmentBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relinquishmentRequest: list[RelinquishmentRequestItem] = Field(
        ..., max_length=MAXIMUM_BATCH_SIZE
    )


class RelinquishmentResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    grantId: str | None = None
    response: ResponseObject


class RelinquishmentBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relinquishmentResponse: list[RelinquishmentResponseItem]
