"""Grant-time GWPZ protection (canonical predicate + process_grant)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from models.models import AdminInjectedData, FccIdRecord, Grant, UserIdRecord
from services.data_injection_service import KIND_WISP, upsert_wisp_record
from services.geometry import within_geojson_buffer_m
from services.grant_service import process_grant
from services.gwpz_protection import (
    GWPZ_BUFFER_M,
    GwpzProtectionError,
    grant_blocked_by_gwpz,
    gwpz_blocks,
    gwpz_blocks_any,
)
from services.iap.pre_iap_exclusions import evaluate_pre_iap_exclusions
from tests.fixtures.factories import make_cbsd

SUCCESS = 0
INTERFERENCE = 400

# Synthetic GWPZ — deliberately away from quiet-zone / EXZ_1 / GPR_3 fixtures.
_ZONE_LAT, _ZONE_LON = 35.25, -110.40
_HALF = 0.05
_GWPZ_LOW = 3_580_000_000
_GWPZ_HIGH = 3_620_000_000
_OVERLAP_LOW = 3_590_000_000
_OVERLAP_HIGH = 3_600_000_000
_NONOVERLAP_LOW = 3_550_000_000
_NONOVERLAP_HIGH = 3_560_000_000
_INSIDE_LAT, _INSIDE_LON = _ZONE_LAT, _ZONE_LON
_OUTSIDE_LAT, _OUTSIDE_LON = _ZONE_LAT + 1.0, _ZONE_LON - 1.0
# Midpoint of northern edge (on boundary).
_BOUNDARY_LAT = _ZONE_LAT + _HALF
_BOUNDARY_LON = _ZONE_LON


def _square_zone(lon: float, lat: float, half: float) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon - half, lat - half],
                            [lon + half, lat - half],
                            [lon + half, lat + half],
                            [lon - half, lat + half],
                            [lon - half, lat - half],
                        ]
                    ],
                },
            }
        ],
    }


def _gwpz_payload(
    *,
    wisp_id: str = "incumbent/uls/gwpz-synth",
    lat: float = _ZONE_LAT,
    lon: float = _ZONE_LON,
    half: float = _HALF,
    low_hz: int = _GWPZ_LOW,
    high_hz: int = _GWPZ_HIGH,
) -> dict:
    return {
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
        "zone": _square_zone(lon, lat, half),
    }


def _reg(
    lat: float,
    lon: float,
    *,
    fcc: str,
    serial: str,
    cat: str = "A",
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
            "antennaGain": 16.0,
            "antennaBeamwidth": 30,
            "antennaAzimuth": 90,
            "eirpCapability": 47,
        },
    }


def _seed_identity(db: Session, fcc: str, serial: str) -> None:
    db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47))
    db.add(UserIdRecord(user_id=f"user-{serial}"))
    db.commit()


def _cbsd_at(
    db: Session,
    *,
    lat: float,
    lon: float,
    fcc: str,
    serial: str,
    cat: str = "A",
):
    _seed_identity(db, fcc, serial)
    return make_cbsd(
        db,
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id=f"user-{serial}",
        cbsd_category=cat,
        registration=_reg(lat, lon, fcc=fcc, serial=serial, cat=cat),
    )


def _grant_req(cbsd_id: str, low: int, high: int, max_eirp: float = 10.0) -> dict:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": max_eirp,
            "operationFrequencyRange": {
                "lowFrequency": low,
                "highFrequency": high,
            },
        },
    }


def _inject(db: Session, payload: dict | None = None) -> None:
    assert upsert_wisp_record(db, payload or _gwpz_payload())


# --- Predicate unit (H: synthetic IDs/coords) ---


def test_h_synthetic_ids_not_gpr3_constants():
    payload = _gwpz_payload()
    assert "GPR_3" not in json.dumps(payload)
    assert payload["record"]["id"].startswith("incumbent/uls/gwpz")
    assert abs(_ZONE_LAT - 39.05) > 1.0


def test_a_predicate_inside_overlap_true():
    assert (
        gwpz_blocks(
            _INSIDE_LAT,
            _INSIDE_LON,
            _OVERLAP_LOW,
            _OVERLAP_HIGH,
            _gwpz_payload(),
        )
        is True
    )


def test_b_predicate_inside_nonoverlap_false():
    assert (
        gwpz_blocks(
            _INSIDE_LAT,
            _INSIDE_LON,
            _NONOVERLAP_LOW,
            _NONOVERLAP_HIGH,
            _gwpz_payload(),
        )
        is False
    )


def test_c_predicate_outside_overlap_false():
    assert (
        gwpz_blocks(
            _OUTSIDE_LAT,
            _OUTSIDE_LON,
            _OVERLAP_LOW,
            _OVERLAP_HIGH,
            _gwpz_payload(),
        )
        is False
    )


def test_d_boundary_matches_within_geojson_buffer_m():
    payload = _gwpz_payload()
    zone = payload["zone"]
    canonical = within_geojson_buffer_m(
        _BOUNDARY_LAT, _BOUNDARY_LON, zone, GWPZ_BUFFER_M
    )
    blocked = gwpz_blocks(
        _BOUNDARY_LAT,
        _BOUNDARY_LON,
        _OVERLAP_LOW,
        _OVERLAP_HIGH,
        payload,
    )
    assert blocked is canonical
    assert blocked is True


def test_e_category_independent_predicate():
    payload = _gwpz_payload()
    for _cat in ("A", "B"):
        assert (
            gwpz_blocks(
                _INSIDE_LAT,
                _INSIDE_LON,
                _OVERLAP_LOW,
                _OVERLAP_HIGH,
                payload,
            )
            is True
        )


def test_g_malformed_indeterminate_fail_closed_helper():
    bad = {"record": {"id": "x", "type": "GWPZ"}, "zone": {"type": "Polygon"}}
    assert gwpz_blocks(0.0, 0.0, _OVERLAP_LOW, _OVERLAP_HIGH, bad) is None
    with pytest.raises(GwpzProtectionError):
        gwpz_blocks_any(
            0.0,
            0.0,
            _OVERLAP_LOW,
            _OVERLAP_HIGH,
            [bad],
            fail_closed_on_indeterminate=True,
        )


# --- Grant-time (A–E, G) ---


def test_a_grant_inside_overlap_400_no_persist(db_session: Session):
    _inject(db_session)
    cbsd = _cbsd_at(
        db_session,
        lat=_INSIDE_LAT,
        lon=_INSIDE_LON,
        fcc="gwpz-a-fcc",
        serial="gwpz-a-sn",
    )
    resp = process_grant(
        db_session, [_grant_req(cbsd.cbsd_id, _OVERLAP_LOW, _OVERLAP_HIGH)]
    )[0]
    assert resp["response"]["responseCode"] == INTERFERENCE
    assert "grantId" not in resp
    assert db_session.query(Grant).filter_by(cbsd_id=cbsd.cbsd_id).count() == 0


def test_b_grant_inside_nonoverlap_not_denied_by_gwpz(db_session: Session):
    _inject(db_session)
    cbsd = _cbsd_at(
        db_session,
        lat=_INSIDE_LAT,
        lon=_INSIDE_LON,
        fcc="gwpz-b-fcc",
        serial="gwpz-b-sn",
    )
    assert (
        grant_blocked_by_gwpz(
            db_session, _INSIDE_LAT, _INSIDE_LON, _NONOVERLAP_LOW, _NONOVERLAP_HIGH
        )
        is False
    )
    resp = process_grant(
        db_session, [_grant_req(cbsd.cbsd_id, _NONOVERLAP_LOW, _NONOVERLAP_HIGH)]
    )[0]
    # Unrelated protections may still deny; GWPZ must not be the reason when
    # SUCCESS — and when INTERFERENCE, GWPZ predicate alone is False.
    if resp["response"]["responseCode"] == SUCCESS:
        assert "grantId" in resp
    else:
        assert not grant_blocked_by_gwpz(
            db_session, _INSIDE_LAT, _INSIDE_LON, _NONOVERLAP_LOW, _NONOVERLAP_HIGH
        )


def test_c_grant_outside_overlap_not_denied_by_gwpz(db_session: Session):
    _inject(db_session)
    cbsd = _cbsd_at(
        db_session,
        lat=_OUTSIDE_LAT,
        lon=_OUTSIDE_LON,
        fcc="gwpz-c-fcc",
        serial="gwpz-c-sn",
    )
    assert (
        grant_blocked_by_gwpz(
            db_session, _OUTSIDE_LAT, _OUTSIDE_LON, _OVERLAP_LOW, _OVERLAP_HIGH
        )
        is False
    )
    resp = process_grant(
        db_session, [_grant_req(cbsd.cbsd_id, _OVERLAP_LOW, _OVERLAP_HIGH)]
    )[0]
    if resp["response"]["responseCode"] == SUCCESS:
        assert "grantId" in resp


def test_e_grant_category_a_and_b_parity(db_session: Session):
    _inject(db_session)
    codes = []
    for cat, tag in (("A", "ea"), ("B", "eb")):
        cbsd = _cbsd_at(
            db_session,
            lat=_INSIDE_LAT,
            lon=_INSIDE_LON,
            fcc=f"gwpz-{tag}-fcc",
            serial=f"gwpz-{tag}-sn",
            cat=cat,
        )
        resp = process_grant(
            db_session, [_grant_req(cbsd.cbsd_id, _OVERLAP_LOW, _OVERLAP_HIGH)]
        )[0]
        codes.append(resp["response"]["responseCode"])
        assert db_session.query(Grant).filter_by(cbsd_id=cbsd.cbsd_id).count() == 0
    assert codes == [INTERFERENCE, INTERFERENCE]


def test_g_grant_malformed_wisp_fail_closed(db_session: Session):
    db_session.add(
        AdminInjectedData(
            kind=KIND_WISP,
            data_json=json.dumps(
                {
                    "record": {
                        "id": "incumbent/uls/gwpz-bad",
                        "type": "GWPZ",
                        "deploymentParam": [{"operationParam": {}}],
                    },
                    "zone": {"type": "FeatureCollection", "features": []},
                }
            ),
        )
    )
    db_session.commit()
    cbsd = _cbsd_at(
        db_session,
        lat=_INSIDE_LAT,
        lon=_INSIDE_LON,
        fcc="gwpz-g-fcc",
        serial="gwpz-g-sn",
    )
    with pytest.raises(GwpzProtectionError):
        grant_blocked_by_gwpz(
            db_session, _INSIDE_LAT, _INSIDE_LON, _OVERLAP_LOW, _OVERLAP_HIGH
        )
    resp = process_grant(
        db_session, [_grant_req(cbsd.cbsd_id, _OVERLAP_LOW, _OVERLAP_HIGH)]
    )[0]
    assert resp["response"]["responseCode"] == INTERFERENCE
    assert db_session.query(Grant).filter_by(cbsd_id=cbsd.cbsd_id).count() == 0


# --- F: pre-IAP still yields gwpz_exclusion ---


def test_f_pre_iap_gwpz_exclusion_reason():
    payload = _gwpz_payload()
    frozen = SimpleNamespace(
        grant_pk=1,
        latitude=_INSIDE_LAT,
        longitude=_INSIDE_LON,
        low_hz=_OVERLAP_LOW,
        high_hz=_OVERLAP_HIGH,
        terminated=False,
        cbsd_category="A",
    )
    records = [(KIND_WISP, "gwpz", json.dumps(payload))]
    hits = evaluate_pre_iap_exclusions([frozen], records)
    assert len(hits) == 1
    assert hits[0][1] == "gwpz_exclusion"


def test_f_pre_iap_skips_incomplete_wisp():
    """Preserve historical pre-IAP skip of incomplete WISP (not fail-closed)."""
    frozen = SimpleNamespace(
        grant_pk=2,
        latitude=_INSIDE_LAT,
        longitude=_INSIDE_LON,
        low_hz=_OVERLAP_LOW,
        high_hz=_OVERLAP_HIGH,
        terminated=False,
        cbsd_category="B",
    )
    bad = {"record": {"id": "x", "type": "GWPZ"}, "zone": {"type": "Polygon"}}
    hits = evaluate_pre_iap_exclusions(
        [frozen], [(KIND_WISP, "bad", json.dumps(bad))]
    )
    assert hits == []
