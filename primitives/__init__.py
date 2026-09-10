"""Generic value objects: frequency, power, time, geography.

Seed of the primitive catalog. This package must not import adapters, ORM,
or protocol modules. Production request paths stay unchanged until a later
authorized extraction task wires these types.
"""

from primitives.access import AccessClass, OrderedAccess, bind_request_class
from primitives.admission import evaluate_admission, power_exceeds
from primitives.authorization import (
    AuthorizedArea,
    ExclusionZone,
    FixedWindow,
    Lease,
    LeaseState,
)
from primitives.availability import (
    AvailabilityChangeEvent,
    AvailabilityConstraint,
    AvailabilityEventKind,
    AvailabilityMode,
    AvailabilityScope,
    AvailabilityZoneKind,
    any_constraint_allows,
)
from primitives.channelization import assignment_channels
from primitives.constraint import Constraint, ConstraintKind
from primitives.coordination import (
    CoordinationCycle,
    FrozenEvaluation,
    run_snapshot_evaluate_apply,
    writeback_decisions,
)
from primitives.decision import Decision, DecisionAction, is_apply_write
from primitives.entitlement import ProtectionEntitlement
from primitives.frequency import FrequencyRange
from primitives.geography import GeoPoint, LinearRing, PointRadius, haversine_m
from primitives.power import PowerDbm, PowerMw, dbm_to_mw, mw_to_dbm
from primitives.preemption import class_preempts
from primitives.profile_context import ProfileContext
from primitives.refresh import PeriodicRefresh, open_until
from primitives.registry import (
    MechanismAxis,
    MechanismContract,
    MechanismRegistry,
    builtin_mechanism_registry,
    discovered_mechanism_registry,
    select_optional_access,
)
from primitives.request import SpectrumRequest, TransmissionFootprint
from primitives.rf_arithmetic import (
    db_from_linear,
    linear_from_db,
    received_power_dbm,
    received_power_mw,
    sum_linear_mw,
    within_threshold_dbm,
    within_threshold_mw,
)
from primitives.station_limits import (
    AntennaHeightLimit,
    DuplexMode,
    DuplexModeRequirement,
    ForbiddenDeviceRoles,
    MaxAssignmentBandwidth,
)
from primitives.time import TimeInterval, UtcInstant

__all__ = [
    "AccessClass",
    "assignment_channels",
    "AuthorizedArea",
    "AvailabilityChangeEvent",
    "AvailabilityConstraint",
    "AvailabilityEventKind",
    "AvailabilityMode",
    "AvailabilityScope",
    "AvailabilityZoneKind",
    "Constraint",
    "ConstraintKind",
    "CoordinationCycle",
    "Decision",
    "DecisionAction",
    "ExclusionZone",
    "FixedWindow",
    "FrequencyRange",
    "FrozenEvaluation",
    "GeoPoint",
    "Lease",
    "LeaseState",
    "LinearRing",
    "MechanismAxis",
    "MechanismContract",
    "MechanismRegistry",
    "PointRadius",
    "PowerDbm",
    "OrderedAccess",
    "PeriodicRefresh",
    "PowerMw",
    "ProfileContext",
    "ProtectionEntitlement",
    "SpectrumRequest",
    "TimeInterval",
    "TransmissionFootprint",
    "UtcInstant",
    "AntennaHeightLimit",
    "any_constraint_allows",
    "DuplexMode",
    "DuplexModeRequirement",
    "ForbiddenDeviceRoles",
    "MaxAssignmentBandwidth",
    "bind_request_class",
    "builtin_mechanism_registry",
    "discovered_mechanism_registry",
    "class_preempts",
    "db_from_linear",
    "dbm_to_mw",
    "evaluate_admission",
    "haversine_m",
    "is_apply_write",
    "linear_from_db",
    "mw_to_dbm",
    "open_until",
    "power_exceeds",
    "received_power_dbm",
    "received_power_mw",
    "run_snapshot_evaluate_apply",
    "select_optional_access",
    "sum_linear_mw",
    "within_threshold_dbm",
    "within_threshold_mw",
    "writeback_decisions",
]
