"""Typed schema for versioned protection / RF dataset manifests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DatasetPresence = Literal[
    "version_marker",  # requires VERSION file under relative_path
    "files_glob",  # requires ≥ min_files matching file_glob
    "dir_exists",  # requires directory only
]


class DatasetSlot(BaseModel):
    """One versioned dataset or model package slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    kind: str = Field(
        ...,
        min_length=1,
        description=(
            "Logical kind: itm, terrain_ned, nlcd, antenna, dpa, fss, gwbl, "
            "zones, census, …"
        ),
    )
    version: str = Field(..., min_length=1)
    relative_path: str = Field(
        ...,
        min_length=1,
        description="Path relative to the dataset data root (no abs / ..).",
    )
    required: bool = True
    presence: DatasetPresence = "version_marker"
    file_glob: str | None = Field(
        default=None,
        description="Glob relative to relative_path when presence=files_glob.",
    )
    min_files: int = Field(default=1, ge=0)
 # When True, absence of matching payload files fails only under strict mode.
    payload_optional_unless_strict: bool = False
    description: str = ""

    @field_validator("relative_path")
    @classmethod
    def _no_escape(cls, value: str) -> str:
        cleaned = value.strip().replace("\\", "/").lstrip("/")
        if not cleaned or cleaned.startswith("..") or "/../" in f"/{cleaned}/":
            raise ValueError(f"invalid relative_path {value!r}")
        return cleaned

    @field_validator("file_glob")
    @classmethod
    def _glob_no_escape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().replace("\\", "/")
        if not cleaned:
            raise ValueError("file_glob must be non-empty when set")
        if cleaned.startswith("/") or cleaned.startswith("..") or "/../" in f"/{cleaned}/":
            raise ValueError(f"invalid file_glob {value!r} (path escape)")
        return cleaned

    @model_validator(mode="after")
    def _glob_rules(self) -> DatasetSlot:
        if self.presence == "files_glob":
            if not self.file_glob:
                raise ValueError(f"slot {self.id}: file_glob required for files_glob")
            if self.min_files < 1:
                raise ValueError(
                    f"slot {self.id}: min_files must be >= 1 for files_glob"
                )
        return self


class DatasetBundle(BaseModel):
    """Versioned bundle listing all protection-data slots for a profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    description: str = ""
    rule_applied: str = Field(default="winnforum_protection_data_v1")
    slots: list[DatasetSlot] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_ids_and_kinds(self) -> DatasetBundle:
        ids = [s.id for s in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset slot ids must be unique")
        kinds = {s.kind for s in self.slots}
        required_kinds = {
            "itm",
            "terrain_ned",
            "nlcd",
            "antenna",
            "dpa",
            "fss",
            "gwbl",
            "zones",
            "census",
        }
        missing = sorted(required_kinds - kinds)
        if missing:
            raise ValueError(f"bundle missing required dataset kinds: {missing}")
        return self


class DatasetSlotStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    kind: str
    version: str
    required: bool
    ok: bool
    soft_payload_gap: bool = False
    detail: str


class DatasetValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str
    bundle_version: str
    data_root: str
    strict: bool
    slots: list[DatasetSlotStatus]

    @property
    def ok(self) -> bool:
        for slot in self.slots:
            if slot.ok:
                continue
            if not slot.required:
                continue
            if slot.soft_payload_gap and not self.strict:
                continue
            return False
        return True

    def missing_required(self) -> list[DatasetSlotStatus]:
        return [
            s
            for s in self.slots
            if s.required
            and not s.ok
            and (self.strict or not s.soft_payload_gap)
        ]
