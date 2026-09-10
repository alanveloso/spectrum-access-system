"""Unit tests for unsupported CBSD-SAS version response builder ()."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.cbsd_version import (
    PROCEDURE_SPECS,
    build_unsupported_version_body,
    is_supported_cbsd_sas_version,
)
from services.error_handlers import VERSION_UNSUPPORTED


def test_supported_version_set():
    assert is_supported_cbsd_sas_version("v1.2")
    assert not is_supported_cbsd_sas_version("v1.0")
    assert not is_supported_cbsd_sas_version("v9")


def test_all_six_procedures_registered():
    assert set(PROCEDURE_SPECS) == {
        "registration",
        "spectrumInquiry",
        "grant",
        "heartbeat",
        "relinquishment",
        "deregistration",
    }


def test_empty_batch_returns_empty_response_array():
    body = build_unsupported_version_body(
        "registration", {"registrationRequest": []}
    )
    assert body == {"registrationResponse": []}


def test_mixed_batch_echoes_and_code_100():
    now = datetime(2026, 8, 6, 15, 0, 0, tzinfo=timezone.utc)
    body = build_unsupported_version_body(
        "heartbeat",
        {
            "heartbeatRequest": [
                {"cbsdId": "a", "grantId": "g1"},
                {"cbsdId": "b", "grantId": "g2", "operationState": "AUTHORIZED"},
            ]
        },
        now=now,
    )
    items = body["heartbeatResponse"]
    assert len(items) == 2
    assert items[0]["cbsdId"] == "a"
    assert items[0]["grantId"] == "g1"
    assert items[0]["response"]["responseCode"] == VERSION_UNSUPPORTED
    assert items[0]["transmitExpireTime"] == "2026-08-06T14:59:59Z"
    assert items[1]["transmitExpireTime"] == "2026-08-06T14:59:59Z"


def test_missing_request_key_treated_as_empty_batch():
    body = build_unsupported_version_body("grant", {})
    assert body == {"grantResponse": []}


def test_non_list_request_key_rejected():
    from services.cbsd_version import UnsupportedVersionBatchError

    with pytest.raises(UnsupportedVersionBatchError, match="must be a list"):
        build_unsupported_version_body(
            "grant", {"grantRequest": {"cbsdId": "x"}}
        )


def test_oversized_batch_rejected():
    from services.cbsd_version import UnsupportedVersionBatchError
    from services.error_handlers import MAXIMUM_BATCH_SIZE

    huge = [{"cbsdId": f"c-{i}"} for i in range(MAXIMUM_BATCH_SIZE + 1)]
    with pytest.raises(UnsupportedVersionBatchError, match="MaximumBatchSize"):
        build_unsupported_version_body("grant", {"grantRequest": huge})
