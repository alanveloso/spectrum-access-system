"""GPR (GWPZ), PPR (PPA), FPR (FSS/GWBL) on the production CPAS/IAP path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from models.models import AdminInjectedData, Cbsd, Grant, PeerFadRecord, PeerSas
from services.cpas_service import (
    CpasRfEvaluationError,
    apply_cpas_decisions,
    evaluate_cpas_protections,
    execute_cpas_pipeline,
    freeze_cpas_snapshot,
)
from services.data_injection_service import (
    persist_zone_data,
    upsert_fss_record,
    upsert_wisp_record,
)
from services.iap import dbm_to_mw
from services.iap.models import ProtectedEntityKind
from services.iap.protection_points import (
    ProtectionEntityError,
    build_protection_points_from_frozen,
    parse_fss_ttc,
    protection_point_from_zone_payload,
    protection_points_from_fss_payload,
)
from services.lifecycle import GrantState
from services.pal_service import upsert_pal_record


def _constant_coupling(mw_per_mw_eirp: float):
    def coupling(grant, point, channel, eirp_dbm_mhz):
        return dbm_to_mw(eirp_dbm_mhz) * mw_per_mw_eirp

    return coupling


def _square(lon: float, lat: float, d: float = 0.05) -> dict:
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
    low_hz: int = 3_655_000_000,
    high_hz: int = 3_660_000_000,
) -> Grant:
    cbsd = Cbsd(
        cbsd_id=cbsd_id,
        fcc_id="fcc-c3",
        user_id="user-c3",
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
    low_hz: int = 3_655_000_000,
    high_hz: int = 3_660_000_000,
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


def _inject_gwpz(
    db: Session,
    *,
    wisp_id: str = "wisp/c3-gwpz",
    lat: float = 39.05,
    lon: float = -77.0,
    low_hz: int = 3_650_000_000,
    high_hz: int = 3_700_000_000,
    zone_half_deg: float = 0.002,
) -> None:
    """Inject GWPZ whose polygon does not contain typical test grants at ~39.001.

    Grants remain inside the 40 km IAP neighborhood of the zone representative
    point so aggregate protection (not EZ purge) is exercised.
    """
    assert upsert_wisp_record(
        db,
        {
            "record": {
                "id": wisp_id,
                "type": "GWPZ",
                "deploymentParam": [
                    {
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": low_hz,
                                "highFrequency": high_hz,
                            }
                        }
                    }
                ],
            },
            "zone": _square(lon, lat, zone_half_deg),
        },
    )


def _inject_ppa_with_pal(
    db: Session,
    *,
    zone_id: str = "zone/c3-ppa",
    pal_id: str = "pal-c3",
    lat: float = 39.0,
    lon: float = -77.0,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_560_000_000,
) -> None:
    upsert_pal_record(
        db,
        {
            "palId": pal_id,
            "userId": "user-c3",
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": low_hz,
                    "highFrequency": high_hz,
                }
            },
        },
    )
    persist_zone_data(
        db,
        {
            "record": {
                "id": zone_id,
                "type": "PPA",
                "usage": "PPA",
                "ppaInfo": {"palId": [pal_id], "cbsdReferenceId": []},
                "zone": _square(lon, lat),
            }
        },
    )


def _inject_fss(
    db: Session,
    *,
    fss_id: str = "fss/c3-1",
    lat: float = 39.0,
    lon: float = -77.0,
    low_hz: int = 3_600_000_000,
    high_hz: int = 4_200_000_000,
    ttc: bool | None = False,
) -> None:
    body: dict = {
        "record": {
            "id": fss_id,
            "type": "FSS",
            "deploymentParam": [
                {
                    "installationParam": {
                        "latitude": lat,
                        "longitude": lon,
                        "height": 1.5,
                        "heightType": "AGL",
                    },
                    "operationParam": {
                        "operationFrequencyRange": {
                            "lowFrequency": low_hz,
                            "highFrequency": high_hz,
                        }
                    },
                }
            ],
        }
    }
    if ttc is not None:
        body["ttc"] = ttc
    assert upsert_fss_record(db, body)


def _inject_gwbl(db: Session, *, lat: float, lon: float, record_id: str = "gwbl-c3") -> None:
    db.add(
        AdminInjectedData(
            kind="gwbl",
            data_json=json.dumps(
                {"id": record_id, "latitude": lat, "longitude": lon}
            ),
        )
    )


# ---------------------------------------------------------------------------
# GPR
# ---------------------------------------------------------------------------


def test_gpr_a_no_violation(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from services.iap import coupling as coupling_mod

    _inject_gwpz(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-gpr-a",
        grant_id="g-gpr-a",
        lat=39.001,
        lon=-77.001,
        eirp=0.0,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1e-12),
    )
    snap = freeze_cpas_snapshot(db_session)
    assert any(k == "wisp" for k, _r, _d in snap.protection_records)
    decisions = evaluate_cpas_protections(db_session, snap)
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_gpr_b_local_aggregate_protects(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_gwpz(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-gpr-b",
        grant_id="g-gpr-b",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated or (
        grant.max_eirp is not None and float(grant.max_eirp) < 37.0
    )


def test_gpr_c_d_peer_changes_local_never_mutated(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_gwpz(db_session)
    local = _add_grant(
        db_session,
        cbsd_id="c3-gpr-c",
        grant_id="g-gpr-c",
        lat=39.001,
        lon=-77.001,
        eirp=10.0,
    )
    peer = _add_peer_grant(
        db_session,
        peer_hash="peer-gpr-c",
        record_id="peer-cbsd-gpr",
        grant_id="peer-g-gpr",
        lat=39.002,
        lon=-77.002,
        eirp=37.0,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    snap = freeze_cpas_snapshot(db_session)
    decisions = evaluate_cpas_protections(db_session, snap)
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-gpr")
    assert all(d.grant_id != peer_gid for d in decisions)
    assert any(d.grant_id == local.grant_id and d.reason == "iap" for d in decisions)
    apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    row = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-gpr")
        .one()
    )
    assert "peer-g-gpr" in row.data_json


def test_gpr_e_out_of_band_does_not_contribute(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_gwpz(db_session, low_hz=3_650_000_000, high_hz=3_700_000_000)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-gpr-e",
        grant_id="g-gpr-e",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_gpr_f_coupling_unavailable_fail_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.coupling import IapCouplingUnavailable

    _inject_gwpz(db_session)
    _add_grant(
        db_session, cbsd_id="c3-gpr-f", grant_id="g-gpr-f", lat=39.001, lon=-77.001
    )
    db_session.commit()

    def _boom(**_k):
        raise IapCouplingUnavailable("itm missing")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError):
        evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))


def test_gpr_g_order_invariant(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from services.iap import coupling as coupling_mod

    _inject_gwpz(db_session)
    g1 = _add_grant(
        db_session, cbsd_id="c3-gpr-g1", grant_id="g-gpr-g1", lat=39.001, lon=-77.001, eirp=30.0
    )
    g2 = _add_grant(
        db_session, cbsd_id="c3-gpr-g2", grant_id="g-gpr-g2", lat=39.002, lon=-77.002, eirp=30.0
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(0.5),
    )
    d1 = {
        (
            d.grant_id,
            d.action,
            round(d.authorized_eirp_dbm_mhz, 3)
            if d.authorized_eirp_dbm_mhz is not None
            else None,
        )
        for d in evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
        if d.grant_id in {g1.grant_id, g2.grant_id}
    }
    # Re-freeze + evaluate — deterministic for the same frozen generation.
    d2 = {
        (
            d.grant_id,
            d.action,
            round(d.authorized_eirp_dbm_mhz, 3)
            if d.authorized_eirp_dbm_mhz is not None
            else None,
        )
        for d in evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
        if d.grant_id in {g1.grant_id, g2.grant_id}
    }
    assert d1 == d2


# ---------------------------------------------------------------------------
# PPR
# ---------------------------------------------------------------------------


def test_ppr_a_valid_ppa_recognized(db_session: Session):
    _inject_ppa_with_pal(db_session)
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    points = build_protection_points_from_frozen(snap.protection_records)
    assert any(p.entity_kind is ProtectedEntityKind.PPA for p in points)
    assert any(k == "pal" for k, _r, _d in snap.protection_records)


def test_ppr_b_non_ppa_zone_excluded():
    assert (
        protection_point_from_zone_payload(
            {
                "record": {
                    "id": "zone/other",
                    "zone": _square(-77.0, 39.0),
                }
            }
        )
        is None
    )


def test_ppr_c_single_sas_aggregate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_ppa_with_pal(db_session, low_hz=3_655_000_000, high_hz=3_660_000_000)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-ppr-c",
        grant_id="g-ppr-c",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_655_000_000,
        high_hz=3_660_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated or (
        grant.max_eirp is not None and float(grant.max_eirp) < 37.0
    )


def test_ppr_d_e_multi_sas_peer_no_action(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_ppa_with_pal(db_session, low_hz=3_655_000_000, high_hz=3_660_000_000)
    local = _add_grant(
        db_session,
        cbsd_id="c3-ppr-d",
        grant_id="g-ppr-d",
        lat=39.001,
        lon=-77.001,
        eirp=10.0,
        low_hz=3_655_000_000,
        high_hz=3_660_000_000,
    )
    peer = _add_peer_grant(
        db_session,
        peer_hash="peer-ppr-d",
        record_id="peer-cbsd-ppr",
        grant_id="peer-g-ppr",
        lat=39.002,
        lon=-77.002,
        eirp=37.0,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-ppr")
    assert all(d.grant_id != peer_gid for d in decisions)
    assert any(d.grant_id == local.grant_id and d.reason == "iap" for d in decisions)


def test_ppr_f_frequency_respected(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_ppa_with_pal(db_session, low_hz=3_550_000_000, high_hz=3_560_000_000)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-ppr-f",
        grant_id="g-ppr-f",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_655_000_000,
        high_hz=3_660_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_ppr_g_missing_pal_no_silent_allow(db_session: Session):
    persist_zone_data(
        db_session,
        {
            "record": {
                "id": "zone/c3-ppa-orphan",
                "type": "PPA",
                "usage": "PPA",
                "ppaInfo": {"palId": ["missing-pal"], "cbsdReferenceId": []},
                "zone": _square(-77.0, 39.0),
            }
        },
    )
    db_session.commit()
    snap = freeze_cpas_snapshot(db_session)
    points = build_protection_points_from_frozen(snap.protection_records)
    assert not any(p.entity_kind is ProtectedEntityKind.PPA for p in points)


# ---------------------------------------------------------------------------
# FPR
# ---------------------------------------------------------------------------


def test_fpr_a_fss_cochannel(db_session: Session):
    _inject_fss(db_session, low_hz=3_600_000_000, high_hz=4_200_000_000, ttc=False)
    db_session.commit()
    points = build_protection_points_from_frozen(
        freeze_cpas_snapshot(db_session).protection_records
    )
    kinds = {p.entity_kind for p in points}
    assert ProtectedEntityKind.FSS_COCHANNEL in kinds


def test_fpr_b_fss_blocking_distinct_from_cochannel():
    # Narrow FSS below CBRS high → blocking only (no co-channel).
    blocking_only = protection_points_from_fss_payload(
        {
            "record": {
                "id": "fss/bl-only",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": 39.0, "longitude": -77.0},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_620_000_000,
                                "highFrequency": 3_625_000_000,
                            }
                        },
                    }
                ],
            }
        }
    )
    assert len(blocking_only) == 1
    assert blocking_only[0].entity_kind is ProtectedEntityKind.FSS_BLOCKING

    # Co-channel without blocking when FSS starts at band edge (no below-edge).
    cc_only = protection_points_from_fss_payload(
        {
            "record": {
                "id": "fss/cc-only",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": 39.0, "longitude": -77.0},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_550_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            },
            "ttc": False,
        }
    )
    assert any(p.entity_kind is ProtectedEntityKind.FSS_COCHANNEL for p in cc_only)
    assert not any(p.entity_kind is ProtectedEntityKind.FSS_BLOCKING for p in cc_only)


def test_fpr_c_ttc_true_enables_blocking_in_ttc_band():
    pts = protection_points_from_fss_payload(
        {
            "record": {
                "id": "fss/ttc-true",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": 39.0, "longitude": -77.0},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_700_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            },
            "ttc": True,
        }
    )
    assert any(p.entity_kind is ProtectedEntityKind.FSS_BLOCKING for p in pts)


def test_fpr_d_ttc_false_skips_blocking_in_ttc_band():
    pts = protection_points_from_fss_payload(
        {
            "record": {
                "id": "fss/ttc-false",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": 39.0, "longitude": -77.0},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_700_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            },
            "ttc": False,
        }
    )
    assert not any(p.entity_kind is ProtectedEntityKind.FSS_BLOCKING for p in pts)


def test_fpr_ttc_missing_in_ttc_band_fail_closed():
    with pytest.raises(ProtectionEntityError):
        protection_points_from_fss_payload(
            {
                "record": {
                    "id": "fss/ttc-missing",
                    "deploymentParam": [
                        {
                            "installationParam": {"latitude": 39.0, "longitude": -77.0},
                            "operationParam": {
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_700_000_000,
                                    "highFrequency": 4_200_000_000,
                                }
                            },
                        }
                    ],
                }
            }
        )
    assert parse_fss_ttc({"ttc": True}) is True
    assert parse_fss_ttc({"ttc": False}) is False
    assert parse_fss_ttc({}) is None


def test_fpr_e_gwbl_pre_iap_exclusion(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session, low_hz=3_650_000_000, high_hz=4_200_000_000, ttc=False)
    _inject_gwbl(db_session, lat=39.0, lon=-77.0)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-fpr-e",
        grant_id="g-fpr-e",
        lat=39.001,
        lon=-77.001,
        eirp=20.0,
        low_hz=3_655_000_000,
        high_hz=3_660_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1e-20),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert any(
        d.grant_id == grant.grant_id and d.reason == "fss_gwbl_exclusion" for d in decisions
    )


def test_fpr_f_out_of_band_no_contribute(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod

    # Blocking protects 3550–3600; grant above FSS low does not overlap.
    _inject_fss(db_session, low_hz=3_600_000_000, high_hz=3_625_000_000, ttc=None)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-fpr-f",
        grant_id="g-fpr-f",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_650_000_000,
        high_hz=3_655_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    assert all(
        not (d.grant_id == grant.grant_id and d.reason == "iap" and d.action != "keep")
        for d in decisions
    )


def test_fpr_g_h_i_aggregate_peer(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.peer_fad import peer_grant_rf_id

    _inject_fss(db_session, low_hz=3_600_000_000, high_hz=4_200_000_000, ttc=False)
    local = _add_grant(
        db_session,
        cbsd_id="c3-fpr-g",
        grant_id="g-fpr-g",
        lat=39.001,
        lon=-77.001,
        eirp=10.0,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
    )
    peer = _add_peer_grant(
        db_session,
        peer_hash="peer-fpr-g",
        record_id="peer-cbsd-fpr",
        grant_id="peer-g-fpr",
        lat=39.002,
        lon=-77.002,
        eirp=37.0,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    decisions = evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))
    peer_gid = peer_grant_rf_id(peer.id, "peer-g-fpr")
    assert all(d.grant_id != peer_gid for d in decisions)
    assert any(d.grant_id == local.grant_id and d.reason == "iap" for d in decisions)


def test_fpr_j_itm_unavailable_fail_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from services.iap import coupling as coupling_mod
    from services.iap.coupling import IapCouplingUnavailable

    _inject_fss(db_session)
    _add_grant(
        db_session, cbsd_id="c3-fpr-j", grant_id="g-fpr-j", lat=39.001, lon=-77.001
    )
    db_session.commit()

    def _boom(**_k):
        raise IapCouplingUnavailable("itm")

    monkeypatch.setattr(coupling_mod, "make_production_iap_coupling", _boom)
    with pytest.raises(CpasRfEvaluationError):
        evaluate_cpas_protections(db_session, freeze_cpas_snapshot(db_session))


def test_fpr_k_invalid_fss_rx_is_domain_error():
    with pytest.raises(ProtectionEntityError):
        protection_points_from_fss_payload(
            {
                "record": {
                    "id": "fss/bad",
                    "deploymentParam": [
                        {
                            "installationParam": {"latitude": 39.0, "longitude": -77.0},
                            "operationParam": {
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_700_000_000,
                                    "highFrequency": 3_600_000_000,
                                }
                            },
                        }
                    ],
                }
            }
        )


def test_fpr_e2e_pipeline_fss(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from services.iap import coupling as coupling_mod

    _inject_fss(db_session)
    grant = _add_grant(
        db_session,
        cbsd_id="c3-fpr-e2e",
        grant_id="g-fpr-e2e",
        lat=39.001,
        lon=-77.001,
        eirp=37.0,
        low_hz=3_620_000_000,
        high_hz=3_625_000_000,
    )
    db_session.commit()
    monkeypatch.setattr(
        coupling_mod,
        "make_production_iap_coupling",
        lambda **_k: _constant_coupling(1.0),
    )
    result = execute_cpas_pipeline(db_session)
    assert result["ok"] is True
    db_session.refresh(grant)
    assert grant.terminated or (
        grant.max_eirp is not None and float(grant.max_eirp) < 37.0
    )
