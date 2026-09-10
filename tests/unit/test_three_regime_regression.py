"""three design regimes on the same core — CBRS + BR + eLSA (local).

Does not claim WInnForum PASS_OFFICIAL or ETSI conformity.
"""

from __future__ import annotations

from pathlib import Path

from adapters.cbsd import CbsdDeviceAdapter
from adapters.elsa1 import Elsa1ProtocolAdapter
from adapters.managed_consumer import ManagedNetworkAdapter
from adapters.protocol import DomainOperation
from primitives.availability import AvailabilityZoneKind
from profiles.selection import (
    DEFAULT_PROFILE_ID,
    active_profile_id,
    clear_profile_override,
)
from profiles import get_active_profile_document, primary_spectrum_range
from profiles.context import profile_context_from_document, profile_hash
from profiles.cost import measure_profile_cost
from profiles.doctor import run_profile_doctor
from profiles.parse import load_profile

_REPO = Path(__file__).resolve().parents[2]
_REGIME_IDS = ("cbrs_winnforum", "br_anatel_slp_3700", "eu_elsa")


def test_default_active_remains_cbrs_after_three_regime_loads() -> None:
    clear_profile_override()
    assert DEFAULT_PROFILE_ID == "cbrs_winnforum"
    _ = load_profile("cbrs_winnforum")
    _ = load_profile("br_anatel_slp_3700")
    _ = load_profile("eu_elsa")
    assert active_profile_id() == "cbrs_winnforum"
    active = get_active_profile_document()
    band = primary_spectrum_range(active)
    assert band.low_hz == 3_550_000_000
    assert band.high_hz == 3_700_000_000


def test_three_profile_contexts_are_isolated() -> None:
    cbrs = load_profile("cbrs_winnforum")
    br = load_profile("br_anatel_slp_3700")
    elsa = load_profile("eu_elsa")
    hashes = {
        profile_hash(cbrs),
        profile_hash(br),
        profile_hash(elsa),
    }
    assert len(hashes) == 3
    ctx = {p.metadata.id: profile_context_from_document(p) for p in (cbrs, br, elsa)}
    assert ctx["cbrs_winnforum"].profile_id == "cbrs_winnforum"
    assert ctx["br_anatel_slp_3700"].profile_id == "br_anatel_slp_3700"
    assert ctx["eu_elsa"].profile_id == "eu_elsa"

    # Distinct regime shapes on the same Spectrum Profile / core contracts
    assert cbrs.access is not None
    assert br.access is None
    assert elsa.access is None
    assert cbrs.authorization is not None and cbrs.authorization.mechanism == "dynamic_lease"
    assert br.authorization is not None and br.authorization.mechanism == "static_authorization"
    assert elsa.authorization is not None and elsa.authorization.mechanism == "fixed_window"
    assert cbrs.rf is not None and cbrs.rf.required is True
    assert br.rf is not None and br.rf.required is False
    assert elsa.rf is not None and elsa.rf.required is False
    assert elsa.temporal is not None and elsa.temporal.availability is not None
    assert elsa.temporal.availability.mechanism == "availability_constraint"
    assert elsa.requirements is not None
    assert elsa.requirements.network_capabilities
    assert not elsa.requirements.device_capabilities
    assert "geolocation" not in elsa.requirements.network_capabilities


def test_all_three_reference_profiles_pass_doctor_and_cost() -> None:
    for profile_id in _REGIME_IDS:
        report = run_profile_doctor(profile_id=profile_id)
        assert report.ok, profile_id + ": " + "; ".join(
            f"{f.name}={f.detail}" for f in report.findings if not f.ok
        )
        cost = measure_profile_cost(profile_id=profile_id, repo_root=_REPO)
        assert cost.mechanism_reuse_pct == 100.0, profile_id
        assert cost.mechanisms_novel == ()
        assert cost.profile_python_loc == 0


def test_builtin_catalog_resolves_all_three_regimes() -> None:
    for regime in _REGIME_IDS:
        doc = load_profile(regime)
        assert doc.metadata.id == regime


def test_interleaved_three_regime_loads_preserve_bands() -> None:
    for _ in range(3):
        cbrs = load_profile("cbrs_winnforum")
        br = load_profile("br_anatel_slp_3700")
        elsa = load_profile("eu_elsa")
        assert cbrs.spectrum.ranges[0].low_hz == 3_550_000_000
        assert br.spectrum.ranges[0].low_hz == 3_700_000_000
        assert elsa.spectrum.ranges[0].low_hz == 2_300_000_000


def test_same_core_adapters_compose_without_fake_cbsd() -> None:
    """CBRS device path and eLSA network+protocol path share ConsumerView/Protocol contracts."""
    cbsd = CbsdDeviceAdapter()
    cbsd_view = cbsd.to_consumer(
        {
            "cbsdId": "cbsd-1",
            "installationParam": {
                "latitude": 39.0,
                "longitude": -77.0,
                "height": 10.0,
                "heightType": "AGL",
            },
            "operationParam": {
                "maxEirp": 23.0,
                "operationFrequencyRange": {
                    "lowFrequency": 3550000000,
                    "highFrequency": 3560000000,
                },
            },
        }
    )
    assert "geolocation" in cbsd_view.capabilities

    network = ManagedNetworkAdapter()
    elsa = Elsa1ProtocolAdapter()
    inbound = elsa.decode(
        {
            "procedure": "elsraiNotification",
            "transaction_id": "tx-elsa-regression",
            "requested_at": "2026-08-20T12:00:00+00:00",
            "consumer": {
                "network_id": "mfcn-1",
                "vsp_id": "vsp-a",
                "ring": [[0, 0], [1, 0], [1, 1], [0, 0]],
                "low_hz": 2300000000,
                "high_hz": 2310000000,
                "eirp_dbm": 30.0,
            },
            "elsrai": {
                "zones": [
                    {
                        "id": "z1",
                        "kind": "allowance",
                        "low_hz": 2300000000,
                        "high_hz": 2320000000,
                        "ring": [[0, 0], [1, 0], [1, 1], [0, 0]],
                        "validity_start": "2026-08-20T12:00:00+00:00",
                        "validity_end": "2026-08-20T18:00:00+00:00",
                        "mode": "scheduled",
                    }
                ],
                "event_kind": "updated",
                "event_id": "ev-1",
                "observed_at": "2026-08-20T12:00:01+00:00",
            },
        },
        network,
    )
    assert inbound.operation is DomainOperation.APPLY_AVAILABILITY
    assert inbound.request.holder_id == "vsp-a/mfcn-1"
    assert "geolocation" not in network.advertised_capabilities()
    assert inbound.availability_constraints[0].zone_kind is AvailabilityZoneKind.ALLOWANCE
    # Distinct holders — network path must not look like CBSD id scheme from protocol.
    assert inbound.request.holder_id != cbsd_view.holder_id
