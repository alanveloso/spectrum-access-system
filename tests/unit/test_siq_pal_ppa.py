"""Behavioral tests for WINNF_FT_S_SIQ PAL/PPA, WISP, FSS-edge and
measReport branches not covered by tests/unit/test_spectrum_inquiry_protocol.py.
"""

from __future__ import annotations

import json

from adapters.cbsd import CbsdDeviceAdapter
from models.models import AdminInjectedData
from services.meas_report import FLAG_MEAS_REG, MEAS_WITHOUT_GRANT, set_admin_flag
from services.spectrum_inquiry_service import (
    _build_available_channels,
    _cbsd_location,
    _fss_location_and_freq,
    _pal_freq,
    _point_in_geojson,
    _split_10mhz,
    _subtract_range,
    _wisp_freq,
    process_spectrum_inquiry,
)
from tests.fixtures.factories import (
    cat_a_install,
    full_cbrs_meas_report,
    make_cbsd,
    make_fss,
    make_pal,
    make_ppa_with_pal,
    square_polygon,
)

SUCCESS = 0
MISSING_PARAM = 102
INVALID_PARAM = 103

CBRS_LOW_HZ = 3_550_000_000
CBRS_HIGH_HZ = 3_700_000_000
LAT, LON = 40.0, -105.27


def _registered_cbsd(db_session, *, user_id: str | None = None):
    cbsd = make_cbsd(db_session, cbsd_category="A", user_id=user_id)
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": cat_a_install(lat=LAT, lon=LON),
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


def _full_cbrs_request(cbsd_id: str) -> dict:
    return {
        "cbsdId": cbsd_id,
        "inquiredSpectrum": [
            {"lowFrequency": CBRS_LOW_HZ, "highFrequency": CBRS_HIGH_HZ}
        ],
    }


def _channel_types(channels: list[dict]) -> set[str]:
    return {c["channelType"] for c in channels}


# --- PAL/PPA: cluster owner / cluster non-owner / geographic outsider ----------


def test_pal_cluster_owner_gets_pal_channel_type(db_session):
    cbsd = _registered_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id=cbsd.user_id,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    make_ppa_with_pal(
        db_session, pal=pal, cbsd_reference_ids=[cbsd.cbsd_id]
    )

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    pal_channels = [c for c in channels if c["channelType"] == "PAL"]
    assert len(pal_channels) == 1
    assert pal_channels[0]["frequencyRange"] == {
        "lowFrequency": 3_550_000_000,
        "highFrequency": 3_560_000_000,
    }
    # Remaining spectrum stays GAA; PAL band is not duplicated as GAA.
    gaa_ranges = [c["frequencyRange"] for c in channels if c["channelType"] == "GAA"]
    assert all(r["lowFrequency"] >= 3_560_000_000 for r in gaa_ranges)


def test_pal_cluster_member_without_matching_user_protects_band_only(db_session):
    """Cluster member CBSD whose userId does not own the PAL: band excluded, no PAL type."""
    cbsd = _registered_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id="someone-else",
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    make_ppa_with_pal(
        db_session, pal=pal, cbsd_reference_ids=[cbsd.cbsd_id]
    )

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    assert _channel_types(channels) == {"GAA"}
    assert all(
        c["frequencyRange"]["lowFrequency"] >= 3_560_000_000 for c in channels
    )


def test_ppa_geographic_outsider_not_in_cluster_protects_pal_band(db_session):
    """CBSD physically inside the PPA polygon but not listed in the cluster."""
    cbsd = _registered_cbsd(db_session)
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

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    assert _channel_types(channels) == {"GAA"}
    assert all(
        c["frequencyRange"]["lowFrequency"] >= 3_560_000_000 for c in channels
    )


def test_outside_ppa_and_not_in_cluster_pal_band_stays_gaa(db_session):
    """Baseline 'outsider': no cluster membership and outside any PPA polygon."""
    cbsd = _registered_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id=cbsd.user_id,
        low_hz=3_550_000_000,
        high_hz=3_560_000_000,
    )
    far_zone = square_polygon(LON + 5.0, LAT + 5.0, delta=0.01)
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["other-fcc/other-serial"],
        zone=far_zone,
    )

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    assert _channel_types(channels) == {"GAA"}
    assert len(channels) == 15


def test_pal_band_misaligned_leaves_fragment_head_channel(db_session):
    """PAL band not on the 10 MHz grid leaves a misaligned GAA head fragment."""
    cbsd = _registered_cbsd(db_session)
    pal = make_pal(
        db_session,
        user_id="not-this-cbsd-user",
        low_hz=3_550_000_000,
        high_hz=3_555_000_000,
    )
    make_ppa_with_pal(
        db_session,
        pal=pal,
        cbsd_reference_ids=["other-fcc/other-serial"],
        zone=square_polygon(LON, LAT),
    )

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    ranges = [c["frequencyRange"] for c in channels]
    assert {"lowFrequency": 3_555_000_000, "highFrequency": 3_560_000_000} in ranges


# --- WISP exclusion --------------------------------------------------------------


def test_wisp_zone_subtracts_overlapping_frequency(db_session):
    cbsd = _registered_cbsd(db_session)
    db_session.add(
        AdminInjectedData(
            kind="wisp",
            data_json=json.dumps(
                {
                    "zone": square_polygon(LON, LAT),
                    "record": {
                        "deploymentParam": [
                            {
                                "operationParam": {
                                    "operationFrequencyRange": {
                                        "lowFrequency": 3_600_000_000,
                                        "highFrequency": 3_610_000_000,
                                    }
                                }
                            }
                        ]
                    },
                }
            ),
        )
    )
    db_session.commit()

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    for c in channels:
        fr = c["frequencyRange"]
        assert not (fr["lowFrequency"] < 3_610_000_000 and fr["highFrequency"] > 3_600_000_000)
    assert len(channels) == 14


# --- FSS edge cases: missing deploymentParam / missing freq info ---------------


def test_fss_without_deployment_param_is_skipped_without_crashing(db_session):
    cbsd = _registered_cbsd(db_session)
    make_fss(db_session, payload={"record": {"id": "fss-empty", "type": "FSS"}})

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert len(resp[0]["availableChannel"]) == 15


def test_fss_with_incomplete_freq_info_is_skipped(db_session):
    cbsd = _registered_cbsd(db_session)
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-incomplete",
                "deploymentParam": [
                    {"installationParam": {"latitude": LAT, "longitude": LON}}
                ],
            }
        },
    )

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert len(resp[0]["availableChannel"]) == 15


# --- CBSD without location / malformed registration_json ----------------------


def test_cbsd_without_installation_param_still_returns_gaa(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert len(resp[0]["availableChannel"]) == 15
    assert _channel_types(resp[0]["availableChannel"]) == {"GAA"}


def test_cbsd_with_malformed_registration_json_treated_as_no_location(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = "not-json{"
    db_session.commit()

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert len(resp[0]["availableChannel"]) == 15


def test_admin_injected_malformed_json_is_skipped(db_session):
    cbsd = _registered_cbsd(db_session)
    db_session.add(AdminInjectedData(kind="wisp", data_json="not-json{"))
    db_session.commit()

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert len(resp[0]["availableChannel"]) == 15


# --- inquiredSpectrum validation branches --------------------------------------


def test_inquired_spectrum_not_a_list_returns_102(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session, [{"cbsdId": cbsd.cbsd_id, "inquiredSpectrum": "not-a-list"}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_inquired_spectrum_item_not_dict_returns_102(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session, [{"cbsdId": cbsd.cbsd_id, "inquiredSpectrum": ["not-a-dict"]}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_inquired_spectrum_missing_keys_returns_102(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session,
        [{"cbsdId": cbsd.cbsd_id, "inquiredSpectrum": [{"lowFrequency": CBRS_LOW_HZ}]}],
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_inquired_spectrum_null_values_returns_102(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "inquiredSpectrum": [{"lowFrequency": None, "highFrequency": None}],
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_inquired_spectrum_non_numeric_returns_103(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "inquiredSpectrum": [
                    {"lowFrequency": "abc", "highFrequency": "def"}
                ],
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM


# --- measReport required when FLAG_MEAS_REG + WITHOUT_GRANT capability --------


def _cbsd_with_meas_capability(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": cat_a_install(lat=LAT, lon=LON),
        "measCapability": [MEAS_WITHOUT_GRANT],
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


def test_siq_missing_meas_report_returns_102_when_flag_set(db_session):
    cbsd = _cbsd_with_meas_capability(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_siq_invalid_meas_report_returns_103_when_flag_set(db_session):
    cbsd = _cbsd_with_meas_capability(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)

    req = _full_cbrs_request(cbsd.cbsd_id)
    req["measReport"] = {"rcvdPowerMeasReports": []}
    resp = process_spectrum_inquiry(db_session, [req])
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM


def test_siq_valid_full_meas_report_succeeds_when_flag_set(db_session):
    cbsd = _cbsd_with_meas_capability(db_session)
    set_admin_flag(db_session, FLAG_MEAS_REG)

    req = _full_cbrs_request(cbsd.cbsd_id)
    req["measReport"] = full_cbrs_meas_report()
    resp = process_spectrum_inquiry(db_session, [req])
    assert resp[0]["response"]["responseCode"] == SUCCESS


# --- Direct unit tests on private geometry/frequency helpers -------------------


def test_subtract_range_segment_fully_outside_exclusion_unchanged():
    result = _subtract_range([(3_550_000_000, 3_560_000_000)], 3_600_000_000, 3_610_000_000)
    assert result == [(3_550_000_000, 3_560_000_000)]


def test_subtract_range_trailing_remainder_kept():
    result = _subtract_range([(3_550_000_000, 3_600_000_000)], 3_550_000_000, 3_560_000_000)
    assert result == [(3_560_000_000, 3_600_000_000)]


def test_split_10mhz_zero_width_returns_empty():
    assert _split_10mhz(3_700_000_000, 3_700_000_000) == []


def test_split_10mhz_below_cbrs_band_returns_empty():
    assert _split_10mhz(3_000_000_000, 3_010_000_000) == []


def test_point_in_geojson_alias_matches_point_in_geojson():
    zone = square_polygon(LON, LAT)
    assert _point_in_geojson(LAT, LON, zone) is True
    assert _point_in_geojson(LAT + 10, LON + 10, zone) is False


def test_pal_freq_missing_frequencies_returns_none():
    assert _pal_freq({"channelAssignment": {}}) is None


def test_wisp_freq_missing_deployment_param_returns_none():
    assert _wisp_freq({"record": {}}) is None


def test_wisp_freq_missing_frequencies_returns_none():
    wisp = {"record": {"deploymentParam": [{"operationParam": {}}]}}
    assert _wisp_freq(wisp) is None


def test_fss_location_and_freq_missing_deployment_param_returns_none():
    assert _fss_location_and_freq({"record": {}}) is None


def test_fss_location_and_freq_missing_fields_returns_none():
    fss = {
        "record": {
            "deploymentParam": [{"installationParam": {"latitude": LAT}}]
        }
    }
    assert _fss_location_and_freq(fss) is None


def test_cbsd_location_missing_lat_lon_returns_none_none(db_session):
    cbsd = make_cbsd(db_session, cbsd_category="A")
    cbsd.registration_json = json.dumps({"installationParam": {}})
    db_session.commit()
    adapter = CbsdDeviceAdapter()
    assert _cbsd_location(cbsd, adapter) == (None, None)


def test_build_available_channels_degenerate_inquired_range_ignored(db_session):
    """Bypass process_spectrum_inquiry's own validation to exercise the
    'no valid CBRS overlap' branch of _build_available_channels directly."""
    cbsd = _registered_cbsd(db_session)
    adapter = CbsdDeviceAdapter()
    channels = _build_available_channels(
        db_session,
        cbsd,
        [{"lowFrequency": 3_000_000_000, "highFrequency": 3_010_000_000}],
        adapter,
    )
    assert channels == []
