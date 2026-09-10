"""CBSD device adapter maps WInnForum fields to ConsumerView."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adapters.cbsd import CbsdDeviceAdapter
from adapters.device import ConsumerAdapter, consumer_meets_requirements
from adapters.winnforum_rest import WinnForumRestProtocolAdapter
from primitives.request import SpectrumRequest


def _radio_item() -> dict[str, object]:
    return {
        "fccId": "testfcc",
        "cbsdSerialNumber": "sn-1",
        "installationParam": {"latitude": 39.1, "longitude": -77.2},
        "operationParam": {
            "maxEirp": 23.0,
            "operationFrequencyRange": {
                "lowFrequency": 3550_000_000,
                "highFrequency": 3560_000_000,
            },
        },
    }


def test_cbsd_adapter_produces_opaque_holder_and_footprint():
    adapter = CbsdDeviceAdapter()
    assert isinstance(adapter, ConsumerAdapter)
    view = adapter.to_consumer(_radio_item())
    assert view.holder_id == "testfcc/sn-1"
    consumer_meets_requirements(view, ("geolocation", "frequency_range", "max_eirp"))
    fp = view.footprints[0]
    assert fp.frequency.low_hz == 3550_000_000
    assert fp.power.dbm == pytest.approx(23.0)
    assert fp.location.latitude_deg == pytest.approx(39.1)


def test_cbsd_id_preferred_and_incomplete_payload_fail_closed():
    adapter = CbsdDeviceAdapter()
    item = dict(_radio_item())
    item["cbsdId"] = "fcc/sn-registered"
    assert adapter.to_consumer(item).holder_id == "fcc/sn-registered"
    with pytest.raises(ValueError, match="installationParam"):
        adapter.to_consumer({"cbsdId": "x", "operationParam": item["operationParam"]})
    # operationParam without maxEirp falls back to category ceiling when geo present.
    view = adapter.to_consumer(
        {
            "cbsdId": "x",
            "cbsdCategory": "A",
            "installationParam": {"latitude": 0.0, "longitude": 0.0},
            "operationParam": {
                "operationFrequencyRange": {
                    "lowFrequency": 1,
                    "highFrequency": 2,
                }
            },
        }
    )
    assert view.footprints[0].power.dbm == pytest.approx(30.0)


def test_winnforum_decode_uses_cbsd_adapter_not_generic_core_types():
    protocol = WinnForumRestProtocolAdapter()
    inbound = protocol.decode(
        {"grantRequest": [_radio_item() | {"cbsdId": "holder-a"}]},
        CbsdDeviceAdapter(),
    )
    assert isinstance(inbound.request, SpectrumRequest)
    assert inbound.request.holder_id == "holder-a"
    assert inbound.request.footprints[0].frequency.high_hz == 3560_000_000


def test_primitives_still_have_no_cbsd_imports():
    root = Path(__file__).resolve().parents[2] / "primitives"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "cbsd" not in node.module
                assert not node.module.startswith("adapters")
