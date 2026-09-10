"""Peer FAD CBSD registration normalization for IAP GrantRfInfo."""

from __future__ import annotations

from services.iap.peer_fad import (
    grant_rf_infos_from_frozen_peer_cbsds,
    grant_rf_infos_from_peer_cbsd_record,
    peer_grant_rf_id,
)

_SRC = "peer-sas-9"
_LAT = 38.815291
_LON = -100.253004
_H = 7.6
_LOW = 3_630_000_000
_HIGH = 3_640_000_000
_EIRP = 10.0


def _grant(
    gid: str = "SAMPLE_ID_1",
    *,
    terminated: bool = False,
    low: int = _LOW,
    high: int = _HIGH,
    eirp: float = _EIRP,
) -> dict:
    return {
        "id": gid,
        "terminated": terminated,
        "channelType": "GAA",
        "operationParam": {
            "maxEirp": eirp,
            "operationFrequencyRange": {
                "lowFrequency": low,
                "highFrequency": high,
            },
        },
    }


def _install(
    *,
    lat: float = _LAT,
    lon: float = _LON,
    height: float = _H,
    height_type: str = "AGL",
    indoor: bool = False,
) -> dict:
    return {
        "latitude": lat,
        "longitude": lon,
        "height": height,
        "heightType": height_type,
        "indoorDeployment": indoor,
    }


def _nested(
    *,
    cbsd_id: str = "cbsd/peer-a",
    category: str | None = "B",
    install: dict | None = None,
    grants: list | None = None,
    extra_reg: dict | None = None,
) -> dict:
    reg: dict = {"installationParam": install if install is not None else _install()}
    if category is not None:
        reg["cbsdCategory"] = category
    if extra_reg:
        reg.update(extra_reg)
    return {
        "id": cbsd_id,
        "registration": reg,
        "grants": grants if grants is not None else [_grant()],
    }


def _top_level(
    *,
    cbsd_id: str = "cbsd/peer-top",
    category: str | None = "A",
    install: dict | None = None,
    grants: list | None = None,
) -> dict:
    out: dict = {
        "id": cbsd_id,
        "installationParam": install if install is not None else _install(
            lat=39.0, lon=-98.0, height=4.0
        ),
        "grants": grants if grants is not None else [_grant("TOP_G1")],
    }
    if category is not None:
        out["cbsdCategory"] = category
    return out


def test_a_official_nested_registration():
    infos = grant_rf_infos_from_peer_cbsd_record(_nested(), source_sas_id=_SRC)
    assert len(infos) == 1
    g = infos[0]
    assert g.latitude == _LAT
    assert g.longitude == _LON
    assert g.height_m == _H
    assert g.height_is_agl is True
    assert g.indoor is False
    assert g.cbsd_category == "B"
    assert g.low_hz == _LOW
    assert g.high_hz == _HIGH
    assert g.max_eirp_dbm_mhz == _EIRP
    assert g.is_managing_sas is False
    assert g.source_sas_id == _SRC
    assert g.grant_id == peer_grant_rf_id(_SRC, "SAMPLE_ID_1")
    assert g.cbsd_id == "cbsd/peer-a"
    assert g.grant_pk is None


def test_b_top_level_compatibility_unchanged():
    infos = grant_rf_infos_from_peer_cbsd_record(
        _top_level(), source_sas_id=7
    )
    assert len(infos) == 1
    g = infos[0]
    assert g.latitude == 39.0
    assert g.longitude == -98.0
    assert g.height_m == 4.0
    assert g.height_is_agl is True
    assert g.cbsd_category == "A"
    assert g.grant_id == "peer/7/TOP_G1"
    assert g.is_managing_sas is False
    assert g.source_sas_id == "7"


def test_c_registration_precedes_top_level():
    rec = _nested(category="B", install=_install(lat=1.0, lon=2.0, height=9.0))
    rec["installationParam"] = _install(lat=99.0, lon=99.0, height=1.0)
    rec["cbsdCategory"] = "A"
    infos = grant_rf_infos_from_peer_cbsd_record(rec, source_sas_id=_SRC)
    assert len(infos) == 1
    assert infos[0].latitude == 1.0
    assert infos[0].longitude == 2.0
    assert infos[0].height_m == 9.0
    assert infos[0].cbsd_category == "B"


def test_d_category_nested_top_level_and_missing():
    nested = grant_rf_infos_from_peer_cbsd_record(
        _nested(category="b"), source_sas_id=_SRC
    )
    assert nested[0].cbsd_category == "B"

    top = grant_rf_infos_from_peer_cbsd_record(
        _top_level(category="a"), source_sas_id=_SRC
    )
    assert top[0].cbsd_category == "A"

    missing = _nested()
    del missing["registration"]["cbsdCategory"]
    infos = grant_rf_infos_from_peer_cbsd_record(missing, source_sas_id=_SRC)
    assert infos[0].cbsd_category is None

    invalid = _nested(category="Z")
    infos_inv = grant_rf_infos_from_peer_cbsd_record(invalid, source_sas_id=_SRC)
    assert infos_inv[0].cbsd_category is None


def test_e_incomplete_nested_falls_back_to_top_level_install():
    rec = {
        "id": "cbsd/peer-fallback",
        "registration": {"cbsdCategory": "B"},  # no installationParam
        "installationParam": _install(lat=12.0, lon=34.0, height=5.0),
        "grants": [_grant("FB1")],
    }
    infos = grant_rf_infos_from_peer_cbsd_record(rec, source_sas_id=_SRC)
    assert len(infos) == 1
    assert infos[0].latitude == 12.0
    assert infos[0].longitude == 34.0
    assert infos[0].cbsd_category == "B"

    no_install = {
        "id": "cbsd/peer-empty",
        "registration": {"installationParam": "bad"},
        "grants": [_grant("X")],
    }
    assert grant_rf_infos_from_peer_cbsd_record(no_install, source_sas_id=_SRC) == []


def test_f_malformed_installation_skipped():
    bad_lat = _nested(install={"latitude": "x", "longitude": -100.0})
    assert grant_rf_infos_from_peer_cbsd_record(bad_lat, source_sas_id=_SRC) == []

    missing_lon = _nested(install={"latitude": 1.0})
    assert grant_rf_infos_from_peer_cbsd_record(missing_lon, source_sas_id=_SRC) == []


def test_g_multiple_peer_grants_same_cbsd_install():
    rec = _nested(
        grants=[
            _grant("G1", low=3_620_000_000, high=3_630_000_000),
            _grant("G2", low=3_630_000_000, high=3_640_000_000, eirp=8.0),
        ]
    )
    infos = grant_rf_infos_from_peer_cbsd_record(rec, source_sas_id=_SRC)
    assert len(infos) == 2
    assert {i.grant_id for i in infos} == {
        peer_grant_rf_id(_SRC, "G1"),
        peer_grant_rf_id(_SRC, "G2"),
    }
    assert all(i.latitude == _LAT and i.longitude == _LON for i in infos)
    assert all(i.cbsd_category == "B" for i in infos)


def test_h_terminated_peer_grant_skipped():
    rec = _nested(
        grants=[_grant("LIVE"), _grant("DEAD", terminated=True)]
    )
    infos = grant_rf_infos_from_peer_cbsd_record(rec, source_sas_id=_SRC)
    assert len(infos) == 1
    assert infos[0].grant_id == peer_grant_rf_id(_SRC, "LIVE")


def test_i_peer_grant_id_namespacing_unchanged():
    assert peer_grant_rf_id(3, "SAMPLE_ID_32360") == "peer/3/SAMPLE_ID_32360"
    infos = grant_rf_infos_from_peer_cbsd_record(
        _nested(grants=[_grant("SAMPLE_ID_32360")]), source_sas_id=3
    )
    assert infos[0].grant_id == "peer/3/SAMPLE_ID_32360"


def test_frozen_rows_helper_parses_nested():
    rows = [
        (1, _nested(cbsd_id="cbsd/a", grants=[_grant("A1")])),
        (2, _nested(cbsd_id="cbsd/b", grants=[_grant("B1"), _grant("B2")])),
    ]
    infos = grant_rf_infos_from_frozen_peer_cbsds(rows)
    assert len(infos) == 3
    assert infos[0].source_sas_id == "1"
    assert infos[0].grant_id == "peer/1/A1"
