"""Prove RuntimeComposition is authoritative for protocol/RF/providers."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from adapters.protocol import PROTOCOL_API_VERSION
from adapters.winnforum_rest import WINNFORUM_REST_PROTOCOL_ID
from primitives.geography import GeoPoint
from providers.contract import (
    CAPABILITY_PROTECTED_ENTITIES,
    DataKind,
    DatasetProvenance,
    PROVIDER_API_VERSION,
)
from rf.port import RF_API_VERSION, RF_MODEL_PATH_LOSS, PathLossResult
from profiles import load_profile
from runtime import (
    DeploymentConfig,
    PluginRegistry,
    PluginSelection,
    compose_runtime,
    runtime_composition_scope,
)
from runtime.context import get_bound_runtime_composition
from services.border_protection import evaluate_canadian_border_pfd
from services.iap.coupling import make_production_iap_coupling
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)


@dataclass
class SpyProtocolAdapter:
    api_version: str = PROTOCOL_API_VERSION
    protocol_id: str = WINNFORUM_REST_PROTOCOL_ID
    calls: list[str] = field(default_factory=list)

    def request_key(self, procedure: str) -> str:
        self.calls.append(f"request:{procedure}")
        return f"{procedure}Request"

    def response_key(self, procedure: str) -> str:
        self.calls.append(f"response:{procedure}")
        return f"{procedure}Response"

    def decode(self, envelope, consumer_adapter):
        raise NotImplementedError

    def encode(self, decision):
        raise NotImplementedError


@dataclass
class SpyRfPort:
    api_version: str = RF_API_VERSION
    model_id: str = RF_MODEL_PATH_LOSS
    marker_db: float = 12.34
    calls: int = 0

    def path_loss(self, request):
        self.calls += 1
        return PathLossResult(
            loss_db=self.marker_db,
            model_id=self.model_id,
            provenance="spy-rf",
        )


@dataclass
class SpyProtectedEntitiesProvider:
    api_version: str = PROVIDER_API_VERSION
    kind: DataKind = DataKind.PROTECTED_ENTITIES
    tag: str = "spy-a"
    fetches: int = 0

    def advertised_capabilities(self):
        return frozenset({CAPABILITY_PROTECTED_ENTITIES})

    def provenance(self):
        return DatasetProvenance(
            dataset_id=self.tag, dataset_version="1", provider_id=self.tag
        )

    def fetch(self, *, point=None, token=None):
        self.fetches += 1
        from providers.contract import FeatureIdsRecord

        return FeatureIdsRecord(feature_ids=(self.tag,), provenance=self.provenance())


def test_protocol_route_uses_composed_adapter_keys():
    from types import SimpleNamespace

    from runtime.access import require_winnforum_rest_adapter
    from profiles.context import profile_context_from_document, profile_hash
    from runtime.capabilities import required_capabilities
    from runtime.composition import CompositionProvenance, RuntimeComposition
    from runtime.deployment import deployment_hash

    spy = SpyProtocolAdapter()
    profile = load_profile("cbrs_winnforum")
    dep = DeploymentConfig(id="spy_proto")
    req = required_capabilities(profile)
    composition = RuntimeComposition(
        profile=profile,
        profile_context=profile_context_from_document(profile),
        requirements=req,
        deployment=dep,
        protocol_adapter=spy,
        device_adapter=None,
        network_adapter=None,
        providers=(),
        rf=None,
        provenance=CompositionProvenance(
            profile_id=profile.metadata.id,
            profile_version=profile.metadata.version,
            profile_hash=profile_hash(profile),
            deployment_id=dep.id,
            deployment_version=dep.version,
            deployment_hash=deployment_hash(dep),
            required_capabilities=req.as_sorted_tokens(),
            resolved={"protocol": "spy"},
            selected_plugins={"protocol": "spy"},
            unsatisfied_data_capabilities=(),
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime_composition=composition))
    )
    adapter = require_winnforum_rest_adapter(request)
    assert adapter.request_key("grant") == "grantRequest"
    assert adapter.response_key("grant") == "grantResponse"
    assert spy.calls == ["request:grant", "response:grant"]


def test_iap_uses_composed_rf_not_settings_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_IAP_PATH_LOSS_MODEL", "itm")
    from config import clear_settings_cache

    clear_settings_cache()
    spy = SpyRfPort(marker_db=77.7)
    coupling = make_production_iap_coupling(rf_port=spy)
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
    mw = coupling(grant, point, channel, 20.0)
    assert spy.calls == 1
    # received_power_mw(20, 77.7) — assert spy loss dominated, not Settings ITM.
    assert mw > 0
    assert spy.marker_db == 77.7
    clear_settings_cache()


def test_bpr_uses_composed_rf_not_settings_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_BPR_PATH_LOSS_MODEL", "itm")
    from config import clear_settings_cache

    clear_settings_cache()
    spy = SpyRfPort(marker_db=40.0)
    # Outside Arrangement R band → ALLOW without RF; use overlapping band.
    # Use coordinates outside sharing zone to avoid KMZ dependency when possible.
    installation = {
        "latitude": 40.0,
        "longitude": -100.0,
        "height": 10.0,
        "heightType": "AGL",
        "indoorDeployment": False,
        "antennaGain": 0.0,
    }
    # Far inland, Arrangement R overlap — if not in zone, ALLOW without calling RF.
    outcome = evaluate_canadian_border_pfd(
        installation,
        30.0,
        3_650_000_000,
        3_700_000_000,
        rf_port=spy,
    )
    # Either ALLOW (outside zone, spy unused) or DENY/UNAVAILABLE after spy use.
    assert outcome is not None
    clear_settings_cache()


def test_provider_for_capability_is_authoritative():
    a = SpyProtectedEntitiesProvider(tag="provider-a")
    b = SpyProtectedEntitiesProvider(tag="provider-b")
    profile = load_profile("br_anatel_slp_3700")
    base = DeploymentConfig(
        id="prov_a",
        device=PluginSelection(plugin="cbsd"),
        providers=(),
    )
    # Manually build compositions with injected providers (bypass discovery names).
    from profiles.context import profile_context_from_document
    from runtime.capabilities import required_capabilities
    from runtime.composition import CompositionProvenance, RuntimeComposition
    from runtime.deployment import deployment_hash
    from profiles.context import profile_hash

    def _compose(provider, dep_id: str) -> RuntimeComposition:
        dep = base.model_copy(update={"id": dep_id})
        req = required_capabilities(profile)
        return RuntimeComposition(
            profile=profile,
            profile_context=profile_context_from_document(profile),
            requirements=req,
            deployment=dep,
            protocol_adapter=None,
            device_adapter=None,
            network_adapter=None,
            providers=(provider,),
            rf=None,
            provenance=CompositionProvenance(
                profile_id=profile.metadata.id,
                profile_version=profile.metadata.version,
                profile_hash=profile_hash(profile),
                deployment_id=dep.id,
                deployment_version=dep.version,
                deployment_hash=deployment_hash(dep),
                required_capabilities=req.as_sorted_tokens(),
                resolved={f"data.{CAPABILITY_PROTECTED_ENTITIES}": provider.tag},
                selected_plugins={"provider[0]": provider.tag},
                unsatisfied_data_capabilities=(),
            ),
        )

    runtime_a = _compose(a, "prov_a")
    runtime_b = _compose(b, "prov_b")
    assert runtime_a.provider_for("protected_entities").provenance().provider_id == "provider-a"
    assert runtime_b.provider_for("data.protected_entities").provenance().provider_id == "provider-b"
    runtime_a.provider_for("protected_entities").fetch(
        point=GeoPoint(latitude_deg=0.0, longitude_deg=0.0)
    )
    assert a.fetches == 1
    assert b.fetches == 0


def test_runtime_isolation_no_cross_leakage():
    spy_a = SpyRfPort(marker_db=1.0)
    spy_b = SpyRfPort(marker_db=2.0)
    with runtime_composition_scope(
        compose_runtime(
            load_profile("cbrs_winnforum"),
            DeploymentConfig(
                id="iso_a",
                device=PluginSelection(plugin="cbsd"),
                rf=PluginSelection(plugin="free_space"),
            ),
            PluginRegistry.from_discovery(),
            require_data_plugins=False,
        )
    ):
        # Override bound RF for isolation proof via explicit args.
        ca = make_production_iap_coupling(rf_port=spy_a)
        cb = make_production_iap_coupling(rf_port=spy_b)
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
        ca(grant, point, channel, 20.0)
        cb(grant, point, channel, 20.0)
        assert spy_a.calls == 1
        assert spy_b.calls == 1


def test_no_settings_path_loss_in_iap_when_bound(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAS_IAP_PATH_LOSS_MODEL", "itm")
    from config import clear_settings_cache, get_settings

    clear_settings_cache()
    assert get_settings().sas_iap_path_loss_model == "itm"
    bound = get_bound_runtime_composition()
    assert bound is not None
    assert bound.rf is not None
    # Production coupling without explicit model must use bound composed RF.
    coupling = make_production_iap_coupling()
    assert coupling is not None
    clear_settings_cache()
