"""ANATEL SLP third-regime vertical slice through generic composition.

Proves one outdoor base/nodal station request executes via the existing
br_anatel_slp_3700 Profile + DeploymentConfig + RuntimeComposition +
MappingDeviceAdapter + Profile-derived constraint primitives without
profile-id dispatch in the generic core.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.device import AdapterKind, ConsumerAdapter, MappingDeviceAdapter
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing
from primitives.request import SpectrumRequest
from primitives.station_limits import (
    AntennaHeightLimit,
    DuplexModeRequirement,
    ForbiddenDeviceRoles,
    MaxAssignmentBandwidth,
)
from primitives.time import UtcInstant
from profiles import load_profile
from profiles.doctor import run_profile_doctor
from profiles.trust import builtin_profiles_dir
from runtime import PluginRegistry, compose_runtime
from runtime.capabilities import required_capabilities
from runtime.deployment import DeploymentConfig, PluginSelection
from runtime.errors import MissingCapabilityError

_PLUGIN_IMPLEMENTATION_TOKENS = (
    "plugin:",
    "entry_point",
    "protocol_adapter",
    "CbsdDeviceAdapter",
    "adapters.cbsd",
    "MappingDeviceAdapter",
)


def _anatel_deployment() -> DeploymentConfig:
    return DeploymentConfig(
        id="anatel_vertical_slice",
        version="1.0.0",
        device=PluginSelection(plugin="mapping"),
        providers=(
            PluginSelection(plugin="bundle_protected_entities"),
            PluginSelection(plugin="bundle_boundaries"),
        ),
    )


def _constraints(profile):
    return {item.mechanism: item.to_primitive() for item in profile.constraints}


def _outdoor_base_rule(profile):
    return next(
        rule
        for rule in profile.power.rules
        if rule.indoor_outdoor == "outdoor" and rule.device_class == "base_nodal"
    )


def _authorized_ring(profile) -> LinearRing:
    return LinearRing.from_lon_lat(profile.geography.authorized_areas[0].ring)


def _interior_point(ring: LinearRing) -> GeoPoint:
    verts = ring.coordinates[:-1] if ring.coordinates[0] == ring.coordinates[-1] else ring.coordinates
    lon = sum(vertex[0] for vertex in verts) / len(verts)
    lat = sum(vertex[1] for vertex in verts) / len(verts)
    return GeoPoint(latitude_deg=lat, longitude_deg=lon)


def test_anatel_vertical_slice_through_generic_composition() -> None:
    profile_path = Path(builtin_profiles_dir()) / "br_anatel_slp_3700.yaml"
    profile_yaml = profile_path.read_text(encoding="utf-8")
    for token in _PLUGIN_IMPLEMENTATION_TOKENS:
        assert token not in profile_yaml

    profile = load_profile("br_anatel_slp_3700")
    assert profile.metadata.id == "br_anatel_slp_3700"
    doctor = run_profile_doctor(profile_id="br_anatel_slp_3700")
    assert doctor.ok
    assert profile.access is None
    assert profile.authorization is not None
    assert profile.authorization.mechanism == "static_authorization"
    assert profile.authorization.duration_s is None
    assert profile.temporal is None or profile.temporal.reevaluation is None

    req = required_capabilities(profile)
    assert req.device_capabilities == frozenset(
        {"geolocation", "frequency_range", "max_eirp"}
    )
    assert not req.network_capabilities
    assert req.data_capabilities == frozenset({"protected_entities", "boundaries"})
    assert not req.rf_required

    composition = compose_runtime(
        profile, _anatel_deployment(), PluginRegistry.from_discovery()
    )
    adapter = composition.device_adapter
    assert isinstance(adapter, MappingDeviceAdapter)
    assert isinstance(adapter, ConsumerAdapter)
    assert adapter.kind is AdapterKind.DEVICE
    assert composition.network_adapter is None
    assert composition.rf is None
    assert composition.protocol_adapter is None
    assert composition.provenance.selected_plugins["device"] == "mapping"
    assert composition.provenance.unsatisfied_data_capabilities == ()
    assert composition.provider_for("protected_entities") is not None
    assert composition.provider_for("boundaries") is not None

    limits = _constraints(profile)
    duplex = limits["duplex_mode"]
    bandwidth = limits["max_assignment_bandwidth"]
    height = limits["antenna_height_limit"]
    roles = limits["forbidden_device_roles"]
    assert isinstance(duplex, DuplexModeRequirement)
    assert isinstance(bandwidth, MaxAssignmentBandwidth)
    assert isinstance(height, AntennaHeightLimit)
    assert isinstance(roles, ForbiddenDeviceRoles)

    outdoor_base = _outdoor_base_rule(profile)
    area = _authorized_ring(profile)
    inside = _interior_point(area)
    band = FrequencyRange(
        low_hz=profile.spectrum.ranges[0].low_hz,
        high_hz=profile.spectrum.ranges[0].high_hz,
    )
    low_hz = band.low_hz
    high_hz = low_hz + bandwidth.max_bandwidth_hz

    view = adapter.to_consumer(
        {
            "holder_id": "anatel-station-1",
            "latitude_deg": inside.latitude_deg,
            "longitude_deg": inside.longitude_deg,
            "low_hz": low_hz,
            "high_hz": high_hz,
            "eirp_dbm": outdoor_base.max_eirp_dbm,
        }
    )
    assert view.holder_id == "anatel-station-1"
    assert "geolocation" in view.capabilities
    footprint = view.footprints[0]
    assert isinstance(footprint.location, GeoPoint)
    assert band.contains(footprint.frequency)
    assert area.contains(footprint.location)
    assert footprint.power.dbm <= outdoor_base.max_eirp_dbm

    request = SpectrumRequest(
        request_id="anatel-slice-1",
        holder_id=view.holder_id,
        footprints=view.footprints,
        requested_at=UtcInstant(datetime(2026, 9, 10, tzinfo=timezone.utc)),
        access_class_id=None,
    )
    assert request.access_class_id is None

    indoor_outdoor = "outdoor"
    device_class = "base_nodal"
    assert duplex.allows("tdd")
    assert bandwidth.allows(low_hz, high_hz, indoor_outdoor=indoor_outdoor)
    assert height.allows(
        height.max_height_m,
        indoor_outdoor=indoor_outdoor,
        device_class=device_class,
    )
    assert roles.allows(device_class)

    assert not duplex.allows("fdd")
    outside = GeoPoint(
        latitude_deg=inside.latitude_deg,
        longitude_deg=min(vertex[0] for vertex in area.coordinates) - 0.01,
    )
    assert not area.contains(outside)


def test_anatel_composition_fails_closed_without_required_data() -> None:
    profile = load_profile("br_anatel_slp_3700")
    with pytest.raises(MissingCapabilityError, match="protected_entities|boundaries"):
        compose_runtime(
            profile,
            DeploymentConfig(
                id="anatel_missing_data",
                device=PluginSelection(plugin="mapping"),
            ),
            PluginRegistry.from_discovery(),
        )
