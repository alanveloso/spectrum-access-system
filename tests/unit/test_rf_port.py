"""RF port wraps existing CBRS engines without changing numbers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from primitives.geography import GeoPoint
from rf.cbrs_winnforum import CbrsWinnForumRfAdapter, free_space_rf_adapter
from rf.discovery import RfModelDiscovery
from rf.port import PathLossRequest, RfPort, RfUnavailableError
from services.iap.coupling import free_space_path_loss_db
from services.iap.models import GrantRfInfo, ProtectedEntityKind, ProtectionPoint
from services.propagation.errors import PropagationUnavailableError
from services.propagation.rel1ext_dpa import calc_p2108_clutter_db, compose_dpa_pathloss_db

_TX = GeoPoint(38.0, -77.0)
_RX = GeoPoint(38.01, -77.01)


def _request(*, height_m: float = 10.0) -> PathLossRequest:
    return PathLossRequest(
        tx=_TX,
        rx=_RX,
        tx_height_m=height_m,
        rx_height_m=1.5,
        frequency_hz=3_625_000_000,
    )


def test_free_space_matches_iap_coupling():
    req = _request()
    adapter = CbrsWinnForumRfAdapter(backend="free_space")
    assert isinstance(adapter, RfPort)
    got = adapter.path_loss(req)
    grant = GrantRfInfo(
        grant_id="rf-port",
        cbsd_id="rf-port",
        latitude=_TX.latitude_deg,
        longitude=_TX.longitude_deg,
        height_m=req.tx_height_m,
        low_hz=req.frequency_hz,
        high_hz=req.frequency_hz + 1,
        max_eirp_dbm_mhz=0.0,
    )
    point = ProtectionPoint(
        point_id="rx",
        latitude=_RX.latitude_deg,
        longitude=_RX.longitude_deg,
        low_hz=req.frequency_hz,
        high_hz=req.frequency_hz + 1,
        threshold_dbm=0.0,
        entity_kind=ProtectedEntityKind.GENERIC,
    )
    expect = free_space_path_loss_db(
        grant, point, freq_mhz=3625.0, rx_height_m=req.rx_height_m
    )
    assert got.loss_db == expect
    assert got.model_id == "path_loss"


def test_rel1ext_matches_compose_with_injected_itm():
    req = _request(height_m=3.0)

    def itm_fn(_grant, _point, _rx_h, _freq) -> float:
        return 110.0

    adapter = CbrsWinnForumRfAdapter(backend="rel1ext", itm_fn=itm_fn)
    got = adapter.path_loss(req)
    clutter = calc_p2108_clutter_db(
        _TX.latitude_deg,
        _TX.longitude_deg,
        req.tx_height_m,
        _RX.latitude_deg,
        _RX.longitude_deg,
    )
    expect = compose_dpa_pathloss_db(110.0, clutter)
    assert got.loss_db == expect


def test_itm_unavailable_is_fail_closed():
    """Injected ITM failure must map to RfUnavailableError (Rel1Ext path).

    ``backend="itm"`` delegates to SpectrumPropagationRfAdapter and ignores
    ``itm_fn``; Rel1Ext is the injectable composition path under test.
    """

    def boom(*_a, **_k):
        raise PropagationUnavailableError("no terrain")

    adapter = CbrsWinnForumRfAdapter(backend="rel1ext", itm_fn=boom)
    with pytest.raises(RfUnavailableError):
        adapter.path_loss(_request())


def test_rf_discovery_loads_free_space_factory():
    discovery = RfModelDiscovery(
        overlays={"free_space": free_space_rf_adapter},
        list_entry_points=lambda _g: (),
    )
    loaded = discovery.load("free_space")
    assert loaded.path_loss(_request()).provenance.endswith("free_space")
    with pytest.raises(ValueError, match="unknown RF model"):
        discovery.load("missing")


def test_generic_rf_modules_have_no_regime_nouns():
    root = Path(__file__).resolve().parents[2] / "rf"
    banned = (
        "cbsd",
        "pal",
        "gaa",
        "grant",
        "heartbeat",
        "fcc",
    )
    for name in ("port.py", "discovery.py"):
        source = (root / name).read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in source, f"{name} contains {token!r}"
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("services")
                assert not node.module.startswith("models")
                assert not node.module.startswith("routes")
