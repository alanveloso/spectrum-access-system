"""Shared protection-data bundle resolution for reference dataset providers.

Resolves manifest slot paths under a configured data root. Regulatory consumers
must not import this module — they consume composed ``DataProvider`` instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from protection_data.loader import get_data_root, load_dataset_bundle
from protection_data.schema import DatasetBundle, DatasetSlot


@dataclass(frozen=True, slots=True)
class BundleContext:
    """Immutable view of one protection-data bundle at a data root."""

    bundle_id: str
    data_root: Path
    bundle: DatasetBundle

    @classmethod
    def load(
        cls,
        bundle_id: str,
        *,
        data_root: Path | str | None = None,
    ) -> BundleContext:
        root = Path(data_root).resolve() if data_root is not None else get_data_root()
        bundle = load_dataset_bundle(bundle_id)
        return cls(bundle_id=bundle_id, data_root=root, bundle=bundle)

    def slot(self, slot_id: str) -> DatasetSlot:
        for item in self.bundle.slots:
            if item.id == slot_id:
                return item
        raise ValueError(f"bundle {self.bundle_id!r} has no slot {slot_id!r}")

    def slot_dir(self, slot_id: str) -> Path:
        slot = self.slot(slot_id)
        return (self.data_root / slot.relative_path).resolve()

    def slot_files(self, slot_id: str, *, pattern: str | None = None) -> tuple[Path, ...]:
        slot = self.slot(slot_id)
        directory = self.slot_dir(slot_id)
        if not directory.is_dir():
            return ()
        glob = pattern or slot.file_glob or "*"
        return tuple(sorted(directory.glob(glob)))

    def slot_file(self, slot_id: str, *, pattern: str | None = None) -> Path | None:
        files = self.slot_files(slot_id, pattern=pattern)
        return files[0] if files else None

    def provenance_base(self, *, provider_id: str) -> tuple[str, str, str]:
        return (self.bundle_id, self.bundle.version, provider_id)
