"""Device adapter Lego proof: substitutable via DeploymentConfig only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.cbsd import CbsdDeviceAdapter
from adapters.device import MappingDeviceAdapter
from models.models import Cbsd
from profiles import load_profile
from runtime import PluginRegistry, UnknownPluginError, compose_runtime, load_deployment
from services.consumer_semantics import cbsd_geo_coordinates, geo_coordinates
from services.grant_service import _cbsd_location

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENT_A = REPO / "tests" / "fixtures" / "deployments" / "device_provider_a.yaml"
DEPLOYMENT_B = REPO / "tests" / "fixtures" / "deployments" / "device_provider_b.yaml"
PROFILE = load_profile("cbrs_winnforum")

_WINNF_REG = {
    "fccId": "testfcc",
    "cbsdSerialNumber": "sn-lego",
    "cbsdCategory": "A",
    "installationParam": {
        "latitude": 39.12,
        "longitude": -77.21,
        "height": 6.0,
        "heightType": "AGL",
        "indoorDeployment": True,
        "eirpCapability": 23.0,
    },
    "airInterface": {"radioTechnology": "E_UTRA"},
}

_MAPPING_PAYLOAD = {
    "holder_id": "testfcc/sn-lego",
    "latitude_deg": 48.44,
    "longitude_deg": -95.12,
    "low_hz": 3_550_000_000,
    "high_hz": 3_560_000_000,
    "eirp_dbm": 23.0,
}


def _compose(path: Path):
    return compose_runtime(
        PROFILE,
        load_deployment(path),
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )


def test_device_deployments_select_different_plugins():
    dep_a = load_deployment(DEPLOYMENT_A)
    dep_b = load_deployment(DEPLOYMENT_B)
    assert dep_a.device is not None and dep_b.device is not None
    assert dep_a.device.plugin == "cbsd"
    assert dep_b.device.plugin == "mapping"
    assert dep_a.protocol == dep_b.protocol


def test_device_adapter_for_resolves_via_discovery():
    runtime_a = _compose(DEPLOYMENT_A)
    runtime_b = _compose(DEPLOYMENT_B)
    assert runtime_a.device_adapter is not runtime_b.device_adapter
    assert runtime_a.provenance.resolved["device.geolocation"] == "cbsd"
    assert runtime_b.provenance.resolved["device.geolocation"] == "mapping"
    assert runtime_a.profile.metadata.id == runtime_b.profile.metadata.id


def test_device_lego_geo_semantics_follow_composed_adapter():
    runtime_a = _compose(DEPLOYMENT_A)
    runtime_b = _compose(DEPLOYMENT_B)
    assert runtime_a.device_adapter is not None
    assert runtime_b.device_adapter is not None

    lat_a, lon_a = geo_coordinates(runtime_a.device_adapter.to_consumer(_WINNF_REG))
    lat_b, lon_b = geo_coordinates(runtime_b.device_adapter.to_consumer(_MAPPING_PAYLOAD))

    assert lat_a == pytest.approx(39.12)
    assert lon_a == pytest.approx(-77.21)
    assert lat_b == pytest.approx(48.44)
    assert lon_b == pytest.approx(-95.12)


def test_grant_location_uses_adapter_not_raw_installation():
    # Mapping-format snapshot with legacy WInnForum noise the adapter must ignore.
    reg_snapshot = {
        **_MAPPING_PAYLOAD,
        "installationParam": {
            "latitude": 39.0,
            "longitude": -77.0,
        },
    }
    cbsd = Cbsd(
        cbsd_id="testfcc/sn-lego",
        fcc_id="testfcc",
        user_id="user1",
        cbsd_serial_number="sn-lego",
        registration_json=json.dumps(reg_snapshot),
    )
    mapping = MappingDeviceAdapter()
    lat, lon = _cbsd_location(cbsd, mapping)
    assert lat == pytest.approx(48.44)
    assert lon == pytest.approx(-95.12)

    cbsd_adapter = CbsdDeviceAdapter()
    lat_c, lon_c = cbsd_geo_coordinates(cbsd, cbsd_adapter)
    assert lat_c == pytest.approx(39.0)
    assert lon_c == pytest.approx(-77.0)


def test_mapping_device_plugin_absent_fails_closed():
    from adapters.discovery import AdapterDiscovery, GROUP_DEVICE_ADAPTERS
    from importlib.metadata import entry_points

    limited = PluginRegistry.from_discovery(
        adapters=AdapterDiscovery(
            overlays={},
            list_entry_points=lambda group: [
                ep
                for ep in entry_points(group=group)
                if not (group == GROUP_DEVICE_ADAPTERS and ep.name == "mapping")
            ],
        ),
    )
    with pytest.raises(UnknownPluginError):
        compose_runtime(
            PROFILE,
            load_deployment(DEPLOYMENT_B),
            limited,
            require_data_plugins=False,
        )


def test_cbsd_adapter_supports_registration_snapshot_without_operation_param():
    adapter = CbsdDeviceAdapter()
    view = adapter.to_consumer(_WINNF_REG)
    assert view.holder_id == "testfcc/sn-lego"
    lat, lon = geo_coordinates(view)
    assert lat == pytest.approx(39.12)
