"""Composition diagnostics (deployment + profile satisfiability)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from profiles.schema import ProfileDocument
from runtime.capabilities import required_capabilities
from runtime.composition import RuntimeComposition, compose_runtime
from runtime.deployment import DeploymentConfig
from runtime.errors import RuntimeCompositionError
from runtime.registry import PluginRegistry


@dataclass
class CompositionDoctorReport:
    profile_id: str
    profile_hash: str | None
    deployment_id: str
    required_capabilities: list[str] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    status: str = "NOT READY"
    detail: str | None = None
    composition: RuntimeComposition | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_hash": self.profile_hash,
            "deployment_id": self.deployment_id,
            "required_capabilities": list(self.required_capabilities),
            "resolved": dict(self.resolved),
            "missing": list(self.missing),
            "status": self.status,
            "detail": self.detail,
        }


def diagnose_composition(
    profile: ProfileDocument,
    deployment: DeploymentConfig,
    registry: PluginRegistry,
    *,
    require_data_plugins: bool = True,
) -> CompositionDoctorReport:
    """Non-destructive composition check for CLI/doctor."""
    from profiles.context import profile_hash

    requirements = required_capabilities(profile)
    report = CompositionDoctorReport(
        profile_id=profile.metadata.id,
        profile_hash=profile_hash(profile),
        deployment_id=deployment.id,
        required_capabilities=list(requirements.as_sorted_tokens()),
    )
    try:
        composition = compose_runtime(
            profile,
            deployment,
            registry,
            require_data_plugins=require_data_plugins,
        )
    except RuntimeCompositionError as exc:
        report.status = "NOT READY"
        report.detail = str(exc)
        if exc.capability:
            report.missing = [exc.capability]
        return report

    report.composition = composition
    report.resolved = dict(composition.provenance.resolved)
    report.missing = list(composition.provenance.unsatisfied_data_capabilities)
    if report.missing:
        # Advisory READY only when an explicit test fixture opts out of data enforcement.
        if require_data_plugins:
            report.status = "NOT READY"
            report.detail = f"unsatisfied data capabilities: {report.missing}"
        else:
            report.status = "READY"
            report.detail = (
                "READY with advisory unsatisfied data capabilities: "
                f"{report.missing}"
            )
    else:
        report.status = "READY"
        report.detail = None
    return report


def render_composition_doctor_report(report: CompositionDoctorReport) -> str:
    lines = [
        f"Profile: {report.profile_id}",
        f"Profile hash: {report.profile_hash}",
        f"Deployment: {report.deployment_id}",
        f"Required capabilities: {', '.join(report.required_capabilities) or '(none)'}",
        "Capability authorities:",
    ]
    data_required = [
        tok.removeprefix("data.")
        for tok in report.required_capabilities
        if tok.startswith("data.")
    ]
    if data_required:
        for cap in data_required:
            key = f"data.{cap}"
            authority = report.resolved.get(key)
            if authority:
                lines.append(f"  {cap} -> {authority} -> SATISFIED")
            elif cap in report.missing:
                lines.append(f"  {cap} -> none -> UNSATISFIED")
            else:
                lines.append(f"  {cap} -> (unresolved) -> UNKNOWN")
    else:
        lines.append("  (no data capabilities required)")
    lines.append("Resolved:")
    if report.resolved:
        for key, value in sorted(report.resolved.items()):
            lines.append(f"  {key} -> {value}")
    else:
        lines.append("  (none)")
    lines.append("Missing:")
    if report.missing:
        for item in report.missing:
            lines.append(f"  {item}")
    else:
        lines.append("  (none)")
    lines.append(f"Status: {report.status}")
    if report.detail:
        lines.append(f"Detail: {report.detail}")
    return "\n".join(lines)
