"""C5 — QPR / WDB / PCR / FDB residual local coverage."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

from models.models import AdminInjectedData, CpiUser, PalRecord
from services.cpas_reevaluation import (
    cpas_reevaluation_required,
    clear_cpas_reevaluation_required,
    mark_cpas_reevaluation_required,
)
from services.data_injection_service import get_injection_generations
from services.database_sync_service import (
    DatabaseSyncError,
    _apply_scheduled_dpa,
    _sync_cpi,
    _sync_pal,
)
from services.federal_db_service import get_sync_meta
from services.pal_service import load_pal_records, replace_pal_records
from services.ppa_service import KIND_PCR_CONFIG, create_ppa, get_ppa_creation_status
from services.quiet_zone_service import (
    FCC_OFFICE_RADIUS_CAT_A_KM,
    FCC_OFFICE_RADIUS_CAT_B_KM,
    NRQZ_EAST,
    NRQZ_NORTH,
    NRQZ_SOUTH,
    NRQZ_WEST,
    QuietZoneUnavailable,
    fcc_office_radius_km,
    grant_blocked_by_quiet_zone,
    in_nrao_nrro_quiet_zone,
    near_fcc_field_office,
    near_table_mountain,
    quiet_zone_blocks_location,
    reset_fcc_office_cache,
    table_mountain_coordination_km,
)
from tests.fixtures.factories import make_cbsd, make_pal, square_polygon


# Synthetic offset from Allegan MI FCC office (47 CFR 0.121) — not a harness fixture.
_ALLEGAN_LAT = 42.6055833
_ALLEGAN_LON = -85.9555833


def _offset_deg(km: float) -> float:
    return km / 111.32


# ---------------------------------------------------------------------------
# QPR
# ---------------------------------------------------------------------------


def test_qpr2_nrqz_bounds_still_reject():
    lat = (NRQZ_SOUTH + NRQZ_NORTH) / 2.0
    lon = (NRQZ_WEST + NRQZ_EAST) / 2.0
    assert in_nrao_nrro_quiet_zone(lat, lon)
    assert quiet_zone_blocks_location(lat, lon) == "nrqz"


def test_qpr_fcc_cat_a_inside_boundary_and_outside():
    reset_fcc_office_cache()
    assert fcc_office_radius_km("A") == FCC_OFFICE_RADIUS_CAT_A_KM
    # Immediately inside 2.4 km
    lat = _ALLEGAN_LAT + _offset_deg(2.3)
    lon = _ALLEGAN_LON
    assert near_fcc_field_office(lat, lon, cbsd_category="A") is True
    # Exactly at ~2.4 km (use slightly under due to haversine)
    lat_lim = _ALLEGAN_LAT + _offset_deg(2.39)
    assert near_fcc_field_office(lat_lim, lon, cbsd_category="A") is True
    # Outside
    lat_out = _ALLEGAN_LAT + _offset_deg(2.5)
    assert near_fcc_field_office(lat_out, lon, cbsd_category="A") is False


def test_qpr_fcc_cat_b_uses_4_8_km_not_2_4():
    reset_fcc_office_cache()
    assert fcc_office_radius_km("B") == FCC_OFFICE_RADIUS_CAT_B_KM
    lat = _ALLEGAN_LAT + _offset_deg(3.0)  # outside Cat A, inside Cat B
    lon = _ALLEGAN_LON
    assert near_fcc_field_office(lat, lon, cbsd_category="A") is False
    assert near_fcc_field_office(lat, lon, cbsd_category="B") is True
    lat_out = _ALLEGAN_LAT + _offset_deg(5.0)
    assert near_fcc_field_office(lat_out, lon, cbsd_category="B") is False


def test_qpr_table_mountain_distances():
    assert table_mountain_coordination_km("A") == 3.8
    assert table_mountain_coordination_km("B", 5.0) == 38.0
    assert table_mountain_coordination_km("B", 15.0) == 54.0
    assert table_mountain_coordination_km("B", 25.0) == 64.0
    assert table_mountain_coordination_km("B", 40.0) == 80.0
    assert near_table_mountain(40.130660, -105.244596, cbsd_category="A")


def test_qpr_config_disabled_skips_fcc(db_session):
    reset_fcc_office_cache()
    db_session.add(
        AdminInjectedData(
            kind="quiet_zone_config",
            data_json=json.dumps(
                {
                    "fccOfficesEnabled": False,
                    "tableMountainEnabled": False,
                    "configurableAreasEnabled": False,
                }
            ),
        )
    )
    db_session.commit()
    lat = _ALLEGAN_LAT
    lon = _ALLEGAN_LON
    assert (
        quiet_zone_blocks_location(lat, lon, cbsd_category="A", db=db_session) is None
    )


def test_qpr_configurable_area(db_session):
    db_session.add(
        AdminInjectedData(
            kind="quiet_protected_area",
            data_json=json.dumps(
                {"latitude": 35.0, "longitude": -97.0, "radiusKm": 1.0}
            ),
        )
    )
    db_session.add(
        AdminInjectedData(
            kind="quiet_zone_config",
            data_json=json.dumps(
                {
                    "fccOfficesEnabled": False,
                    "tableMountainEnabled": False,
                    "configurableAreasEnabled": True,
                }
            ),
        )
    )
    db_session.commit()
    assert (
        quiet_zone_blocks_location(35.0, -97.0, db=db_session)
        == "configurable_protected_area"
    )
    assert quiet_zone_blocks_location(36.0, -97.0, db=db_session) is None


def test_qpr_missing_fcc_dataset_fail_closed(monkeypatch):
    from dataclasses import dataclass

    from providers.contract import (
        CAPABILITY_REFERENCE_DATA,
        DataKind,
        DatasetProvenance,
        PROVIDER_API_VERSION,
        TOKEN_FCC_FIELD_OFFICES,
    )
    from runtime.composition import RuntimeComposition
    from runtime.context import runtime_composition_scope

    @dataclass(frozen=True, slots=True)
    class _EmptyFccProvider:
        api_version = PROVIDER_API_VERSION
        kind = DataKind.REFERENCE_DATA

        def advertised_capabilities(self):
            return frozenset({CAPABILITY_REFERENCE_DATA})

        def provenance(self):
            return DatasetProvenance("spy", "1", "empty_fcc")

        def fetch(self, *, point=None, token=None):
            if token != TOKEN_FCC_FIELD_OFFICES:
                raise ValueError("unexpected token")
            raise ValueError("FCC office CSV missing")

    composition = RuntimeComposition(
        profile=object(),  # type: ignore[arg-type]
        profile_context=object(),
        requirements=object(),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        protocol_adapter=None,
        device_adapter=None,
        network_adapter=None,
        providers=(_EmptyFccProvider(),),
        rf=None,
        provenance=object(),  # type: ignore[arg-type]
    )

    reset_fcc_office_cache()
    with runtime_composition_scope(composition):
        with pytest.raises(QuietZoneUnavailable):
            near_fcc_field_office(40.0, -90.0, cbsd_category="A")
        assert grant_blocked_by_quiet_zone(40.0, -90.0) is True
    reset_fcc_office_cache()


def test_qpr_grant_blocked_near_fcc():
    reset_fcc_office_cache()
    assert grant_blocked_by_quiet_zone(
        _ALLEGAN_LAT, _ALLEGAN_LON, cbsd_category="A"
    )


# ---------------------------------------------------------------------------
# WDB
# ---------------------------------------------------------------------------


def test_wdb_pal_replace_removes_absent(db_session):
    replace_pal_records(
        db_session,
        [
            {
                "palId": "pal-keep",
                "userId": "u1",
                "licenseStatus": "VALID",
                "channelAssignment": {
                    "primaryAssignment": {
                        "lowFrequency": 3550_000_000,
                        "highFrequency": 3560_000_000,
                    }
                },
            },
            {
                "palId": "pal-drop",
                "userId": "u1",
                "licenseStatus": "VALID",
                "channelAssignment": {
                    "primaryAssignment": {
                        "lowFrequency": 3560_000_000,
                        "highFrequency": 3570_000_000,
                    }
                },
            },
        ],
    )
    assert {p["palId"] for p in load_pal_records(db_session)} == {
        "pal-keep",
        "pal-drop",
    }
    gen1 = get_injection_generations(db_session).get("pal", 0)
    replace_pal_records(
        db_session,
        [
            {
                "palId": "pal-keep",
                "userId": "u1",
                "licenseStatus": "VALID",
                "channelAssignment": {
                    "primaryAssignment": {
                        "lowFrequency": 3550_000_000,
                        "highFrequency": 3560_000_000,
                    }
                },
            }
        ],
    )
    assert {p["palId"] for p in load_pal_records(db_session)} == {"pal-keep"}
    assert get_injection_generations(db_session).get("pal", 0) == gen1 + 1


def test_wdb_pal_sync_checksum_and_reeval(db_session):
    payload = [
        {
            "palId": "pal-sync",
            "userId": "u1",
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3550_000_000,
                    "highFrequency": 3560_000_000,
                }
            },
        }
    ]
    body = json.dumps(payload).encode()
    digest = hashlib.sha1(body).hexdigest()
    with patch(
        "services.database_sync_service._http_get", return_value=body
    ):
        _sync_pal(db_session, "https://example.test/pal.json", checksum=digest)
        db_session.commit()
    assert load_pal_records(db_session)[0]["palId"] == "pal-sync"
    assert cpas_reevaluation_required(db_session) is not None

    with patch(
        "services.database_sync_service._http_get", return_value=body
    ):
        with pytest.raises(DatabaseSyncError, match="checksum"):
            _sync_pal(db_session, "https://example.test/pal.json", checksum="deadbeef")


def test_wdb_cpi_reconcile_revokes_absent(db_session):
    db_session.add(
        CpiUser(cpi_id="cpi-old", cpi_name="old", cpi_public_key="OLDKEY")
    )
    db_session.commit()
    index = "cpiId,status,publicKeyIdentifier\ncpi-new,ACTIVE,https://keys/new\n"
    key_body = b"NEWKEY"

    def _get(url, *, auth=False):
        if "keys" in url:
            return key_body
        return index.encode()

    with patch("services.database_sync_service._http_get", side_effect=_get):
        _sync_cpi(db_session, "https://example.test/cpi.csv")
        db_session.commit()
    ids = {r.cpi_id for r in db_session.query(CpiUser).all()}
    assert ids == {"cpi-new"}
    assert cpas_reevaluation_required(db_session) is not None


def test_wdb_n_n1_reevaluation_flag(db_session):
    clear_cpas_reevaluation_required(db_session)
    mark_cpas_reevaluation_required(db_session, reason="test", generation={"pal": 1})
    db_session.commit()
    flag = cpas_reevaluation_required(db_session)
    assert flag and flag["reason"] == "test"
    clear_cpas_reevaluation_required(db_session)
    db_session.commit()
    assert cpas_reevaluation_required(db_session) is None


# ---------------------------------------------------------------------------
# PCR
# ---------------------------------------------------------------------------


def _cbsd_at(db, *, user_id: str, lat: float, lon: float):
    cbsd = make_cbsd(db, user_id=user_id)
    cbsd.registration_json = json.dumps(
        {
            "fccId": cbsd.fcc_id,
            "cbsdSerialNumber": cbsd.cbsd_serial_number,
            "userId": user_id,
            "cbsdCategory": "A",
            "installationParam": {
                "latitude": lat,
                "longitude": lon,
                "height": 10,
                "heightType": "AGL",
                "indoorDeployment": False,
                "antennaGain": 0,
                "antennaBeamwidth": 360,
            },
        }
    )
    db.commit()
    return cbsd


def _ppa_body(data: dict | None = None, **kwargs):
    from tests.fixtures.ppa_rf import fake_ppa_rf_engines

    body = dict(data or {})
    body.update(kwargs)
    body.setdefault("_rfEngines", fake_ppa_rf_engines())
    return body


def test_pcr_clips_hull_to_service_area(db_session):
    area = square_polygon(-97.0, 35.0, 0.02)
    pal = make_pal(
        db_session,
        user_id="holder",
        record_json={
            "palId": "pal-sa",
            "userId": "holder",
            "licenseStatus": "VALID",
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3550_000_000,
                    "highFrequency": 3560_000_000,
                }
            },
            "license": {
                "licenseArea": {
                    "type": "FeatureCollection",
                    "features": [
                        {"type": "Feature", "properties": {}, "geometry": area}
                    ],
                }
            },
        },
        pal_id="pal-sa",
    )
    cbsd = _cbsd_at(db_session, user_id="holder", lat=35.0, lon=-97.0)
    ppa_id = create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}
    ))
    assert ppa_id
    assert get_ppa_creation_status(db_session)["withError"] is False


def test_pcr_claimed_boundary_alias(db_session):
    area = square_polygon(-97.0, 35.0, 0.05)
    pal = make_pal(db_session, user_id="holder")
    cbsd = _cbsd_at(db_session, user_id="holder", lat=35.0, lon=-97.0)
    claimed = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": square_polygon(-97.0, 35.0, 0.0005),
            }
        ],
    }
    # No service area on PAL — claimedBoundary accepted when CBSDs inside.
    ppa_id = create_ppa(db_session, _ppa_body({
            "palIds": [pal.pal_id],
            "cbsdIds": [cbsd.cbsd_id],
            "claimedBoundary": claimed,
        }),
    )
    assert ppa_id
    del area


def test_pcr_census_required_fail_closed(db_session):
    db_session.add(
        AdminInjectedData(
            kind=KIND_PCR_CONFIG,
            data_json=json.dumps({"requireCensusClip": True}),
        )
    )
    db_session.commit()
    pal = make_pal(db_session, user_id="holder")
    cbsd = _cbsd_at(db_session, user_id="holder", lat=35.0, lon=-97.0)
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}
        ))
        == ""
    )
    assert get_ppa_creation_status(db_session)["withError"] is True


def test_pcr_inactive_pal_rejected(db_session):
    pal = make_pal(db_session, user_id="holder")
    row = db_session.query(PalRecord).filter_by(pal_id=pal.pal_id).one()
    row.license_status = "EXPIRED"
    data = json.loads(row.record_json)
    data["licenseStatus"] = "EXPIRED"
    row.record_json = json.dumps(data)
    db_session.commit()
    cbsd = _cbsd_at(db_session, user_id="holder", lat=35.0, lon=-97.0)
    assert (
        create_ppa(db_session, _ppa_body({"palIds": [pal.pal_id], "cbsdIds": [cbsd.cbsd_id]}
        ))
        == ""
    )


# ---------------------------------------------------------------------------
# FDB residual
# ---------------------------------------------------------------------------


def test_fdb_scheduled_dpa_materializes_activation(db_session, tmp_path):
    """Known catalogue dpaId materializes; unknown ids are covered in test_fdb_scheduled_dpa."""
    from pathlib import Path

    from services.dpa_service import bulk_dpa_activation, list_active_activations, load_dpas

    kml = tmp_path / "fdb-sched.kml"
    kml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>portal-dpa-1</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -75.0,38.0,0 -75.1,38.0,0 -75.1,38.1,0 -75.0,38.1,0 -75.0,38.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
""",
        encoding="utf-8",
    )
    load_dpas(db_session, kml_paths=[Path(kml)])
    bulk_dpa_activation(db_session, activate=False)
    db_session.commit()
    body = json.dumps(
        {
            "activations": [
                {
                    "dpaId": "portal-dpa-1",
                    "frequencyRange": {
                        "lowFrequency": 3550_000_000,
                        "highFrequency": 3560_000_000,
                    },
                }
            ]
        }
    ).encode()
    _apply_scheduled_dpa(db_session, body)
    db_session.commit()
    meta = get_sync_meta(db_session)
    assert meta["dpa"] >= 1

    acts = list_active_activations(db_session)
    assert any(a.get("dpaId") == "portal-dpa-1" for a in acts)
    raw = db_session.query(AdminInjectedData).filter_by(kind="scheduled_dpa").all()
    assert raw
    payload = json.loads(raw[0].data_json)
    assert payload.get("activations")


def test_fdb_scheduled_dpa_invalid_json_raises(db_session):
    with pytest.raises(DatabaseSyncError):
        _apply_scheduled_dpa(db_session, b"not-json")


def test_fdb_scheduled_dpa_unknown_id_rejected(db_session):
    with pytest.raises(DatabaseSyncError, match="unknown_dpaId"):
        _apply_scheduled_dpa(
            db_session,
            json.dumps(
                {
                    "activations": [
                        {
                            "dpaId": "no-such-dpa",
                            "frequencyRange": {
                                "lowFrequency": 3550_000_000,
                                "highFrequency": 3560_000_000,
                            },
                        }
                    ]
                }
            ).encode(),
        )
