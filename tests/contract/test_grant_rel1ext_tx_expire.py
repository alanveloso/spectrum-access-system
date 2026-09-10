"""Contract: Rel1Ext allows transmitExpireTime on grantResponse (HBT.13)."""

from __future__ import annotations

from schemas.common import ResponseObject
from schemas.grant import GrantResponseItem


def test_grant_response_item_accepts_rel1ext_transmit_expire_time():
    """WINNF-TS-4010 V1.1.0 §6.4.4.13 CHECK + REL1Ext-R1-IPM-02/03 note.

    Baseline ``GrantResponse.schema.json`` (TS-0016 draft) omits
    ``transmitExpireTime``, but Rel1Ext HBT.13 explicitly verifies the field on
    successful ``grantResponse`` messages. The UUT therefore emits it.
    """
    item = GrantResponseItem.model_validate(
        {
            "cbsdId": "fcc/sn",
            "grantId": "grant/1",
            "grantExpireTime": "2026-08-07T12:00:00Z",
            "transmitExpireTime": "2026-08-07T12:00:00Z",
            "heartbeatInterval": 60,
            "channelType": "GAA",
            "response": {"responseCode": 0},
        }
    )
    assert item.transmitExpireTime == "2026-08-07T12:00:00Z"
    assert isinstance(item.response, ResponseObject)
