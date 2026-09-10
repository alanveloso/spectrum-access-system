"""Pydantic schemas for CBSD Deregistration (batch format)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from schemas.common import ResponseObject
from services.error_handlers import MAXIMUM_BATCH_SIZE


class DeregistrationRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cbsdId: str | None = None


class DeregistrationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deregistrationRequest: list[DeregistrationRequestItem] = Field(
        ..., max_length=MAXIMUM_BATCH_SIZE
    )


class DeregistrationResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    response: ResponseObject


class DeregistrationBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deregistrationResponse: list[DeregistrationResponseItem]
