"""RF architectural boundary — what is generic vs regulatory.

This module documents the stable RfPort contract and classifies remaining
WInnForum/reference-model paths. It does not implement propagation.

See also: docs/architecture/rf.md
"""

from __future__ import annotations

# Capability token advertised by path-loss RfPort plugins (matches model_id).
RF_CAPABILITY_PATH_LOSS = "rf.path_loss"

# Classification of remaining concrete RF-related paths (enforcement phase):
#
# IAP generic coupling     -> uses RuntimeComposition.rf (RfPort.path_loss)  [A]
# BPR path-loss            -> uses RuntimeComposition.rf when composed       [A]
# ESC IAP incidence/antenna-> reference_models ITM+antenna; not RfPort       [C/D]
# DPA Rel1Ext compose      -> ITM median + P.2108 clutter + activity (reg.)  [B+C]
# Terrain NED/HAAT         -> data/provider concern, not RfPort selection    [A data]
# NLCD / hybrid PPA        -> hybrid path; not RfPort.path_loss              [C]
#
# Outcome for RfPort in this phase: contract sufficient for IAP/BPR path-loss
# substitutability (Outcome A). ESC/DPA remain specialized debt (Outcome C).
#
# External extraction: Free Space + WInnForum-bridged ITM live in sibling
# package ``spectrum-propagation``; SAS consumes via ``rf.external_propagation``.
