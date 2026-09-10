"""CLI: python -m tools.run_winnforum …"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.winnforum.runner import RunnerConfig, run


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.run_winnforum",
        description=(
            "Run selected WInnForum SAS UUT harness suites against sas-core "
            "and capture reproducible artifacts."
        ),
    )
    p.add_argument(
        "--harness-ref",
        default=None,
        help="Harness git commit/tag to checkout into .cache/ (or with --update-harness-ref).",
    )
    p.add_argument(
        "--harness-dir",
        type=Path,
        default=None,
        help="Existing harness checkout (Spectrum-Access-System root or src/harness).",
    )
    p.add_argument(
        "--update-harness-ref",
        action="store_true",
        help="Allow destructive git checkout of --harness-ref inside --harness-dir.",
    )
    p.add_argument(
        "--harness-repo",
        default=None,
        help="Override harness clone URL when --harness-ref clones into .cache/.",
    )
    p.add_argument(
        "--family",
        action="append",
        default=[],
        help="Family code (REG, SIQ, …). Repeatable.",
    )
    p.add_argument(
        "--case",
        action="append",
        default=[],
        help="Case selector (REG.1, test_WINNF_FT_S_REG_1, testcases.mod, or mod::method).",
    )
    p.add_argument("--host", default="localhost")
    p.add_argument("--rsa-port", type=int, default=9000)
    p.add_argument("--ecc-port", type=int, default=9001)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument(
        "--client-cert",
        type=Path,
        default=None,
        help="Admin client certificate path (or WINNFORUM_CLIENT_CERT). Required for real runs.",
    )
    p.add_argument(
        "--client-key",
        type=Path,
        default=None,
        help="Admin client key path (or WINNFORUM_CLIENT_KEY). Required for real runs.",
    )
    p.add_argument(
        "--ca-certs",
        type=Path,
        default=None,
        help="CA bundle path (or WINNFORUM_CA_CERTS). Required for real runs.",
    )
    p.add_argument("--admin-id", default="sas_admin_id")
    p.add_argument("--cbsd-sas-version", default="v1.2")
    p.add_argument("--sas-sas-version", default="v1.3")
    p.add_argument("--max-batch-size", type=int, default=100)
    p.add_argument("--sas-profile", default="cbrs_winnforum")
    p.add_argument(
        "--artifacts-root",
        type=Path,
        default=None,
        help="Default: <repo>/artifacts/winnforum",
    )
    p.add_argument(
        "--start-uut",
        action="store_true",
        help="Start python main.py for the duration of the run.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare artifacts/sas.cfg only; do not start UUT or execute harness.",
    )
    p.add_argument(
        "--skip-healthcheck",
        action="store_true",
        help="Skip mTLS Admin readiness probe (not recommended).",
    )
    p.add_argument("--health-timeout-seconds", type=float, default=60.0)
    p.add_argument(
        "--python",
        dest="python_executable",
        default=None,
        help="Python executable for the harness process (prefer 3.11 venv).",
    )
    p.add_argument(
        "--docker-harness",
        action="store_true",
        help="Execute official harness inside reproducible Docker image (Python 3.11 / Shapely 1.7.1).",
    )
    p.add_argument(
        "--docker-image",
        default="winnforum-sas-harness:928c3150",
        help="Docker image tag for --docker-harness.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _repo_root()
    if not args.dry_run and args.harness_dir is None and not args.harness_ref and not args.docker_harness:
        print(
            "error: provide --harness-ref, --harness-dir, or --docker-harness (or use --dry-run)",
            file=sys.stderr,
        )
        return 2
    if not args.family and not args.case:
        print("error: provide at least one --family or --case", file=sys.stderr)
        return 2

    cfg = RunnerConfig(
        repo_root=root,
        artifacts_root=(args.artifacts_root or (root / "artifacts" / "winnforum")),
        harness_dir=args.harness_dir,
        harness_repo=args.harness_repo
        or "https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System.git",
        harness_ref=args.harness_ref,
        families=list(args.family),
        cases=list(args.case),
        host=args.host,
        rsa_port=args.rsa_port,
        ecc_port=args.ecc_port,
        certs_dir=args.certs_dir,
        client_cert=args.client_cert,
        client_key=args.client_key,
        ca_certs=args.ca_certs,
        admin_id=args.admin_id,
        cbsd_sas_version=args.cbsd_sas_version,
        sas_sas_version=args.sas_sas_version,
        maximum_batch_size=args.max_batch_size,
        start_uut=args.start_uut,
        dry_run=args.dry_run,
        skip_healthcheck=args.skip_healthcheck,
        health_timeout_seconds=args.health_timeout_seconds,
        sas_profile=args.sas_profile,
        update_harness_ref=args.update_harness_ref,
        python_executable=args.python_executable or sys.executable,
        docker_harness=args.docker_harness,
        docker_image=args.docker_image,
    )
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
