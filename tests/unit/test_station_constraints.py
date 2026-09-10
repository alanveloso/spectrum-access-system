"""reusable station/spectrum limit primitives + BR profile wiring."""

from __future__ import annotations

import pytest

from primitives.registry import MechanismAxis, builtin_mechanism_registry
from primitives.station_limits import (
    AntennaHeightLimit,
    DuplexMode,
    DuplexModeRequirement,
    ForbiddenDeviceRoles,
    MaxAssignmentBandwidth,
)
from profiles.doctor import run_profile_doctor
from profiles.parse import load_profile, parse_profile_document
from profiles.errors import ProfileValidationError


def test_new_mechanisms_registered_on_expected_axes() -> None:
    reg = builtin_mechanism_registry()
    assert reg.on_axis(MechanismAxis.SPECTRUM, "duplex_mode").version == "1.0.0"
    assert reg.on_axis(MechanismAxis.SPECTRUM, "max_assignment_bandwidth")
    assert reg.on_axis(MechanismAxis.POWER, "antenna_height_limit")
    assert reg.on_axis(MechanismAxis.ACCESS, "forbidden_device_roles")


def test_duplex_and_bandwidth_checks() -> None:
    req = DuplexModeRequirement(mode=DuplexMode.TDD)
    assert req.allows("tdd")
    assert not req.allows("fdd")
    cap = MaxAssignmentBandwidth(max_bandwidth_hz=50_000_000, indoor_outdoor="outdoor")
    assert cap.allows(3_700_000_000, 3_750_000_000, indoor_outdoor="outdoor")
    assert not cap.allows(3_700_000_000, 3_760_000_000, indoor_outdoor="outdoor")
    assert cap.allows(3_700_000_000, 3_800_000_000, indoor_outdoor="indoor")


def test_antenna_height_and_forbidden_roles() -> None:
    lim = AntennaHeightLimit(
        max_height_m=6.0, indoor_outdoor="outdoor", device_class="base_nodal"
    )
    assert lim.allows(6.0, indoor_outdoor="outdoor", device_class="base_nodal")
    assert not lim.allows(6.1, indoor_outdoor="outdoor", device_class="base_nodal")
    assert lim.allows(20.0, indoor_outdoor="indoor", device_class="base_nodal")
    deny = ForbiddenDeviceRoles(roles=frozenset({"repeater", "booster"}))
    assert deny.allows("base_nodal")
    assert not deny.allows("repeater")


def test_br_profile_constraints_and_doctor() -> None:
    parsed = load_profile("br_anatel_slp_3700")
    mechs = [c.mechanism for c in parsed.constraints]
    assert mechs == [
        "duplex_mode",
        "max_assignment_bandwidth",
        "antenna_height_limit",
        "forbidden_device_roles",
    ]
    duplex = parsed.constraints[0].to_primitive()
    assert isinstance(duplex, DuplexModeRequirement)
    assert duplex.mode == DuplexMode.TDD
    bw = parsed.constraints[1].to_primitive()
    assert isinstance(bw, MaxAssignmentBandwidth)
    assert bw.max_bandwidth_hz == 50_000_000
    height = parsed.constraints[2].to_primitive()
    assert isinstance(height, AntennaHeightLimit)
    assert height.max_height_m == 6.0
    roles = parsed.constraints[3].to_primitive()
    assert isinstance(roles, ForbiddenDeviceRoles)
    assert "repeater" in roles.roles
    report = run_profile_doctor(profile_id="br_anatel_slp_3700")
    assert report.ok, "; ".join(f.name for f in report.findings if not f.ok)


def test_unknown_constraint_mechanism_fails_closed() -> None:
    doc = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "x", "version": "1", "status": "custom"},
        "spectrum": {"ranges": [{"id": "r", "low_hz": 1, "high_hz": 2}]},
        "constraints": [{"mechanism": "duplex_mode", "mode": "not_a_mode"}],
    }
    with pytest.raises(ProfileValidationError):
        parse_profile_document(doc)


def test_cbrs_reference_still_loads_without_constraints() -> None:
    parsed = load_profile("cbrs_winnforum")
    assert parsed.constraints == ()
    assert parsed.metadata.id == "cbrs_winnforum"
