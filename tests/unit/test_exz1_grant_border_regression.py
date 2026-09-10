"""Regression: EXZ_1-like grant outcomes without the official harness.

Arrangement R BPR membership used to fail-closed (responseCode 400, no grantId)
for *all* grants above 3650 MHz when numpy/reference_models were unavailable —
including CBSDs far outside the Canadian border sharing zone and outside the
injected exclusion zone.

EXZ_1 geometry is harness-injected GeoJSON (not NTIA KML).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from models.models import FccIdRecord, UserIdRecord
from services.exclusion_zone_service import persist_exclusion_zone
from services.grant_service import process_grant
from tests.fixtures.factories import make_cbsd

# WINNF FT EXZ_1 N2 (outside 50 m) / N3 (inside) coordinates + Arrangement R freqs.
_N2_LAT, _N2_LON = 42.37477, -100.93139
_N3_LAT, _N3_LON = 42.65012, -100.79956
_LOW = 3_650_000_000
_HIGH = 3_660_000_000


def _exz_record_1() -> dict:
    """Load harness EXZ polygon used by official EXZ_1 (exz_record_1)."""
    import os

    candidates: list[Path] = []
    for env in ("SAS_HARNESS_DIR", "WINNFORUM_HARNESS_DIR"):
        raw = os.environ.get(env)
        if raw:
            candidates.append(Path(raw) / "testcases" / "testdata" / "exz_record_1.json")
    candidates.append(
        Path(
            ".cache/winnforum-harness-928c3150adf7b31e53a96b695bf1fbdd3284ecb2"
            "/src/harness/testcases/testdata/exz_record_1.json"
        )
    )
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    # Minimal stand-in covering N3 but not N2 (same bbox idea as harness fixture).
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
                            [-101.0834351, 42.4828454],
                            [-99.8364868, 42.4828454],
                            [-99.8364868, 42.9771201],
                            [-101.0834351, 42.9771201],
                            [-101.0834351, 42.4828454],
                        ]
                    ],
                },
            }
        ],
    }


def _reg(lat: float, lon: float, *, fcc: str, serial: str, cat: str = "A") -> dict:
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


@pytest.fixture()
def exz1_db(db_session: Session) -> Session:
    zone = _exz_record_1()
    persist_exclusion_zone(
        db_session,
        {
            "zone": zone,
            "frequencyRanges": [
                {"lowFrequency": _LOW, "highFrequency": _HIGH},
                {"lowFrequency": 3_660_000_000, "highFrequency": 3_670_000_000},
            ],
        },
    )
    return db_session


def test_exz1_outside_zone_grant_has_grant_id_on_arrangement_r(exz1_db: Session):
    """N2 analogue: outside EXZ + Arrangement R → responseCode 0 + grantId."""
    fcc, serial = "exz1-fcc-n2", "exz1-serial-n2"
    exz1_db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47))
    exz1_db.add(UserIdRecord(user_id=f"user-{serial}"))
    exz1_db.commit()
    cbsd = make_cbsd(
        exz1_db,
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id=f"user-{serial}",
        cbsd_category="A",
        registration=_reg(_N2_LAT, _N2_LON, fcc=fcc, serial=serial),
    )
    resp = process_grant(
        exz1_db,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": 20.0,
                    "operationFrequencyRange": {
                        "lowFrequency": _LOW,
                        "highFrequency": _HIGH,
                    },
                },
            }
        ],
    )[0]
    assert resp["response"]["responseCode"] == 0
    assert "grantId" in resp
    assert resp["cbsdId"] == cbsd.cbsd_id


def test_exz1_inside_zone_grant_interference_no_grant_id(exz1_db: Session):
    """N3 analogue: inside injected EXZ → INTERFERENCE 400, no grantId."""
    fcc, serial = "exz1-fcc-n3", "exz1-serial-n3"
    exz1_db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47))
    exz1_db.add(UserIdRecord(user_id=f"user-{serial}"))
    exz1_db.commit()
    cbsd = make_cbsd(
        exz1_db,
        fcc_id=fcc,
        cbsd_serial_number=serial,
        user_id=f"user-{serial}",
        cbsd_category="A",
        registration=_reg(_N3_LAT, _N3_LON, fcc=fcc, serial=serial),
    )
    resp = process_grant(
        exz1_db,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "operationParam": {
                    "maxEirp": 20.0,
                    "operationFrequencyRange": {
                        "lowFrequency": _LOW,
                        "highFrequency": _HIGH,
                    },
                },
            }
        ],
    )[0]
    assert resp["response"]["responseCode"] == 400
    assert "grantId" not in resp
    assert resp["cbsdId"] == cbsd.cbsd_id
