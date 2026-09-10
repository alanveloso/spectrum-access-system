"""Pydantic schemas for admin inject/test-control endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InjectFccIdRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    fccId: str
    fccMaxEirp: float = 47.0


class InjectUserIdRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    userId: str


class ConditionalRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    registrationData: list[dict[str, Any]] = Field(default_factory=list)


class InjectCpiUserRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    cpiId: str
    cpiName: str = ""
    cpiPublicKey: str = ""


class BlacklistFccIdRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    fccId: str = Field(min_length=1)


class BlacklistFccIdSerialRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    fccId: str = Field(min_length=1)
    cbsdSerialNumber: str = Field(min_length=1)
