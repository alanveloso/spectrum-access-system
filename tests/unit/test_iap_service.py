"""IAP fairshare + aggregate interference (injectable coupling)."""

from __future__ import annotations

import json

from models.models import Cbsd, Grant
from services.cpas_service import (
    CpasSnapshot,
    apply_cpas_decisions,
    evaluate_cpas_protections,
)
from services.iap import (
    FrequencyChannel,
    GrantRfInfo,
    ProtectedEntityKind,
    ProtectionPoint,
    aggregate_channel,
    apply_pre_iap_margin_db,
    dbm_to_mw,
    grant_overlaps_channel,
    overlapping_iap_channels,
    run_iap,
    run_iap_for_point,
)
from services.iap.models import GrantChannelContribution
from services.lifecycle import GrantState


def _constant_coupling(mw_per_mw_eirp: float):
    """interference_mw = linear_eirp * factor (factor already in mW per mW)."""

    def coupling(grant, point, channel, eirp_dbm_mhz):
        return dbm_to_mw(eirp_dbm_mhz) * mw_per_mw_eirp

    return coupling


def test_overlapping_iap_channels_align_to_5mhz():
    chans = overlapping_iap_channels(3_555_000_000, 3_562_000_000)
    assert chans[0].low_hz == 3_555_000_000
    assert chans[0].high_hz == 3_560_000_000
    assert chans[-1].high_hz >= 3_560_000_000


def test_pre_iap_margin_reduces_threshold():
    assert apply_pre_iap_margin_db(-109.0, 1.0) == -110.0


def test_aggregate_channel_sums_managing_and_peer():
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    contribs = [
        GrantChannelContribution(
            grant_id="g1", channel=ch, interference_mw=2.0, eirp_dbm_mhz=10.0
        ),
        GrantChannelContribution(
            grant_id="g2", channel=ch, interference_mw=3.0, eirp_dbm_mhz=10.0
        ),
    ]
    result = aggregate_channel(
        contribs, channel=ch, threshold_mw=10.0, managing_grant_ids={"g1"}
    )
    assert result.aggregate_mw == 5.0
    assert result.managing_sas_mw == 2.0
    assert result.within_threshold is True


def test_iap_keep_when_under_fairshare():
    point = ProtectionPoint(
        point_id="esc-1",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=0.0,  # 1 mW after margin → still large vs tiny coupling
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    grant = GrantRfInfo(
        grant_id="g1",
        cbsd_id="c1",
        latitude=39.01,
        longitude=-77.01,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,
        grant_pk=1,
    )
    # Tiny coupling → always under fairshare
    result = run_iap_for_point(point, [grant], coupling=_constant_coupling(1e-12))
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "keep"
    assert result.decisions[0].authorized_eirp_dbm_mhz == 10.0


def test_iap_reduces_power_to_meet_fairshare():
    point = ProtectionPoint(
        point_id="esc-1",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-20.0,  # 0.01 mW
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    grant = GrantRfInfo(
        grant_id="g1",
        cbsd_id="c1",
        latitude=39.01,
        longitude=-77.01,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=20.0,  # 100 mW
        grant_pk=1,
    )
    # coupling 1.0 → interference = linear EIRP; need interf < 0.01 → EIRP < -20
    result = run_iap_for_point(point, [grant], coupling=_constant_coupling(1.0))
    decision = result.decisions[0]
    assert decision.action == "reduce_power"
    assert decision.authorized_eirp_dbm_mhz < 20.0
    assert decision.authorized_eirp_dbm_mhz < -20.0
    assert decision.authorized_eirp_dbm_mhz >= -21.0 - 1e-9


def test_iap_peer_grant_counts_in_fairshare_but_no_local_decision():
    """Peer FAD grants participate in fairshare; only managing SAS emits decisions."""
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-10.0,  # 0.1 mW
        pre_iap_margin_db=0.0,
    )
    local = GrantRfInfo(
        grant_id="local",
        cbsd_id="cl",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=30.0,
        is_managing_sas=True,
        grant_pk=1,
    )
    peer = GrantRfInfo(
        grant_id="peer",
        cbsd_id="cp",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=30.0,
        is_managing_sas=False,
        grant_pk=None,
    )
    alone = run_iap_for_point(point, [local], coupling=_constant_coupling(1.0))
    with_peer = run_iap_for_point(
        point, [local, peer], coupling=_constant_coupling(1.0)
    )
    assert len(with_peer.decisions) == 1
    assert with_peer.decisions[0].grant_id == "local"
    # With a peer sharing the quota, local authorized EIRP is lower (or equal).
    assert with_peer.decisions[0].authorized_eirp_dbm_mhz <= alone.decisions[
        0
    ].authorized_eirp_dbm_mhz


def test_apply_cpas_unknown_action_is_skipped(db_session):
    from datetime import datetime, timedelta, timezone

    from services.cpas_service import CpasDecision, apply_cpas_decisions

    cbsd = Cbsd(
        cbsd_id="fcc-unk/s1",
        user_id="u",
        fcc_id="fcc-unk",
        cbsd_serial_number="s1",
        registration_json="{}",
    )
    db_session.add(cbsd)
    db_session.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id="grant-unk",
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3_550_000_000,
        high_frequency=3_555_000_000,
        max_eirp=20.0,
        lifecycle_state=GrantState.AUTHORIZED.value,
        authorized=True,
        grant_expire_time=expire.replace(tzinfo=None),
        grant_json="{}",
    )
    db_session.add(grant)
    db_session.commit()
    changed = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=grant.grant_id,
                cbsd_id=grant.cbsd_id,
                reason="iap",
                action="not_a_real_action",
                explanation="should skip",
            )
        ],
    )
    db_session.commit()
    db_session.refresh(grant)
    assert changed == 0
    assert grant.lifecycle_state == GrantState.AUTHORIZED.value
    assert float(grant.max_eirp) == 20.0


def test_iap_terminates_when_floor_reached():
    point = ProtectionPoint(
        point_id="esc-1",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-200.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    grant = GrantRfInfo(
        grant_id="g1",
        cbsd_id="c1",
        latitude=39.01,
        longitude=-77.01,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=0.0,
        grant_pk=1,
    )
    result = run_iap_for_point(
        point,
        [grant],
        coupling=_constant_coupling(1.0),
    )
    assert result.decisions[0].action == "terminate"


def test_iap_two_grants_share_threshold_fairly():
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-10.0,  # 0.1 mW
        pre_iap_margin_db=0.0,
    )
    grants = [
        GrantRfInfo(
            grant_id="a",
            cbsd_id="ca",
            latitude=0.0,
            longitude=0.0,
            low_hz=3_550_000_000,
            high_hz=3_555_000_000,
            max_eirp_dbm_mhz=30.0,
            grant_pk=1,
        ),
        GrantRfInfo(
            grant_id="b",
            cbsd_id="cb",
            latitude=0.0,
            longitude=0.0,
            low_hz=3_550_000_000,
            high_hz=3_555_000_000,
            max_eirp_dbm_mhz=30.0,
            grant_pk=2,
        ),
    ]
    result = run_iap_for_point(point, grants, coupling=_constant_coupling(1.0))
    assert len(result.decisions) == 2
    for d in result.decisions:
        assert d.action in {"reduce_power", "keep"}
        # fairshare 0.05 mW ≈ -13 dBm
        assert d.authorized_eirp_dbm_mhz <= -10.0


def test_iap_repeatable():
    point = ProtectionPoint(
        point_id="p1",
        latitude=1.0,
        longitude=2.0,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        threshold_dbm=-15.0,
        pre_iap_margin_db=1.0,
    )
    grants = [
        GrantRfInfo(
            grant_id="g",
            cbsd_id="c",
            latitude=1.0,
            longitude=2.0,
            low_hz=3_550_000_000,
            high_hz=3_560_000_000,
            max_eirp_dbm_mhz=25.0,
            grant_pk=9,
        )
    ]
    a = run_iap([point], grants, coupling=_constant_coupling(0.5))
    b = run_iap([point], grants, coupling=_constant_coupling(0.5))
    assert a.merged_decisions == b.merged_decisions


def test_cpas_iap_reduce_power_updates_max_eirp(db_session):
    from datetime import datetime, timedelta, timezone

    cbsd = Cbsd(
        cbsd_id="fcc-iap/s-iap-1",
        user_id="u",
        fcc_id="fcc-iap",
        cbsd_serial_number="s-iap-1",
        registration_json=json.dumps(
            {
                "fccId": "fcc-iap",
                "cbsdSerialNumber": "s-iap-1",
                "cbsdCategory": "A",
                "installationParam": {
                    "latitude": 39.0,
                    "longitude": -77.0,
                    "height": 4.0,
                    "heightType": "AGL",
                },
            }
        ),
    )
    db_session.add(cbsd)
    db_session.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id="grant-iap-1",
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3_550_000_000,
        high_frequency=3_555_000_000,
        max_eirp=20.0,
        lifecycle_state=GrantState.AUTHORIZED.value,
        authorized=True,
        grant_expire_time=expire.replace(tzinfo=None),
        grant_json="{}",
    )
    db_session.add(grant)
    db_session.commit()

    snapshot = CpasSnapshot(
        frozen_at="t",
        active_grant_pks=(grant.id,),
    )
    point = ProtectionPoint(
        point_id="esc-x",
        latitude=39.0,
        longitude=-77.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-20.0,
        entity_kind=ProtectedEntityKind.ESC,
        pre_iap_margin_db=0.0,
    )
    decisions = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[point],
        iap_coupling=_constant_coupling(1.0),
    )
    assert any(d.reason == "iap" and d.action == "reduce_power" for d in decisions)
    changed = apply_cpas_decisions(db_session, decisions)
    db_session.commit()
    assert changed >= 1
    db_session.refresh(grant)
    assert grant.max_eirp is not None
    assert float(grant.max_eirp) < 20.0
    assert grant.lifecycle_state == GrantState.AUTHORIZED.value


def test_grant_overlaps_channel_helper():
    g = GrantRfInfo(
        grant_id="g",
        cbsd_id="c",
        latitude=0,
        longitude=0,
        low_hz=3_552_000_000,
        high_hz=3_558_000_000,
        max_eirp_dbm_mhz=0,
    )
    ch = FrequencyChannel(low_hz=3_550_000_000, high_hz=3_555_000_000)
    assert grant_overlaps_channel(g, ch) is True


def _peer_cbsd_json(
    *,
    cbsd_id: str,
    grant_id: str,
    lat: float,
    lon: float,
    eirp: float = 10.0,
    low_hz: int = 3_550_000_000,
    high_hz: int = 3_555_000_000,
    terminated: bool = False,
) -> str:
    return json.dumps(
        {
            "id": cbsd_id,
            "installationParam": {
                "latitude": lat,
                "longitude": lon,
                "height": 4.0,
                "heightType": "AGL",
            },
            "grants": [
                {
                    "id": grant_id,
                    "terminated": terminated,
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
    )


def test_peer_fad_converter_skips_invalid_and_terminated():
    from services.iap.peer_fad import grant_rf_infos_from_peer_cbsd_record

    ok = grant_rf_infos_from_peer_cbsd_record(
        json.loads(_peer_cbsd_json(cbsd_id="p/a", grant_id="pg1", lat=1.0, lon=2.0)),
        source_sas_id=7,
    )
    assert len(ok) == 1
    assert ok[0].is_managing_sas is False
    assert ok[0].source_sas_id == "7"
    assert ok[0].grant_pk is None
    assert ok[0].grant_id.startswith("peer/7/")

    assert (
        grant_rf_infos_from_peer_cbsd_record(
            json.loads(
                _peer_cbsd_json(
                    cbsd_id="p/b", grant_id="pg2", lat=1.0, lon=2.0, terminated=True
                )
            ),
            source_sas_id=7,
        )
        == []
    )
    assert (
        grant_rf_infos_from_peer_cbsd_record(
            {"id": "x", "grants": [{"id": "g"}]}, source_sas_id=1
        )
        == []
    )


def test_peer_order_does_not_change_iap_result():
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=13.0,
        pre_iap_margin_db=0.0,
    )
    local = GrantRfInfo(
        grant_id="local",
        cbsd_id="cl",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=True,
        grant_pk=1,
    )
    p1 = GrantRfInfo(
        grant_id="peer/1/g",
        cbsd_id="c1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=False,
        source_sas_id="1",
    )
    p2 = GrantRfInfo(
        grant_id="peer/2/g",
        cbsd_id="c2",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=False,
        source_sas_id="2",
    )
    a = run_iap([point], [local, p1, p2], coupling=_constant_coupling(1.0))
    b = run_iap([point], [local, p2, p1], coupling=_constant_coupling(1.0))
    assert a.merged_decisions == b.merged_decisions
    assert all(d.grant_id == "local" for d in a.merged_decisions)


def test_only_peer_grants_produce_no_managing_decisions():
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=-10.0,
        pre_iap_margin_db=0.0,
    )
    peers = [
        GrantRfInfo(
            grant_id="peer/1/a",
            cbsd_id="ca",
            latitude=0.0,
            longitude=0.0,
            low_hz=3_550_000_000,
            high_hz=3_555_000_000,
            max_eirp_dbm_mhz=30.0,
            is_managing_sas=False,
            source_sas_id="1",
        )
    ]
    result = run_iap_for_point(point, peers, coupling=_constant_coupling(1.0))
    assert result.decisions == ()


def test_local_keep_until_peer_forces_reduce():
    """Conceptual: local alone under quota; local+peer forces local reduce only."""
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=13.0,  # ~20 mW
        pre_iap_margin_db=0.0,
    )
    local = GrantRfInfo(
        grant_id="local",
        cbsd_id="cl",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,  # 10 mW
        is_managing_sas=True,
        grant_pk=1,
    )
    peer = GrantRfInfo(
        grant_id="peer/9/pg",
        cbsd_id="cp",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        max_eirp_dbm_mhz=10.0,
        is_managing_sas=False,
        source_sas_id="9",
    )
    alone = run_iap_for_point(point, [local], coupling=_constant_coupling(1.0))
    assert alone.decisions[0].action == "keep"
    with_peer = run_iap_for_point(
        point, [local, peer], coupling=_constant_coupling(1.0)
    )
    assert len(with_peer.decisions) == 1
    assert with_peer.decisions[0].grant_id == "local"
    assert with_peer.decisions[0].action == "reduce_power"
    assert with_peer.decisions[0].authorized_eirp_dbm_mhz < 10.0


def test_cpas_iap_uses_frozen_peer_not_live_n_plus_one(db_session):
    from datetime import datetime, timedelta, timezone

    from models.models import PeerFadRecord, PeerSas
    from services.cpas_service import freeze_cpas_snapshot

    cbsd = Cbsd(
        cbsd_id="fcc-frz/s1",
        user_id="u",
        fcc_id="fcc-frz",
        cbsd_serial_number="s1",
        registration_json=json.dumps(
            {
                "installationParam": {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "height": 4.0,
                    "heightType": "AGL",
                }
            }
        ),
    )
    db_session.add(cbsd)
    db_session.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id="grant-frz",
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3_550_000_000,
        high_frequency=3_555_000_000,
        max_eirp=10.0,
        lifecycle_state=GrantState.AUTHORIZED.value,
        authorized=True,
        grant_expire_time=expire.replace(tzinfo=None),
        grant_json="{}",
    )
    peer = PeerSas(certificate_hash="peer-frz", url="https://localhost/v1.3")
    db_session.add(peer)
    db_session.flush()
    # Generation N: peer contributes interference → local must reduce.
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id="peer-cbsd-n",
            data_json=_peer_cbsd_json(
                cbsd_id="peer-cbsd-n",
                grant_id="peer-g-n",
                lat=0.0,
                lon=0.0,
                eirp=10.0,
            ),
        )
    )
    db_session.add(grant)
    db_session.commit()

    snapshot = freeze_cpas_snapshot(db_session)
    assert snapshot.peer_record_count == 1
    assert snapshot.peer_records[0][0] == peer.id

    # Live DB replaced with N+1 (no peer grants) after freeze — must not affect IAP.
    live = (
        db_session.query(PeerFadRecord)
        .filter_by(peer_sas_id=peer.id, record_id="peer-cbsd-n")
        .one()
    )
    live.data_json = json.dumps({"id": "peer-cbsd-n", "grants": []})
    db_session.commit()

    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=13.0,
        pre_iap_margin_db=0.0,
    )
    decisions = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[point],
        iap_coupling=_constant_coupling(1.0),
    )
    assert any(
        d.action == "reduce_power" and d.grant_pk == grant.id for d in decisions
    ), decisions

    # Control: evaluating a fresh freeze of N+1 must keep (no peer interference).
    snap_n1 = freeze_cpas_snapshot(db_session)
    keep_decisions = evaluate_cpas_protections(
        db_session,
        snap_n1,
        iap_points=[point],
        iap_coupling=_constant_coupling(1.0),
    )
    assert keep_decisions == []


def test_cpas_iap_multiple_peers_and_no_peer_mutation(db_session):
    from datetime import datetime, timedelta, timezone

    from models.models import PeerSas
    from services.cpas_service import CpasDecision

    cbsd = Cbsd(
        cbsd_id="fcc-mp/s1",
        user_id="u",
        fcc_id="fcc-mp",
        cbsd_serial_number="s1",
        registration_json=json.dumps(
            {
                "installationParam": {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "height": 4.0,
                    "heightType": "AGL",
                }
            }
        ),
    )
    db_session.add(cbsd)
    db_session.flush()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    grant = Grant(
        grant_id="grant-mp",
        cbsd_pk=cbsd.id,
        cbsd_id=cbsd.cbsd_id,
        low_frequency=3_550_000_000,
        high_frequency=3_555_000_000,
        max_eirp=10.0,
        lifecycle_state=GrantState.AUTHORIZED.value,
        authorized=True,
        grant_expire_time=expire.replace(tzinfo=None),
        grant_json="{}",
    )
    p1 = PeerSas(certificate_hash="p1", url="https://localhost/v1.3")
    p2 = PeerSas(certificate_hash="p2", url="https://peer2/v1.3")
    db_session.add_all([grant, p1, p2])
    db_session.flush()
    db_session.commit()

    snapshot = CpasSnapshot(
        frozen_at="t",
        active_grant_pks=(grant.id,),
        peer_records=(
            (
                p2.id,
                "cbsd",
                "c2",
                _peer_cbsd_json(cbsd_id="c2", grant_id="g2", lat=0.0, lon=0.0),
            ),
            (
                p1.id,
                "cbsd",
                "c1",
                _peer_cbsd_json(cbsd_id="c1", grant_id="g1", lat=0.0, lon=0.0),
            ),
        ),
        peer_record_count=2,
    )
    point = ProtectionPoint(
        point_id="p1",
        latitude=0.0,
        longitude=0.0,
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
        threshold_dbm=13.0,
        pre_iap_margin_db=0.0,
    )
    decisions = evaluate_cpas_protections(
        db_session,
        snapshot,
        iap_points=[point],
        iap_coupling=_constant_coupling(1.0),
    )
    assert decisions, "expected IAP reduce with two peer FAD grants"
    assert all(d.grant_pk == grant.id for d in decisions)
    assert all(not str(d.grant_id).startswith("peer/") for d in decisions)

    # Malicious peer-shaped decision must not mutate local row.
    before = float(grant.max_eirp)
    skipped = apply_cpas_decisions(
        db_session,
        [
            CpasDecision(
                grant_pk=grant.id,
                grant_id=f"peer/{p1.id}/g1",
                cbsd_id="c1",
                reason="iap",
                action="terminate",
                explanation="must skip",
            )
        ],
    )
    db_session.commit()
    db_session.refresh(grant)
    assert skipped == 0
    assert float(grant.max_eirp) == before
    assert grant.lifecycle_state == GrantState.AUTHORIZED.value


def test_eirp_floor_matches_winnforum_fad_bound():
    from services.iap import DEFAULT_EIRP_FLOOR_DBM_MHZ

    assert DEFAULT_EIRP_FLOOR_DBM_MHZ == -137.0
