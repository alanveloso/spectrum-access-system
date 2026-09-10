"""Pydantic schemas for CBSD Registration (batch format)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import (
    AirInterface,
    CbsdCategory,
    CpiSignatureData,
    GroupingParam,
    InstallationParam,
    MeasCapability,
    ResponseObject,
)
from services.error_handlers import MAXIMUM_BATCH_SIZE


class RegistrationRequestItem(BaseModel):
    """Strict registration item — unknown top-level keys forbidden."""

    model_config = ConfigDict(extra="forbid")

    userId: str | None = None
    fccId: str | None = None
    cbsdSerialNumber: str | None = None
    cbsdCategory: CbsdCategory | None = None
    callSign: str | None = None
    measCapability: list[MeasCapability] | None = None
    airInterface: AirInterface | None = None
    installationParam: InstallationParam | None = None
    cpiSignatureData: CpiSignatureData | None = None
    groupingParam: list[GroupingParam] | None = None
    cbsdInfo: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _category_b_conditionals(self) -> RegistrationRequestItem:
        if self.cbsdCategory == "B":
            if self.installationParam is None and self.cpiSignatureData is None:
                raise ValueError(
                    "cbsdCategory B requires installationParam or cpiSignatureData"
                )
        return self


class RegistrationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registrationRequest: list[RegistrationRequestItem] = Field(
        ..., max_length=MAXIMUM_BATCH_SIZE
    )


class RegistrationResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    cbsdId: str | None = None
    response: ResponseObject
    measReportConfig: list[str] | None = None


class RegistrationBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registrationResponse: list[RegistrationResponseItem]
