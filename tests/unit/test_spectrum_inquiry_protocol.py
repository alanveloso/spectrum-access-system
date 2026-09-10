"""Behavioral tests for WINNF_FT_S_SIQ (Spectrum Inquiry) protocol."""

from __future__ import annotations

import json

from services.blacklist_service import add_fcc_id_blacklist
from services.spectrum_inquiry_service import process_spectrum_inquiry
from tests.fixtures.factories import cat_a_install, make_cbsd, make_fss

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300

CBRS_LOW_HZ = 3_550_000_000
CBRS_HIGH_HZ = 3_700_000_000


def _full_cbrs_request(cbsd_id: str) -> dict:
    return {
        "cbsdId": cbsd_id,
        "inquiredSpectrum": [
            {"lowFrequency": CBRS_LOW_HZ, "highFrequency": CBRS_HIGH_HZ}
        ],
    }


def _registered_cbsd(db_session, **overrides):
    """Create a CBSD with a full Cat A registration (install params attached)."""
    certificate_hash = overrides.pop("certificate_hash", None)
    cbsd = make_cbsd(db_session, cbsd_category="A", certificate_hash=certificate_hash)
    registration = {
        "fccId": cbsd.fcc_id,
        "cbsdSerialNumber": cbsd.cbsd_serial_number,
        "userId": cbsd.user_id,
        "cbsdCategory": "A",
        "installationParam": cat_a_install(),
    }
    cbsd.registration_json = json.dumps(registration)
    db_session.commit()
    return cbsd


def test_success_gaa_channels_for_full_cbrs_inquire(db_session):
    cbsd = _registered_cbsd(db_session)

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])

    assert len(resp) == 1
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[0]["cbsdId"] == cbsd.cbsd_id
    channels = resp[0]["availableChannel"]
    assert len(channels) == 15
    assert all(ch["channelType"] == "GAA" for ch in channels)
    assert channels[0]["frequencyRange"]["lowFrequency"] == CBRS_LOW_HZ
    assert channels[-1]["frequencyRange"]["highFrequency"] == CBRS_HIGH_HZ


def test_missing_cbsd_id_returns_102_without_echo(db_session):
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "inquiredSpectrum": [
                    {"lowFrequency": CBRS_LOW_HZ, "highFrequency": CBRS_HIGH_HZ}
                ]
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert "cbsdId" not in resp[0]


def test_unknown_cbsd_id_returns_103_without_echo(db_session):
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": "no-such-cbsd/serial",
                "inquiredSpectrum": [
                    {"lowFrequency": CBRS_LOW_HZ, "highFrequency": CBRS_HIGH_HZ}
                ],
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]


def test_invalid_inquired_spectrum_empty_returns_102_with_echo(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session, [{"cbsdId": cbsd.cbsd_id, "inquiredSpectrum": []}]
    )
    assert resp[0]["response"]["responseCode"] == MISSING_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_invalid_inquired_spectrum_high_leq_low_returns_103_with_echo(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "inquiredSpectrum": [
                    {"lowFrequency": 3_560_000_000, "highFrequency": 3_550_000_000}
                ],
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_invalid_inquired_spectrum_out_of_band_returns_300_with_echo(db_session):
    cbsd = _registered_cbsd(db_session)
    resp = process_spectrum_inquiry(
        db_session,
        [
            {
                "cbsdId": cbsd.cbsd_id,
                "inquiredSpectrum": [
                    {"lowFrequency": 3_000_000_000, "highFrequency": 3_010_000_000}
                ],
            }
        ],
    )
    assert resp[0]["response"]["responseCode"] == UNSUPPORTED_SPECTRUM
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_blacklisted_fcc_id_returns_101_with_echo(db_session):
    cbsd = _registered_cbsd(db_session)
    add_fcc_id_blacklist(db_session, cbsd.fcc_id)

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == BLACKLISTED
    assert resp[0]["cbsdId"] == cbsd.cbsd_id


def test_certificate_mismatch_returns_103_without_echo(db_session):
    owner = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
    other = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44"
    cbsd = _registered_cbsd(db_session, certificate_hash=owner)

    resp = process_spectrum_inquiry(
        db_session,
        [_full_cbrs_request(cbsd.cbsd_id)],
        certificate_hash=other,
    )
    assert resp[0]["response"]["responseCode"] == INVALID_PARAM
    assert "cbsdId" not in resp[0]


def test_fss_nearby_excludes_3650_to_3700(db_session):
    cbsd = _registered_cbsd(db_session)
    make_fss(
        db_session,
        payload={
            "record": {
                "id": "fss-siq",
                "type": "FSS",
                "deploymentParam": [
                    {
                        "installationParam": {"latitude": 39.1, "longitude": -94.58},
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

    resp = process_spectrum_inquiry(db_session, [_full_cbrs_request(cbsd.cbsd_id)])
    assert resp[0]["response"]["responseCode"] == SUCCESS
    channels = resp[0]["availableChannel"]
    assert len(channels) == 10
    assert all(
        ch["frequencyRange"]["highFrequency"] <= 3_650_000_000 for ch in channels
    )
    assert all(ch["channelType"] == "GAA" for ch in channels)


def test_batch_cardinality_two_requests_two_responses(db_session):
    cbsd = _registered_cbsd(db_session)

    resp = process_spectrum_inquiry(
        db_session,
        [
            _full_cbrs_request(cbsd.cbsd_id),
            {
                "inquiredSpectrum": [
                    {"lowFrequency": CBRS_LOW_HZ, "highFrequency": CBRS_HIGH_HZ}
                ]
            },
        ],
    )
    assert len(resp) == 2
    assert resp[0]["response"]["responseCode"] == SUCCESS
    assert resp[1]["response"]["responseCode"] == MISSING_PARAM
