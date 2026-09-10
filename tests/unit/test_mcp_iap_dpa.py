"""MCP.1 multi-constraint IAP + DPA (GAA/PAL mix, no harness hardcodes)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import Cbsd, EscSensor, Grant
from services.cpas_service import CpasSnapshot, evaluate_cpas_protections, freeze_cpas_snapshot
from services.data_injection_service import upsert_fss_record
from services.dpa_protection import DpaPathLossModel, make_path_loss_fn
from services.dpa_service import activate_dpa, clear_activations, load_dpas
from services.iap import ProtectedEntityKind, ProtectionPoint, dbm_to_mw
from services.iap.protection_points import (
    IapThresholdProfile,
    build_protection_points_from_db,
    clip_frequency_to_cbrs,
)
from services.lifecycle import GrantState
from services.mcp_protection import (
    effective_eirp_by_grant_id,
    merge_constraint_decisions,
    resolve_iap_points,
)


_SYNTH_DPA_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>McpOffshore</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
        <Data name="catA_Outdoor_NeighborhoodDistanceKm"><value>50</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>80</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>50</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -75.05,38.05,0 -75.0,38.05,0 -75.0,38.0,0 -75.05,38.0,0 -75.05,38.05,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


def _constant_coupling(mw_per_mw_eirp: float):
    def coupling(grant, point, channel, eirp_dbm_mhz):
        return dbm_to_mw(eirp_dbm_mhz) * mw_per_mw_eirp

    return coupling


def _stub_itm_fn(itm_db: float = 100.0):
    def _itm(grant, lat_rx, lon_rx, height_rx):
        return float(itm_db)

    return make_path_loss_fn(model=DpaPathLossModel.ITM_REL1EXT, itm_median_fn=_itm)


def _add_cbsd_grant(
    db: Session,
    *,
    cbsd_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    low_hz: int,
    high_hz: int,
    eirp: float,
    channel_type: str,
) -> Grant:
    cbsd = Cbsd(
        cbsd_id=cbsd_id,
        fcc_id="fcc-mcp",
        user_id="user-mcp",
        cbsd_serial_number=f"sn-{cbsd_id}",
        cbsd_category="A",
        registration_json=json.dumps(
            {
                "cbsdCategory": "A",
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": 4.0,
                    "heightType": "AGL",
                    "indoorDeployment": False,
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id=grant_id,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd_id,
        low_frequency=low_hz,
        high_frequency=high_hz,
        max_eirp=eirp,
        channel_type=channel_type,
        lifecycle_state=GrantState.AUTHORIZED.value,
        authorized=True,
        grant_expire_time=expire.replace(tzinfo=None),
        grant_json="{}",
    )
    db.add(grant)
    db.flush()
    return grant


@pytest.fixture
def synth_dpa_kml(tmp_path: Path) -> Path:
    path = tmp_path / "mcp-dpa.kml"
    path.write_text(_SYNTH_DPA_KML, encoding="utf-8")
    return path


def test_clip_frequency_to_cbrs():
    assert clip_frequency_to_cbrs(3_600_000_000, 3_800_000_000) == (
        3_600_000_000,
        3_700_000_000,
    )
    assert clip_frequency_to_cbrs(3_400_000_000, 3_500_000_000) is None


def test_zone_without_ppa_markers_is_not_iap_point():
    from services.iap.protection_points import protection_point_from_zone_payload

    assert (
        protection_point_from_zone_payload(
            {
                "record": {
                    "id": "zone/other/1",
                    "zone": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-99.0, 39.0], [-98.9, 39.0], [-98.9, 39.1], [-99.0, 39.0]]
                        ],
                    },
                }
            }
        )
        is None
    )
    ppa = protection_point_from_zone_payload(
        {
            "record": {
                "id": "zone/ppa/1",
                "usage": "PPA",
                "ppaInfo": {"palId": ["pal-x"]},
                "zone": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-99.0, 39.0], [-98.9, 39.0], [-98.9, 39.1], [-99.0, 39.0]]
                    ],
                },
            }
        },
        pal_by_id={
            "pal-x": {
                "palId": "pal-x",
                "channelAssignment": {
                    "primaryAssignment": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_560_000_000,
                    }
                },
            }
        },
    )
    assert ppa is not None
    assert ppa.entity_kind == ProtectedEntityKind.PPA


def test_default_cpas_without_entities_skips_iap_legitimately(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """No protection entities → IAP does not run (case A); no coupling probe."""
    from services.iap import coupling as coupling_mod

    calls: list[str] = []

    def _boom(*_a, **_k):
        calls.append("coupling")
        raise AssertionError("must not build coupling without IAP entities")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    grant = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-nobuild",
        grant_id="grant-mcp-nobuild",
        lat=39.0,
        lon=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        eirp=10.0,
        channel_type="GAA",
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    # Connectivity is always frozen; without sensors/zones there are no IAP points.
    assert all(k == "esc_state" for k, _r, _d in snap.protection_records)
    from services.iap.protection_points import build_protection_points_from_frozen

    assert build_protection_points_from_frozen(snap.protection_records) == []
    decisions = evaluate_cpas_protections(db_session, snap)
    assert calls == []
    assert all(d.reason != "iap" for d in decisions)
    assert grant.id in snap.active_grant_pks


def test_build_points_from_fss_and_esc(db_session: Session):
    assert upsert_fss_record(
        db_session,
        {
            "record": {
                "id": "incumbent/ibfs/mcp-fss",
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {
                            "latitude": 38.5,
                            "longitude": -97.5,
                        },
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_650_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            }
        },
    )
    db_session.add(
        EscSensor(
            record_id="esc_sensor/admin/mcp-1",
            data_json=json.dumps(
                {
                    "id": "esc_sensor/admin/mcp-1",
                    "installationParam": {
                        "latitude": 39.1,
                        "longitude": -77.1,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                    "protectionFrequencyRange": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_650_000_000,
                    },
                }
            ),
        )
    )
    db_session.commit()
    points = build_protection_points_from_db(
        db_session, profile=IapThresholdProfile(pre_iap_margin_db=0.0)
    )
    kinds = {p.entity_kind for p in points}
    assert ProtectedEntityKind.FSS_COCHANNEL in kinds
    assert ProtectedEntityKind.ESC in kinds
    fss = next(p for p in points if p.entity_kind == ProtectedEntityKind.FSS_COCHANNEL)
    assert fss.high_hz <= 3_700_000_000


def test_resolve_iap_points_explicit_overrides_db(db_session: Session):
    explicit = [
        ProtectionPoint(
            point_id="manual",
            latitude=1.0,
            longitude=2.0,
            low_hz=3_550_000_000,
            high_hz=3_555_000_000,
            threshold_dbm=-20.0,
            entity_kind=ProtectedEntityKind.GENERIC,
            pre_iap_margin_db=0.0,
        )
    ]
    assert resolve_iap_points(db_session, explicit) == explicit


def test_mcp_iap_and_dpa_joint_on_gaa_pal_mix(db_session: Session, synth_dpa_kml: Path):
    """GAA near IAP entity reduced; PAL near DPA may be moved — both constraints apply."""
    load_dpas(db_session, kml_paths=[synth_dpa_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "McpOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )

    # GAA on ESC channel (away from DPA geometry) — IAP reduce.
    gaa = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-gaa",
        grant_id="grant-mcp-gaa",
        lat=39.01,
        lon=-77.01,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
        eirp=20.0,
        channel_type="GAA",
    )
    # PAL inside DPA neighborhood — DPA movelist with strong ITM coupling.
    pal = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-pal",
        grant_id="grant-mcp-pal",
        lat=38.02,
        lon=-75.02,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        eirp=30.0,
        channel_type="PAL",
    )
    db_session.commit()

    esc_point = ProtectionPoint(
        point_id="esc-mcp",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
        threshold_dbm=-20.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    snapshot = CpasSnapshot(
        frozen_at="t",
        active_grant_pks=(gaa.id, pal.id),
    )
    decisions = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[esc_point],
        iap_coupling=_constant_coupling(1.0),
        path_loss_fn=_stub_itm_fn(80.0),
    )
    by_gid = {d.grant_id: d for d in decisions}
    assert "grant-mcp-gaa" in by_gid
    assert by_gid["grant-mcp-gaa"].reason == "iap"
    assert by_gid["grant-mcp-gaa"].action == "reduce_power"
    assert "grant-mcp-pal" in by_gid
    assert by_gid["grant-mcp-pal"].reason == "dpa_movelist"
    assert by_gid["grant-mcp-pal"].action == "terminate"
    assert {gaa.channel_type, pal.channel_type} == {"GAA", "PAL"}


def test_mcp_dpa_uses_post_iap_eirp_not_pre_iap(
    db_session: Session, synth_dpa_kml: Path
):
    """After IAP reduce, DPA must evaluate the reduced EIRP (joint satisfaction)."""
    load_dpas(db_session, kml_paths=[synth_dpa_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "McpOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    # Single grant overlapping both IAP point and DPA channel.
    grant = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-joint",
        grant_id="grant-mcp-joint",
        lat=38.02,
        lon=-75.02,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        eirp=37.0,
        channel_type="GAA",
    )
    db_session.commit()

    # IAP with tiny coupling → keep (no reduce). DPA with low path loss → move.
    point_keep = ProtectionPoint(
        point_id="iap-weak",
        latitude=38.025,
        longitude=-75.025,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=40.0,
        entity_kind=ProtectedEntityKind.GENERIC,
        pre_iap_margin_db=0.0,
    )
    snapshot = CpasSnapshot(frozen_at="t", active_grant_pks=(grant.id,))
    decisions_move = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[point_keep],
        iap_coupling=_constant_coupling(1e-18),
        path_loss_fn=_stub_itm_fn(70.0),
    )
    assert any(
        d.grant_id == "grant-mcp-joint" and d.reason == "dpa_movelist"
        for d in decisions_move
    )

    # Strong IAP coupling forces deep reduce; with high path loss, reduced EIRP
    # may leave aggregate under DPA threshold — then no dpa_movelist terminate.
    point_hard = ProtectionPoint(
        point_id="iap-hard",
        latitude=38.025,
        longitude=-75.025,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-40.0,
        entity_kind=ProtectedEntityKind.GENERIC,
        pre_iap_margin_db=0.0,
    )
    decisions_joint = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[point_hard],
        iap_coupling=_constant_coupling(1.0),
        path_loss_fn=_stub_itm_fn(160.0),
    )
    joint = {d.grant_id: d for d in decisions_joint}
    assert "grant-mcp-joint" in joint
    assert joint["grant-mcp-joint"].reason == "iap"
    assert joint["grant-mcp-joint"].action == "reduce_power"
    # Effective EIRP recorded for DPA overlay
    eirp_map = effective_eirp_by_grant_id(decisions_joint)
    assert eirp_map["grant-mcp-joint"] < 37.0


def test_iap_points_without_coupling_fail_closed(
    db_session: Session, synth_dpa_kml: Path, monkeypatch: pytest.MonkeyPatch
):
    """Explicit points without coupling and unavailable production backend → error."""
    from services.cpas_service import CpasRfEvaluationError
    from services.iap import coupling as coupling_mod
    from services.propagation.errors import PropagationUnavailableError

    load_dpas(db_session, kml_paths=[synth_dpa_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "McpOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    grant = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-nocouple",
        grant_id="grant-mcp-nocouple",
        lat=38.02,
        lon=-75.02,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        eirp=30.0,
        channel_type="GAA",
    )
    db_session.commit()
    point = ProtectionPoint(
        point_id="esc-x",
        latitude=38.025,
        longitude=-75.025,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-20.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )

    def _no_coupling(*_a, **_k):
        raise PropagationUnavailableError("ITM missing")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _no_coupling)
    with pytest.raises(CpasRfEvaluationError, match="coupling unavailable"):
        evaluate_cpas_protections(
            db_session,
            CpasSnapshot(frozen_at="t", active_grant_pks=(grant.id,)),
            iap_points=[point],
            iap_coupling=None,
            path_loss_fn=_stub_itm_fn(80.0),
        )


def test_merge_terminate_overrides_reduce():
    from services.cpas_service import CpasDecision

    peer: list[CpasDecision] = []
    iap = [
        CpasDecision(
            grant_pk=1,
            grant_id="g1",
            cbsd_id="c1",
            reason="iap",
            action="reduce_power",
            authorized_eirp_dbm_mhz=-10.0,
            explanation="iap",
        )
    ]
    dpa = [
        CpasDecision(
            grant_pk=1,
            grant_id="g1",
            cbsd_id="c1",
            reason="dpa_movelist",
            action="terminate",
            explanation="dpa_movelist",
        )
    ]
    merged = merge_constraint_decisions(peer, iap, dpa)
    assert len(merged) == 1
    assert merged[0].action == "terminate"
    assert merged[0].reason == "dpa_movelist"


def test_mcp_dpa_uses_frozen_local_pks_not_live_n_plus_one(
    db_session: Session, synth_dpa_kml: Path, monkeypatch: pytest.MonkeyPatch
):
    """A frozen at N; B inserted mid-run must not enter DPA/IAP of that execution."""
    from services.cpas_service import freeze_cpas_snapshot
    from services import dpa_protection as dpa_mod

    load_dpas(db_session, kml_paths=[synth_dpa_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "McpOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    grant_a = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-freeze-a",
        grant_id="grant-mcp-freeze-a",
        lat=38.02,
        lon=-75.02,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        eirp=30.0,
        channel_type="GAA",
    )
    db_session.commit()
    snapshot_n = freeze_cpas_snapshot(db_session)
    assert grant_a.id in snapshot_n.active_grant_pks

    grant_b = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-freeze-b",
        grant_id="grant-mcp-freeze-b",
        lat=38.021,
        lon=-75.021,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        eirp=37.0,
        channel_type="PAL",
    )
    db_session.commit()
    assert grant_b.id not in snapshot_n.active_grant_pks

    seen: dict[str, object] = {}
    real_refresh = dpa_mod.refresh_activation_movelists

    def _spy_refresh(db, **kwargs):
        seen["grant_pks"] = kwargs.get("grant_pks")
        seen["local_grants"] = kwargs.get("local_grants")
        return real_refresh(db, **kwargs)

    monkeypatch.setattr(dpa_mod, "refresh_activation_movelists", _spy_refresh)

    esc_point = ProtectionPoint(
        point_id="esc-freeze",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
        threshold_dbm=40.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    # Grant A is on DPA channel; IAP point is elsewhere → DPA-only for A.
    decisions_n = evaluate_cpas_protections(
        db_session,
        snapshot_n,
        iap_points=[esc_point],
        iap_coupling=_constant_coupling(1e-18),
        path_loss_fn=_stub_itm_fn(80.0),
    )
    local_ids = {g.grant_id for g in (seen.get("local_grants") or [])}
    assert "grant-mcp-freeze-a" in local_ids
    assert "grant-mcp-freeze-b" not in local_ids
    decided_ids = {d.grant_id for d in decisions_n}
    assert "grant-mcp-freeze-b" not in decided_ids
    assert "grant-mcp-freeze-a" in decided_ids
    assert all(d.grant_pk != grant_b.id for d in decisions_n)

    # Same local set for IAP path: only frozen PKs are loaded into RF grants.
    iap_seen: list[str] = []
    cpas_mod = __import__("services.cpas_service", fromlist=["_evaluate_iap_decisions_from_frozen"])
    real_iap = cpas_mod._evaluate_iap_decisions_from_frozen

    def _spy_iap(local_grants, **kwargs):
        iap_seen.extend(g.grant_id for g in local_grants)
        return real_iap(local_grants, **kwargs)

    monkeypatch.setattr(cpas_mod, "_evaluate_iap_decisions_from_frozen", _spy_iap)
    evaluate_cpas_protections(
        db_session,
        snapshot_n,
        iap_points=[
            ProtectionPoint(
                point_id="iap-a",
                latitude=38.025,
                longitude=-75.025,
                low_hz=3_550_000_000,
                high_hz=3_560_000_000,
                threshold_dbm=40.0,
                pre_iap_margin_db=0.0,
            )
        ],
        iap_coupling=_constant_coupling(1e-18),
        path_loss_fn=_stub_itm_fn(160.0),
    )
    assert iap_seen == ["grant-mcp-freeze-a"]

    # Next freeze (N+1) includes B.
    snap_n1 = freeze_cpas_snapshot(db_session)
    assert grant_b.id in snap_n1.active_grant_pks
    decisions_n1 = evaluate_cpas_protections(
        db_session,
        snap_n1,
        iap_points=[esc_point],
        iap_coupling=_constant_coupling(1e-18),
        path_loss_fn=_stub_itm_fn(80.0),
    )
    assert {d.grant_id for d in decisions_n1} >= {
        "grant-mcp-freeze-a",
        "grant-mcp-freeze-b",
    }


def test_mcp_peer_grants_contribute_but_are_not_mutated(
    db_session: Session, synth_dpa_kml: Path
):
    from models.models import PeerFadRecord, PeerSas
    from services.cpas_service import (
        _frozen_peer_cbsd_rows,
        apply_cpas_decisions,
        freeze_cpas_snapshot,
    )
    from services.dpa_protection import dpa_grants_from_frozen_peer_cbsds
    from services.iap.peer_fad import peer_grant_rf_id

    load_dpas(db_session, kml_paths=[synth_dpa_kml])
    clear_activations(db_session)
    activate_dpa(
        db_session,
        {
            "dpaId": "McpOffshore",
            "frequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    )
    local = _add_cbsd_grant(
        db_session,
        cbsd_id="cbsd-mcp-peerlocal",
        grant_id="grant-mcp-peerlocal",
        lat=38.02,
        lon=-75.02,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        eirp=10.0,
        channel_type="GAA",
    )
    peer = PeerSas(certificate_hash="peer-mcp-dpa", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="peer-cbsd-mcp",
            data_json=json.dumps(
                {
                    "id": "peer-cbsd-mcp",
                    "installationParam": {
                        "latitude": 38.021,
                        "longitude": -75.021,
                        "height": 4.0,
                        "heightType": "AGL",
                        "indoorDeployment": False,
                    },
                    "grants": [
                        {
                            "id": "peer-g-mcp",
                            "terminated": False,
                            "operationParam": {
                                "maxEirp": 37.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_550_000_000,
                                    "highFrequency": 3_560_000_000,
                                },
                            },
                        }
                    ],
                }
            ),
        )
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    peer_rf = dpa_grants_from_frozen_peer_cbsds(_frozen_peer_cbsd_rows(snap))
    assert any(not g.is_managing_sas for g in peer_rf)
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-mcp")
    assert any(g.grant_id == peer_gid for g in peer_rf)

    decisions = evaluate_cpas_protections(
        db_session,
        snap,
        path_loss_fn=_stub_itm_fn(80.0),
    )
    assert all(d.grant_pk is not None for d in decisions)
    assert all(d.grant_id != peer_gid for d in decisions)
    apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    # Peer FAD row unchanged (no local mutation of peer grants).
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-mcp")
        .one()
    )
    assert "peer-g-mcp" in row.data_json
    db_session.refresh(local)
    # Local may be terminated via dpa_movelist; peer never is.
    assert local.grant_id == "grant-mcp-peerlocal"
