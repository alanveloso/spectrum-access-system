"""Profile-driven runtime composition.

Spectrum Profile describes regulatory requirements.
DeploymentConfig selects implementations.
Plugin discovery advertises capabilities.
RuntimeComposition validates and resolves the combination.
"""

from __future__ import annotations

from runtime.capabilities import RuntimeRequirements, required_capabilities
from runtime.composition import CompositionProvenance, RuntimeComposition, compose_runtime
from runtime.bootstrap import (
    ProcessCompositionAlreadyInitializedError,
    get_process_runtime_composition,
    initialize_process_runtime_composition,
)
from runtime.context import (
    get_bound_runtime_composition,
    lookup_runtime_composition,
    runtime_composition_scope,
)
from runtime.deployment import (
    DEFAULT_DEPLOYMENT_PATH,
    DEPLOYMENT_ENV,
    DeploymentConfig,
    PluginSelection,
    deployment_hash,
    load_deployment,
    load_deployment_for_startup,
    parse_deployment_dict,
    resolve_deployment_path,
)
from runtime.doctor import (
    CompositionDoctorReport,
    diagnose_composition,
    render_composition_doctor_report,
)
from runtime.errors import (
    IncompatiblePluginError,
    InvalidDeploymentConfigError,
    MissingCapabilityError,
    RuntimeCompositionError,
    UnknownPluginError,
)
from runtime.registry import PluginRegistry
from runtime.toggle_reconciliation import (
    RequiredCapabilityDisabledError,
    reconcile_operational_toggles,
)

__all__ = [
    "CompositionDoctorReport",
    "CompositionProvenance",
    "DEFAULT_DEPLOYMENT_PATH",
    "DEPLOYMENT_ENV",
    "DeploymentConfig",
    "IncompatiblePluginError",
    "InvalidDeploymentConfigError",
    "MissingCapabilityError",
    "PluginRegistry",
    "PluginSelection",
    "ProcessCompositionAlreadyInitializedError",
    "RequiredCapabilityDisabledError",
    "RuntimeComposition",
    "RuntimeCompositionError",
    "RuntimeRequirements",
    "UnknownPluginError",
    "compose_runtime",
    "deployment_hash",
    "diagnose_composition",
    "get_bound_runtime_composition",
    "get_process_runtime_composition",
    "initialize_process_runtime_composition",
    "load_deployment",
    "load_deployment_for_startup",
    "lookup_runtime_composition",
    "parse_deployment_dict",
    "reconcile_operational_toggles",
    "render_composition_doctor_report",
    "required_capabilities",
    "resolve_deployment_path",
    "runtime_composition_scope",
]
