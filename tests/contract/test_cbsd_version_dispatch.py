"""Contract: unsupported CBSD-SAS versions → responseCode 100 on all six procedures."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from main import app
from services.cbsd_version import PROCEDURE_SPECS
from services.error_handlers import VERSION_UNSUPPORTED

client = TestClient(app)

_SAMPLE_ITEM = {
    "registration": {"userId": "u", "fccId": "f", "cbsdSerialNumber": "s"},
    "spectrumInquiry": {"cbsdId": "c-1"},
    "grant": {"cbsdId": "c-1"},
    "heartbeat": {"cbsdId": "c-1", "grantId": "g-1", "operationState": "GRANTED"},
    "relinquishment": {"cbsdId": "c-1", "grantId": "g-1"},
    "deregistration": {"cbsdId": "c-1"},
}


def _post(version: str, procedure: str, payload: dict):
    return client.post(f"/{version}/{procedure}", json=payload)


def test_future_and_old_versions_return_100_for_all_procedures():
    for version in ("v9", "v1.0", "v0.1"):
        for procedure, spec in PROCEDURE_SPECS.items():
            sample = _SAMPLE_ITEM[procedure]
            response = _post(
                version,
                procedure,
                {spec.request_key: [sample, dict(sample)]},
            )
            assert response.status_code == 200, (version, procedure, response.text)
            items = response.json()[spec.response_key]
            assert len(items) == 2
            for item in items:
                assert item["response"]["responseCode"] == VERSION_UNSUPPORTED


def test_empty_batch_on_unsupported_version():
    response = _post("v9", "registration", {"registrationRequest": []})
    assert response.status_code == 200
    assert response.json()["registrationResponse"] == []


def test_heartbeat_transmit_expire_time_is_in_the_past():
    before = datetime.now(timezone.utc).replace(microsecond=0)
    response = _post(
        "v1.3",
        "heartbeat",
        {
            "heartbeatRequest": [
                {"cbsdId": "c-echo", "grantId": "g-echo", "operationState": "AUTHORIZED"}
            ]
        },
    )
    assert response.status_code == 200
    item = response.json()["heartbeatResponse"][0]
    assert item["cbsdId"] == "c-echo"
    assert item["grantId"] == "g-echo"
    assert item["response"]["responseCode"] == VERSION_UNSUPPORTED
    tx = datetime.strptime(item["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert tx < before or tx <= datetime.now(timezone.utc)


def test_supported_v1_2_still_served_by_concrete_routes():
    # Unknown operationState → schema 103 on /v1.2, not version 100.
    response = client.post(
        "/v1.2/heartbeat",
        json={
            "heartbeatRequest": [
                {"cbsdId": "c", "grantId": "g", "operationState": "NOPE"}
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["heartbeatResponse"][0]["response"]["responseCode"] == 103


def test_invalid_json_on_unsupported_version_returns_103_envelope():
    response = client.post(
        "/v9/registration",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["registrationResponse"][0]["response"]["responseCode"] == 103


def test_oversized_batch_on_unsupported_version_returns_http_400():
    from services.error_handlers import MAXIMUM_BATCH_SIZE

    huge = [{"cbsdId": f"c-{i}"} for i in range(MAXIMUM_BATCH_SIZE + 1)]
    response = _post("v9", "grant", {"grantRequest": huge})
    assert response.status_code == 400
    assert response.json()["responseCode"] == 103
