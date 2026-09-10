"""Per-channel aggregate interference helpers."""

from __future__ import annotations

from primitives.power import dbm_to_mw, mw_to_dbm
from primitives.rf_arithmetic import sum_linear_mw, within_threshold_mw
from services.iap.models import (
    ChannelAggregateResult,
    FrequencyChannel,
    GrantChannelContribution,
    GrantRfInfo,
    ProtectedEntityKind,
)

IAPBW_HZ = 5_000_000
EIRP_STEP_DB = 1.0
# CBRS / WINNF lower bound for grant maxEirp (dBm/MHz). Asserted by harness
# WINNF.FT.S.FAD.1 (requestedOperationParam.maxEirp >= -137) and used here as
# the IAP authorization floor: below this the managing SAS terminates rather
# than authorize an out-of-protocol EIRP.
DEFAULT_EIRP_FLOOR_DBM_MHZ = -137.0
# WINNF interference.ESC_CAT_A_HIGH_FREQ_HZ — Cat A not considered for ESC
# constraints at/above this edge (grant or channel low).
ESC_CAT_A_HIGH_FREQ_HZ = 3_660_000_000


def apply_pre_iap_margin_db(threshold_dbm: float, margin_db: float) -> float:
    """Threshold used inside IAP after subtracting pre-IAP margin (dB)."""
    return threshold_dbm - margin_db


def resolve_iap_band_origin_hz() -> int:
    """Lower edge of the active Profile operating band (IAP 5 MHz grid origin).

 Uses the primary spectrum range, not assignment ``channelization.origin_hz``.
 """
    from services.iap.protection_points import cbrs_band_hz

    return cbrs_band_hz()[0]


def overlapping_iap_channels(
    low_hz: int,
    high_hz: int,
    *,
    channel_hz: int = IAPBW_HZ,
    band_origin_hz: int | None = None,
) -> list[FrequencyChannel]:
    """Return 5 MHz IAP channels overlapping ``[low_hz, high_hz)``."""
    if high_hz <= low_hz or channel_hz <= 0:
        return []
    origin = int(band_origin_hz) if band_origin_hz is not None else resolve_iap_band_origin_hz()
    start = origin + ((max(low_hz, origin) - origin) // channel_hz) * channel_hz
    channels: list[FrequencyChannel] = []
    cursor = start
    while cursor < high_hz:
        ch_high = cursor + channel_hz
        if ch_high > low_hz and cursor < high_hz:
            channels.append(FrequencyChannel(low_hz=cursor, high_hz=ch_high))
        cursor = ch_high
        if cursor > high_hz + channel_hz:
            break
    return channels


def grant_overlaps_channel(
    grant: GrantRfInfo,
    channel: FrequencyChannel,
    *,
    entity_kind: ProtectedEntityKind | None = None,
) -> bool:
    """True when grant overlaps channel; applies ESC Cat-A 3660 MHz rule when kind is ESC."""
    if not (grant.low_hz < channel.high_hz and grant.high_hz > channel.low_hz):
        return False
    if entity_kind is ProtectedEntityKind.ESC:
        cat = (grant.cbsd_category or "").strip().upper()
        if cat == "A" and (
            grant.low_hz >= ESC_CAT_A_HIGH_FREQ_HZ
            or channel.low_hz >= ESC_CAT_A_HIGH_FREQ_HZ
        ):
            return False
    return True


def aggregate_channel(
    contributions: list[GrantChannelContribution],
    *,
    channel: FrequencyChannel,
    threshold_mw: float,
    managing_grant_ids: set[str] | None = None,
) -> ChannelAggregateResult:
    managing = managing_grant_ids or set()
    matched = [
        c
        for c in contributions
        if c.channel.low_hz == channel.low_hz and c.channel.high_hz == channel.high_hz
    ]
    agg = sum(c.interference_mw for c in matched)
    asas = sum(c.interference_mw for c in matched if c.grant_id in managing)
    n = max(1, len({c.grant_id for c in matched}))
    fairshare = threshold_mw / float(n) if threshold_mw > 0 else 0.0
    return ChannelAggregateResult(
        channel=channel,
        aggregate_mw=float(agg),
        managing_sas_mw=float(asas),
        threshold_mw=threshold_mw,
        fairshare_mw=fairshare,
        within_threshold=within_threshold_mw(agg, threshold_mw),
    )


def sum_interference_mw(values: list[float]) -> float:
    return sum_linear_mw(values)


__all__ = (
    "DEFAULT_EIRP_FLOOR_DBM_MHZ",
    "EIRP_STEP_DB",
    "ESC_CAT_A_HIGH_FREQ_HZ",
    "IAPBW_HZ",
    "aggregate_channel",
    "apply_pre_iap_margin_db",
    "dbm_to_mw",
    "grant_overlaps_channel",
    "mw_to_dbm",
    "overlapping_iap_channels",
    "resolve_iap_band_origin_hz",
    "sum_interference_mw",
)
