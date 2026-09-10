"""IAP / aggregate interference."""

from __future__ import annotations

from services.iap.aggregate import (
    DEFAULT_EIRP_FLOOR_DBM_MHZ,
    EIRP_STEP_DB,
    IAPBW_HZ,
    aggregate_channel,
    apply_pre_iap_margin_db,
    dbm_to_mw,
    grant_overlaps_channel,
    mw_to_dbm,
    overlapping_iap_channels,
    resolve_iap_band_origin_hz,
    sum_interference_mw,
)
from services.iap.engine import (
    IapEngineConfig,
    InterferenceCoupling,
    merge_iap_decisions,
    run_iap,
    run_iap_for_point,
)
from services.iap.models import (
    ChannelAggregateResult,
    FrequencyChannel,
    GrantChannelContribution,
    GrantRfInfo,
    IapAction,
    IapGrantDecision,
    IapPointResult,
    IapRunResult,
    ProtectedEntityKind,
    ProtectionPoint,
)
from services.iap.peer_fad import (
    grant_rf_infos_from_frozen_peer_cbsds,
    grant_rf_infos_from_peer_cbsd_record,
    peer_grant_rf_id,
)

__all__ = [
    "DEFAULT_EIRP_FLOOR_DBM_MHZ",
    "EIRP_STEP_DB",
    "IAPBW_HZ",
    "ChannelAggregateResult",
    "FrequencyChannel",
    "GrantChannelContribution",
    "GrantRfInfo",
    "IapAction",
    "IapEngineConfig",
    "IapGrantDecision",
    "IapPointResult",
    "IapRunResult",
    "InterferenceCoupling",
    "ProtectedEntityKind",
    "ProtectionPoint",
    "aggregate_channel",
    "apply_pre_iap_margin_db",
    "dbm_to_mw",
    "grant_overlaps_channel",
    "grant_rf_infos_from_frozen_peer_cbsds",
    "grant_rf_infos_from_peer_cbsd_record",
    "merge_iap_decisions",
    "mw_to_dbm",
    "overlapping_iap_channels",
    "peer_grant_rf_id",
    "resolve_iap_band_origin_hz",
    "run_iap",
    "run_iap_for_point",
    "sum_interference_mw",
]
