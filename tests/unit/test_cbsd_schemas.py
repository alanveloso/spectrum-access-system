"""strict CBSD schemas and per-item validation mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.common import FrequencyRange
from schemas.grant import GrantRequestItem
from schemas.heartbeat import HeartbeatRequestItem
from schemas.registration import RegistrationRequestItem
from schemas.spectrum_inquiry import SpectrumInquiryRequestItem
from services.cbsd_batch import merge_schema_and_service_responses, parse_item_batch
from services.error_handlers import INVALID_VALUE, MISSING_PARAM

SUCCESS = 0


def test_frequency_range_requires_ordered_hz():
    FrequencyRange(lowFrequency=3550_000_000, highFrequency=3700_000_000)
    with pytest.raises(ValidationError):
        FrequencyRange(lowFrequency=3700_000_000, highFrequency=3550_000_000)


def test_heartbeat_rejects_unknown_operation_state():
    with pytest.raises(ValidationError):
        HeartbeatRequestItem(
            cbsdId="x",
            grantId="g",
            operationState="NOT_A_STATE",  # type: ignore[arg-type]
        )
    item = HeartbeatRequestItem(
        cbsdId="x", grantId="g", operationState="AUTHORIZED"
    )
    assert item.operationState == "AUTHORIZED"


def test_registration_batch_uses_item_models_not_bare_dicts():
    from schemas.registration import RegistrationBatchRequest

    ann = RegistrationBatchRequest.model_fields["registrationRequest"].annotation
    assert ann is not None
    assert "RegistrationRequestItem" in str(ann)
    assert "dict" not in str(ann).replace("TypedDict", "")


def test_category_b_requires_installation_or_cpi():
    with pytest.raises(ValidationError, match="Category B"):
        RegistrationRequestItem(
            userId="u",
            fccId="f",
            cbsdSerialNumber="s",
            cbsdCategory="B",
        )
    ok = RegistrationRequestItem(
        userId="u",
        fccId="f",
        cbsdSerialNumber="s",
        cbsdCategory="B",
        installationParam={"latitude": 1.0, "longitude": 2.0, "height": 3.0, "heightType": "AGL"},
    )
    assert ok.installationParam is not None


def test_grant_max_eirp_bounds():
    with pytest.raises(ValidationError):
        GrantRequestItem(
            cbsdId="c",
            operationParam={
                "maxEirp": 99,
                "operationFrequencyRange": {
                    "lowFrequency": 3550_000_000,
                    "highFrequency": 3560_000_000,
                },
            },
        )


def test_parse_item_batch_mixed_schema_errors():
    raw = [
        {"cbsdId": "ok", "grantId": "g1", "operationState": "GRANTED"},
        {"cbsdId": "bad", "grantId": "g2", "operationState": "NOPE"},
        {"cbsdId": "ok2", "grantId": "g3", "operationState": "AUTHORIZED"},
    ]
    parsed = parse_item_batch(raw, item_model=HeartbeatRequestItem)
    assert parsed.schema_error_codes[0] is None
    assert parsed.schema_error_codes[1] == INVALID_VALUE
    assert parsed.schema_error_codes[2] is None
    assert len(parsed.items_for_service) == 2
    assert parsed.service_index_map == [0, 2]

    merged = merge_schema_and_service_responses(
        schema_error_codes=parsed.schema_error_codes,
        service_index_map=parsed.service_index_map,
        service_responses=[
            {"cbsdId": "ok", "response": {"responseCode": SUCCESS}},
            {"cbsdId": "ok2", "response": {"responseCode": SUCCESS}},
        ],
        echo_from_raw=raw,
    )
    assert len(merged) == 3
    assert merged[0]["response"]["responseCode"] == SUCCESS
    assert merged[1]["response"]["responseCode"] == INVALID_VALUE
    assert merged[1]["cbsdId"] == "bad"
    assert merged[2]["response"]["responseCode"] == SUCCESS


def test_siq_empty_inquired_spectrum_invalid():
    with pytest.raises(ValidationError):
        SpectrumInquiryRequestItem(cbsdId="c", inquiredSpectrum=[])


def test_registration_forbids_unknown_top_level_keys():
    with pytest.raises(ValidationError):
        RegistrationRequestItem(
            userId="u",
            fccId="f",
            cbsdSerialNumber="s",
            cbsdCategory="A",
            notARealField=True,  # type: ignore[call-arg]
        )


def test_parse_preserves_installation_param_extras():
    raw = {
        "userId": "u",
        "fccId": "f",
        "cbsdSerialNumber": "s",
        "cbsdCategory": "A",
        "installationParam": {
            "latitude": 1.0,
            "longitude": 2.0,
            "height": 3.0,
            "heightType": "AGL",
            "vendorExtensionField": "keep-me",
        },
    }
    parsed = parse_item_batch([raw], item_model=RegistrationRequestItem)
    assert parsed.schema_error_codes == [None]
    inst = parsed.items_for_service[0]["installationParam"]
    assert inst["vendorExtensionField"] == "keep-me"
    assert inst["latitude"] == 1.0


def test_parse_does_not_invent_none_indoor_deployment():
    """Absent optional installation fields must stay absent (REG pending → 200)."""
    raw = {
        "userId": "u",
        "fccId": "f",
        "cbsdSerialNumber": "s",
        "cbsdCategory": "A",
        "installationParam": {
            "latitude": 1.0,
            "longitude": 2.0,
            "height": 3.0,
            "heightType": "AGL",
        },
    }
    parsed = parse_item_batch([raw], item_model=RegistrationRequestItem)
    assert parsed.schema_error_codes == [None]
    inst = parsed.items_for_service[0]["installationParam"]
    assert "indoorDeployment" not in inst


def test_siq_schema_error_does_not_echo_freeform_cbsd_id():
    raw = [
        {
            "cbsdId": "INVALID_CBSD_ID_12345",
            "inquiredSpectrum": [
                {"lowFrequency": 3700_000_000, "highFrequency": 3550_000_000}
            ],
        }
    ]
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
    assert "cbsdId" not in merged[0]
    assert merged[0]["response"]["responseCode"] == INVALID_VALUE


def test_siq_schema_error_echoes_syntactic_cbsd_id():
    raw = [
        {
            "cbsdId": "fcc-lab/sn-1",
            "inquiredSpectrum": [
                {"lowFrequency": 3700_000_000, "highFrequency": 3550_000_000}
            ],
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
    assert merged[0]["cbsdId"] == "fcc-lab/sn-1"


def test_missing_nested_frequency_maps_to_missing_or_invalid():
    with pytest.raises(ValidationError) as exc:
        FrequencyRange.model_validate({"lowFrequency": 1})
    from schemas.common import winnf_code_from_validation_errors

    code = winnf_code_from_validation_errors(list(exc.value.errors()))
    assert code in (MISSING_PARAM, INVALID_VALUE)
