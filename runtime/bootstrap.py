"""Process-local RuntimeComposition lifecycle (resolve once per OS process).

Construction belongs here (and FastAPI/Celery startup hooks), not in business
services. Lookup never discovers plugins or recomposes.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from runtime.composition import RuntimeComposition, compose_runtime
from runtime.errors import RuntimeCompositionError

if TYPE_CHECKING:
    from profiles.schema import ProfileDocument
    from runtime.deployment import DeploymentConfig
    from runtime.registry import PluginRegistry

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_process_composition: RuntimeComposition | None = None


class ProcessCompositionAlreadyInitializedError(RuntimeCompositionError):
    """Raised when a second, conflicting composition is requested in-process."""


def get_process_runtime_composition() -> RuntimeComposition | None:
    """Return the process-local composition when initialized; else None."""
    return _process_composition


def initialize_process_runtime_composition(
    *,
    composition: RuntimeComposition | None = None,
    profile: ProfileDocument | None = None,
    deployment: DeploymentConfig | None = None,
    registry: PluginRegistry | None = None,
    require_data_plugins: bool = True,
) -> RuntimeComposition:
    """Resolve and bind RuntimeComposition exactly once for this OS process.

    Prefer passing an already-built ``composition`` (API startup) or omit all
    of profile/deployment/registry to load Settings + discover plugins once.

    Idempotent when the existing composition matches ``deployment_hash`` /
    ``profile_hash``. A conflicting second initialization fails closed.

    Data capabilities are validated fail-closed (``require_data_plugins=True``).
    Passing ``False`` is test-only and must not be used by production bootstrap.
    """
    global _process_composition

    with _lock:
        existing = _process_composition
        if composition is None:
            explicit_inputs = (
                profile is not None
                or deployment is not None
                or registry is not None
            )
            # Pure re-entry (worker_ready / second hook): keep existing authority.
            if existing is not None and not explicit_inputs:
                return existing
            # Same Profile+Deployment already bound (e.g. dual uvicorn listeners):
            # reuse without a second plugin discovery.
            if (
                existing is not None
                and profile is not None
                and deployment is not None
            ):
                from profiles.context import profile_hash
                from runtime.deployment import deployment_hash as dep_hash

                if (
                    profile_hash(profile) == existing.provenance.profile_hash
                    and dep_hash(deployment) == existing.provenance.deployment_hash
                ):
                    return existing
            composition = _build_composition(
                profile=profile,
                deployment=deployment,
                registry=registry,
                require_data_plugins=require_data_plugins,
            )

        if existing is not None:
            if existing is composition:
                return existing
            if _same_authority(existing, composition):
                return existing
            raise ProcessCompositionAlreadyInitializedError(
                "RuntimeComposition already initialized for this process; "
                "restart the process to apply a different Profile/Deployment",
                profile_id=getattr(
                    composition.provenance, "profile_id", None
                ),
                reason="hot_swap_forbidden",
            )

        _process_composition = composition
        logger.info(
            "runtime_composition_initialized profile=%s deployment=%s hash=%s",
            composition.provenance.profile_id,
            composition.provenance.deployment_id,
            composition.provenance.deployment_hash[:12],
        )
        return composition


def require_process_runtime_composition() -> RuntimeComposition:
    """Fail closed when the process has no initialized composition."""
    active = get_process_runtime_composition()
    if active is None:
        raise RuntimeCompositionError(
            "RuntimeComposition is not initialized for this process",
            reason="missing_process_composition",
        )
    return active


def _reset_process_runtime_composition_for_tests() -> None:
    """TEST-ONLY: clear process-local authority between fixtures."""
    global _process_composition
    with _lock:
        _process_composition = None


def _same_authority(left: RuntimeComposition, right: RuntimeComposition) -> bool:
    return (
        left.provenance.profile_hash == right.provenance.profile_hash
        and left.provenance.deployment_hash == right.provenance.deployment_hash
        and dict(left.provenance.selected_plugins)
        == dict(right.provenance.selected_plugins)
    )


def _build_composition(
    *,
    profile: ProfileDocument | None,
    deployment: DeploymentConfig | None,
    registry: PluginRegistry | None,
    require_data_plugins: bool,
) -> RuntimeComposition:
    from config import get_settings
    from profiles import get_active_profile_document
    from runtime.deployment import load_deployment_for_startup
    from runtime.registry import PluginRegistry

    settings = get_settings()
    doc = profile if profile is not None else get_active_profile_document()
    dep = (
        deployment
        if deployment is not None
        else load_deployment_for_startup(explicit=settings.sas_deployment_config)
    )
    reg = registry if registry is not None else PluginRegistry.from_discovery()
    return compose_runtime(
        doc,
        dep,
        reg,
        require_data_plugins=require_data_plugins,
    )
