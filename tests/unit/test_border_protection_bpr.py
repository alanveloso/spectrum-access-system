"""BPR fail-closed when border KMZ / RF backend unavailable."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType
from typing import Any

import pytest

from services.border_protection import (
    BorderPfdOutcome,
    BorderProtectionUnavailable,
    evaluate_canadian_border_pfd,
    violates_canadian_border_pfd,
)


# Near Montreal — inside Arrangement R sharing zone with real border KMZ.
_INSTALL = {
    "latitude": 45.0,
    "longitude": -73.5,
    "height": 10.0,
    "heightType": "AGL",
    "indoorDeployment": False,
    "antennaGain": 0.0,
}
# Interior CONUS (WINNF EXZ_1 N2_1) — far from US/Canada sharing zone.
_INTERIOR = {
    "latitude": 42.37477,
    "longitude": -100.93139,
    "height": 6.0,
    "heightType": "AGL",
    "indoorDeployment": False,
    "antennaGain": 16.0,
    "antennaAzimuth": 90,
    "antennaBeamwidth": 30,
}
_LOW = 3_655_000_000
_HIGH = 3_670_000_000


def test_outside_arrangement_r_allows_without_models():
    outcome = evaluate_canadian_border_pfd(
        _INSTALL, max_eirp=30.0, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    assert outcome is BorderPfdOutcome.ALLOW
    assert violates_canadian_border_pfd(
        _INSTALL, 30.0, 3_550_000_000, 3_560_000_000
    ) is False


def test_interior_arrangement_r_allows_without_reference_models(monkeypatch):
    """EXZ_1 root cause: interior sites must not be denied for missing ITM/numpy."""
    real_import = builtins.__import__

    def _block_reference_models(name, *args, **kwargs):
        if name == "reference_models" or name.startswith("reference_models."):
            raise ImportError("blocked for BPR interior allow test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_reference_models)
    outcome = evaluate_canadian_border_pfd(
        _INTERIOR, max_eirp=20.0, low_hz=_LOW, high_hz=_HIGH
    )
    assert outcome is BorderPfdOutcome.ALLOW
    assert violates_canadian_border_pfd(_INTERIOR, 20.0, _LOW, _HIGH) is False


def test_missing_reference_models_is_unavailable_fail_closed(monkeypatch):
    """Missing ITM backend inside sharing zone must not authorize (fail-closed).

    Lab override ``path_loss_model="itm"`` uses ``rf.external_propagation``; a
    failed ITM call maps to DENY (same reject path as UNAVAILABLE for grants).
    """
    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        lambda *a, **k: (True, 45.1, -73.4),
    )
    from rf.port import RfUnavailableError

    def _boom(**_k: Any) -> Any:
        raise RfUnavailableError("WInnForum harness reference_models not found")

    monkeypatch.setattr(
        "rf.external_propagation.itm_path_loss_with_geometry",
        _boom,
    )
    outcome = evaluate_canadian_border_pfd(
        _INSTALL,
        max_eirp=37.0,
        low_hz=_LOW,
        high_hz=_HIGH,
        path_loss_model="itm",
    )
    assert outcome is BorderPfdOutcome.DENY
    assert (
        violates_canadian_border_pfd(
            _INSTALL, 37.0, _LOW, _HIGH, path_loss_model="itm"
        )
        is True
    )


def test_missing_installation_coords_fail_closed_in_arrangement_r():
    outcome = evaluate_canadian_border_pfd(
        {"height": 6.0}, max_eirp=20.0, low_hz=_LOW, high_hz=_HIGH
    )
    assert outcome is BorderPfdOutcome.UNAVAILABLE
    assert violates_canadian_border_pfd({"height": 6.0}, 20.0, _LOW, _HIGH) is True


def test_zone_check_exception_is_unavailable(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("dataset missing")

    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        _boom,
    )
    with pytest.raises(BorderProtectionUnavailable):
        evaluate_canadian_border_pfd(_INSTALL, 30.0, _LOW, _HIGH)
    assert violates_canadian_border_pfd(_INSTALL, 30.0, _LOW, _HIGH) is True


def test_missing_border_kmz_fail_closed(monkeypatch, tmp_path):
    from dataclasses import dataclass

    from providers.contract import (
        CAPABILITY_BOUNDARIES,
        DataKind,
        DatasetProvenance,
        PROVIDER_API_VERSION,
        TOKEN_US_CANADA_BORDER,
    )
    from runtime.composition import RuntimeComposition
    from runtime.context import runtime_composition_scope
    from services import border_geometry

    @dataclass(frozen=True, slots=True)
    class _MissingBorderProvider:
        api_version = PROVIDER_API_VERSION
        kind = DataKind.BOUNDARIES

        def advertised_capabilities(self):
            return frozenset({CAPABILITY_BOUNDARIES})

        def provenance(self):
            return DatasetProvenance("spy", "1", "missing_border")

        def fetch(self, *, point=None, token=None):
            if token != TOKEN_US_CANADA_BORDER:
                raise ValueError("unexpected token")
            raise ValueError("us_canada_border KMZ unavailable in composed bundle")

    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_MissingBorderProvider(),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    border_geometry.reset_border_geometry_cache()
    with runtime_composition_scope(composition):
        with pytest.raises(BorderProtectionUnavailable):
            evaluate_canadian_border_pfd(_INTERIOR, 20.0, _LOW, _HIGH)
        assert violates_canadian_border_pfd(_INTERIOR, 20.0, _LOW, _HIGH) is True
    border_geometry.reset_border_geometry_cache()


def test_outside_sharing_zone_allows(monkeypatch):
    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        lambda *a, **k: (False, None, None),
    )
    outcome = evaluate_canadian_border_pfd(_INSTALL, 40.0, _LOW, _HIGH)
    assert outcome is BorderPfdOutcome.ALLOW
    assert violates_canadian_border_pfd(_INSTALL, 40.0, _LOW, _HIGH) is False


def test_explicit_free_space_profile_computes_pfd(monkeypatch):
    """free_space is allowed only when explicitly selected — never silent ITM sub."""

    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        lambda *a, **k: (True, 45.1, -73.4),
    )

    antenna_mod = ModuleType("reference_models.antenna")
    antenna = ModuleType("reference_models.antenna.antenna")
    antenna.GetStandardAntennaGains = lambda *a, **k: 0.0  # type: ignore[attr-defined]
    antenna_mod.antenna = antenna  # type: ignore[attr-defined]
    prop_pkg = ModuleType("reference_models.propagation")
    wf_itm = ModuleType("reference_models.propagation.wf_itm")
    wf_itm.CalcItmPropagationLoss = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("ITM must not run for free_space profile")
    )
    prop_pkg.wf_itm = wf_itm  # type: ignore[attr-defined]
    root = ModuleType("reference_models")
    root.antenna = antenna_mod  # type: ignore[attr-defined]
    root.propagation = prop_pkg  # type: ignore[attr-defined]
    for name, mod in (
        ("reference_models", root),
        ("reference_models.antenna", antenna_mod),
        ("reference_models.antenna.antenna", antenna),
        ("reference_models.propagation", prop_pkg),
        ("reference_models.propagation.wf_itm", wf_itm),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    deny = evaluate_canadian_border_pfd(
        _INSTALL,
        max_eirp=50.0,
        low_hz=_LOW,
        high_hz=_HIGH,
        path_loss_model="free_space",
    )
    assert deny is BorderPfdOutcome.DENY

    allow = evaluate_canadian_border_pfd(
        _INSTALL,
        max_eirp=-40.0,
        low_hz=_LOW,
        high_hz=_HIGH,
        path_loss_model="free_space",
    )
    assert allow is BorderPfdOutcome.ALLOW


def test_itm_failure_inside_zone_denies(monkeypatch):
    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        lambda *a, **k: (True, 45.1, -73.4),
    )
    antenna_mod = ModuleType("reference_models.antenna")
    antenna = ModuleType("reference_models.antenna.antenna")
    antenna.GetStandardAntennaGains = lambda *a, **k: 0.0  # type: ignore[attr-defined]
    antenna_mod.antenna = antenna  # type: ignore[attr-defined]
    root = ModuleType("reference_models")
    root.antenna = antenna_mod  # type: ignore[attr-defined]
    for name, mod in (
        ("reference_models", root),
        ("reference_models.antenna", antenna_mod),
        ("reference_models.antenna.antenna", antenna),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    from rf.port import RfUnavailableError

    def _itm_fail(**_kwargs: Any) -> Any:
        raise RfUnavailableError("NED tile missing")

    monkeypatch.setattr(
        "rf.external_propagation.itm_path_loss_with_geometry",
        _itm_fail,
    )

    assert (
        evaluate_canadian_border_pfd(
            _INSTALL, 30.0, _LOW, _HIGH, path_loss_model="itm"
        )
        is BorderPfdOutcome.DENY
    )


def test_composed_rf_port_used_not_direct_wf_itm(monkeypatch):
    """Border protection must call composed RfPort, never wf_itm."""
    monkeypatch.setattr(
        "services.border_geometry.check_cbsd_in_border_sharing_zone",
        lambda *a, **k: (True, 45.1, -73.4),
    )
    antenna_mod = ModuleType("reference_models.antenna")
    antenna = ModuleType("reference_models.antenna.antenna")
    antenna.GetStandardAntennaGains = lambda *a, **k: 0.0  # type: ignore[attr-defined]
    antenna_mod.antenna = antenna  # type: ignore[attr-defined]
    root = ModuleType("reference_models")
    root.antenna = antenna_mod  # type: ignore[attr-defined]
    # If anything imports wf_itm, fail the test.
    prop_pkg = ModuleType("reference_models.propagation")

    def _boom_attr(name: str) -> Any:
        raise AssertionError(f"must not access reference_models.propagation.{name}")

    prop_pkg.__getattr__ = _boom_attr  # type: ignore[attr-defined]
    root.propagation = prop_pkg  # type: ignore[attr-defined]
    for name, mod in (
        ("reference_models", root),
        ("reference_models.antenna", antenna_mod),
        ("reference_models.antenna.antenna", antenna),
        ("reference_models.propagation", prop_pkg),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    calls: list[float] = []

    class _FakeRf:
        provenance = "test:fake"

        def path_loss(self, request: Any) -> Any:
            from rf.port import PathLossResult

            calls.append(request.tx_height_m)
            return PathLossResult(loss_db=200.0, model_id="path_loss", provenance="test")

    outcome = evaluate_canadian_border_pfd(
        _INSTALL,
        max_eirp=50.0,
        low_hz=_LOW,
        high_hz=_HIGH,
        rf_port=_FakeRf(),
    )
    assert outcome is BorderPfdOutcome.ALLOW  # high PL → low PFD
    assert len(calls) == 1
