"""Typed errors for profile-driven runtime composition."""

from __future__ import annotations


class RuntimeCompositionError(Exception):
    """Base class for composition failures. Startup must fail closed."""

    def __init__(
        self,
        message: str,
        *,
        profile_id: str | None = None,
        capability: str | None = None,
        plugin: str | None = None,
        category: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.capability = capability
        self.plugin = plugin
        self.category = category
        self.reason = reason or message
        parts = [message]
        if profile_id:
            parts.append(f"profile={profile_id}")
        if capability:
            parts.append(f"capability={capability}")
        if category:
            parts.append(f"category={category}")
        if plugin:
            parts.append(f"plugin={plugin}")
        if reason and reason != message:
            parts.append(f"reason={reason}")
        super().__init__("; ".join(parts))


class MissingCapabilityError(RuntimeCompositionError):
    """A required capability is not satisfied by the deployment selection."""


class UnknownPluginError(RuntimeCompositionError):
    """Deployment selected a plugin name that is not in the registry."""


class IncompatiblePluginError(RuntimeCompositionError):
    """Plugin exists but does not meet category/capability/interface expectations."""


class InvalidDeploymentConfigError(RuntimeCompositionError):
    """DeploymentConfig document is invalid or incomplete."""
