"""in-process mechanism registry and contracts."""

from __future__ import annotations

import pytest

from primitives.registry import (
    MechanismAxis,
    MechanismContract,
    MechanismRegistry,
    builtin_mechanism_registry,
    select_optional_access,
)


def test_builtin_catalog_covers_g2_mechanisms_without_flat_access():
    registry = builtin_mechanism_registry()
    ids = registry.ids()
    assert "ordered_classes" in ids
    assert "dynamic_lease" in ids
    assert "fixed_window" in ids
    assert "static_authorization" in ids
    assert "authorized_area" in ids
    assert "exclusion_zone" in ids
    assert "preemption" in ids
    assert "protection_entitlement" in ids
    assert "snapshot_evaluate_apply" in ids
    assert "flat_access" not in ids
    access = registry.on_axis(MechanismAxis.ACCESS, "ordered_classes")
    assert access.version == "1.0.0"


def test_unknown_duplicate_and_wrong_axis_fail_closed():
    registry = builtin_mechanism_registry()
    with pytest.raises(ValueError):
        registry.get("not_a_mechanism")
    with pytest.raises(ValueError):
        registry.on_axis(MechanismAxis.ACCESS, "dynamic_lease")
    with pytest.raises(ValueError):
        registry.register(
            MechanismContract("ordered_classes", MechanismAxis.ACCESS, "1.0.0")
        )
    extra = MechanismRegistry()
    extra.register(MechanismContract("custom_one", MechanismAxis.SPECTRUM, "1.0.0"))
    assert extra.require(("custom_one",))[0].mechanism_id == "custom_one"


def test_access_mechanism_may_be_omitted():
    registry = builtin_mechanism_registry()
    assert select_optional_access(registry, None) is None
    bound = select_optional_access(registry, "ordered_classes")
    assert bound is not None
    assert bound.mechanism_id == "ordered_classes"
    with pytest.raises(ValueError):
        select_optional_access(registry, "exclusion_zone")
