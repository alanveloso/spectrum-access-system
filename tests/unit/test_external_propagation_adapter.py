"""Parity and integration tests for external spectrum_propagation RfPort adapter."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from primitives.geography import GeoPoint
from profiles import load_profile
from rf.external_propagation import (
    free_space_rf_adapter,
    itm_rf_adapter,
)
from rf.port import PathLossRequest, RfUnavailableError
from runtime import (
    PluginRegistry,
    compose_runtime,
    load_deployment,
)
from runtime.deployment import DEFAULT_DEPLOYMENT_PATH
from services.iap.coupling import free_space_path_loss_db, make_production_iap_coupling
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)

REPO = Path(__file__).resolve().parents[2]
PARITY = Path(__file__).resolve().parents[1] / "fixtures" / "free_space_parity.json"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_earth_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * r_earth_km * math.asin(min(1.0, math.sqrt(a)))


def _oracle_fspl(
    tx_lat: float,
    tx_lon: float,
    rx_lat: float,
    rx_lon: float,
    tx_h: float,
    rx_h: float,
    freq_hz: int,
) -> float:
    freq_mhz = freq_hz / 1_000_000.0
    dist_km = _haversine_km(tx_lat, tx_lon, rx_lat, rx_lon)
    dh_km = abs(tx_h - rx_h) / 1000.0
    slant_km = max(math.sqrt(dist_km**2 + dh_km**2), 1e-6)
    return float(20.0 * math.log10(slant_km) + 20.0 * math.log10(freq_mhz) + 32.44)


def test_free_space_parity_exact_against_oracle() -> None:
    cases = json.loads(PARITY.read_text(encoding="utf-8"))["cases"]
    adapter = free_space_rf_adapter()
    exact = 0
    for case in cases:
        req = PathLossRequest(
            tx=GeoPoint(latitude_deg=case["tx_lat"], longitude_deg=case["tx_lon"]),
            rx=GeoPoint(latitude_deg=case["rx_lat"], longitude_deg=case["rx_lon"]),
            tx_height_m=case["tx_height_m"],
            rx_height_m=case["rx_height_m"],
            frequency_hz=case["frequency_hz"],
            indoor=case.get("indoor", False),
        )
        got = adapter.path_loss(req).loss_db
        oracle = _oracle_fspl(
            case["tx_lat"],
            case["tx_lon"],
            case["rx_lat"],
            case["rx_lon"],
            case["tx_height_m"],
            case["rx_height_m"],
            case["frequency_hz"],
        )
        assert got == oracle == case["path_loss_db"]
        exact += 1
    assert exact == len(cases)


def test_iap_coupling_fspl_matches_adapter() -> None:
    grant = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=39.0,
        longitude=-77.0,
        height_m=30.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    point = ProtectionPoint(
        point_id="p",
        latitude=39.1,
        longitude=-77.1,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.GENERIC,
    )
    via_coupling = free_space_path_loss_db(grant, point, freq_mhz=3625.0, rx_height_m=1.5)
    via_adapter = free_space_rf_adapter().path_loss(
        PathLossRequest(
            tx=GeoPoint(latitude_deg=39.0, longitude_deg=-77.0),
            rx=GeoPoint(latitude_deg=39.1, longitude_deg=-77.1),
            tx_height_m=30.0,
            rx_height_m=1.5,
            frequency_hz=3_625_000_000,
        )
    ).loss_db
    assert via_coupling == via_adapter


def test_reference_deployment_uses_external_fspl_provenance() -> None:
    profile = load_profile("cbrs_winnforum")
    deployment = load_deployment(DEFAULT_DEPLOYMENT_PATH).model_copy(
        update={"providers": []}
    )
    runtime = compose_runtime(
        profile,
        deployment,
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    assert runtime.rf is not None
    assert "spectrum-propagation" in runtime.rf.provenance
    req = PathLossRequest(
        tx=GeoPoint(latitude_deg=39.0, longitude_deg=-77.0),
        rx=GeoPoint(latitude_deg=39.01, longitude_deg=-77.01),
        tx_height_m=10.0,
        rx_height_m=1.5,
        frequency_hz=3_625_000_000,
    )
    assert runtime.rf.path_loss(req).loss_db > 0


def test_iap_uses_composed_external_rf() -> None:
    profile = load_profile("cbrs_winnforum")
    deployment = load_deployment(DEFAULT_DEPLOYMENT_PATH).model_copy(
        update={"providers": []}
    )
    runtime = compose_runtime(
        profile,
        deployment,
        PluginRegistry.from_discovery(),
        require_data_plugins=False,
    )
    calls: list[float] = []
    real = runtime.rf.path_loss

    def spy(request: PathLossRequest):
        result = real(request)
        calls.append(result.loss_db)
        return result

    runtime.rf.path_loss = spy  # type: ignore[method-assign]
    coupling = make_production_iap_coupling(rf_port=runtime.rf)
    grant = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=39.0,
        longitude=-77.0,
        height_m=10.0,
        height_is_agl=True,
        indoor=False,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=20.0,
    )
    point = ProtectionPoint(
        point_id="p",
        latitude=39.01,
        longitude=-77.01,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-80.0,
        entity_kind=ProtectedEntityKind.GENERIC,
    )
    channel = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_560_000_000)
    coupling(grant, point, channel, 20.0)
    assert len(calls) == 1


def test_itm_adapter_fail_closed_without_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``itm_path_loss`` binds ``resolve_harness_dir`` into its own module; patch both.
    monkeypatch.setattr(
        "spectrum_propagation.integrations.winnforum.harness.resolve_harness_dir",
        lambda explicit=None: None,
    )
    monkeypatch.setattr(
        "spectrum_propagation.integrations.winnforum.itm.resolve_harness_dir",
        lambda explicit=None: None,
    )
    from spectrum_propagation.integrations.winnforum.harness import clear_itm_cache

    clear_itm_cache()
    adapter = itm_rf_adapter()
    req = PathLossRequest(
        tx=GeoPoint(latitude_deg=39.0, longitude_deg=-77.0),
        rx=GeoPoint(latitude_deg=39.01, longitude_deg=-77.01),
        tx_height_m=10.0,
        rx_height_m=1.5,
        frequency_hz=3_625_000_000,
    )
    with pytest.raises(RfUnavailableError):
        adapter.path_loss(req)


def test_domain_services_do_not_import_spectrum_propagation() -> None:
    """Static check: IAP/BPR/routes must not import the external package."""
    import ast

    roots = [
        REPO / "services" / "iap",
        REPO / "services" / "border_protection.py",
        REPO / "routes",
        REPO / "primitives",
        REPO / "profiles",
    ]
    offenders: list[str] = []
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "spectrum_propagation":
                        offenders.append(str(path.relative_to(REPO)))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "spectrum_propagation":
                    offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
