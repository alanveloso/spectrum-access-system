"""Federal-sync FSS is not a grant-time IAP constraint."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from models.models import AdminInjectedData, FccIdRecord, UserIdRecord
from services.concurrency import reset_resource_locks_for_tests
from services.data_injection_service import upsert_fss_record
from services.federal_db_service import (
    grant_blocked_by_fss_gwbl,
    replace_fss_from_federal_payload,
)
from services.fss_provenance import (
    KIND_FSS_PROVENANCE,
    SOURCE_ADMIN_INJECTED,
    SOURCE_FEDERAL_DB_SYNC,
    exclude_federal_sync_fss_from_grant_admission,
    load_fss_provenance_map,
    persist_fss_provenance_map,
)
from services.grant_service import process_grant
from services.iap.admission import (
    proposed_grant_violates_iap,
    record_iap_admission_generation,
)
from services.iap.models import ProtectedEntityKind, ProtectionPoint
from tests.fixtures.factories import make_cbsd

SUCCESS = 0
INTERFERENCE = 400

# Inland synthetic site — not official FDB coordinates.
_FSS_LAT, _FSS_LON = 40.25, -99.40
_NEAR_LAT, _NEAR_LON = 40.26, -99.41
_FAR_GWBL_LAT, _FAR_GWBL_LON = 42.0, -104.0
_NEAR_GWBL_LAT, _NEAR_GWBL_LON = 40.30, -99.50
_COEXIST_LOW = 3_650_000_000
_COEXIST_HIGH = 3_660_000_000
_ESC_LAT, _ESC_LON = 41.0, -99.0


def _federal_site(*, fss_number: str = "SYNTH-FSS-A") -> dict:
    return {
        "earth_station_latitude_decimal": _FSS_LAT,
        "earth_station_longitude_decimal": _FSS_LON,
        "lower_frequency": "3650",
        "upper_frequency": "4200",
        "FSS_number": fss_number,
        "tracking_telemetry_control": "false",
    }


def _admin_fss_body(*, fss_id: str = "admin-fss-1") -> dict:
    return {
        "record": {
            "id": fss_id,
            "type": "FSS",
            "deploymentParam": [
                {
                    "installationParam": {
                        "latitude": _FSS_LAT,
                        "longitude": _FSS_LON,
                    },
                    "operationParam": {
                        "operationFrequencyRange": {
                            "lowFrequency": 3_650_000_000,
                            "highFrequency": 4_200_000_000,
                        }
                    },
                }
            ],
        },
        "ttc": False,
    }


def _reg(lat: float, lon: float, *, fcc: str, serial: str) -> dict:
    return {
        "fccId": fcc,
        "cbsdSerialNumber": serial,
        "userId": f"user-{serial}",
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "measCapability": [],
        "installationParam": {
            "latitude": lat,
            "longitude": lon,
            "height": 6.0,
            "heightType": "AGL",
            "indoorDeployment": False,
            "antennaGain": 16.0,
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


def _grant_req(cbsd_id: str, *, low: int, high: int, eirp: float = 10.0) -> dict:
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


def _add_gwbl(db: Session, *, lat: float, lon: float, gwbl_id: str) -> None:
    db.add(
        AdminInjectedData(
            kind="gwbl",
            data_json=f'{{"id": "{gwbl_id}", "latitude": {lat}, "longitude": {lon}}}',
        )
    )
    db.commit()


def _esc_point() -> ProtectionPoint:
    return ProtectionPoint(
        point_id="esc/synth/fss-admission",
        latitude=_ESC_LAT,
        longitude=_ESC_LON,
        low_hz=3_550_000_000,
        high_hz=3_680_000_000,
        threshold_dbm=-109.0,
        entity_kind=ProtectedEntityKind.ESC,
        neighborhood_km=80.0,
        receiver_height_m=10.0,
        receiver_antenna_azimuth_deg=0.0,
        receiver_antenna_gain_pattern_dbi=tuple(0.0 for _ in range(360)),
    )


@pytest.fixture(autouse=True)
def _reset_locks():
    reset_resource_locks_for_tests()
    yield
    reset_resource_locks_for_tests()


def test_a_federal_sync_fss_only_grant_succeeds_without_iap_deny(
    db_session: Session,
) -> None:
    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    db_session.commit()
    mapping = load_fss_provenance_map(db_session)
    assert SOURCE_FEDERAL_DB_SYNC in mapping.values()

    record_iap_admission_generation(db_session)
    db_session.commit()

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-a", serial="ser-a"),
    )
    _ensure_ids(db_session, "fcc-a", cbsd.user_id)
    assert grant_blocked_by_fss_gwbl(
        db_session, _NEAR_LAT, _NEAR_LON, _COEXIST_LOW, _COEXIST_HIGH
    ) is False
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_COEXIST_LOW,
            high_hz=_COEXIST_HIGH,
            max_eirp_dbm_mhz=10.0,
        )
        is False
    )
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_COEXIST_LOW, high=_COEXIST_HIGH)],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert "grantId" in resp[0]


def test_b_federal_sync_fss_near_gwbl_still_denies_400(db_session: Session) -> None:
    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    _add_gwbl(db_session, lat=_NEAR_GWBL_LAT, lon=_NEAR_GWBL_LON, gwbl_id="gwbl-near")
    assert grant_blocked_by_fss_gwbl(
        db_session, _NEAR_LAT, _NEAR_LON, _COEXIST_LOW, _COEXIST_HIGH
    ) is True

    record_iap_admission_generation(db_session)
    db_session.commit()
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-b", serial="ser-b"),
    )
    _ensure_ids(db_session, "fcc-b", cbsd.user_id)
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_COEXIST_LOW, high=_COEXIST_HIGH)],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE
    assert "grantId" not in resp[0]


def test_c_federal_sync_fss_after_gwbl_moved_grant_succeeds(
    db_session: Session,
) -> None:
    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    _add_gwbl(db_session, lat=_FAR_GWBL_LAT, lon=_FAR_GWBL_LON, gwbl_id="gwbl-far")
    assert grant_blocked_by_fss_gwbl(
        db_session, _NEAR_LAT, _NEAR_LON, _COEXIST_LOW, _COEXIST_HIGH
    ) is False

    record_iap_admission_generation(db_session)
    db_session.commit()
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-c", serial="ser-c"),
    )
    _ensure_ids(db_session, "fcc-c", cbsd.user_id)
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_COEXIST_LOW, high=_COEXIST_HIGH)],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_d_admin_injected_fss_remains_grant_time_iap_constraint(
    db_session: Session,
) -> None:
    assert upsert_fss_record(db_session, _admin_fss_body()) is True
    mapping = load_fss_provenance_map(db_session)
    assert mapping.get("admin-fss-1") == SOURCE_ADMIN_INJECTED

    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-d", serial="ser-d"),
    )
    _ensure_ids(db_session, "fcc-d", cbsd.user_id)
    # No coherent generation + applicable injected FSS → fail-closed DENY.
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_COEXIST_LOW,
            high_hz=_COEXIST_HIGH,
            max_eirp_dbm_mhz=10.0,
        )
        is True
    )
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=_COEXIST_LOW, high=_COEXIST_HIGH)],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


def test_e_esc_admission_still_fail_closed_without_generation(
    db_session: Session,
) -> None:
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_ESC_LAT + 0.01, _ESC_LON - 0.01, fcc="fcc-e", serial="ser-e"),
    )
    _ensure_ids(db_session, "fcc-e", cbsd.user_id)
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=3_630_000_000,
            high_hz=3_640_000_000,
            max_eirp_dbm_mhz=10.0,
            points=[_esc_point()],
            coupling=lambda *a, **k: 1e-15,  # noqa: ARG005
        )
        is True
    )


def test_g_ordinary_grant_without_iap_succeeds(db_session: Session) -> None:
    cbsd = make_cbsd(
        db_session,
        registration=_reg(35.5, -101.5, fcc="fcc-g", serial="ser-g"),
    )
    _ensure_ids(db_session, "fcc-g", cbsd.user_id)
    resp = process_grant(
        db_session,
        [_grant_req(cbsd.cbsd_id, low=3_620_000_000, high=3_630_000_000)],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_federal_sync_does_not_delete_admin_injected_fss(db_session: Session) -> None:
    assert upsert_fss_record(db_session, _admin_fss_body(fss_id="keep-admin")) is True
    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="SYNTH-FED")]}
    )
    db_session.commit()
    mapping = load_fss_provenance_map(db_session)
    assert mapping.get("keep-admin") == SOURCE_ADMIN_INJECTED
    assert SOURCE_FEDERAL_DB_SYNC in mapping.values()


def test_federal_replace_upserts_unlabelled_same_id_and_drops_removed_ids(
    db_session: Session,
) -> None:
    """FAD-style unlabelled FSS with the same id becomes federal-sync.

    A later federal file that omits that id must not leave an IAP-eligible
    leftover (the official FSS-table replacement case).
    """
    db_session.add(
        AdminInjectedData(
            kind="fss",
            data_json=(
                '{"record":{"id":"synth-pre","type":"FSS","deploymentParam":'
                '[{"installationParam":{"latitude":40.25,"longitude":-99.4},'
                '"operationParam":{"operationFrequencyRange":'
                '{"lowFrequency":3650000000,"highFrequency":4200000000}}}]},"ttc":false}'
            ),
        )
    )
    db_session.commit()
    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="synth-pre")]}
    )
    db_session.commit()
    assert load_fss_provenance_map(db_session).get("synth-pre") == SOURCE_FEDERAL_DB_SYNC
    n_fss = db_session.query(AdminInjectedData).filter_by(kind="fss").count()
    assert n_fss == 1

    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="synth-next")]}
    )
    db_session.commit()
    mapping = load_fss_provenance_map(db_session)
    assert "synth-pre" not in mapping
    assert mapping.get("synth-next") == SOURCE_FEDERAL_DB_SYNC
    ids = []
    for row in db_session.query(AdminInjectedData).filter_by(kind="fss"):
        ids.append(row.data_json)
    assert all("synth-pre" not in blob for blob in ids)
    n_prov = (
        db_session.query(AdminInjectedData).filter_by(kind="fss_provenance").count()
    )
    assert n_prov == 1


def test_two_federal_sites_share_one_provenance_row(db_session: Session) -> None:
    replace_fss_from_federal_payload(
        db_session,
        {
            "result": [
                _federal_site(fss_number="synth-x"),
                _federal_site(fss_number="synth-y"),
            ]
        },
    )
    db_session.commit()
    mapping = load_fss_provenance_map(db_session)
    assert mapping.get("synth-x") == SOURCE_FEDERAL_DB_SYNC
    assert mapping.get("synth-y") == SOURCE_FEDERAL_DB_SYNC
    n_prov = (
        db_session.query(AdminInjectedData).filter_by(kind="fss_provenance").count()
    )
    assert n_prov == 1


def test_exclude_helper_drops_only_federal_fss_points(db_session: Session) -> None:
    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    db_session.commit()
    fed_id = next(
        k
        for k, v in load_fss_provenance_map(db_session).items()
        if v == SOURCE_FEDERAL_DB_SYNC
    )
    points = [
        ProtectionPoint(
            point_id=f"fss-cc:{fed_id}",
            latitude=_FSS_LAT,
            longitude=_FSS_LON,
            low_hz=_COEXIST_LOW,
            high_hz=3_700_000_000,
            threshold_dbm=-129.0,
            entity_kind=ProtectedEntityKind.FSS_COCHANNEL,
            source_entity_id=fed_id,
        ),
        _esc_point(),
    ]
    filtered = exclude_federal_sync_fss_from_grant_admission(db_session, points)
    kinds = {p.entity_kind for p in filtered}
    assert ProtectedEntityKind.ESC in kinds
    assert ProtectedEntityKind.FSS_COCHANNEL not in kinds


def test_untagged_fss_remains_grant_time_iap_eligible(db_session: Session) -> None:
    db_session.add(
        AdminInjectedData(
            kind="fss",
            data_json=json.dumps(_admin_fss_body(fss_id="untagged-fss")),
        )
    )
    db_session.commit()
    assert "untagged-fss" not in load_fss_provenance_map(db_session)
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_NEAR_LAT, _NEAR_LON, fcc="fcc-u", serial="ser-u"),
    )
    _ensure_ids(db_session, "fcc-u", cbsd.user_id)
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=_COEXIST_LOW,
            high_hz=_COEXIST_HIGH,
            max_eirp_dbm_mhz=10.0,
        )
        is True
    )


def test_mixed_federal_fss_and_esc_still_fail_closed(db_session: Session) -> None:
    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    db_session.commit()
    cbsd = make_cbsd(
        db_session,
        registration=_reg(_ESC_LAT + 0.01, _ESC_LON - 0.01, fcc="fcc-m", serial="ser-m"),
    )
    _ensure_ids(db_session, "fcc-m", cbsd.user_id)
    fed_id = next(
        k
        for k, v in load_fss_provenance_map(db_session).items()
        if v == SOURCE_FEDERAL_DB_SYNC
    )
    federal_pt = ProtectionPoint(
        point_id=f"fss-cc:{fed_id}",
        latitude=_FSS_LAT,
        longitude=_FSS_LON,
        low_hz=_COEXIST_LOW,
        high_hz=3_700_000_000,
        threshold_dbm=-129.0,
        entity_kind=ProtectedEntityKind.FSS_COCHANNEL,
        source_entity_id=fed_id,
        neighborhood_km=150.0,
    )
    assert (
        proposed_grant_violates_iap(
            db_session,
            cbsd,
            low_hz=3_630_000_000,
            high_hz=3_640_000_000,
            max_eirp_dbm_mhz=10.0,
            points=[federal_pt, _esc_point()],
            coupling=lambda *a, **k: 1e-15,  # noqa: ARG005
        )
        is True
    )


def test_same_id_federal_sync_overrides_admin_injected(db_session: Session) -> None:
    assert upsert_fss_record(db_session, _admin_fss_body(fss_id="shared-id")) is True
    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="shared-id")]}
    )
    db_session.commit()
    assert load_fss_provenance_map(db_session).get("shared-id") == SOURCE_FEDERAL_DB_SYNC
    assert db_session.query(AdminInjectedData).filter_by(kind="fss").count() == 1


def test_same_id_injectfss_overrides_federal_sync(db_session: Session) -> None:
    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="shared-id")]}
    )
    db_session.commit()
    assert upsert_fss_record(db_session, _admin_fss_body(fss_id="shared-id")) is True
    assert load_fss_provenance_map(db_session).get("shared-id") == SOURCE_ADMIN_INJECTED
    assert db_session.query(AdminInjectedData).filter_by(kind="fss").count() == 1


def test_duplicate_sidecar_rows_collapse_last_id_wins(db_session: Session) -> None:
    db_session.add(
        AdminInjectedData(
            kind=KIND_FSS_PROVENANCE,
            data_json='{"by_id": {"dup-id": "admin_injected"}}',
        )
    )
    db_session.add(
        AdminInjectedData(
            kind=KIND_FSS_PROVENANCE,
            data_json='{"by_id": {"dup-id": "federal_db_sync"}}',
        )
    )
    db_session.commit()
    mapping = load_fss_provenance_map(db_session)
    assert mapping.get("dup-id") == SOURCE_FEDERAL_DB_SYNC
    persist_fss_provenance_map(db_session, mapping)
    db_session.commit()
    assert (
        db_session.query(AdminInjectedData).filter_by(kind=KIND_FSS_PROVENANCE).count()
        == 1
    )
    assert load_fss_provenance_map(db_session).get("dup-id") == SOURCE_FEDERAL_DB_SYNC


def test_duplicate_fss_rows_same_id_collapsed_on_federal_sync(
    db_session: Session,
) -> None:
    blob = json.dumps(_admin_fss_body(fss_id="dup-fss"))
    db_session.add(AdminInjectedData(kind="fss", data_json=blob))
    db_session.add(AdminInjectedData(kind="fss", data_json=blob))
    db_session.commit()
    replace_fss_from_federal_payload(
        db_session, {"result": [_federal_site(fss_number="dup-fss")]}
    )
    db_session.commit()
    assert db_session.query(AdminInjectedData).filter_by(kind="fss").count() == 1
    assert load_fss_provenance_map(db_session).get("dup-fss") == SOURCE_FEDERAL_DB_SYNC


def test_reset_clears_fss_provenance(db_session: Session) -> None:
    from database import SessionLocal, reset_db

    replace_fss_from_federal_payload(db_session, {"result": [_federal_site()]})
    db_session.commit()
    db_session.close()
    reset_db()
    session = SessionLocal()
    try:
        assert session.query(AdminInjectedData).filter_by(kind="fss").count() == 0
        assert (
            session.query(AdminInjectedData).filter_by(kind=KIND_FSS_PROVENANCE).count()
            == 0
        )
    finally:
        session.close()
