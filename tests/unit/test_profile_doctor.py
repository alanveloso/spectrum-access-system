"""Spectrum Profile doctor CLI — structure, semantics, plugins, data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from adapters.device import MappingDeviceAdapter, MappingNetworkAdapter
from adapters.discovery import GROUP_DEVICE_ADAPTERS, AdapterDiscovery
from providers.contract import DATA_CAPABILITIES, DatasetProvenance, MappingTerrainProvider
from providers.discovery import DataProviderDiscovery
from rf.cbrs_winnforum import CbrsWinnForumRfAdapter
from rf.discovery import RfModelDiscovery
from profiles.doctor import (
    diagnose_profile,
    render_profile_doctor_report,
    run_profile_doctor,
)
from profiles.parse import parse_profile_document
from tools.profile_doctor import main as profile_doctor_main

_PROV = DatasetProvenance(dataset_id="mem", dataset_version="1", provider_id="map")


def _minimal_doc(**overrides: object) -> dict:
    doc: dict = {
        "api_version": "spectrum-access/v2",
        "kind": "SpectrumProfile",
        "metadata": {"id": "example", "version": "1.0.0", "status": "custom"},
        "spectrum": {"ranges": [{"id": "main", "low_hz": 1000, "high_hz": 2000}]},
    }
    doc.update(overrides)
    return doc


def test_cbrs_reference_profile_id_passes_default_doctor():
    report = run_profile_doctor(profile_id="cbrs_winnforum")
    assert report.ok
    assert report.profile_id == "cbrs_winnforum"
    assert report.profile_hash
    by_name = {f.name: f for f in report.findings}
    assert by_name["structure"].ok
    assert by_name["semantics"].ok
    assert by_name["device_plugins"].ok
    assert by_name["rf_plugins"].ok
    # No data_providers entry points in the base package → advisory OK.
    assert by_name["data_plugins"].ok


def test_path_load_and_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "custom.yaml"
    path.write_text(
        yaml.safe_dump(
            _minimal_doc(
                rf={
                    "required": True,
                    "policy": "path_loss_plus_aggregate",
                    "propagation_model": "path_loss",
                },
                data={"required_capabilities": ["terrain"]},
                requirements={
                    "device_capabilities": ["geolocation", "frequency_range", "max_eirp"]
                },
            )
        ),
        encoding="utf-8",
    )
    report = run_profile_doctor(
        path=path,
        adapter_discovery=AdapterDiscovery(
            overlays={GROUP_DEVICE_ADAPTERS: {"dev": MappingDeviceAdapter}},
            list_entry_points=lambda _g: (),
        ),
        rf_discovery=RfModelDiscovery(
            overlays={"fs": lambda: CbrsWinnForumRfAdapter(backend="free_space")},
            list_entry_points=lambda _g: (),
        ),
        data_discovery=DataProviderDiscovery(
            overlays={
                "terrain": lambda: MappingTerrainProvider({(0.0, 0.0): 1.0}, _PROV)
            },
            list_entry_points=lambda _g: (),
        ),
    )
    assert report.ok
    text = render_profile_doctor_report(report)
    assert "RESULT: PASS" in text

    rc = profile_doctor_main(["--id", "cbrs_winnforum", "--json", "--no-check-plugins"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile_id"] == "cbrs_winnforum"


def test_structure_fail_unknown_mechanism(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            _minimal_doc(
                access={"mechanism": "not_a_real_mechanism", "classes": []}
            )
        ),
        encoding="utf-8",
    )
    report = run_profile_doctor(path=path, check_plugins=False)
    assert not report.ok
    assert report.findings[0].name == "structure"
    assert report.findings[0].ok is False


def test_device_plugin_fail_when_only_network_adapters():
    from adapters.discovery import GROUP_NETWORK_ADAPTERS

    parsed = parse_profile_document(
        _minimal_doc(
            requirements={
                "device_capabilities": ["geolocation", "frequency_range", "max_eirp"]
            }
        )
    )
    report = diagnose_profile(
        parsed,
        source="mem",
        adapter_discovery=AdapterDiscovery(
            overlays={
                GROUP_DEVICE_ADAPTERS: {},
                GROUP_NETWORK_ADAPTERS: {"net": MappingNetworkAdapter},
            },
            list_entry_points=lambda _g: (),
        ),
        rf_discovery=RfModelDiscovery(list_entry_points=lambda _g: ()),
        data_discovery=DataProviderDiscovery(list_entry_points=lambda _g: ()),
    )
    assert not report.ok
    device = next(f for f in report.findings if f.name == "device_plugins")
    assert device.ok is False
    assert "no installed adapter" in device.detail


def test_rf_plugin_fail_when_no_matching_model():
    parsed = parse_profile_document(
        _minimal_doc(
            rf={
                "required": True,
                "policy": "path_loss_plus_aggregate",
                "propagation_model": "path_loss",
            },
            data={"required_capabilities": ["terrain"]},
            requirements={"device_capabilities": ["geolocation"]},
        )
    )
    report = diagnose_profile(
        parsed,
        source="mem",
        adapter_discovery=AdapterDiscovery(
            overlays={GROUP_DEVICE_ADAPTERS: {"dev": MappingDeviceAdapter}},
            list_entry_points=lambda _g: (),
        ),
        rf_discovery=RfModelDiscovery(list_entry_points=lambda _g: ()),
        data_discovery=DataProviderDiscovery(list_entry_points=lambda _g: ()),
    )
    assert not report.ok
    rf = next(f for f in report.findings if f.name == "rf_plugins")
    assert rf.ok is False


def test_require_data_plugins_fails_when_none_installed():
    report = run_profile_doctor(
        profile_id="cbrs_winnforum",
        require_data_plugins=True,
        data_discovery=DataProviderDiscovery(list_entry_points=lambda _g: ()),
        adapter_discovery=AdapterDiscovery(
            overlays={GROUP_DEVICE_ADAPTERS: {"dev": MappingDeviceAdapter}},
            list_entry_points=lambda _g: (),
        ),
        rf_discovery=RfModelDiscovery(
            overlays={"fs": lambda: CbrsWinnForumRfAdapter(backend="free_space")},
            list_entry_points=lambda _g: (),
        ),
    )
    assert not report.ok
    data = next(f for f in report.findings if f.name == "data_plugins")
    assert data.ok is False
    assert set(DATA_CAPABILITIES)  # sanity: catalog still defined
