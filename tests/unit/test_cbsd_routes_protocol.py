"""Behavioral tests exercising the concrete CBSD-SAS v1.2 routes and the
unsupported-version catch-all via FastAPI's TestClient (see
tests/unit/test_cbsd_auth.py for the pattern). Targets routes/cbsd_routes.py and
routes/cbsd_version_routes.py branches not reached by direct service-level tests.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from main import app
from routes.cbsd_version_routes import router as cbsd_version_router
from services.cbsd_version import PROCEDURE_SPECS
from services.error_handlers import INVALID_VALUE, MAXIMUM_BATCH_SIZE
from tests.fixtures.factories import cat_a_install, make_fcc_id, make_user_id

client = TestClient(app)

SUCCESS = 0
MISSING_PARAM = 102
VERSION_UNSUPPORTED = 100


def _registration_payload(fcc_id: str, serial: str, user_id: str) -> dict:
    return {
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "userId": user_id,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": cat_a_install(),
    }


# --- Concrete /v1.2 routes: each endpoint entry point ---------------------------


def test_registration_route_success(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    resp = client.post(
        "/v1.2/registration",
        json={
            "registrationRequest": [
                _registration_payload(fcc.fcc_id, "sn-route-1", user.user_id)
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["registrationResponse"][0]
    assert body["response"]["responseCode"] == SUCCESS
    assert body["cbsdId"]


def test_registration_route_pending_when_indoor_deployment_absent(db_session):
    """Schema dump must not invent indoorDeployment=None (WINNF REG.6 / REG.11)."""
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    payload = _registration_payload(fcc.fcc_id, "sn-route-pend", user.user_id)
    del payload["installationParam"]["indoorDeployment"]
    resp = client.post(
        "/v1.2/registration",
        json={"registrationRequest": [payload]},
    )
    assert resp.status_code == 200
    body = resp.json()["registrationResponse"][0]
    assert body["response"]["responseCode"] == 200
    assert "cbsdId" not in body


def test_spectrum_inquiry_route_schema_error_omits_freeform_cbsd_id(db_session):
    resp = client.post(
        "/v1.2/spectrumInquiry",
        json={
            "spectrumInquiryRequest": [
                {
                    "cbsdId": "INVALID_CBSD_ID_12345",
                    "inquiredSpectrum": [
                        {
                            "lowFrequency": 3700_000_000,
                            "highFrequency": 3550_000_000,
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["spectrumInquiryResponse"][0]
    assert body["response"]["responseCode"] == INVALID_VALUE
    assert "cbsdId" not in body


def test_spectrum_inquiry_route_schema_error_echoes_syntactic_cbsd_id(db_session):
    resp = client.post(
        "/v1.2/spectrumInquiry",
        json={
            "spectrumInquiryRequest": [
                {
                    "cbsdId": "fcc-route/sn-echo",
                    "inquiredSpectrum": [
                        {
                            "lowFrequency": 3700_000_000,
                            "highFrequency": 3550_000_000,
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["spectrumInquiryResponse"][0]
    assert body["response"]["responseCode"] == INVALID_VALUE
    assert body["cbsdId"] == "fcc-route/sn-echo"


def _register_via_route(db_session, serial: str) -> str:
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    resp = client.post(
        "/v1.2/registration",
        json={"registrationRequest": [_registration_payload(fcc.fcc_id, serial, user.user_id)]},
    )
    return resp.json()["registrationResponse"][0]["cbsdId"]


def test_grant_route_success(db_session):
    cbsd_id = _register_via_route(db_session, "sn-route-2")
    resp = client.post(
        "/v1.2/grant",
        json={
            "grantRequest": [
                {
                    "cbsdId": cbsd_id,
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3_550_000_000,
                            "highFrequency": 3_560_000_000,
                        },
                    },
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["grantResponse"][0]
    assert body["response"]["responseCode"] == SUCCESS
    assert cbsd_id and body["grantId"]


def test_spectrum_inquiry_route_success(db_session):
    cbsd_id = _register_via_route(db_session, "sn-route-3")
    resp = client.post(
        "/v1.2/spectrumInquiry",
        json={
            "spectrumInquiryRequest": [
                {
                    "cbsdId": cbsd_id,
                    "inquiredSpectrum": [
                        {"lowFrequency": 3_550_000_000, "highFrequency": 3_700_000_000}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["spectrumInquiryResponse"][0]
    assert body["response"]["responseCode"] == SUCCESS


def test_heartbeat_route_success_via_client(db_session):
    cbsd_id = _register_via_route(db_session, "sn-route-4")
    grant_resp = client.post(
        "/v1.2/grant",
        json={
            "grantRequest": [
                {
                    "cbsdId": cbsd_id,
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3_550_000_000,
                            "highFrequency": 3_560_000_000,
                        },
                    },
                }
            ]
        },
    ).json()["grantResponse"][0]
    grant_id = grant_resp["grantId"]

    resp = client.post(
        "/v1.2/heartbeat",
        json={
            "heartbeatRequest": [
                {
                    "cbsdId": cbsd_id,
                    "grantId": grant_id,
                    "operationState": "GRANTED",
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["heartbeatResponse"][0]["response"]["responseCode"] == SUCCESS


def test_relinquishment_route_success(db_session):
    cbsd_id = _register_via_route(db_session, "sn-route-5")
    grant_id = client.post(
        "/v1.2/grant",
        json={
            "grantRequest": [
                {
                    "cbsdId": cbsd_id,
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3_550_000_000,
                            "highFrequency": 3_560_000_000,
                        },
                    },
                }
            ]
        },
    ).json()["grantResponse"][0]["grantId"]

    resp = client.post(
        "/v1.2/relinquishment",
        json={"relinquishmentRequest": [{"cbsdId": cbsd_id, "grantId": grant_id}]},
    )
    assert resp.status_code == 200
    assert resp.json()["relinquishmentResponse"][0]["response"]["responseCode"] == SUCCESS


def test_deregistration_route_success(db_session):
    cbsd_id = _register_via_route(db_session, "sn-route-6")
    resp = client.post(
        "/v1.2/deregistration",
        json={"deregistrationRequest": [{"cbsdId": cbsd_id}]},
    )
    assert resp.status_code == 200
    assert resp.json()["deregistrationResponse"][0]["response"]["responseCode"] == SUCCESS


# --- Batch envelope edge cases (shared _run_batch helper) -----------------------


def test_registration_request_key_missing_returns_missing_param(db_session):
    resp = client.post("/v1.2/registration", json={})
    assert resp.status_code == 200
    assert (
        resp.json()["registrationResponse"][0]["response"]["responseCode"]
        == MISSING_PARAM
    )


def test_registration_request_not_a_list_returns_invalid_value(db_session):
    resp = client.post(
        "/v1.2/registration", json={"registrationRequest": "not-a-list"}
    )
    assert resp.status_code == 200
    assert (
        resp.json()["registrationResponse"][0]["response"]["responseCode"]
        == INVALID_VALUE
    )


def test_grant_request_oversized_batch_returns_http_400(db_session):
    items = [{"cbsdId": f"c-{i}"} for i in range(MAXIMUM_BATCH_SIZE + 1)]
    resp = client.post("/v1.2/grant", json={"grantRequest": items})
    assert resp.status_code == 400
    assert resp.json()["responseCode"] == INVALID_VALUE


# --- Unsupported-version catch-all (/{version}/{procedure}) --------------------


def test_unsupported_version_registration_returns_100(db_session):
    fcc = make_fcc_id(db_session)
    user = make_user_id(db_session)
    resp = client.post(
        "/v1.3/registration",
        json={
            "registrationRequest": [
                _registration_payload(fcc.fcc_id, "sn-unsup-1", user.user_id)
            ]
        },
    )
    assert resp.status_code == 200
    assert (
        resp.json()["registrationResponse"][0]["response"]["responseCode"]
        == VERSION_UNSUPPORTED
    )


def test_unsupported_version_heartbeat_echoes_cbsd_and_grant_ids():
    resp = client.post(
        "/v1.3/heartbeat",
        json={
            "heartbeatRequest": [
                {"cbsdId": "c-echo", "grantId": "g-echo", "operationState": "GRANTED"}
            ]
        },
    )
    assert resp.status_code == 200
    item = resp.json()["heartbeatResponse"][0]
    assert item["response"]["responseCode"] == VERSION_UNSUPPORTED
    assert item["cbsdId"] == "c-echo"
    assert item["grantId"] == "g-echo"
    assert "transmitExpireTime" in item


def test_unsupported_version_registration_ignores_non_dict_items():
    resp = client.post(
        "/v1.3/registration",
        json={"registrationRequest": ["not-a-dict", 42, None]},
    )
    assert resp.status_code == 200
    responses = resp.json()["registrationResponse"]
    assert len(responses) == 3
    assert all(r["response"]["responseCode"] == VERSION_UNSUPPORTED for r in responses)


def test_unsupported_version_malformed_json_body_returns_wrapped_code():
    resp = client.post(
        "/v1.3/heartbeat",
        content="{not-json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    item = resp.json()["heartbeatResponse"][0]
    assert item["response"]["responseCode"] == INVALID_VALUE
    assert "transmitExpireTime" in item


def test_unsupported_version_non_list_request_key_returns_wrapped_invalid():
    resp = client.post(
        "/v1.3/registration",
        json={"registrationRequest": {"not": "a-list"}},
    )
    assert resp.status_code == 200
    assert (
        resp.json()["registrationResponse"][0]["response"]["responseCode"]
        == INVALID_VALUE
    )


def test_unsupported_version_top_level_non_object_json_returns_wrapped_invalid():
    resp = client.post(
        "/v1.3/registration",
        content="[1, 2, 3]",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert (
        resp.json()["registrationResponse"][0]["response"]["responseCode"]
        == INVALID_VALUE
    )


def test_unsupported_version_oversized_batch_returns_http_400():
    items = [{"cbsdId": f"c-{i}"} for i in range(MAXIMUM_BATCH_SIZE + 1)]
    resp = client.post("/v1.3/grant", json={"grantRequest": items})
    assert resp.status_code == 400
    assert resp.json()["responseCode"] == INVALID_VALUE


def test_unsupported_version_unknown_procedure_returns_404():
    resp = client.post("/v1.3/not-a-real-procedure", json={})
    assert resp.status_code == 404


# --- Direct coverage of defensive/otherwise-unreachable branches --------------


def test_register_procedure_rejects_unknown_procedure():
    from routes.cbsd_version_routes import _register_procedure

    with pytest.raises(KeyError):
        _register_procedure("not-a-real-procedure")


def test_build_unsupported_version_body_rejects_unknown_procedure():
    from services.cbsd_version import build_unsupported_version_body

    with pytest.raises(KeyError):
        build_unsupported_version_body("not-a-real-procedure", {})


def test_malformed_body_response_rejects_unknown_procedure():
    from services.cbsd_version import malformed_body_response

    with pytest.raises(KeyError):
        malformed_body_response("not-a-real-procedure")


def test_catch_all_endpoint_reports_404_for_a_supported_version_directly():
    """Direct call bypassing HTTP routing precedence to exercise the branch
    where the catch-all itself would see an already-supported version string
    (defensive: concrete /v1.2 routes always claim real v1.2 traffic first)."""
    route = next(
        r
        for r in cbsd_version_router.routes
        if getattr(r, "name", None) == "registration_unsupported_version"
    )
    response = asyncio.run(route.endpoint("v1.2", None))
    assert response.status_code == 404


def test_build_unsupported_version_item_skips_non_dict_raw_item():
    from services.cbsd_version import PROCEDURE_SPECS, build_unsupported_version_item

    spec = PROCEDURE_SPECS["heartbeat"]
    item = build_unsupported_version_item(spec, "not-a-dict")
    assert item["response"]["responseCode"] == VERSION_UNSUPPORTED
    assert "cbsdId" not in item
    assert "transmitExpireTime" in item


def test_all_procedure_specs_registered_on_router():
    names = {getattr(r, "name", None) for r in cbsd_version_router.routes}
    for procedure in PROCEDURE_SPECS:
        assert f"{procedure}_unsupported_version" in names
