"""Behavioral tests for WINNF_FT_S_GRA PAL/PPA, EXZ/FSS-GWBL, peer
conflict and remaining validation branches not covered by
tests/unit/test_grant_protocol.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from models.models import AdminInjectedData, PeerFadRecord, PeerSas
from services.fad_service import fad_cbsd_id
from services.grant_service import (
    _has_freq_conflict,
    _max_allowed_eirp_mhz,
    _parse_freq,
    process_grant,
)
from services.meas_report import FLAG_MEAS_REG, MEAS_WITHOUT_GRANT, set_admin_flag
from tests.fixtures.factories import (
    cat_a_install,
    make_cbsd,
    make_fss,
    make_grant,
    make_pal,
    make_ppa_with_pal,
    square_polygon,
)

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300
INTERFERENCE = 400
GRANT_CONFLICT = 401

LAT, LON = 40.0, -105.27


def _op_param(*, low_hz=3_550_000_000, high_hz=3_560_000_000, max_eirp=20.0) -> dict:
    return {
        "maxEirp": max_eirp,
        "operationFrequencyRange": {"lowFrequency": low_hz, "highFrequency": high_hz},
    }


def _located_cbsd(db_session, *, user_id: str | None = None, extra: dict | None = None):
    cbsd = make_cbsd(db_session, cbsd_category="A", user_id=user_id)
    installation = cat_a_install(lat=LAT, lon=LON)
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": installation,
    }
    if extra:
        registration.update(extra)
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


# --- PAL/PPA: cluster owner success, outsider interference, partial overlap ---


def test_pal_cluster_owner_grant_returns_pal_channel_capped_by_license(db_session):
    cbsd = _located_cbsd(db_session)
    license_exp = (datetime.utcnow() + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pal = make_pal(
        db_session,
        user_id=cbsd.user_id,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
        license_expiration=license_exp,
    )
    make_ppa_with_pal(db_session, pal=pal, cbsd_reference_ids=[cbsd.cbsd_id])

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0]["channelType"] == "PAL"
    assert resp[0]["grantExpireTime"] == license_exp


def test_ppa_outsider_overlap_returns_interference_400(db_session):
    """CBSD inside a PPA polygon but not in the protected cluster: 400 on overlap."""
    cbsd = _located_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id=cbsd.user_id,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["other-fcc/other-serial"],
        zone=square_polygon(LON, LAT),
    )

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


def test_cluster_member_partial_overlap_returns_invalid_103(db_session):
    """Cluster member requesting a range that only partially overlaps the PAL band."""
    cbsd = _located_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id=cbsd.user_id,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    make_ppa_with_pal(db_session, pal=pal, cbsd_reference_ids=[cbsd.cbsd_id])

    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_555_000_000, high_hz=3_565_000_000, max_eirp=20.0
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_fully_inside_pal_channel_without_ownership_returns_interference(db_session):
    """Cluster member fully inside a PAL band it does not own → interference."""
    cbsd = _located_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id="someone-else",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    make_ppa_with_pal(db_session, pal=pal, cbsd_reference_ids=[cbsd.cbsd_id])

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


# --- Exclusion zone (EXZ) -------------------------------------------------------


def test_exclusion_zone_overlap_blocks_grant_400(db_session):
    from services.exclusion_zone_service import persist_exclusion_zone

    cbsd = _located_cbsd(db_session)
    persist_exclusion_zone(
        db_session,
        {
            "zone": square_polygon(LON, LAT),
            "frequencyRanges": [
                {"lowFrequency": 3_550_000_000, "highFrequency": 3_560_000_000}
            ],
        },
    )

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


# --- FSS/GWBL neighborhood block (FDB.6) ----------------------------------------


def test_fss_with_neighboring_gwbl_blocks_grant_400(db_session):
    cbsd = _located_cbsd(db_session)
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-gwbl",
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": LAT, "longitude": LON},
                        "operationParam": {
                            "operationFrequencyRange": {
                                "lowFrequency": 3_700_000_000,
                                "highFrequency": 4_200_000_000,
                            }
                        },
                    }
                ],
            }
        },
    )
    db_session.add(
        AdminInjectedData(
            kind="gwbl",
            data_json=json.dumps({"latitude": LAT, "longitude": LON}),
        )
    )
    db_session.commit()

    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_660_000_000, high_hz=3_670_000_000, max_eirp=20.0
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


# --- Peer SAS grant conflict (GRA_5) --------------------------------------------


def test_peer_sas_active_grant_returns_401(db_session):
    cbsd = _located_cbsd(db_session)
    peer = PeerSas(certificate_hash="peer-cert-1", url="https://peer.example.test/v1.3")
    db_session.add(peer)
    db_session.commit()
    target_id = fad_cbsd_id(cbsd.fcc_id, cbsd.cbsd_serial_number)
    db_session.add(
        PeerFadRecord(
            peer_sas_id=peer.id,
            record_type="cbsd",
            record_id=target_id,
            data_json=json.dumps(
                {"id": target_id, "grants": [{"terminated": False}]}
            ),
        )
    )
    db_session.commit()

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == GRANT_CONFLICT


def test_peer_sas_row_without_fad_records_does_not_return_401(db_session):
    """GRA_5 requires ingested peer FAD grants, not merely an InjectPeerSas row."""
    cbsd = _located_cbsd(db_session)
    db_session.add(
        PeerSas(
            certificate_hash="peer-cert-empty",
            url="https://127.0.0.1:19002/v1.2",
        )
    )
    db_session.commit()

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=20.0)}],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


# --- Blacklist / measReport / operationParam validation branches --------------


def test_blacklisted_cbsd_returns_101_with_echo(db_session):
    from services.blacklist_service import add_fcc_id_blacklist

    cbsd = make_cbsd(db_session)
    add_fcc_id_blacklist(db_session, cbsd.fcc_id)

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param()}],
    )
    assert resp[0]["response"]["responseCode"] == BLACKLISTED
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_missing_operation_param_key_returns_102(db_session):
    cbsd = make_cbsd(db_session)
    resp = process_grant(db_session, [{"cbsdId": cbsd.cbsd_id}])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_operation_param_not_a_dict_returns_102(db_session):
    cbsd = make_cbsd(db_session)
    resp = process_grant(
        db_session, [{"cbsdId": cbsd.cbsd_id, "operationParam": "not-a-dict"}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_operation_frequency_range_not_dict_returns_102(db_session):
    cbsd = make_cbsd(db_session)
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": 20.0,
                    "operationFrequencyRange": "not-a-dict",
                },
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_max_eirp_non_numeric_returns_103(db_session):
    cbsd = make_cbsd(db_session)
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": "not-a-number",
                    "operationFrequencyRange": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_560_000_000,
                    },
                },
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_eirp_capability_from_registration_limits_max_eirp(db_session):
    """installationParam.eirpCapability tightens the allowed max EIRP below cat default."""
    cbsd = make_cbsd(db_session, cbsd_category="A")
    installation = cat_a_install(lat=LAT, lon=LON)
    installation["eirpCapability"] = 15.0
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": installation,
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()

    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param(max_eirp=10.0)}],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_measreport_missing_returns_102_when_flag_set(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "measCapability": [MEAS_WITHOUT_GRANT],
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    set_admin_flag(db_session, FLAG_MEAS_REG)

    resp = process_grant(
        db_session, [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param()}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_registration_json_malformed_treated_as_no_location(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = "not-json{"
    db_session.commit()

    resp = process_grant(
        db_session, [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param()}]
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


# --- Defensive re-check-under-lock branches (race conditions) -----------------


def test_cbsd_disappears_between_lookup_and_lock_returns_103(db_session, monkeypatch):
    cbsd = make_cbsd(db_session)

    monkeypatch.setattr("services.concurrency.lock_cbsd_row", lambda db, cbsd_id: None)
    resp = process_grant(
        db_session, [{"cbsdId": cbsd.cbsd_id, "operationParam": _op_param()}]
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


def test_certificate_rebound_between_check_and_lock_returns_103(db_session, monkeypatch):
    """Simulates a race where the cert binding changes between the pre-lock and
    post-lock certificate checks (defensive re-validation under the row lock)."""
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = make_cbsd(db_session, certificate_hash=owner)
    cbsd_id = cbsd.cbsd_id

    import services.concurrency as concurrency_module

    original_lock = concurrency_module.lock_cbsd_row

    def _rebind_under_lock(db, looked_up_id):
        row = original_lock(db, looked_up_id)
        if row is not None:
            row.certificate_hash = other
        return row

    monkeypatch.setattr(
        "services.concurrency.lock_cbsd_row", _rebind_under_lock
    )
    resp = process_grant(
        db_session,
        [{"cbsdId": cbsd_id, "operationParam": _op_param()}],
        certificate_hash=owner,
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


# --- Direct unit tests on private helpers --------------------------------------


def test_parse_freq_operation_frequency_range_missing_keys():
    assert _parse_freq({"operationFrequencyRange": {"lowFrequency": 1}}) == (
        MISSING_PARAM,
        None,
        None,
    )


def test_parse_freq_low_or_high_none():
    freq = {"operationFrequencyRange": {"lowFrequency": None, "highFrequency": None}}
    assert _parse_freq(freq) == (MISSING_PARAM, None, None)


def test_parse_freq_none_op_returns_missing():
    assert _parse_freq(None) == (MISSING_PARAM, None, None)


def test_has_freq_conflict_continues_past_non_overlapping_existing(db_session):
    cbsd = make_cbsd(db_session)
    non_overlap = make_grant(
        db_session, cbsd, low_hz=3_620_000_000, high_hz=3_630_000_000
    )
    overlap = make_grant(
        db_session, cbsd, low_hz=3_550_000_000, high_hz=3_560_000_000
    )
    existing = [non_overlap, overlap]
    assert _has_freq_conflict(existing, 3_555_000_000, 3_558_000_000) is True


def test_has_freq_conflict_checks_also_pending_list():
    assert (
        _has_freq_conflict(
            [], 3_555_000_000, 3_558_000_000, also_pending=[(3_550_000_000, 3_560_000_000)]
        )
        is True
    )


def test_max_allowed_eirp_uses_registration_eirp_cap(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = json.dumps(
        {"cbsdCategory": "A", "installationParam": {"eirpCapability": 12.0}}
    )
    db_session.commit()
    # Cap should be min(30, 47, 12) - 10 = 2.0
    assert _max_allowed_eirp_mhz(cbsd, 47.0) == 2.0
