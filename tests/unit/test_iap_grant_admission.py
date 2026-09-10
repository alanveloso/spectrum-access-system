"""Post-CPAS IAP/ESC grant admission gate."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import Session

from models.models import FccIdRecord, Grant, PeerFadRecord, PeerSas, UserIdRecord
from services.concurrency import reset_resource_locks_for_tests
from services.cpas_reevaluation import (
    clear_cpas_reevaluation_required,
    mark_cpas_reevaluation_required,
)
from services.grant_service import process_grant
from services.iap.admission import (
    collect_local_authorized_grants,
    collect_peer_grants,
    current_generation_fingerprint,
    evaluate_proposal_against_headroom,
    load_iap_admission_generation,
    proposed_grant_rf_info,
    proposed_grant_violates_iap,
    record_iap_admission_generation,
)
from services.iap.aggregate import ESC_CAT_A_HIGH_FREQ_HZ, grant_overlaps_channel
from services.iap.models import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
)
from tests.fixtures.factories import make_cbsd, make_grant

SUCCESS = 0
INTERFERENCE = 400

_ESC_LAT, _ESC_LON = 41.0, -99.0
_NEAR_LAT, _NEAR_LON = 41.01, -99.01
# Inland US — avoid Canadian border PFD (BPR) and coastal DPA neighborhoods.
_FAR_LAT, _FAR_LON = 35.5, -101.5
_BAND_LOW = 3_630_000_000
_BAND_HIGH = 3_640_000_000


def _pattern(gain: float = 0.0) -> tuple[float, ...]:
    return tuple(float(gain) for _ in range(360))


def _esc_point(
    *,
    lat: float = _ESC_LAT,
    lon: float = _ESC_LON,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_680_000_000,
    threshold_dbm: float = -109.0,
    margin_db: float = 1.0,
) -> ProtectionPoint:
    return ProtectionPoint(
        point_id="esc/synth/0",
        latitude=lat,
        longitude=lon,
        low_hz=low_hz,
        high_hz=high_hz,
        threshold_dbm=threshold_dbm,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=margin_db,
        neighborhood_km=80.0,
        receiver_height_m=10.0,
        receiver_antenna_azimuth_deg=0.0,
        receiver_antenna_gain_pattern_dbi=_pattern(0.0),
    )


def _const_coupling(mw: float):
    def _fn(grant, point, channel, eirp):  # noqa: ARG001
        return float(mw)

    return _fn


def _eirp_scaled_coupling(scale: float = 1e-12):
    def _fn(grant, point, channel, eirp):  # noqa: ARG001
        return (10.0 ** (float(eirp) / 10.0)) * float(scale)

    return _fn


def _reg(
    lat: float,
    lon: float,
    *,
    fcc: str,
    serial: str,
    cat: str = "A",
    ant_gain: float = 16.0,
) -> dict:
    return {
        "fccId": fcc,
        "cbsdSerialNumber": serial,
        "userId": f"user-{serial}",
        "cbsdCategory": cat,
        "airInterface": {"radioTechnology": "E_UTRA"},
        "measCapability": [],
        "installationParam": {
            "latitude": lat,
            "longitude": lon,
            "height": 6.0,
            "heightType": "AGL",
            "indoorDeployment": False,
            "antennaGain": ant_gain,
            "antennaBeamwidth": 60.0,
            "antennaAzimuth": 0.0,
        },
    }


def _ensure_ids(db: Session, fcc: str, user: str) -> None:
    if not db.query(FccIdRecord).filter_by(fcc_id=fcc).first():
        db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47.0))
    if not db.query(UserIdRecord).filter_by(user_id=user).first():
        db.add(UserIdRecord(user_id=user))
    db.commit()


def _grant_req(cbsd_id: str, *, low: int, high: int, eirp: float) -> dict:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": eirp,
            "operationFrequencyRange": {
                "lowFrequency": low,
                "highFrequency": high,
            },
        },
    }


@pytest.fixture(autouse=True)
def _reset_locks():
    reset_resource_locks_for_tests()
    yield
    reset_resource_locks_for_tests()


# ---------------------------------------------------------------------------
# Pure headroom evaluation (A/B/C/D/E core)
# ---------------------------------------------------------------------------


def test_a_unsafe_proposal_denied_by_headroom():
    proposed = GrantRfInfo(
        grant_id="__proposed__/iap_admission",
        cbsd_id="cbsd/a",
        latitude=_NEAR_LAT,
        longitude=_NEAR_LON,
        height_m=6.0,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
    )
    decision = evaluate_proposal_against_headroom(
        proposed,
        baseline=[],
        points=[_esc_point()],
        coupling=_const_coupling(1.0),  # >> threshold (~1e-11 mW)
    )
    assert decision.allow is False
    assert decision.reason == "iap_headroom_exceeded"
    assert decision.applicable is True


def test_b_safe_proposal_within_headroom():
    proposed = GrantRfInfo(
        grant_id="__proposed__/iap_admission",
        cbsd_id="cbsd/b",
        latitude=_NEAR_LAT,
        longitude=_NEAR_LON,
        height_m=6.0,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
    )
    decision = evaluate_proposal_against_headroom(
        proposed,
        baseline=[],
        points=[_esc_point()],
        coupling=_const_coupling(1e-15),
    )
    assert decision.allow is True
    assert decision.reason == "within_iap_headroom"


def test_c_outside_neighborhood_not_applicable():
    proposed = GrantRfInfo(
        grant_id="__proposed__/iap_admission",
        cbsd_id="cbsd/c",
        latitude=_FAR_LAT,
        longitude=_FAR_LON,
        height_m=6.0,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
    )
    decision = evaluate_proposal_against_headroom(
        proposed,
        baseline=[],
        points=[_esc_point()],
        coupling=_const_coupling(1.0),
    )
    assert decision.allow is True
    assert decision.applicable is False
    assert decision.reason == "no_applicable_iap_constraint"


def test_d_cat_a_above_3660_excluded_from_esc_admission():
    ch = FrequencyChannel(low_hz=3_660_000_000, high_hz=3_665_000_000)
    grant = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=_NEAR_LAT,
        longitude=_NEAR_LON,
        height_m=6.0,
        low_hz=3_660_000_000,
        high_hz=3_670_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
    )
    assert grant.low_hz >= ESC_CAT_A_HIGH_FREQ_HZ
    assert (
        grant_overlaps_channel(grant, ch, entity_kind=ProtectedEntityKind.ESC)
        is False
    )
    proposed = grant.model_copy(update={"grant_id": "__proposed__/iap_admission"})
    decision = evaluate_proposal_against_headroom(
        proposed,
        baseline=[],
        points=[
            _esc_point(low_hz=3_660_000_000, high_hz=3_680_000_000),
        ],
        coupling=_const_coupling(1.0),
    )
    assert decision.allow is True
    assert decision.applicable is False


def test_e_peer_contribution_shrinks_headroom():
    proposed = GrantRfInfo(
        grant_id="__proposed__/iap_admission",
        cbsd_id="cbsd/local",
        latitude=_NEAR_LAT,
        longitude=_NEAR_LON,
        height_m=6.0,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        cbsd_category="A",
    )
    peer = GrantRfInfo(
        grant_id="peer/1/g1",
        cbsd_id="cbsd/peer",
        latitude=_NEAR_LAT,
        longitude=_NEAR_LON,
        height_m=6.0,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=False,
        source_sas_id="1",
        cbsd_category="A",
    )
    # Without peer: tiny residual still fits 1e-15.
    ok = evaluate_proposal_against_headroom(
        proposed,
        baseline=[],
        points=[_esc_point()],
        coupling=_const_coupling(1e-15),
    )
    assert ok.allow is True
    # With peer taking almost all headroom, same proposal denies.
    # threshold ~ 1e-11 mW; peer uses 1e-11, proposal needs 1e-15 → deny.
    from services.iap.aggregate import apply_pre_iap_margin_db, dbm_to_mw

    thr = dbm_to_mw(apply_pre_iap_margin_db(-109.0, 1.0))

    def coupling(grant, point, channel, eirp):  # noqa: ARG001
        if not grant.is_managing_sas:
            return float(thr)
        return 1e-12

    denied = evaluate_proposal_against_headroom(
        proposed,
        baseline=[peer],
        points=[_esc_point()],
        coupling=coupling,
    )
    assert denied.allow is False
    assert denied.channels[0].peer_mw == pytest.approx(thr)
    assert denied.channels[0].managing_mw == 0.0


# ---------------------------------------------------------------------------
# Generation coherence (F/G)
# ---------------------------------------------------------------------------


def test_f_coherent_generation_rejects_mix(db_session: Session):
    peer = PeerSas(
        certificate_hash="hash-f",
        url="https://peer.example/f",
        last_fad_generation="GEN-N",
    )
    db_session.add(peer)
    db_session.commit()
    record_iap_admission_generation(db_session)
    db_session.commit()
    assert load_iap_admission_generation(db_session) is not None

    peer.last_fad_generation = "GEN-N+1"
    db_session.commit()
    live = current_generation_fingerprint(db_session)
    marker = load_iap_admission_generation(db_session)
    assert marker["peer_generations"] != live["peer_generations"]

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-f", serial="ser-f"),
    )
    _ensure_ids(db_session, "fcc-f", cbsd.user_id)
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )


def test_g_reevaluation_or_missing_marker_fail_closed(db_session: Session):
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-g", serial="ser-g"),
    )
    _ensure_ids(db_session, "fcc-g", cbsd.user_id)
    # Applicable IAP + missing marker → fail-closed DENY.
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )
    record_iap_admission_generation(db_session)
    db_session.commit()
    # Coherent marker + tiny coupling → allow.
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is False
    )
    mark_cpas_reevaluation_required(db_session, reason="test")
    db_session.commit()
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )
    clear_cpas_reevaluation_required(db_session)
    db_session.commit()


def test_missing_marker_applicable_iap_fail_closed(db_session: Session):
    """Policy I: applicable IAP + missing marker → DENY."""
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-im", serial="ser-im"),
    )
    _ensure_ids(db_session, "fcc-im", cbsd.user_id)
    assert load_iap_admission_generation(db_session) is None
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )


def test_missing_marker_no_iap_fast_allow(db_session: Session):
    """Policy J: no applicable IAP + missing marker → ALLOW without RF backend."""
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-jm", serial="ser-jm"),
    )
    _ensure_ids(db_session, "fcc-jm", cbsd.user_id)
    assert load_iap_admission_generation(db_session) is None
    # Empty protection set → no applicable constraint.
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[],
            coupling=_const_coupling(1.0),
        )
        is False
    )


# ---------------------------------------------------------------------------
# process_grant integration (A/B/H/I) via monkeypatched gate inputs
# ---------------------------------------------------------------------------


def test_b_i_process_grant_safe_persists_requested_eirp(
    db_session: Session, monkeypatch
):
    import services.iap.admission as admission_mod

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-bi", serial="ser-bi"),
    )
    _ensure_ids(db_session, "fcc-bi", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    assert (
        orig(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[far_esc],
            coupling=_const_coupling(1e-15),
        )
        is False
    )

    monkeypatch.setattr(
        admission_mod,
        "proposed_grant_violates_iap",
        lambda db, cbsd, **kwargs: orig(
            db,
            cbsd,
            points=[far_esc],
            coupling=_const_coupling(1e-15),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        ),
    )

    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
    )[0]
    assert resp["response"]["responseCode"] == SUCCESS
    assert "grantId" in resp
    assert "operationParam" not in resp
    row = db_session.query(Grant).filter_by(grant_id=resp["grantId"]).one()
    assert float(row.max_eirp) == 10.0


def test_a_h_process_grant_unsafe_denies_no_persist(db_session: Session, monkeypatch):
    import services.iap.admission as admission_mod

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-ah", serial="ser-ah"),
    )
    _ensure_ids(db_session, "fcc-ah", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    assert (
        orig(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[far_esc],
            coupling=_const_coupling(1.0),
        )
        is True
    )

    monkeypatch.setattr(
        admission_mod,
        "proposed_grant_violates_iap",
        lambda db, cbsd, **kwargs: orig(
            db,
            cbsd,
            points=[far_esc],
            coupling=_const_coupling(1.0),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        ),
    )

    before = db_session.query(Grant).count()
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
    )[0]
    assert resp["response"]["responseCode"] == INTERFERENCE
    assert resp.get("cbsdId") == cbsd.cbsd_id
    assert "grantId" not in resp
    assert "operationParam" not in resp
    assert db_session.query(Grant).count() == before


def test_c_outside_via_process_grant_not_iap_denied(db_session: Session, monkeypatch):
    """CBSD far from ESC point — admission not applicable → SUCCESS."""
    import services.iap.admission as admission_mod

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-cout", serial="ser-cout"),
    )
    _ensure_ids(db_session, "fcc-cout", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()
    # ESC remains at original synth location; CBSD is far away.
    orig = admission_mod.proposed_grant_violates_iap
    monkeypatch.setattr(
        admission_mod,
        "proposed_grant_violates_iap",
        lambda db, cbsd, **kwargs: orig(
            db,
            cbsd,
            points=[_esc_point()],  # ESC at _ESC_LAT/_ESC_LON
            coupling=_const_coupling(1.0),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        ),
    )
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
    )[0]
    assert resp["response"]["responseCode"] == SUCCESS


def test_g_coupling_unavailable_fail_closed(db_session: Session, monkeypatch):
    from services.iap.coupling import IapCouplingUnavailable

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-g2", serial="ser-g2"),
    )
    _ensure_ids(db_session, "fcc-g2", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()
    monkeypatch.setattr(
        "services.iap.protection_points.build_protection_points_from_db",
        lambda db, profile=None: [_esc_point()],
    )

    def _boom(db, **kwargs):
        raise IapCouplingUnavailable("missing backend")

    monkeypatch.setattr("services.mcp_protection.resolve_iap_context", _boom)
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
        )
        is True
    )


def test_proposed_grant_rf_info_wires_antennas(db_session: Session):
    cbsd = make_cbsd(
        db_session,
        registration=_reg(
            _NEAR_LAT, _NEAR_LON, fcc="fcc-rf", serial="ser-rf", ant_gain=90.0
        ),
    )
    rf = proposed_grant_rf_info(
        cbsd, low_hz=_BAND_LOW, high_hz=_BAND_HIGH, max_eirp_dbm_mhz=10.0
    )
    assert rf.antenna_gain_dbi == 90.0
    assert rf.antenna_beamwidth_deg == 60.0
    assert rf.antenna_azimuth_deg == 0.0
    assert rf.grant_id == "__proposed__/iap_admission"
    assert rf.max_eirp_dbm_mhz == 10.0


def test_e_peer_grants_loaded_as_non_managing(db_session: Session):
    peer = PeerSas(
        certificate_hash="hash-e",
        url="https://peer.example/e",
        last_fad_generation="GEN-E",
    )
    db_session.add(peer)
    db_session.flush()
    record = {
        "id": "cbsd/peer-e",
        "registration": {
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": _NEAR_LAT,
                "longitude": _NEAR_LON,
                "height": 6.0,
                "heightType": "AGL",
                "indoorDeployment": False,
                "antennaGain": 16.0,
                "antennaBeamwidth": 60.0,
                "antennaAzimuth": 0.0,
            },
        },
        "grants": [
            {
                "id": "grant/peer-e",
                "operationParam": {
                    "maxEirp": 10.0,
                    "operationFrequencyRange": {
                        "lowFrequency": _BAND_LOW,
                        "highFrequency": _BAND_HIGH,
                    },
                },
            }
        ],
    }
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="cbsd/peer-e",
            data_json=json.dumps(record),
        )
    )
    db_session.commit()
    peers = collect_peer_grants(db_session)
    assert len(peers) == 1
    assert peers[0].is_managing_sas is False
    assert peers[0].source_sas_id == str(peer.id)


def test_local_grants_use_persisted_authorized_eirp(db_session: Session):
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-loc", serial="ser-loc"),
    )
    make_grant(
        db_session,
        cbsd,
        low_hz=_BAND_LOW,
        high_hz=_BAND_HIGH,
        max_eirp=-7.0,
    )
    locals_ = collect_local_authorized_grants(db_session)
    assert len(locals_) == 1
    assert locals_[0].max_eirp_dbm_mhz == -7.0
    assert locals_[0].is_managing_sas is True


# ---------------------------------------------------------------------------
# Concurrency: headroom for one, not two
# ---------------------------------------------------------------------------


def test_concurrency_at_most_one_of_two_proposals(
    db_session: Session, monkeypatch
):
    """Residual admits one constant contribution, not two."""
    import services.iap.admission as admission_mod
    from services.iap.aggregate import apply_pre_iap_margin_db, dbm_to_mw

    thr = dbm_to_mw(apply_pre_iap_margin_db(-109.0, 1.0))
    # Each proposal contributes 0.6 * thr → one fits, two do not.
    per = 0.6 * thr

    def coupling(grant, point, channel, eirp):  # noqa: ARG001
        return float(per)

    cbsd_a = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-ca", serial="ser-ca"),
    )
    cbsd_b = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT + 0.001, _FAR_LON, fcc="fcc-cb", serial="ser-cb"),
    )
    _ensure_ids(db_session, "fcc-ca", cbsd_a.user_id)
    _ensure_ids(db_session, "fcc-cb", cbsd_b.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    monkeypatch.setattr(
        admission_mod,
        "proposed_grant_violates_iap",
        lambda db, cbsd, **kwargs: orig(
            db,
            cbsd,
            points=[far_esc],
            coupling=coupling,
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        ),
    )

    bind = db_session.get_bind()
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def _worker(cbsd_id: str) -> None:
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=bind)
        s = Session()
        try:
            barrier.wait(timeout=5)
            resp = process_grant(
                s,
                [_grant_req(cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
            )[0]
            with lock:
                results.append(int(resp["response"]["responseCode"]))
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(_worker, cbsd_a.cbsd_id),
            pool.submit(_worker, cbsd_b.cbsd_id),
        ]
        for f in futs:
            f.result(timeout=30)

    assert results.count(SUCCESS) <= 1
    assert results.count(INTERFERENCE) >= 1
    assert len(results) == 2
    assert db_session.query(Grant).count() <= 1


def test_j_cpas_records_admission_generation(db_session: Session, monkeypatch):
    """CPAS success path stamps coherent generation (marker exists after)."""
    # Lightweight: record helper used by CPAS finalize.
    clear_cpas_reevaluation_required(db_session)
    db_session.commit()
    payload = record_iap_admission_generation(db_session)
    db_session.commit()
    loaded = load_iap_admission_generation(db_session)
    assert loaded is not None
    assert loaded["peer_generations"] == payload["peer_generations"]
    assert loaded["injection_generations"] == payload["injection_generations"]


# ---------------------------------------------------------------------------
# Concurrency / generation closure (A–G, K, advisory key)
# ---------------------------------------------------------------------------


def test_peer_apply_serialized_against_grant_admission(db_session: Session, monkeypatch):
    """A: peer apply blocks while Grant holds IAP admission through persist."""
    import services.iap.admission as admission_mod
    from services.fad_client_service import FadGenerationSnapshot, apply_peer_generation

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-pa", serial="ser-pa"),
    )
    _ensure_ids(db_session, "fcc-pa", cbsd.user_id)
    peer = PeerSas(
        certificate_hash="hash-pa",
        url="https://peer.example/pa",
        last_fad_generation="GEN-N",
    )
    db_session.add(peer)
    db_session.commit()
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    entered = threading.Event()
    release = threading.Event()

    def _gated(db, cbsd_obj, **kwargs):
        # Simulate long evaluate while admission serialization is held by process_grant.
        entered.set()
        assert release.wait(timeout=5)
        return orig(
            db,
            cbsd_obj,
            points=[far_esc],
            coupling=_const_coupling(1e-15),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        )

    monkeypatch.setattr(admission_mod, "proposed_grant_violates_iap", _gated)

    bind = db_session.get_bind()
    grant_code: list[int] = []
    peer_started = threading.Event()
    peer_finished = threading.Event()
    peer_blocked_while_grant = threading.Event()

    def _grant_worker() -> None:
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=bind)()
        try:
            resp = process_grant(
                s,
                [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
            )[0]
            grant_code.append(int(resp["response"]["responseCode"]))
        finally:
            s.close()

    def _peer_worker() -> None:
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=bind)()
        try:
            assert entered.wait(timeout=5)
            peer_started.set()
            snap = FadGenerationSnapshot(
                peer_sas_id=int(peer.id),
                generation_datetime="GEN-N+1",
                description="test",
                files=(),
                records=(),
            )
            from services.concurrency import (
                acquire_iap_admission_xact_lock,
                exclusive_iap_admission,
            )

            # About to block on IAP admission held by Grant.
            peer_blocked_while_grant.set()
            with exclusive_iap_admission():
                acquire_iap_admission_xact_lock(s)
                apply_peer_generation(s, snap)
                s.commit()
            peer_finished.set()
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        g = pool.submit(_grant_worker)
        p = pool.submit(_peer_worker)
        assert entered.wait(timeout=5)
        assert peer_started.wait(timeout=5)
        assert peer_blocked_while_grant.wait(timeout=5)
        # Peer thread should still be waiting on IAP RLock.
        threading.Event().wait(0.05)
        assert not peer_finished.is_set()
        release.set()
        g.result(timeout=30)
        p.result(timeout=30)

    assert grant_code == [SUCCESS]
    db_session.expire_all()
    refreshed = db_session.query(PeerSas).filter_by(id=peer.id).one()
    assert refreshed.last_fad_generation == "GEN-N+1"


def test_peer_mutation_then_grant_fail_closed(db_session: Session):
    """B: after peer gen advances, applicable Grant fails closed on marker mismatch."""
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-pb", serial="ser-pb"),
    )
    _ensure_ids(db_session, "fcc-pb", cbsd.user_id)
    peer = PeerSas(
        certificate_hash="hash-pb",
        url="https://peer.example/pb",
        last_fad_generation="GEN-N",
    )
    db_session.add(peer)
    db_session.commit()
    record_iap_admission_generation(db_session)
    db_session.commit()

    peer.last_fad_generation = "GEN-N+1"
    db_session.commit()
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )


def test_protection_bump_invalidates_admission_marker(db_session: Session):
    """D: protection generation bump makes old marker incoherent."""
    from services.data_injection_service import bump_injection_generation

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-pd", serial="ser-pd"),
    )
    _ensure_ids(db_session, "fcc-pd", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is False
    )
    bump_injection_generation(db_session, "esc_zone")
    db_session.commit()
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_BAND_LOW,
            high_hz=_BAND_HIGH,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=_const_coupling(1e-15),
        )
        is True
    )


def test_protection_write_serialized_against_grant(db_session: Session, monkeypatch):
    """C: protection mutation cannot interleave with Grant admission hold."""
    import services.iap.admission as admission_mod
    from services.data_injection_service import bump_injection_generation

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-pc", serial="ser-pc"),
    )
    _ensure_ids(db_session, "fcc-pc", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    entered = threading.Event()
    release = threading.Event()

    def _gated(db, cbsd_obj, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return orig(
            db,
            cbsd_obj,
            points=[far_esc],
            coupling=_const_coupling(1e-15),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        )

    monkeypatch.setattr(admission_mod, "proposed_grant_violates_iap", _gated)

    bind = db_session.get_bind()
    inject_finished = threading.Event()
    inject_saw_hold = threading.Event()

    def _grant_worker() -> None:
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=bind)()
        try:
            process_grant(
                s,
                [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
            )
        finally:
            s.close()

    def _inject_worker() -> None:
        from sqlalchemy.orm import sessionmaker
        from services.concurrency import (
            acquire_iap_admission_xact_lock,
            exclusive_iap_admission,
        )

        s = sessionmaker(bind=bind)()
        try:
            assert entered.wait(timeout=5)
            inject_saw_hold.set()
            with exclusive_iap_admission():
                acquire_iap_admission_xact_lock(s)
                bump_injection_generation(s, "esc_zone")
                s.commit()
            inject_finished.set()
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        g = pool.submit(_grant_worker)
        i = pool.submit(_inject_worker)
        assert entered.wait(timeout=5)
        assert inject_saw_hold.wait(timeout=5)
        threading.Event().wait(0.05)
        assert not inject_finished.is_set()
        release.set()
        g.result(timeout=30)
        i.result(timeout=30)


def test_cpas_stamps_exact_frozen_generation(db_session: Session, monkeypatch):
    """E: stamp equals frozen authorization_generation, not a drifted live map."""
    from services.cpas_service import (
        _run_pipeline_critical_section,
        freeze_cpas_snapshot,
    )

    monkeypatch.setattr(
        "services.cpas_service.evaluate_cpas_protections",
        lambda db, snapshot: [],
    )
    monkeypatch.setattr(
        "services.cpas_service.apply_cpas_decisions",
        lambda db, decisions: 0,
    )
    monkeypatch.setattr(
        "services.cpas_service.create_full_activity_dump",
        lambda db, commit=True: type("D", (), {"id": 42})(),
    )
    monkeypatch.setattr(
        "services.cpas_schedule_service.mark_scheduled_success_if_applicable",
        lambda db: None,
    )

    snap = freeze_cpas_snapshot(db_session)
    # Drift live injection gens after freeze; apply must refuse stale stamp.
    from services.data_injection_service import bump_injection_generation

    # Revalidation should catch drift — bump then expect drift error.
    bump_injection_generation(db_session, "esc_zone")
    db_session.commit()
    from services.cpas_service import CpasGenerationDriftError

    with pytest.raises(CpasGenerationDriftError):
        _run_pipeline_critical_section(db_session, snap)

    # Fresh freeze+apply without drift stamps exact fingerprint.
    snap2 = freeze_cpas_snapshot(db_session)
    terminated, dump_id, decisions, admission_gen = _run_pipeline_critical_section(
        db_session, snap2
    )
    assert dump_id == 42
    assert admission_gen["peer_generations"] == snap2.authorization_generation[
        "peer_generations"
    ]
    assert admission_gen["injection_generations"] == snap2.authorization_generation[
        "injection_generations"
    ]
    loaded = load_iap_admission_generation(db_session)
    assert loaded["injection_generations"] == snap2.authorization_generation[
        "injection_generations"
    ]


def test_cpas_generation_drift_marks_reevaluation(db_session: Session, monkeypatch):
    """F: drift before apply does not stamp; sets reevaluation-required."""
    from services.cpas_reevaluation import cpas_reevaluation_required
    from services.cpas_service import (
        CpasGenerationDriftError,
        _run_pipeline_critical_section,
        freeze_cpas_snapshot,
    )
    from services.data_injection_service import bump_injection_generation

    snap = freeze_cpas_snapshot(db_session)
    bump_injection_generation(db_session, "fss")
    db_session.commit()
    before = load_iap_admission_generation(db_session)
    with pytest.raises(CpasGenerationDriftError):
        _run_pipeline_critical_section(db_session, snap)
    assert cpas_reevaluation_required(db_session) is not None
    assert load_iap_admission_generation(db_session) == before


def test_grant_and_cpas_serialized(db_session: Session, monkeypatch):
    """G: CPAS critical section waits while Grant holds IAP admission."""
    import services.iap.admission as admission_mod
    from services.cpas_service import freeze_cpas_snapshot, _run_pipeline_critical_section

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_FAR_LAT, _FAR_LON, fcc="fcc-gc", serial="ser-gc"),
    )
    _ensure_ids(db_session, "fcc-gc", cbsd.user_id)
    record_iap_admission_generation(db_session)
    db_session.commit()

    far_esc = _esc_point(lat=_FAR_LAT, lon=_FAR_LON)
    orig = admission_mod.proposed_grant_violates_iap
    entered = threading.Event()
    release = threading.Event()

    def _gated(db, cbsd_obj, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return orig(
            db,
            cbsd_obj,
            points=[far_esc],
            coupling=_const_coupling(1e-15),
            low_hz=kwargs["low_hz"],
            high_hz=kwargs["high_hz"],
            max_eirp_dbm_mhz=kwargs["max_eirp_dbm_mhz"],
        )

    monkeypatch.setattr(admission_mod, "proposed_grant_violates_iap", _gated)
    monkeypatch.setattr(
        "services.cpas_service.evaluate_cpas_protections",
        lambda db, snapshot: [],
    )
    monkeypatch.setattr(
        "services.cpas_service.apply_cpas_decisions",
        lambda db, decisions: 0,
    )
    monkeypatch.setattr(
        "services.cpas_service.create_full_activity_dump",
        lambda db, commit=True: type("D", (), {"id": 7})(),
    )
    monkeypatch.setattr(
        "services.cpas_schedule_service.mark_scheduled_success_if_applicable",
        lambda db: None,
    )

    bind = db_session.get_bind()
    cpas_finished = threading.Event()
    cpas_saw_hold = threading.Event()

    def _grant_worker() -> None:
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=bind)()
        try:
            process_grant(
                s,
                [_grant_req(cbsd.cbsd_id, low=_BAND_LOW, high=_BAND_HIGH, eirp=10.0)],
            )
        finally:
            s.close()

    def _cpas_worker() -> None:
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=bind)()
        try:
            assert entered.wait(timeout=5)
            snap = freeze_cpas_snapshot(s)
            # Entering critical section requires IAP lock — observe hold.
            threading.Event().wait(0.02)
            if not release.is_set():
                cpas_saw_hold.set()
            _run_pipeline_critical_section(s, snap)
            cpas_finished.set()
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        g = pool.submit(_grant_worker)
        c = pool.submit(_cpas_worker)
        assert entered.wait(timeout=5)
        threading.Event().wait(0.05)
        assert not cpas_finished.is_set()
        release.set()
        g.result(timeout=30)
        c.result(timeout=30)
    assert cpas_saw_hold.is_set()
    assert cpas_finished.is_set()


def test_failed_cpas_does_not_stamp_on_fad_failure(db_session: Session, monkeypatch):
    """K: FAD failure rolls back; no valid admission marker from failed CPAS."""
    from services.cpas_service import (
        _run_pipeline_critical_section,
        freeze_cpas_snapshot,
    )

    clear_cpas_reevaluation_required(db_session)
    # Ensure no prior marker.
    from models.models import AdminInjectedData
    from services.iap.admission import KIND_IAP_ADMISSION_GEN

    db_session.query(AdminInjectedData).filter_by(kind=KIND_IAP_ADMISSION_GEN).delete()
    db_session.commit()

    monkeypatch.setattr(
        "services.cpas_service.evaluate_cpas_protections",
        lambda db, snapshot: [],
    )
    monkeypatch.setattr(
        "services.cpas_service.apply_cpas_decisions",
        lambda db, decisions: 0,
    )

    def _boom(db, commit=True):
        raise RuntimeError("fad_publish_failed")

    monkeypatch.setattr("services.cpas_service.create_full_activity_dump", _boom)

    snap = freeze_cpas_snapshot(db_session)
    with pytest.raises(RuntimeError, match="fad_publish_failed"):
        _run_pipeline_critical_section(db_session, snap)
    db_session.rollback()
    assert load_iap_admission_generation(db_session) is None


def test_iap_admission_advisory_key_distinct():
    """PostgreSQL advisory key for IAP admission is distinct from CBSD/FAD/CPAS."""
    from services.concurrency import (
        _ADVISORY_NS_CBSD,
        _ADVISORY_NS_CPAS,
        _ADVISORY_NS_FAD,
        _ADVISORY_NS_GRANT,
        _ADVISORY_NS_IAP,
        _advisory_key,
        iap_admission_advisory_key,
    )

    iap = iap_admission_advisory_key()
    others = {
        _advisory_key(_ADVISORY_NS_CBSD, "x"),
        _advisory_key(_ADVISORY_NS_GRANT, "x"),
        _advisory_key(_ADVISORY_NS_FAD, "publish"),
        _advisory_key(_ADVISORY_NS_CPAS, "pipeline"),
        _advisory_key(_ADVISORY_NS_IAP, "other"),
    }
    assert iap not in others
    assert iap == _advisory_key(_ADVISORY_NS_IAP, "admission")
