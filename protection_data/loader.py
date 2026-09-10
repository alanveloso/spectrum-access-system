"""Load and validate versioned protection-data manifests against a data root."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

import yaml
from pydantic import ValidationError

from protection_data.schema import (
    DatasetBundle,
    DatasetSlot,
    DatasetSlotStatus,
    DatasetValidationReport,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFESTS_DIR = _PACKAGE_DIR / "manifests"
_REPO_ROOT = _PACKAGE_DIR.parent
DEFAULT_DATA_ROOT = _REPO_ROOT / "data"

_cache: dict[str, DatasetBundle] = {}
_cache_lock = RLock()
_manifests_dir_override: Path | None = None
_data_root_override: Path | None = None


class DatasetError(Exception):
    """Base error for protection-data packaging failures."""


class DatasetNotFoundError(DatasetError):
    """Raised when no manifest exists for the requested bundle id."""


class DatasetValidationError(DatasetError):
    """Raised when a manifest fails schema validation."""


class DatasetPathError(DatasetError):
    """Raised when a path escapes an allowlisted directory."""


def get_manifests_dir() -> Path:
    return (_manifests_dir_override or DEFAULT_MANIFESTS_DIR).resolve()


def set_manifests_dir(path: Path | str | None) -> Path:
    global _manifests_dir_override
    if path is None:
        _manifests_dir_override = None
    else:
        _manifests_dir_override = Path(path).resolve()
    clear_dataset_bundle_cache()
    return get_manifests_dir()


def get_data_root() -> Path:
    return (_data_root_override or DEFAULT_DATA_ROOT).resolve()


def set_data_root(path: Path | str | None) -> Path:
    global _data_root_override
    if path is None:
        _data_root_override = None
    else:
        _data_root_override = Path(path).resolve()
    return get_data_root()


def clear_dataset_bundle_cache(bundle_id: str | None = None) -> None:
    with _cache_lock:
        if bundle_id is None:
            _cache.clear()
        else:
            _cache.pop(bundle_id, None)


def _assert_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DatasetPathError(
            f"path '{resolved}' is outside allowlisted directory '{root}'"
        ) from exc
    return resolved


def _candidate_paths(bundle_id: str, manifests_dir: Path) -> list[Path]:
    if not bundle_id or "/" in bundle_id or "\\" in bundle_id or ".." in bundle_id:
        raise DatasetPathError(f"invalid bundle id '{bundle_id}'")
    return [
        manifests_dir / f"{bundle_id}.yaml",
        manifests_dir / f"{bundle_id}.yml",
        manifests_dir / f"{bundle_id}.json",
    ]


def _load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise DatasetValidationError(f"manifest root must be a mapping: {path}")
    return data


def load_dataset_bundle(bundle_id: str) -> DatasetBundle:
    """Load and cache a dataset bundle manifest by id."""
    with _cache_lock:
        cached = _cache.get(bundle_id)
        if cached is not None:
            return cached

    manifests_dir = get_manifests_dir()
    path: Path | None = None
    for candidate in _candidate_paths(bundle_id, manifests_dir):
        if candidate.is_file():
            path = _assert_within(candidate, manifests_dir)
            break
    if path is None:
        raise DatasetNotFoundError(
            f"protection-data bundle '{bundle_id}' not found under {manifests_dir}"
        )
    try:
        bundle = DatasetBundle.model_validate(_load_raw(path))
    except ValidationError as exc:
        raise DatasetValidationError(str(exc)) from exc
    if bundle.id != bundle_id:
        raise DatasetValidationError(
            f"manifest id {bundle.id!r} does not match requested {bundle_id!r}"
        )
    with _cache_lock:
        _cache[bundle_id] = bundle
    return bundle


def _check_slot(slot: DatasetSlot, data_root: Path) -> DatasetSlotStatus:
    slot_dir = data_root / slot.relative_path
    try:
        _assert_within(slot_dir, data_root)
    except DatasetPathError as exc:
        return DatasetSlotStatus(
            slot_id=slot.id,
            kind=slot.kind,
            version=slot.version,
            required=slot.required,
            ok=False,
            detail=str(exc),
        )

    if slot.presence == "dir_exists":
        ok = slot_dir.is_dir()
        return DatasetSlotStatus(
            slot_id=slot.id,
            kind=slot.kind,
            version=slot.version,
            required=slot.required,
            ok=ok,
            detail=(
                f"dir={slot_dir}" if ok else f"missing directory: {slot_dir}"
            ),
        )

    if slot.presence == "version_marker":
        version_file = slot_dir / "VERSION"
        if not version_file.is_file():
            return DatasetSlotStatus(
                slot_id=slot.id,
                kind=slot.kind,
                version=slot.version,
                required=slot.required,
                ok=False,
                detail=f"missing VERSION marker: {version_file}",
            )
        declared = version_file.read_text(encoding="utf-8").strip().splitlines()
        declared_ver = declared[0].strip() if declared else ""
        if declared_ver != slot.version:
            return DatasetSlotStatus(
                slot_id=slot.id,
                kind=slot.kind,
                version=slot.version,
                required=slot.required,
                ok=False,
                detail=(
                    f"VERSION mismatch at {version_file}: "
                    f"got {declared_ver!r} expected {slot.version!r}"
                ),
            )
        return DatasetSlotStatus(
            slot_id=slot.id,
            kind=slot.kind,
            version=slot.version,
            required=slot.required,
            ok=True,
            detail=f"VERSION={declared_ver} path={slot_dir}",
        )

    # files_glob
    if not slot_dir.is_dir():
        return DatasetSlotStatus(
            slot_id=slot.id,
            kind=slot.kind,
            version=slot.version,
            required=slot.required,
            ok=False,
            soft_payload_gap=slot.payload_optional_unless_strict,
            detail=f"missing directory for payload: {slot_dir}",
        )
    matches = sorted(slot_dir.glob(slot.file_glob or "*"))
    files: list[Path] = []
    for candidate in matches:
        if not candidate.is_file():
            continue
        try:
            _assert_within(candidate, slot_dir)
        except DatasetPathError:
            continue
        files.append(candidate)
    if len(files) < slot.min_files:
        return DatasetSlotStatus(
            slot_id=slot.id,
            kind=slot.kind,
            version=slot.version,
            required=slot.required,
            ok=False,
            soft_payload_gap=slot.payload_optional_unless_strict,
            detail=(
                f"payload files {len(files)}<{slot.min_files} "
                f"glob={slot.file_glob!r} under {slot_dir}"
            ),
        )
    return DatasetSlotStatus(
        slot_id=slot.id,
        kind=slot.kind,
        version=slot.version,
        required=slot.required,
        ok=True,
        detail=f"{len(files)} file(s) under {slot_dir}",
    )


def validate_dataset_bundle(
    bundle: DatasetBundle | str,
    *,
    data_root: Path | str | None = None,
    strict: bool = False,
) -> DatasetValidationReport:
    """Validate all slots of a bundle against ``data_root``."""
    resolved_bundle = (
        bundle if isinstance(bundle, DatasetBundle) else load_dataset_bundle(bundle)
    )
    root = Path(data_root).resolve() if data_root is not None else get_data_root()
    statuses = [_check_slot(slot, root) for slot in resolved_bundle.slots]
    return DatasetValidationReport(
        bundle_id=resolved_bundle.id,
        bundle_version=resolved_bundle.version,
        data_root=str(root),
        strict=strict,
        slots=statuses,
    )


def assert_protection_data_ready(
    bundle_id: str,
    *,
    data_root: Path | str | None = None,
    strict: bool = False,
) -> DatasetValidationReport:
    """Load + validate; raise ``DatasetValidationError`` when required slots fail.

    ``strict`` defaults to False to match ``Settings.sas_protection_data_strict``:
    VERSION markers are always required; soft payload gaps fail only when strict.
    """
    report = validate_dataset_bundle(bundle_id, data_root=data_root, strict=strict)
    if not report.ok:
        missing = ", ".join(
            f"{s.slot_id} ({s.detail})" for s in report.missing_required()
        )
        raise DatasetValidationError(
            f"protection-data bundle '{bundle_id}' incomplete: {missing}"
        )
    return report
