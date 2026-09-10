"""Spectrum Inquiry cbsdId echo semantics on schema failures ()."""

from __future__ import annotations

from pathlib import Path

from schemas.spectrum_inquiry import SpectrumInquiryRequestItem
from services.cbsd_batch import (
    is_syntactically_valid_cbsd_id,
    merge_schema_and_service_responses,
    parse_item_batch,
    should_echo_cbsd_id_on_schema_error,
)
from services.error_handlers import INVALID_VALUE, MISSING_PARAM

ROOT = Path(__file__).resolve().parents[2]


def _bad_range() -> list[dict[str, int]]:
    return [{"lowFrequency": 3700_000_000, "highFrequency": 3550_000_000}]


def _good_range() -> list[dict[str, int]]:
    return [{"lowFrequency": 3550_000_000, "highFrequency": 3560_000_000}]


def test_syntactic_cbsd_id_accepts_fcc_slash_serial_only():
    assert is_syntactically_valid_cbsd_id("fcc-1/serial-1") is True
    assert is_syntactically_valid_cbsd_id("a/b") is True
    assert is_syntactically_valid_cbsd_id("INVALID_CBSD_ID_12345") is False
    assert is_syntactically_valid_cbsd_id("no-slash") is False
    assert is_syntactically_valid_cbsd_id("/only-serial") is False
    assert is_syntactically_valid_cbsd_id("only-fcc/") is False
    assert is_syntactically_valid_cbsd_id("a/b/c") is False
    assert is_syntactically_valid_cbsd_id("") is False
    assert is_syntactically_valid_cbsd_id(None) is False
    assert is_syntactically_valid_cbsd_id(123) is False


def test_echo_policy_syntactic_vs_always():
    valid = {"cbsdId": "fcc/sn"}
    bogus = {"cbsdId": "INVALID_CBSD_ID_12345"}
    assert should_echo_cbsd_id_on_schema_error(valid, policy="syntactic") is True
    assert should_echo_cbsd_id_on_schema_error(bogus, policy="syntactic") is False
    assert should_echo_cbsd_id_on_schema_error(bogus, policy="always") is True
    assert should_echo_cbsd_id_on_schema_error({"cbsdId": None}, policy="always") is False
    assert should_echo_cbsd_id_on_schema_error({}, policy="syntactic") is False
    assert should_echo_cbsd_id_on_schema_error(valid, policy="never") is False


def test_valid_cbsd_id_with_invalid_inquired_spectrum_echoes():
    raw = [{"cbsdId": "fcc-x/sn-y", "inquiredSpectrum": _bad_range()}]
    parsed = parse_item_batch(raw, item_model=SpectrumInquiryRequestItem)
    assert parsed.schema_error_codes[0] == INVALID_VALUE
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=[],
        echo_from_raw=raw,
        echo_fields=("cbsdId",),
        cbsd_id_echo="syntactic",
    )
    assert merged[0]["cbsdId"] == "fcc-x/sn-y"
    assert merged[0]["response"]["responseCode"] == INVALID_VALUE


def test_valid_cbsd_id_with_missing_high_frequency_echoes():
    raw = [
        {
            "cbsdId": "fcc-x/sn-y",
            "inquiredSpectrum": [{"lowFrequency": 3550_000_000}],
        }
    ]
    parsed = parse_item_batch(raw, item_model=SpectrumInquiryRequestItem)
    assert parsed.schema_error_codes[0] in (MISSING_PARAM, INVALID_VALUE)
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=[],
        echo_from_raw=raw,
        echo_fields=("cbsdId",),
        cbsd_id_echo="syntactic",
    )
    assert merged[0]["cbsdId"] == "fcc-x/sn-y"


def test_invalid_freeform_cbsd_id_does_not_echo_on_spectrum_error():
    raw = [
        {
            "cbsdId": "INVALID_CBSD_ID_12345",
            "inquiredSpectrum": _bad_range(),
        }
    ]
    parsed = parse_item_batch(raw, item_model=SpectrumInquiryRequestItem)
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=[],
        echo_from_raw=raw,
        echo_fields=("cbsdId",),
        cbsd_id_echo="syntactic",
    )
    assert "cbsdId" not in merged[0]


def test_absent_cbsd_id_does_not_echo():
    raw = [{"inquiredSpectrum": _bad_range()}]
    parsed = parse_item_batch(raw, item_model=SpectrumInquiryRequestItem)
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=[],
        echo_from_raw=raw,
        echo_fields=("cbsdId",),
        cbsd_id_echo="syntactic",
    )
    assert "cbsdId" not in merged[0]


def test_mixed_batch_preserves_order_and_cardinality():
    raw = [
        {"cbsdId": "fcc/ok", "inquiredSpectrum": _good_range()},
        {"cbsdId": "INVALID_CBSD_ID_12345", "inquiredSpectrum": _bad_range()},
        {"cbsdId": "fcc/other", "inquiredSpectrum": _bad_range()},
        {"inquiredSpectrum": _bad_range()},
    ]
    parsed = parse_item_batch(raw, item_model=SpectrumInquiryRequestItem)
    assert len(parsed.schema_error_codes) == 4
    assert parsed.schema_error_codes[0] is None
    assert parsed.schema_error_codes[1] is not None
    assert parsed.schema_error_codes[2] is not None
    assert parsed.schema_error_codes[3] is not None
    assert parsed.service_index_map == [0]

    service = [
        {
            "cbsdId": "fcc/ok",
            "availableChannel": [],
            "response": {"responseCode": 0},
        }
    ]
    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=service,
        echo_from_raw=raw,
        echo_fields=("cbsdId",),
        cbsd_id_echo="syntactic",
    )
    assert len(merged) == 4
    assert merged[0]["cbsdId"] == "fcc/ok"
    assert merged[0]["response"]["responseCode"] == 0
    assert "cbsdId" not in merged[1]
    assert merged[2]["cbsdId"] == "fcc/other"
    assert "cbsdId" not in merged[3]


def test_siq_echo_helpers_have_no_fixture_hardcodes():
    """Echo helpers must not embed harness device IDs or coordinates."""
    text = (ROOT / "services" / "cbsd_batch.py").read_text(encoding="utf-8")
    banned = (
        "INVALID_CBSD_ID_12345",
        "test_fcc_id",
        "device_a",
        "38.859",
        "-97.2",
    )
    for token in banned:
        assert token not in text, f"hardcode leak: {token}"
