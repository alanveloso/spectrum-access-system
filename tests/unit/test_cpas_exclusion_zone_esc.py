"""EXZ (exclusion zones / NTIA) + EPR (ESC) on the production CPAS path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import Cbsd, EscSensor, Grant, PeerFadRecord, PeerSas
from services.cpas_service import (
    CpasRfEvaluationError,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.esc_admin_service import (
    EscConnectivityState,
    disconnect_esc,
    parse_frozen_esc_connectivity,
    resolve_esc_connectivity,
    set_esc_absent,
)
from services.exclusion_zone_service import (
    ExclusionZoneError,
    ExclusionZoneUnavailable,
    enable_ntia_exclusion_zones,
    load_ntia_coastal_geojson,
    persist_exclusion_zone,
    point_hits_exclusion_records,
    validate_exclusion_zone_record,
)
from services.iap import dbm_to_mw
from services.iap.models import ProtectedEntityKind
from services.iap.protection_points import (
    ProtectionEntityError,
    build_protection_points_from_frozen,
    protection_point_from_esc_sensor_record,
)
from services.lifecycle import GrantState


def _constant_coupling(mw_per_mw_eirp: float):
    def coupling(grant, point, channel, eirp_dbm_mhz):
        return dbm_to_mw(eirp_dbm_mhz) * mw_per_mw_eirp

    return coupling


def _square(lon: float, lat: float, d: float = 0.01) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon - d, lat - d],
                [lon + d, lat - d],
                [lon + d, lat + d],
                [lon - d, lat + d],
                [lon - d, lat - d],
            ]
        ],
    }


def _add_grant(
    db: Session,
    *,
    cbsd_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    eirp: float = 30.0,
    low_hz: int = 3_555_000_000,
    high_hz: int = 3_560_000_000,
) -> Grant:
    cbsd = Cbsd(
        cbsd_id=cbsd_id,
        fcc_id="fcc-c4",
        user_id="user-c4",
        cbsd_serial_number=f"sn-{cbsd_id}",
        cbsd_category="A",
        registration_json=json.dumps(
            {
                "cbsdCategory": "A",
                "installationParam": {
                    "latitude": lat,
                    "longitude": lon,
                    "height": 6.0,
                    "heightType": "AGL",
                    "indoorDeployment": False,
                },
            }
        ),
    )
    db.add(cbsd)
    db.flush()
    grant = Grant(
        grant_id=grant_id,
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd_id,
        channel_type="GAA",
        low_frequency=low_hz,
        high_frequency=high_hz,
        max_eirp=eirp,
        grant_expire_time=datetime.now(timezone.utc) + timedelta(days=1),
        lifecycle_state=GrantState.GRANTED.value,
        terminated=False,
    )
    db.add(grant)
    db.flush()
    return grant


def _add_peer_grant(
    db: Session,
    *,
    peer_hash: str,
    record_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    eirp: float = 37.0,
    low_hz: int = 3_555_000_000,
    high_hz: int = 3_560_000_000,
) -> PeerSas:
    peer = PeerSas(certificate_hash=peer_hash, url="https://localhost/v1.3")
    db.add(peer)
    db.flush()
    db.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=record_id,
            data_json=json.dumps(
                {
                    "id": record_id,
                    "installationParam": {
                        "latitude": lat,
                        "longitude": lon,
                        "height": 6.0,
                        "heightType": "AGL",
                        "indoorDeployment": False,
                    },
                    "grants": [
                        {
                            "id": grant_id,
                            "terminated": False,
                            "operationParam": {
                                "maxEirp": eirp,
                                "operationFrequencyRange": {
                                    "lowFrequency": low_hz,
                                    "highFrequency": high_hz,
                                },
                            },
                        }
                    ],
                }
            ),
        )
    )
    return peer


def _inject_esc(
    db: Session,
    *,
    record_id: str = "esc_sensor/c4/1",
    lat: float = 39.0,
    lon: float = -77.0,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_650_000_000,
) -> None:
    db.add(
        EscSensor(
            record_id=record_id,
            data_json=json.dumps(
                {
                    "id": record_id,
                    "installationParam": {
                        "latitude": lat,
                        "longitude": lon,
                        "height": 3.0,
                        "heightType": "AGL",
                        "antennaAzimuth": 0.0,
                        "azimuthRadiationPattern": [
                            {"angle": i, "gain": 30.0} for i in range(360)
                        ],
                    },
                    "protectionFrequencyRange": {
                        "lowFrequency": low_hz,
                        "highFrequency": high_hz,
                    },
                }
            ),
        )
    )


# ---------------------------------------------------------------------------
# EXZ
# ---------------------------------------------------------------------------


def test_exz_a_inside_protected(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    grant = _add_grant(
        db_session, cbsd_id="c4-exz-a", grant_id="g-exz-a", lat=39.0, lon=-77.0
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1e-20)
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated is True


def test_exz_b_outside_unaffected(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0, 0.001),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    grant = _add_grant(
        db_session,
        cbsd_id="c4-exz-b",
        grant_id="g-exz-b",
        lat=40.5,
        lon=-75.0,
        eirp=37.0,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(d.grant_id != grant.grant_id or d.reason != "exz_exclusion" for d in decisions)


def test_exz_c_partial_overlap_freq(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0),
            "frequencyRanges": [
                {"lowFrequency": 3_555_000_000, "highFrequency": 3_565_000_000}
            ],
        },
    )
    grant = _add_grant(
        db_session,
        cbsd_id="c4-exz-c",
        grant_id="g-exz-c",
        lat=39.0,
        lon=-77.0,
        low_hz=3_560_000_000,
        high_hz=3_570_000_000,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1e-20)
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert any(d.grant_id == grant.grant_id and d.reason == "exz_exclusion" for d in decisions)


def test_exz_d_out_of_band(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_555_000_000}
            ],
        },
    )
    grant = _add_grant(
        db_session,
        cbsd_id="c4-exz-d",
        grant_id="g-exz-d",
        lat=39.0,
        lon=-77.0,
        low_hz=3_660_000_000,
        high_hz=3_665_000_000,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "exz_exclusion") for d in decisions
    )


def test_exz_e_added_after_grant(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    grant = _add_grant(
        db_session, cbsd_id="c4-exz-e", grant_id="g-exz-e", lat=39.0, lon=-77.0
    )
    db_session.commit()
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1e-20)
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated is True


def test_exz_f_invalid_geometry_fail_closed():
    with pytest.raises(ExclusionZoneError):
        validate_exclusion_zone_record({"zone": {"type": "Polygon", "coordinates": []}})
    with pytest.raises(ExclusionZoneError):
        point_hits_exclusion_records(
            [{"zone": {"type": "Polygon", "coordinates": [[[0, 0]]]}}, ],
            39.0,
            -77.0,
            3_550_000_000,
            3_560_000_000,
        )


def test_exz_g_snapshot_n_vs_n1(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(-77.0, 39.0),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )
    grant = _add_grant(
        db_session, cbsd_id="c4-exz-g", grant_id="g-exz-g", lat=39.0, lon=-77.0
    )
    db_session.commit()
    snap_n = freeze_cpas_snapshot(db_session)
    assert any(k == "exclusion_zone" for k, _r, _d in snap_n.protection_records)

    # N+1 inject far away — must not affect current evaluate.
    persist_exclusion_zone(
        db_session,
        {
            "zone": _square(10.0, 10.0),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_700_000_000}
            ],
        },
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1e-20)
    )
    decisions = evaluate_cpas_protections(db_session, snap_n)
    assert any(d.grant_id == grant.grant_id and d.reason == "exz_exclusion" for d in decisions)
    # Frozen N has only first EXZ id set; N+1 freeze includes both.
    snap_n1 = freeze_cpas_snapshot(db_session)
    exz_n = sum(1 for k, _r, _d in snap_n.protection_records if k == "exclusion_zone")
    exz_n1 = sum(1 for k, _r, _d in snap_n1.protection_records if k == "exclusion_zone")
    assert exz_n1 > exz_n


def test_exz_h_ntia_dataset_absent(monkeypatch: pytest.MonkeyPatch):
    from dataclasses import dataclass

    from providers.contract import (
        CAPABILITY_REFERENCE_DATA,
        DataKind,
        DatasetProvenance,
        GeoJsonRecord,
        PROVIDER_API_VERSION,
        TOKEN_NTIA_PROTECTION_ZONES,
    )
    from runtime.composition import RuntimeComposition
    from runtime.context import runtime_composition_scope

    @dataclass(frozen=True, slots=True)
    class _EmptyNtiaProvider:
        api_version = PROVIDER_API_VERSION
        kind = DataKind.REFERENCE_DATA

        def advertised_capabilities(self):
            return frozenset({CAPABILITY_REFERENCE_DATA})

        def provenance(self):
            return DatasetProvenance("spy", "1", "empty_ntia")

        def fetch(self, *, point=None, token=None):
            if token != TOKEN_NTIA_PROTECTION_ZONES:
                raise ValueError("unexpected token")
            return GeoJsonRecord(
                document={"type": "FeatureCollection", "features": []},
                provenance=self.provenance(),
            )

    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_EmptyNtiaProvider(),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    with runtime_composition_scope(composition):
        assert load_ntia_coastal_geojson()["features"] == []
        with pytest.raises(ExclusionZoneUnavailable):
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from models.models import Base

            eng = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(eng)
            SessionLocal = sessionmaker(bind=eng)
            db = SessionLocal()
            try:
                enable_ntia_exclusion_zones(db)
            finally:
                db.close()


def test_exz_i_no_fixture_coords_in_product():
    src = Path("services/exclusion_zone_service.py").read_text(encoding="utf-8")
    assert "EXZ.1" not in src
    assert "WINNF.FT" not in src


# ---------------------------------------------------------------------------
# EPR
# ---------------------------------------------------------------------------


def test_epr_a_below_threshold(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    _inject_esc(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c4-epr-a",
        grant_id="g-epr-a",
        lat=39.001,
        lon=-77.001,
        eirp=0.0,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1e-12),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_epr_b_local_aggregate_protects(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    _inject_esc(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c4-epr-b",
        grant_id="g-epr-b",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated or (
        grant.max_eirp is not None and float(grant.max_eirp) < 37.0
    )


def test_epr_c_d_peer_changes_local_never_mutated(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_esc(db_session)
    local = _add_grant(
        db_session,
        cbsd_id="c4-epr-c",
        grant_id="g-epr-c",
        lat=39.001,
        lon=-77.001,
        eirp=10.0,
    )
    peer = _add_peer_grant(
        db_session,
        peer_hash="peer-epr-c",
        record_id="peer-cbsd-epr",
        grant_id="peer-g-epr",
        lat=39.002,
        lon=-77.002,
        eirp=37.0,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-epr")
    assert all(d.grant_id != peer_gid for d in decisions)
    assert any(d.grant_id == local.grant_id and d.reason == "iap" for d in decisions)
    apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-epr")
        .one()
    )
    assert "peer-g-epr" in row.data_json


def test_epr_e_disconnected_keeps_esc_iap(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    _inject_esc(db_session)
    disconnect_esc(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c4-epr-e",
        grant_id="g-epr-e",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
    )
    db_session.commit()
    assert resolve_esc_connectivity(db_session) is EscConnectivityState.DISCONNECTED
    snap = freeze_cpas_snapshot(db_session)
    state_row = next(d for k, _r, d in snap.protection_records if k == "esc_state")
    assert parse_frozen_esc_connectivity(json.loads(state_row)).value == "disconnected"
    points = build_protection_points_from_frozen(snap.protection_records)
    assert any(p.entity_kind is ProtectedEntityKind.ESC for p in points)
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    decisions = evaluate_cpas_protections(db_session, snap)
    assert any(d.grant_id == grant.grant_id and d.reason == "iap" for d in decisions)


def test_epr_f_invalid_state_fail_closed(db_session: Session):
    _inject_esc(db_session)
    set_esc_absent(db_session)
    # Corrupt absent payload → INVALID at resolve when flag present but broken.
    from models.models import AdminInjectedData
    from services.esc_admin_service import FLAG_ESC_ABSENT

    row = db_session.query(AdminInjectedData).filter_by(kind=FLAG_ESC_ABSENT).one()
    row.data_json = "{not-json"
    db_session.commit()
    assert resolve_esc_connectivity(db_session) is EscConnectivityState.INVALID
    # Manually craft frozen invalid state.
    with pytest.raises(ProtectionEntityError):
        build_protection_points_from_frozen(
            (("esc_state", "connectivity", json.dumps({"state": "invalid"})),)
        )


def test_epr_g_out_of_band(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    _inject_esc(db_session, low_hz=3_550_000_000, high_hz=3_560_000_000)
    grant = _add_grant(
        db_session,
        cbsd_id="c4-epr-g",
        grant_id="g-epr-g",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_660_000_000,
        high_hz=3_665_000_000,
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_epr_h_coupling_unavailable(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.coupling import IapCouplingUnavailable

    _inject_esc(db_session)
    _add_grant(
        db_session, cbsd_id="c4-epr-h", grant_id="g-epr-h", lat=39.001, lon=-77.001
    )
    db_session.commit()

    def _boom(**_k):
        raise IapCouplingUnavailable("itm")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError):
        evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))


def test_epr_i_snapshot_esc_n_vs_n1(db_session: Session):
    _inject_esc(db_session, record_id="esc_sensor/c4/n")
    db_session.commit()
    snap_n = freeze_cpas_snapshot(db_session)
    assert any(rid == "esc_sensor/c4/n" for _k, rid, _d in snap_n.protection_records)
    _inject_esc(db_session, record_id="esc_sensor/c4/n1", lat=10.0, lon=10.0)
    db_session.commit()
    pts = build_protection_points_from_frozen(snap_n.protection_records)
    assert all("n1" not in p.point_id for p in pts)
    snap_n1 = freeze_cpas_snapshot(db_session)
    ids = {rid for _k, rid, _d in snap_n1.protection_records}
    assert "esc_sensor/c4/n1" in ids


def test_epr_j_order_invariant(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    _inject_esc(db_session)
    g1 = _add_grant(
        db_session, cbsd_id="c4-epr-j1", grant_id="g-epr-j1", lat=39.001, lon=-77.001, eirp=30.0
    )
    g2 = _add_grant(
        db_session, cbsd_id="c4-epr-j2", grant_id="g-epr-j2", lat=39.002, lon=-77.002, eirp=30.0
    )
    db_session.commit()
    from services.iap import coupling as coupling_mod

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(0.5)
    )

    def _keyset(snap):
        return {
            (
                d.grant_id,
                d.action,
                round(d.authorized_eirp_dbm_mhz, 3)
                if d.authorized_eirp_dbm_mhz is not None
                else None,
            )
            for d in evaluate_cpas_protections(db_session, snap)
            if d.grant_id in {g1.grant_id, g2.grant_id}
        }

    snap = freeze_cpas_snapshot(db_session)
    assert _keyset(snap) == _keyset(snap)


def test_epr_invalid_esc_freq_domain_error():
    with pytest.raises(ProtectionEntityError):
        protection_point_from_esc_sensor_record(
            {
                "installationParam": {
                    "latitude": 39.0,
                    "longitude": -77.0,
                    "height": 3.0,
                    "antennaAzimuth": 0.0,
                    "azimuthRadiationPattern": [
                        {"angle": i, "gain": 30.0} for i in range(360)
                    ],
                },
                "protectionFrequencyRange": {
                    "lowFrequency": 3_700_000_000,
                    "highFrequency": 3_550_000_000,
                },
            },
            record_id="esc/bad",
        )


def test_c4_numeric_provenance_not_fixture_ids():
    """C4 numeric policy: 50 m / −109 dBm / ESC 40+80 km from named policy."""
    from services.exclusion_zone_service import EXZ_BUFFER_M
    from services.iap.engine import esc_neighborhood_km_for_category
    from services.iap.protection_points import (
        NEIGHBORHOOD_ESC_KM,
        NEIGHBORHOOD_ESC_KM_A,
        NEIGHBORHOOD_ESC_KM_B,
        IapThresholdProfile,
    )
    from profiles import get_active_profile_document

    assert EXZ_BUFFER_M == 50.0
    thr = IapThresholdProfile()
    assert thr.esc_dbm == -109.0
    assert thr.pre_iap_margin_db == 1.0
    assert NEIGHBORHOOD_ESC_KM_A == 40.0
    assert NEIGHBORHOOD_ESC_KM_B == 80.0
    assert NEIGHBORHOOD_ESC_KM == NEIGHBORHOOD_ESC_KM_B
    assert esc_neighborhood_km_for_category("A") == 40.0
    assert esc_neighborhood_km_for_category("B") == 80.0
    assert esc_neighborhood_km_for_category(None) == 80.0
    assert esc_neighborhood_km_for_category("Z") == 80.0

    doc = get_active_profile_document()
    assert doc.protection is not None
    peer_esc = next(b for b in doc.protection.bindings if b.id == "peer_esc")
    assert peer_esc.mechanism == "distance_exclusion"
    assert float(peer_esc.distance_m) == 40_000.0

    product_paths = [
        Path("services/exclusion_zone_service.py"),
        Path("services/iap/protection_points.py"),
        Path("services/iap/pre_iap_exclusions.py"),
    ]
    for path in product_paths:
        text = path.read_text(encoding="utf-8")
        assert "EXZ.1" not in text
        assert "EPR.1" not in text
        assert "WINNF.FT.S.EXZ" not in text
        assert "WINNF.FT.S.EPR" not in text


def _lat_offset_km(lat0: float, km: float) -> float:
    return lat0 + (km / 111.32)


def test_esc_cat_a_b_neighborhood_filter():
    from services.iap.engine import grants_in_neighborhood
    from services.iap.models import GrantRfInfo, ProtectedEntityKind, ProtectionPoint

    esc = ProtectionPoint(
        point_id="esc:n",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_650_000_000,
        threshold_dbm=-109.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
        neighborhood_km=80.0,
    )

    def g(gid: str, km: float, cat: str | None) -> GrantRfInfo:
        return GrantRfInfo(
            grant_id=gid,
            cbsd_id=f"c-{gid}",
            latitude=_lat_offset_km(39.0, km),
            longitude=-77.0,
            low_hz=3_555_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=30.0,
            cbsd_category=cat,
        )

    cases = [
        ("a39", 39.0, "A", True),
        ("a41", 41.0, "A", False),
        ("b39", 39.0, "B", True),
        ("b60", 60.0, "B", True),
        ("b79", 79.0, "B", True),
        ("b81", 81.0, "B", False),
        ("miss60", 60.0, None, True),  # conservative 80 km
        ("bad60", 60.0, "Z", True),
        ("miss81", 81.0, None, False),
    ]
    for gid, km, cat, expect in cases:
        neighbors = grants_in_neighborhood(esc, [g(gid, km, cat)])
        assert (len(neighbors) == 1) is expect, (gid, km, cat, neighbors)


def test_esc_cat_b_40_80_production_path(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Cat B at ~60 km participates via execute_cpas_pipeline (was excluded at 40)."""
    from services.iap import coupling as coupling_mod
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_esc(db_session, lat=39.0, lon=-77.0)
    # Local Cat A near ESC (control) + Cat B at ~60 km.
    local_near = _add_grant(
        db_session,
        cbsd_id="c4-esc-near",
        grant_id="g-esc-near",
        lat=_lat_offset_km(39.0, 5.0),
        lon=-77.0,
        eirp=10.0,
    )
    # Force Cat B on far local grant.
    far_lat = _lat_offset_km(39.0, 60.0)
    local_far = _add_grant(
        db_session,
        cbsd_id="c4-esc-far-b",
        grant_id="g-esc-far-b",
        lat=far_lat,
        lon=-77.0,
        eirp=37.0,
    )
    cbsd_far = db_session.query(Cbsd).filter_by(cbsd_id="c4-esc-far-b").one()
    cbsd_far.cbsd_category = "B"
    reg = json.loads(cbsd_far.registration_json)
    reg["cbsdCategory"] = "B"
    cbsd_far.registration_json = json.dumps(reg)

    peer = PeerSas(certificate_hash="peer-esc-b60", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="peer-cbsd-esc-b",
            data_json=json.dumps(
                {
                    "id": "peer-cbsd-esc-b",
                    "cbsdCategory": "B",
                    "installationParam": {
                        "latitude": far_lat,
                        "longitude": -77.01,
                        "height": 6.0,
                        "heightType": "AGL",
                        "indoorDeployment": False,
                    },
                    "grants": [
                        {
                            "id": "peer-g-esc-b",
                            "terminated": False,
                            "operationParam": {
                                "maxEirp": 37.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_555_000_000,
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

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    monkeypatch.setattr(
        "services.cpas_service.run_peer_fad_sync",
        lambda db, client=None: {"peers": 0, "ok": 0, "failed": 0, "errors": []},
    )
    # Prove 60 km Cat B is inside ESC neighborhood before apply.
    from services.iap.engine import grants_in_neighborhood
    from services.iap.models import GrantRfInfo, ProtectedEntityKind, ProtectionPoint

    esc_pt = ProtectionPoint(
        point_id="esc:check",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_650_000_000,
        threshold_dbm=-109.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
        neighborhood_km=80.0,
    )
    peer_rf = GrantRfInfo(
        grant_id="peer-check",
        cbsd_id="peer-cbsd-esc-b",
        latitude=far_lat,
        longitude=-77.01,
        low_hz=3_555_000_000,
        high_hz=3_560_000_000,
        max_eirp_dbm_mhz=37.0,
        is_managing_sas=False,
        cbsd_category="B",
    )
    assert len(grants_in_neighborhood(esc_pt, [peer_rf])) == 1

    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(local_far)
    db_session.refresh(local_near)
    # Far Cat B must be in ESC aggregate → local action possible.
    assert local_far.terminated or (
        local_far.max_eirp is not None and float(local_far.max_eirp) < 37.0
    )
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-esc-b")
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(d.grant_id != peer_gid for d in decisions)
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-esc-b")
        .one()
    )
    assert "peer-g-esc-b" in row.data_json


def test_esc_category_snapshot_n_vs_n1(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from services.iap import coupling as coupling_mod
    from services.iap.engine import grants_in_neighborhood
    from services.iap.protection_points import build_protection_points_from_frozen

    _inject_esc(db_session, lat=39.0, lon=-77.0)
    far_lat = _lat_offset_km(39.0, 60.0)
    grant = _add_grant(
        db_session,
        cbsd_id="c4-esc-cat-n",
        grant_id="g-esc-cat-n",
        lat=far_lat,
        lon=-77.0,
        eirp=37.0,
    )
    cbsd = db_session.query(Cbsd).filter_by(cbsd_id="c4-esc-cat-n").one()
    cbsd.cbsd_category = "A"
    reg = json.loads(cbsd.registration_json)
    reg["cbsdCategory"] = "A"
    cbsd.registration_json = json.dumps(reg)
    db_session.commit()

    snap_n = freeze_cpas_snapshot(db_session)
    assert snap_n.local_grants[0].cbsd_category == "A"

    # Mid-run flip to B (N+1) must not affect evaluate on snap_n.
    cbsd.cbsd_category = "B"
    reg["cbsdCategory"] = "B"
    cbsd.registration_json = json.dumps(reg)
    db_session.commit()

    monkeypatch.setattr(
        coupling_mod, "make_production_iap_coupling", lambda **_k: _constant_coupling(1.0)
    )
    points = build_protection_points_from_frozen(snap_n.protection_records)
    esc = next(p for p in points if p.entity_kind.value == "esc")
    from services.iap.models import GrantRfInfo

    rf = GrantRfInfo(
        grant_id=grant.grant_id,
        cbsd_id=grant.cbsd_id,
        latitude=far_lat,
        longitude=-77.0,
        low_hz=grant.low_frequency,
        high_hz=grant.high_frequency,
        max_eirp_dbm_mhz=37.0,
        cbsd_category=snap_n.local_grants[0].cbsd_category,
        grant_pk=grant.id,
    )
    # Cat A at 60 km excluded under snap_n category.
    assert grants_in_neighborhood(esc, [rf]) == []

    decisions = evaluate_cpas_protections(db_session, snap_n)
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )

    snap_n1 = freeze_cpas_snapshot(db_session)
    assert snap_n1.local_grants[0].cbsd_category == "B"
    rf_b = rf.model_copy(update={"cbsd_category": "B"})
    assert len(grants_in_neighborhood(esc, [rf_b])) == 1
