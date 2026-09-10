"""Reconcile Spectrum Profile requirements with operational feature toggles.

Profile remains regulatory authority. Deployment selects implementations.
Operational settings may configure optional behavior, but must not silently
disable a mechanism the selected Profile declares mandatory.
"""

from __future__ import annotations

from typing import Any

from profiles.context import selected_mechanism_ids
from profiles.schema import ProfileDocument
from runtime.errors import RuntimeCompositionError

# Semantic mechanism controlled by SAS_IAP_ENABLED (IAP aggregate protection).
IAP_PROTECTION_MECHANISM = "aggregate_linear_power"
SAS_IAP_ENABLED_SETTING = "SAS_IAP_ENABLED"


class RequiredCapabilityDisabledError(RuntimeCompositionError):
    """A Profile-mandatory mechanism/capability is disabled by operator settings."""

    def __init__(
        self,
        message: str,
        *,
        profile_id: str | None = None,
        mechanism_id: str | None = None,
        setting: str | None = None,
    ) -> None:
        self.mechanism_id = mechanism_id
        self.setting = setting
        super().__init__(
            message,
            profile_id=profile_id,
            capability=mechanism_id,
            reason=message,
        )
        # Append setting for operators after the base message parts.
        if setting and setting not in str(self):
            self.args = (f"{self.args[0]}; setting={setting}",)


def profile_requires_mechanism(profile: ProfileDocument, mechanism_id: str) -> bool:
    return mechanism_id in selected_mechanism_ids(profile)


def reconcile_operational_toggles(
    profile: ProfileDocument,
    settings: Any,
) -> None:
    """Fail closed when operator settings disable a Profile-mandatory mechanism.

    Called once at startup/composition validation — not per request.
    """
    if not profile_requires_mechanism(profile, IAP_PROTECTION_MECHANISM):
        return
    enabled = bool(getattr(settings, "sas_iap_enabled", True))
    if enabled:
        return
    raise RequiredCapabilityDisabledError(
        (
            f"required mechanism {IAP_PROTECTION_MECHANISM!r} is disabled by "
            f"operational setting {SAS_IAP_ENABLED_SETTING}"
        ),
        profile_id=profile.metadata.id,
        mechanism_id=IAP_PROTECTION_MECHANISM,
        setting=SAS_IAP_ENABLED_SETTING,
    )


def assert_iap_disable_allowed(profile: ProfileDocument) -> None:
    """Request-path guard: disabling IAP is illegal when the Profile requires it."""
    if profile_requires_mechanism(profile, IAP_PROTECTION_MECHANISM):
        raise RequiredCapabilityDisabledError(
            (
                f"required mechanism {IAP_PROTECTION_MECHANISM!r} is disabled by "
                f"operational setting {SAS_IAP_ENABLED_SETTING}"
            ),
            profile_id=profile.metadata.id,
            mechanism_id=IAP_PROTECTION_MECHANISM,
            setting=SAS_IAP_ENABLED_SETTING,
        )
