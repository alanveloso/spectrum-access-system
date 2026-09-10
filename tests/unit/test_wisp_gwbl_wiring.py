"""InjectWisp PART_90 point → canonical GWBL for FSS↔GWBL grant block (FPR_5)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from main import app
from models.models import AdminInjectedData
from services.data_injection_service import (
    KIND_GWBL,
    KIND_WISP,
    load_injected,
    upsert_fss_record,
    upsert_wisp_record,
)
from services.federal_db_service import get_sync_meta, grant_blocked_by_fss_gwbl
from services.grant_service import process_grant
from tests.fixtures.factories import make_cbsd
from tests.unit.test_grant_pal_ppa import LAT, LON, _located_cbsd, _op_param

SUCCESS = 0
INTERFERENCE = 400

client = TestClient(app)

_FSS_NEAR = {
    "record": {
        "id": "incumbent/ibfs/fpr5-fss",
        "type": "FSS",
        "deploymentParam": [
            {
                "installationParam": {"latitude": LAT, "longitude": LON},
                "operationParam": {
                    "operationFrequencyRange": {
                        "lowFrequency": 3_600_000_000,
                        "highFrequency": 3_700_000_000,
                    }
                },
            }
        ],
    },
    "ttc": False,
}

_GWBL_POINT = {
    "record": {
        "id": "incumbent/uls/fpr5-gwbl",
        "type": "PART_90",
        "deploymentParam": [
            {
                "installationParam": {
                    "latitude": LAT + 0.1,
                    "longitude": LON,
                    "antennaBeamwidth": 30,
                    "antennaAzimuth": 270,
                    "antennaDowntilt": -90,
                },
                "operationParam": {
                    "operationFrequencyRange": {
                        "lowFrequency": 3_650_000_000,
                        "highFrequency": 3_700_000_000,
                    }
                },
            }
        ],
    }
}

_GWPZ = {
    "record": {
        "id": "incumbent/uls/fpr5-gwpz",
        "type": "PART_90",
        "deploymentParam": [
            {
                "operationParam": {
                    "operationFrequencyRange": {
                        "lowFrequency": 3_650_000_000,
                        "highFrequency": 3_700_000_000,
                    }
                }
            }
        ],
    },
    "zone": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [LON - 0.05, LAT - 0.05],
                            [LON + 0.05, LAT - 0.05],
                            [LON + 0.05, LAT + 0.05],
                            [LON - 0.05, LAT + 0.05],
                            [LON - 0.05, LAT - 0.05],
                        ]
                    ],
                },
            }
        ],
    },
}


def test_a_inject_wisp_point_persists_canonical_gwbl(db_session):
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    rows = load_injected(db_session, KIND_GWBL)
    assert len(rows) == 1
    assert rows[0]["id"] == "incumbent/uls/fpr5-gwbl"
    assert rows[0]["latitude"] == LAT + 0.1
    assert rows[0]["longitude"] == LON
    assert load_injected(db_session, KIND_WISP) == []
    assert get_sync_meta(db_session).get("gwbl", 0) >= 1
    assert grant_blocked_by_fss_gwbl(
        db_session, LAT, LON, 3_650_000_000, 3_660_000_000
    ) is False  # FSS not injected yet
    assert upsert_fss_record(db_session, _FSS_NEAR) is True
    assert grant_blocked_by_fss_gwbl(
        db_session, LAT, LON, 3_650_000_000, 3_660_000_000
    ) is True


def test_b_protected_grant_blocked_400(db_session):
    cbsd = _located_cbsd(db_session)
    assert upsert_fss_record(db_session, _FSS_NEAR) is True
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_650_000_000, high_hz=3_660_000_000, max_eirp=10.0
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INTERFERENCE


def test_c_non_overlapping_frequency_not_blocked(db_session, monkeypatch):
    """FSS↔GWBL boolean gate does not block 3550–3560 MHz (only 3650–3700).

    Grant-time IAP may still constrain co-located FSS-blocking independently;
    this test isolates the FPR_5 boolean by disabling IAP admission.
    """
    monkeypatch.setattr(
        "services.iap.admission.proposed_grant_violates_iap",
        lambda *args, **kwargs: False,
    )
    cbsd = _located_cbsd(db_session)
    assert upsert_fss_record(db_session, _FSS_NEAR) is True
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    assert (
        grant_blocked_by_fss_gwbl(
            db_session, LAT, LON, 3_550_000_000, 3_560_000_000
        )
        is False
    )
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_550_000_000, high_hz=3_560_000_000, max_eirp=10.0
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_c_far_location_not_blocked(db_session, monkeypatch):
    monkeypatch.setattr(
        "services.iap.admission.proposed_grant_violates_iap",
        lambda *args, **kwargs: False,
    )
    cbsd = make_cbsd(
        db_session,
        registration={
            "installationParam": {
                "latitude": LAT + 5.0,
                "longitude": LON + 5.0,
                "height": 6.0,
                "heightType": "AGL",
                "indoorDeployment": False,
            }
        },
    )
    assert upsert_fss_record(db_session, _FSS_NEAR) is True
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    assert (
        grant_blocked_by_fss_gwbl(
            db_session, LAT + 5.0, LON + 5.0, 3_650_000_000, 3_660_000_000
        )
        is False
    )
    resp = process_grant(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": _op_param(
                    low_hz=3_650_000_000, high_hz=3_660_000_000, max_eirp=10.0
                ),
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == SUCCESS


def test_d_repeated_upsert_no_duplicate_gwbl(db_session):
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    assert upsert_wisp_record(db_session, _GWBL_POINT) is True
    moved = json.loads(json.dumps(_GWBL_POINT))
    moved["record"]["deploymentParam"][0]["installationParam"]["latitude"] = LAT + 0.2
    assert upsert_wisp_record(db_session, moved) is True
    rows = load_injected(db_session, KIND_GWBL)
    assert len(rows) == 1
    assert rows[0]["latitude"] == LAT + 0.2
    assert rows[0]["id"] == "incumbent/uls/fpr5-gwbl"


def test_e_gwpz_wisp_still_kind_wisp_not_gwbl(db_session):
    assert upsert_wisp_record(db_session, _GWPZ) is True
    assert len(load_injected(db_session, KIND_WISP)) == 1
    assert load_injected(db_session, KIND_GWBL) == []


def test_e_direct_gwbl_row_still_blocks(db_session):
    cbsd = _located_cbsd(db_session)
    assert upsert_fss_record(db_session, _FSS_NEAR) is True
    db_session.add(
        AdminInjectedData(
            kind=KIND_GWBL,
            data_json=json.dumps(
                {"id": "gwbl/direct", "latitude": LAT, "longitude": LON}
            ),
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


def test_admin_http_inject_wisp_gwbl_point(db_session):
    assert client.post("/admin/injectdata/fss", json=_FSS_NEAR).status_code == 200
    assert client.post("/admin/injectdata/wisp", json=_GWBL_POINT).status_code == 200
    rows = load_injected(db_session, KIND_GWBL)
    assert any(r.get("id") == "incumbent/uls/fpr5-gwbl" for r in rows)
