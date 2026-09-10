"""Reusable RF arithmetic: dB/linear power, single-link receive, linear aggregates.

Fairshare iteration, receiver masks, terrain models, and move-list policy stay in services.
"""

from __future__ import annotations

from collections.abc import Iterable

from primitives.power import dbm_to_mw, mw_to_dbm


def linear_from_db(db: float) -> float:
    return float(10.0 ** (float(db) / 10.0))


def db_from_linear(value: float) -> float:
    return mw_to_dbm(value)


def received_power_dbm(eirp_dbm: float, path_loss_db: float) -> float:
    return float(eirp_dbm) - float(path_loss_db)


def received_power_mw(eirp_dbm: float, path_loss_db: float) -> float:
    return dbm_to_mw(received_power_dbm(eirp_dbm, path_loss_db))


def sum_linear_mw(values: Iterable[float]) -> float:
    return float(sum(max(0.0, float(v)) for v in values))


def within_threshold_mw(
    aggregate_mw: float, threshold_mw: float, *, abs_tol: float = 1e-15
) -> bool:
    return float(aggregate_mw) <= float(threshold_mw) + abs_tol


def within_threshold_dbm(
    aggregate_dbm: float,
    threshold_dbm: float,
    margin_db: float = 0.0,
    *,
    abs_tol: float = 1e-12,
) -> bool:
    return float(aggregate_dbm) <= float(threshold_dbm) + float(margin_db) + abs_tol
