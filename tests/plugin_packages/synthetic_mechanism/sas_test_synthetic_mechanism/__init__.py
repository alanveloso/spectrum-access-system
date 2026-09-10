"""Test-only external mechanism contract for discovery proofs."""

from __future__ import annotations

from primitives.registry import MechanismAxis, MechanismContract

SYNTHETIC_MECHANISM_ID = "synthetic_mechanism"


def synthetic_mechanism_contract() -> MechanismContract:
    return MechanismContract(
        SYNTHETIC_MECHANISM_ID,
        MechanismAxis.PROTECTION,
        "1.0.0",
    )
