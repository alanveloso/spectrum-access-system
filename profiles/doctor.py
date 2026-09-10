"""Profile doctor: structural, semantic, plugin/capability, and data checks.

YAML is validated; it does not execute. Plugin names are discovered by capability,
never hardcoded in the profile document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapters.discovery import (
    GROUP_DEVICE_ADAPTERS,
    GROUP_NETWORK_ADAPTERS,
    GROUP_PROTOCOL_ADAPTERS,
    AdapterDiscovery,
)
from providers.discovery import DataProviderDiscovery
from rf.discovery import RfModelDiscovery
from profiles.errors import ProfileError
from profiles.context import profile_context_from_document, profile_hash
from profiles.negotiate import (
    adapters_satisfying_device_capabilities,
    adapters_satisfying_network_capabilities,
)
from profiles.parse import load_profile, load_profile_document
from profiles.schema import ProfileDocument
from profiles.trust import ProfileTrustTier


@dataclass(frozen=True, slots=True)
class ProfileDoctorFinding:
    name: str
    ok: bool
    detail: str
    section: str = "general"


@dataclass
class ProfileDoctorReport:
    findings: list[ProfileDoctorFinding] = field(default_factory=list)
    profile_id: str | None = None
    profile_version: str | None = None
    profile_hash: str | None = None
    source: str | None = None

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "findings": [
                {
                    "name": item.name,
                    "ok": item.ok,
                    "section": item.section,
                    "detail": item.detail,
                }
                for item in self.findings
            ],
        }


def _add(
    report: ProfileDoctorReport,
    *,
    name: str,
    ok: bool,
    detail: str,
    section: str,
) -> None:
    report.findings.append(
        ProfileDoctorFinding(name=name, ok=ok, detail=detail, section=section)
    )


def package_bootstrap_adapter_discovery() -> AdapterDiscovery:
    """Use entry points when present; otherwise this package's published plugins.

    Profiles still negotiate by capability tokens, not by these bootstrap names.
    """
    live = AdapterDiscovery()
    if live.names(GROUP_DEVICE_ADAPTERS):
        return live
    from adapters.cbsd import cbsd_device_adapter
    from adapters.device import MappingDeviceAdapter, MappingNetworkAdapter
    from adapters.elsa1 import elsa1_protocol_adapter
    from adapters.managed_consumer import managed_network_adapter
    from adapters.protocol import GenericJsonProtocolAdapter
    from adapters.winnforum_rest import winnforum_rest_protocol_adapter

    return AdapterDiscovery(
        overlays={
            GROUP_DEVICE_ADAPTERS: {
                "mapping": MappingDeviceAdapter,
                "cbsd": cbsd_device_adapter,
            },
            GROUP_NETWORK_ADAPTERS: {
                "mapping": MappingNetworkAdapter,
                "managed": managed_network_adapter,
            },
            GROUP_PROTOCOL_ADAPTERS: {
                "generic_json": GenericJsonProtocolAdapter,
                "winnforum_rest": winnforum_rest_protocol_adapter,
                "elsa1": elsa1_protocol_adapter,
            },
        },
        list_entry_points=lambda _g: (),
    )


def package_bootstrap_rf_discovery() -> RfModelDiscovery:
    live = RfModelDiscovery()
    if live.names():
        return live
    from rf.cbrs_winnforum import free_space_rf_adapter

    return RfModelDiscovery(
        overlays={"free_space": free_space_rf_adapter},
        list_entry_points=lambda _g: (),
    )


def _rf_models_for_propagation(
    discovery: RfModelDiscovery, propagation_model: str
) -> tuple[str, ...]:
    matched: list[str] = []
    for name in sorted(discovery.names()):
        try:
            port = discovery.load(name)
        except ValueError:
            continue
        if port.model_id == propagation_model:
            matched.append(name)
    return tuple(matched)


def _data_provider_capability_coverage(
    discovery: DataProviderDiscovery, required: tuple[str, ...]
) -> tuple[frozenset[str], tuple[str, ...]]:
    have: set[str] = set()
    names = sorted(discovery.names())
    for name in names:
        try:
            provider = discovery.load(name)
        except ValueError:
            continue
        have |= set(provider.advertised_capabilities())
    missing = tuple(cap for cap in required if cap not in have)
    return frozenset(have), missing


def diagnose_profile(
    parsed: ProfileDocument,
    *,
    source: str,
    adapter_discovery: AdapterDiscovery | None = None,
    rf_discovery: RfModelDiscovery | None = None,
    data_discovery: DataProviderDiscovery | None = None,
    check_plugins: bool = True,
    require_data_plugins: bool = False,
    check_protection_data: bool = False,
    protection_bundle: str | None = None,
    data_root: Path | str | None = None,
    protection_strict: bool = False,
    trust_tier: ProfileTrustTier | None = None,
) -> ProfileDoctorReport:
    """Run doctor checks on an already-parsed Spectrum Profile document."""
    report = ProfileDoctorReport(source=source)
    report.profile_id = parsed.metadata.id
    report.profile_version = parsed.metadata.version
    report.profile_hash = profile_hash(parsed)

    ctx = profile_context_from_document(parsed)
    _add(
        report,
        name="structure",
        ok=True,
        detail=(
            f"{parsed.metadata.id} v{parsed.metadata.version} "
            f"status={parsed.metadata.status} hash={report.profile_hash[:12]}…"
        ),
        section="structure",
    )
    based_on_note = (
        f"based_on={parsed.metadata.based_on} (provenance only; no inheritance)"
        if parsed.metadata.based_on
        else "based_on=null"
    )
    tier_note = (
        f"trust_tier={trust_tier.value}"
        if trust_tier is not None
        else "trust_tier=unspecified"
    )
    _add(
        report,
        name="provenance",
        ok=True,
        detail=(
            f"{tier_note} hash={report.profile_hash} "
            f"status={parsed.metadata.status} {based_on_note} source={source}"
        ),
        section="trust",
    )
    _add(
        report,
        name="semantics",
        ok=True,
        detail=(
            f"mechanisms={len(ctx.mechanism_versions)} "
            f"rf={ctx.rf_provenance or 'none'}"
        ),
        section="semantics",
    )

    device_caps = (
        parsed.requirements.device_capabilities
        if parsed.requirements is not None
        else ()
    )
    network_caps = (
        parsed.requirements.network_capabilities
        if parsed.requirements is not None
        else ()
    )
    data_caps = (
        parsed.data.required_capabilities if parsed.data is not None else ()
    )
    _add(
        report,
        name="data_capabilities",
        ok=True,
        detail=(
            "none required"
            if not data_caps
            else "required=" + ",".join(data_caps)
        ),
        section="data",
    )

    if not check_plugins:
        _add(
            report,
            name="plugins",
            ok=True,
            detail="skipped (--no-check-plugins)",
            section="plugins",
        )
        return report

    adapters = adapter_discovery or package_bootstrap_adapter_discovery()
    rf_models = rf_discovery or package_bootstrap_rf_discovery()
    data_providers = data_discovery or DataProviderDiscovery()

    if device_caps:
        try:
            hits = adapters_satisfying_device_capabilities(
                adapters, GROUP_DEVICE_ADAPTERS, device_caps
            )
            _add(
                report,
                name="device_plugins",
                ok=True,
                detail=f"capabilities={list(device_caps)} adapters={list(hits)}",
                section="plugins",
            )
        except ValueError as exc:
            _add(
                report,
                name="device_plugins",
                ok=False,
                detail=str(exc),
                section="plugins",
            )
    else:
        _add(
            report,
            name="device_plugins",
            ok=True,
            detail="no device capabilities required",
            section="plugins",
        )

    if network_caps:
        try:
            hits = adapters_satisfying_network_capabilities(
                adapters, GROUP_NETWORK_ADAPTERS, network_caps
            )
            _add(
                report,
                name="network_plugins",
                ok=True,
                detail=f"capabilities={list(network_caps)} adapters={list(hits)}",
                section="plugins",
            )
        except ValueError as exc:
            _add(
                report,
                name="network_plugins",
                ok=False,
                detail=str(exc),
                section="plugins",
            )
    else:
        _add(
            report,
            name="network_plugins",
            ok=True,
            detail="no network capabilities required",
            section="plugins",
        )

    rf = parsed.rf
    if rf is not None and rf.required:
        model = rf.propagation_model or ""
        matches = _rf_models_for_propagation(rf_models, model)
        if matches:
            _add(
                report,
                name="rf_plugins",
                ok=True,
                detail=f"propagation_model={model!r} models={list(matches)}",
                section="plugins",
            )
        else:
            _add(
                report,
                name="rf_plugins",
                ok=False,
                detail=(
                    f"no installed RF model satisfies propagation_model={model!r} "
                    f"(discovered={sorted(rf_models.names())})"
                ),
                section="plugins",
            )
    else:
        _add(
            report,
            name="rf_plugins",
            ok=True,
            detail="rf not required",
            section="plugins",
        )

    if data_caps:
        installed = sorted(data_providers.names())
        covered, missing = _data_provider_capability_coverage(data_providers, data_caps)
        if not installed:
            _add(
                report,
                name="data_plugins",
                ok=not require_data_plugins,
                detail=(
                    "no spectrum_access.data_providers entry points installed; "
                    "runtime must supply providers for "
                    + ",".join(data_caps)
                ),
                section="plugins",
            )
        elif missing:
            _add(
                report,
                name="data_plugins",
                ok=not require_data_plugins,
                detail=(
                    f"missing capabilities {list(missing)}; "
                    f"covered={sorted(covered)}; providers={installed}"
                ),
                section="plugins",
            )
        else:
            _add(
                report,
                name="data_plugins",
                ok=True,
                detail=f"capabilities covered by providers={installed}",
                section="plugins",
            )
    else:
        _add(
            report,
            name="data_plugins",
            ok=True,
            detail="no data capabilities required",
            section="plugins",
        )

    if check_protection_data:
        bundle_id = protection_bundle or "cbrs_winnforum_protection"
        try:
            from protection_data.loader import DatasetError, validate_dataset_bundle

            pdata = validate_dataset_bundle(
                bundle_id,
                data_root=data_root,
                strict=protection_strict,
            )
            missing_slots = pdata.missing_required()
            if pdata.ok:
                _add(
                    report,
                    name="protection_data",
                    ok=True,
                    detail=(
                        f"{pdata.bundle_id} v{pdata.bundle_version} "
                        f"root={pdata.data_root} slots={len(pdata.slots)}"
                    ),
                    section="data",
                )
            else:
                detail = "; ".join(
                    f"{s.slot_id}:{s.detail}" for s in missing_slots
                ) or "incomplete"
                _add(
                    report,
                    name="protection_data",
                    ok=False,
                    detail=detail,
                    section="data",
                )
        except DatasetError as exc:
            _add(
                report,
                name="protection_data",
                ok=False,
                detail=str(exc),
                section="data",
            )
        except Exception as exc:  # noqa: BLE001 — operator env diagnostics
            _add(
                report,
                name="protection_data",
                ok=False,
                detail=f"protection-data check failed: {exc}",
                section="data",
            )

    return report


def run_profile_doctor(
    *,
    profile_id: str | None = None,
    path: Path | str | None = None,
    adapter_discovery: AdapterDiscovery | None = None,
    rf_discovery: RfModelDiscovery | None = None,
    data_discovery: DataProviderDiscovery | None = None,
    check_plugins: bool = True,
    require_data_plugins: bool = False,
    check_protection_data: bool = False,
    protection_bundle: str | None = None,
    data_root: Path | str | None = None,
    protection_strict: bool = False,
) -> ProfileDoctorReport:
    """Load a Spectrum Profile by id or path and diagnose it."""
    if (profile_id is None) == (path is None):
        report = ProfileDoctorReport(source=None)
        _add(
            report,
            name="input",
            ok=False,
            detail="provide exactly one of --id or a profile YAML path",
            section="structure",
        )
        return report

    try:
        if path is not None:
            source = str(Path(path).expanduser().resolve())
            parsed = load_profile_document(Path(path))
            trust_tier = ProfileTrustTier.OPERATOR_EXPLICIT
        else:
            assert profile_id is not None
            source = f"id:{profile_id}"
            parsed = load_profile(profile_id)
            trust_tier = ProfileTrustTier.BUILTIN
    except ProfileError as exc:
        report = ProfileDoctorReport(source=str(path or profile_id))
        _add(
            report,
            name="structure",
            ok=False,
            detail=str(exc),
            section="structure",
        )
        return report

    return diagnose_profile(
        parsed,
        source=source,
        adapter_discovery=adapter_discovery,
        rf_discovery=rf_discovery,
        data_discovery=data_discovery,
        check_plugins=check_plugins,
        require_data_plugins=require_data_plugins,
        check_protection_data=check_protection_data,
        protection_bundle=protection_bundle,
        data_root=data_root,
        protection_strict=protection_strict,
        trust_tier=trust_tier,
    )


def render_profile_doctor_report(report: ProfileDoctorReport) -> str:
    lines = ["spectrum profile doctor", "=" * 24]
    if report.source:
        lines.append(f"source: {report.source}")
    if report.profile_id:
        lines.append(
            f"profile: {report.profile_id} v{report.profile_version} "
            f"hash={report.profile_hash}"
        )
    lines.append("")
    for item in report.findings:
        status = "OK" if item.ok else "FAIL"
        lines.append(f"[{status}] {item.section}/{item.name}: {item.detail}")
    lines.append("=" * 24)
    lines.append("RESULT: " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(lines)
