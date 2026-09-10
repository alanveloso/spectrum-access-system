"""Contract: CBSD routes return per-item WINNF codes for schema failures."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from services.error_handlers import INVALID_VALUE


client = TestClient(app)


def test_heartbeat_unknown_operation_state_returns_103():
    response = client.post(
        "/v1.2/heartbeat",
        json={
            "heartbeatRequest": [
                {
                    "cbsdId": "cbsd-1",
                    "grantId": "grant-1",
                    "operationState": "TOTALLY_INVALID",
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["heartbeatResponse"][0]["response"]["responseCode"] == INVALID_VALUE


def test_heartbeat_mixed_batch_preserves_cardinality():
    response = client.post(
        "/v1.2/heartbeat",
        json={
            "heartbeatRequest": [
                {
                    "cbsdId": "cbsd-1",
                    "grantId": "grant-1",
                    "operationState": "GRANTED",
                },
                {
                    "cbsdId": "cbsd-2",
                    "grantId": "grant-2",
                    "operationState": "BOGUS",
                },
            ]
        },
    )
    assert response.status_code == 200
    items = response.json()["heartbeatResponse"]
    assert len(items) == 2
    assert items[1]["response"]["responseCode"] == INVALID_VALUE
    assert items[1]["cbsdId"] == "cbsd-2"


def test_missing_request_key_returns_102_envelope():
    response = client.post("/v1.2/heartbeat", json={})
    assert response.status_code == 200
    assert response.json()["heartbeatResponse"][0]["response"]["responseCode"] == 102


def test_oversized_batch_returns_http_400():
    from services.error_handlers import MAXIMUM_BATCH_SIZE

    huge = [
        {"cbsdId": f"c-{i}", "grantId": f"g-{i}", "operationState": "GRANTED"}
        for i in range(MAXIMUM_BATCH_SIZE + 1)
    ]
    response = client.post("/v1.2/heartbeat", json={"heartbeatRequest": huge})
    assert response.status_code == 400
    assert response.json()["responseCode"] == INVALID_VALUE
