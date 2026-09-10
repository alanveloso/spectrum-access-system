"""CLI: python -m tools.profile_doctor — Spectrum Profile doctor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from profiles.doctor import (
    render_profile_doctor_report,
    run_profile_doctor,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.profile_doctor",
        description=(
            "Validate a Spectrum Spectrum Profile YAML: structure, semantics, "
            "plugin/capability discovery, and optional protection-data readiness. "
            "YAML is parsed as configuration, not executed as code."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--id",
        dest="profile_id",
        default=None,
        help="Profile id under profiles/definitions/<id>.yaml",
    )
    src.add_argument(
        "path",
        nargs="?",
        default=None,
        type=Path,
        help="Path to a Spectrum Profile YAML file",
    )
    p.add_argument(
        "--no-check-plugins",
        action="store_true",
        help="Skip device/RF/data plugin discovery checks",
    )
    p.add_argument(
        "--require-data-plugins",
        action="store_true",
        help="Fail when required data capabilities have no installed data_providers",
    )
    p.add_argument(
        "--deployment",
        type=Path,
        default=None,
        help=(
            "Optional DeploymentConfig YAML. When set, also validate "
            "profile+deployment composition (SAS_DEPLOYMENT_CONFIG)."
        ),
    )
    p.add_argument(
        "--check-data",
        action="store_true",
        help="Also validate a protection-data bundle against --data-root",
    )
    p.add_argument(
        "--protection-bundle",
        default=None,
        help="Protection-data bundle id (default: cbrs_winnforum_protection)",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Protection-data root directory for --check-data",
    )
    p.add_argument(
        "--strict-data",
        action="store_true",
        help="Strict protection-data payload checks",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_profile_doctor(
        profile_id=args.profile_id,
        path=args.path,
        check_plugins=not args.no_check_plugins,
        require_data_plugins=args.require_data_plugins,
        check_protection_data=args.check_data,
        protection_bundle=args.protection_bundle,
        data_root=args.data_root,
        protection_strict=args.strict_data,
    )
    composition_ok = True
    composition_text = None
    composition_payload = None
    if args.deployment is not None:
        from profiles import load_profile, load_profile_document
        from runtime import (
            PluginRegistry,
            diagnose_composition,
            load_deployment,
            render_composition_doctor_report,
        )

        if args.profile_id:
            profile = load_profile(args.profile_id)
        else:
            profile = load_profile_document(args.path)
        deployment = load_deployment(args.deployment)
        composition_report = diagnose_composition(
            profile,
            deployment,
            PluginRegistry.from_discovery(),
            require_data_plugins=args.require_data_plugins,
        )
        composition_ok = composition_report.status == "READY"
        composition_text = render_composition_doctor_report(composition_report)
        composition_payload = composition_report.as_dict()

    if args.json:
        payload = report.to_dict()
        if composition_payload is not None:
            payload["composition"] = composition_payload
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_profile_doctor_report(report))
        if composition_text is not None:
            print()
            print(composition_text)
    return 0 if report.ok and composition_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
