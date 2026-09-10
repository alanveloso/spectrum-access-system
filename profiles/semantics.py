"""Closed semantic checks for Spectrum Profile. No expression language."""

from __future__ import annotations

from primitives.frequency import FrequencyRange
from primitives.registry import MechanismAxis, MechanismRegistry
from profiles.schema import ProfileDocument

_RF_PROTECTION = frozenset({"single_link_threshold", "aggregate_linear_power"})


def validate_profile_semantics(
    parsed: ProfileDocument, catalog: MechanismRegistry
) -> None:
    data_caps = (
        set(parsed.data.required_capabilities) if parsed.data is not None else set()
    )
    device_caps = (
        set(parsed.requirements.device_capabilities)
        if parsed.requirements is not None
        else set()
    )
    protection_ids = (
        parsed.protection.mechanisms if parsed.protection is not None else ()
    )

    rf = parsed.rf
    if rf is not None:
        policy = catalog.get(rf.policy)
        if policy.slot != "rf_policy":
            raise ValueError("rf.policy must use an rf_policy registry slot")
        if rf.propagation_model is not None:
            model = catalog.get(rf.propagation_model)
            if model.slot != "rf_model":
                raise ValueError("rf.propagation_model must use an rf_model registry slot")
        if rf.required:
            if "terrain" not in data_caps:
                raise ValueError("rf.required requires data capability 'terrain'")
            if "geolocation" not in device_caps:
                raise ValueError("rf.required requires device capability 'geolocation'")

    if any(item in _RF_PROTECTION for item in protection_ids):
        if rf is None or not rf.required:
            raise ValueError(
                "single_link_threshold/aggregate_linear_power require rf.required"
            )

    if parsed.protection is not None:
        parents = tuple(
            FrequencyRange(low_hz=item.low_hz, high_hz=item.high_hz)
            for item in parsed.spectrum.ranges
        )
        for binding in parsed.protection.bindings:
            catalog.on_axis(MechanismAxis.PROTECTION, binding.mechanism)
            if binding.frequency is None:
                continue
            scope = FrequencyRange(
                low_hz=binding.frequency.low_hz,
                high_hz=binding.frequency.high_hz,
            )
            if not any(parent.contains(scope) for parent in parents):
                raise ValueError(
                    f"protection binding {binding.id!r} frequency scope "
                    f"[{scope.low_hz}, {scope.high_hz}) is not fully contained "
                    "in any declared spectrum range"
                )
