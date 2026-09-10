"""Official SCHEDULED_DPA KML ingestion (JSON compatibility retained)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.models import AdminInjectedData
from services.database_sync_service import DatabaseSyncError, _apply_scheduled_dpa
from services.dpa_service import (
    KIND_CATALOGUE,
    FrequencyRange,
    get_catalogue_definition,
    list_active_activations,
    list_catalogue,
    load_dpas,
    parse_dpa_kml_bytes,
)
from services.federal_db_service import get_sync_meta

# Synthetic portal-style KML (official schema; no harness DPA names).
_KML_V1 = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>PortalSynthOne</name>
      <ExtendedData>
        <Data name="portalOrg"><value>PortalSynthOne</value></Data>
        <Data name="freqRangeMHz"><value>3550-3570</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>40</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>30</value></Data>
        <Data name="antennaBeamwidthDeg"><value>3</value></Data>
        <Data name="minAzimuthDeg"><value>0</value></Data>
        <Data name="maxAzimuthDeg"><value>360</value></Data>
      </ExtendedData>
      <Point><coordinates>-75.05,38.05,0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>PortalSynthTwo</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3580-3600</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>50</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
        <Data name="refHeightMeters"><value>25</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -76.0,39.0,0 -76.1,39.0,0 -76.1,39.1,0 -76.0,39.1,0 -76.0,39.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

_KML_V2 = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>PortalSynthOne</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3550-3560</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>35</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-140</value></Data>
        <Data name="refHeightMeters"><value>28</value></Data>
      </ExtendedData>
      <Point><coordinates>-75.05,38.05,0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>PortalSynthTwo</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3580-3590</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>45</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-140</value></Data>
        <Data name="refHeightMeters"><value>22</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -76.0,39.0,0 -76.1,39.0,0 -76.1,39.1,0 -76.0,39.1,0 -76.0,39.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

_CAT_KML = """<?xml version="1.0" encoding="utf-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>UnrelatedKeepMe</name>
      <ExtendedData>
        <Data name="freqRangeMHz"><value>3620-3630</value></Data>
        <Data name="catBNeighborhoodDistanceKm"><value>10</value></Data>
        <Data name="protectionCritDbmPer10MHz"><value>-144</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -70.0,40.0,0 -70.1,40.0,0 -70.1,40.1,0 -70.0,40.1,0 -70.0,40.0,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


def _sched_json(dpa_id: str = "UnrelatedKeepMe") -> bytes:
    return json.dumps(
        {
            "activations": [
                {
                    "dpaId": dpa_id,
                    "frequencyRange": {
                        "lowFrequency": 3_620_000_000,
                        "highFrequency": 3_630_000_000,
                    },
                }
            ]
        }
    ).encode()


@pytest.fixture
def seeded_unrelated(db_session, tmp_path: Path):
    path = tmp_path / "unrelated.kml"
    path.write_text(_CAT_KML, encoding="utf-8")
    load_dpas(db_session, kml_paths=[path])
    # Deactivate catalogue-wide ESC activations; scheduled path owns activations.
    from services.dpa_service import clear_activations

    clear_activations(db_session, commit=False)
    db_session.commit()
    return path


# --- A: official-style KML accepted -------------------------------------------


def test_a_official_style_kml_accepted(db_session, seeded_unrelated, monkeypatch):
    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        lambda db, channels: None,
    )
    before_meta = get_sync_meta(db_session)
    _apply_scheduled_dpa(db_session, _KML_V1.encode())
    db_session.commit()

    ids = {c["dpaId"] for c in list_catalogue(db_session)}
    assert "PortalSynthOne" in ids
    assert "PortalSynthTwo" in ids
    assert "UnrelatedKeepMe" in ids

    one = get_catalogue_definition(db_session, "PortalSynthOne")
    assert one is not None
    assert one["frequencyRange"]["lowFrequency"] == 3_550_000_000
    assert one["frequencyRange"]["highFrequency"] == 3_570_000_000
    assert one["geometry"]["type"] == "Point"
    assert one["protectionParams"]["protectionCritDbmPer10MHz"] == -144.0
    assert one["neighborhoodKm"]["catBNeighborhoodDistanceKm"] == 40.0

    acts = list_active_activations(db_session)
    scheduled = [a for a in acts if a.get("source") == "scheduled_dpa"]
    assert scheduled
    assert {a["dpaId"] for a in scheduled} == {"PortalSynthOne", "PortalSynthTwo"}
    # 3550-3570 → 2 channels; 3580-3600 → 2 channels
    assert len([a for a in scheduled if a["dpaId"] == "PortalSynthOne"]) == 2
    assert len([a for a in scheduled if a["dpaId"] == "PortalSynthTwo"]) == 2

    meta = get_sync_meta(db_session)
    assert meta.get("dpa", 0) > before_meta.get("dpa", 0)


# --- B: JSON compatibility ----------------------------------------------------


def test_b_json_scheduled_dpa_still_works(db_session, seeded_unrelated, monkeypatch):
    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        lambda db, channels: None,
    )
    _apply_scheduled_dpa(db_session, _sched_json())
    db_session.commit()
    acts = [
        a
        for a in list_active_activations(db_session)
        if a.get("source") == "scheduled_dpa"
    ]
    assert len(acts) == 1
    assert acts[0]["dpaId"] == "UnrelatedKeepMe"


# --- C: malformed KML fail-closed ---------------------------------------------


def test_c_malformed_kml_fail_closed(db_session, seeded_unrelated):
    cat_before = (
        db_session.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).one().data_json
    )
    acts_before = list_active_activations(db_session)
    meta_before = get_sync_meta(db_session)

    with pytest.raises(DatabaseSyncError, match="scheduled_dpa_invalid_kml"):
        _apply_scheduled_dpa(db_session, b"<kml>not-closed")
    db_session.rollback()

    cat_after = (
        db_session.query(AdminInjectedData).filter_by(kind=KIND_CATALOGUE).one().data_json
    )
    assert cat_before == cat_after
    assert list_active_activations(db_session) == acts_before
    assert get_sync_meta(db_session) == meta_before


def test_c_empty_kml_fail_closed(db_session, seeded_unrelated):
    empty = b"""<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document></Document></kml>"""
    with pytest.raises(DatabaseSyncError, match="scheduled_dpa_kml_empty"):
        _apply_scheduled_dpa(db_session, empty)
    db_session.rollback()


# --- D: update/replacement ----------------------------------------------------


def test_d_modified_kml_replaces_scheduled_state(
    db_session, seeded_unrelated, monkeypatch
):
    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        lambda db, channels: None,
    )
    # Manual non-scheduled activation must survive.
    from services.dpa_service import _upsert_activation

    _upsert_activation(
        db_session,
        dpa_id="UnrelatedKeepMe",
        freq=FrequencyRange(3_620_000_000, 3_630_000_000),
        movelist=[],
        source="manual",
    )
    db_session.commit()

    _apply_scheduled_dpa(db_session, _KML_V1.encode())
    db_session.commit()
    _apply_scheduled_dpa(db_session, _KML_V2.encode())
    db_session.commit()

    one = get_catalogue_definition(db_session, "PortalSynthOne")
    assert one["frequencyRange"]["highFrequency"] == 3_560_000_000
    assert one["protectionParams"]["protectionCritDbmPer10MHz"] == -140.0
    assert one["neighborhoodKm"]["catBNeighborhoodDistanceKm"] == 35.0

    scheduled = [
        a
        for a in list_active_activations(db_session)
        if a.get("source") == "scheduled_dpa"
    ]
    # V2: PortalSynthOne 3550-3560 → 1 ch; PortalSynthTwo 3580-3590 → 1 ch
    assert len(scheduled) == 2
    highs = {
        (a["dpaId"], int(a["frequencyRange"]["highFrequency"])) for a in scheduled
    }
    assert ("PortalSynthOne", 3_560_000_000) in highs
    assert ("PortalSynthTwo", 3_590_000_000) in highs
    # Stale 3570 / 3600 channels gone
    assert not any(
        int(a["frequencyRange"]["highFrequency"]) == 3_570_000_000 for a in scheduled
    )

    manual = [
        a
        for a in list_active_activations(db_session)
        if a.get("source") == "manual" and a.get("dpaId") == "UnrelatedKeepMe"
    ]
    assert len(manual) == 1
    assert get_catalogue_definition(db_session, "UnrelatedKeepMe") is not None


# --- E: channel materialization -----------------------------------------------


def test_e_kml_range_channelized(db_session, seeded_unrelated, monkeypatch):
    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        lambda db, channels: None,
    )
    _apply_scheduled_dpa(db_session, _KML_V1.encode())
    db_session.commit()
    one_acts = [
        a
        for a in list_active_activations(db_session)
        if a.get("dpaId") == "PortalSynthOne" and a.get("source") == "scheduled_dpa"
    ]
    ranges = sorted(
        (int(a["frequencyRange"]["lowFrequency"]), int(a["frequencyRange"]["highFrequency"]))
        for a in one_acts
    )
    assert ranges == [
        (3_550_000_000, 3_560_000_000),
        (3_560_000_000, 3_570_000_000),
    ]


# --- F: no hard-coded DPA IDs / data-driven parse -----------------------------


def test_f_parse_bytes_data_driven_synthetic_name():
    defs = parse_dpa_kml_bytes(_KML_V1.encode(), source="unit")
    assert {d.dpa_id for d in defs} == {"PortalSynthOne", "PortalSynthTwo"}


# --- G: protection metadata visible -------------------------------------------


def test_g_protection_metadata_on_catalogue(db_session, seeded_unrelated, monkeypatch):
    monkeypatch.setattr(
        "services.dpa_service.refresh_or_fail_closed_movelists",
        lambda db, channels: None,
    )
    _apply_scheduled_dpa(db_session, _KML_V1.encode())
    db_session.commit()
    _apply_scheduled_dpa(db_session, _KML_V2.encode())
    db_session.commit()
    one = get_catalogue_definition(db_session, "PortalSynthOne")
    assert one["protectionParams"]["protectionCritDbmPer10MHz"] == -140.0
    assert one["protectionParams"]["refHeightMeters"] == 28.0
    # Decision path reads catalogue protectionParams / neighborhoodKm.
    from services.dpa_protection import (
        _ref_height_from_params,
        _threshold_from_params,
    )

    assert _threshold_from_params(one["protectionParams"]) == -140.0
    assert _ref_height_from_params(one["protectionParams"]) == 28.0
