"""Immutable ProfileContext from a validated Spectrum Profile document ."""

from __future__ import annotations

import hashlib
import json

from primitives.profile_context import ProfileContext
from primitives.registry import MechanismRegistry, discovered_mechanism_registry
from profiles.errors import ProfileValidationError
from profiles.selection import (
    active_profile_id,
    set_profile_override,
)
from profiles.schema import (
    ProfileDocument,
    SpectrumRange,
)


def canonical_profile_json(parsed: ProfileDocument) -> str:
    payload = parsed.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def profile_hash(parsed: ProfileDocument) -> str:
    digest = hashlib.sha256(canonical_profile_json(parsed).encode("utf-8"))
    return digest.hexdigest()


def selected_mechanism_ids(parsed: ProfileDocument) -> tuple[str, ...]:
    """Ordered unique mechanism ids referenced by a validated Spectrum Profile document."""
    ids: list[str] = []
    if parsed.spectrum.channelization is not None:
        ids.append(parsed.spectrum.channelization.mechanism)
    if parsed.access is not None:
        ids.append(parsed.access.mechanism)
    if parsed.authorization is not None:
        ids.append(parsed.authorization.mechanism)
    if parsed.power is not None:
        ids.append(parsed.power.mechanism)
    if parsed.geography is not None:
        ids.append(parsed.geography.mechanism)
    if parsed.temporal is not None and parsed.temporal.reevaluation is not None:
        ids.append(parsed.temporal.reevaluation.mechanism)
    if parsed.temporal is not None and parsed.temporal.availability is not None:
        ids.append(parsed.temporal.availability.mechanism)
    if parsed.protection is not None:
        ids.extend(parsed.protection.mechanisms)
    if parsed.coordination is not None:
        ids.append(parsed.coordination.mechanism)
    if parsed.rf is not None:
        ids.append(parsed.rf.policy)
        if parsed.rf.propagation_model is not None:
            ids.append(parsed.rf.propagation_model)
    for constraint in parsed.constraints:
        ids.append(constraint.mechanism)
    seen: set[str] = set()
    ordered: list[str] = []
    for mechanism_id in ids:
        if mechanism_id not in seen:
            seen.add(mechanism_id)
            ordered.append(mechanism_id)
    return tuple(ordered)


def profile_context_from_document(
    parsed: ProfileDocument,
    *,
    registry: MechanismRegistry | None = None,
) -> ProfileContext:
    catalog = registry or discovered_mechanism_registry()
    mechanism_ids = selected_mechanism_ids(parsed)
    mechanism_versions = tuple(
        (mechanism_id, catalog.get(mechanism_id).version) for mechanism_id in mechanism_ids
    )
    dataset_versions: tuple[tuple[str, str], ...] = ()
    if parsed.data is not None:
        dataset_versions = tuple(
            (cap, "required") for cap in parsed.data.required_capabilities
        )
    rf_provenance = None
    if parsed.rf is not None:
        if parsed.rf.propagation_model:
            rf_provenance = f"{parsed.rf.policy}/{parsed.rf.propagation_model}"
        else:
            rf_provenance = parsed.rf.policy
    return ProfileContext(
        profile_id=parsed.metadata.id,
        profile_version=parsed.metadata.version,
        profile_hash=profile_hash(parsed),
        dataset_versions=dataset_versions,
        mechanism_versions=mechanism_versions,
        rf_provenance=rf_provenance,
    )


def primary_spectrum_range(document: ProfileDocument) -> SpectrumRange:
    """Resolve the single primary continuous range for logging / band consumers.

    Fail closed when multiple disconnected ranges lack a unique ``primary`` id.
    """
    ranges = document.spectrum.ranges
    if len(ranges) == 1:
        return ranges[0]
    primary = [item for item in ranges if item.id == "primary"]
    if len(primary) == 1:
        return primary[0]
    raise ProfileValidationError(
        f"profile '{document.metadata.id}' has {len(ranges)} spectrum ranges "
        "without a unique id='primary'; cannot select a single band"
    )


def get_active_profile_document() -> ProfileDocument:
    # Lazy import: parse/trust import hash helpers from this module.
    from profiles.parse import load_profile

    return load_profile(active_profile_id())


def set_active_profile_document(profile_id: str) -> ProfileDocument:
    set_profile_override(profile_id)
    return get_active_profile_document()


def reload_active_profile_document() -> ProfileDocument:
    """Reload the active canonical document (no separate canonical loader cache)."""
    return get_active_profile_document()
