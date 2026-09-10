"""Versioned protection / RF dataset packaging.

Declares ITM, terrain/NED, NLCD, antenna, DPA, FSS/GWBL, zones and census
slots with explicit versions. Binary payloads stay outside git; presence is
validated against the active manifest.
"""

from __future__ import annotations

from protection_data.loader import (
    DatasetError,
    DatasetNotFoundError,
    DatasetPathError,
    DatasetValidationError,
    assert_protection_data_ready,
    clear_dataset_bundle_cache,
    get_data_root,
    load_dataset_bundle,
    set_data_root,
    set_manifests_dir,
    validate_dataset_bundle,
)
from protection_data.schema import (
    DatasetBundle,
    DatasetPresence,
    DatasetSlot,
    DatasetValidationReport,
)

__all__ = [
    "DatasetBundle",
    "DatasetError",
    "DatasetNotFoundError",
    "DatasetPathError",
    "DatasetPresence",
    "DatasetSlot",
    "DatasetValidationError",
    "DatasetValidationReport",
    "assert_protection_data_ready",
    "clear_dataset_bundle_cache",
    "get_data_root",
    "load_dataset_bundle",
    "set_data_root",
    "set_manifests_dir",
    "validate_dataset_bundle",
]
