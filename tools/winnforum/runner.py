"""Orchestrate WInnForum harness runs and artifact capture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO

from tools.winnforum.families import UnittestTarget, resolve_unittest_targets
from tools.winnforum.healthcheck import wait_for_mtls_admin
from tools.winnforum.junit import write_junit_xml
from tools.winnforum.sas_cfg import default_sas_cfg_params, write_sas_cfg
from tools.winnforum.unittest_parse import parse_unittest_output

DEFAULT_HARNESS_REPO = (
    "https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System.git"
)

# Explicit path resolution only — no hardcoded harness certificate basenames.
_ENV_CLIENT_CERT = "WINNFORUM_CLIENT_CERT"
_ENV_CLIENT_KEY = "WINNFORUM_CLIENT_KEY"
_ENV_CA_CERTS = "WINNFORUM_CA_CERTS"

# Official WInnForum harness DatabaseServer Basic Auth (src/harness/database.py).
_WINNFORUM_DB_SERVER_USER = "username"
_WINNFORUM_DB_SERVER_PASSWORD = "password"


def apply_certification_fss_db_sync_auth(env: dict[str, str]) -> dict[str, str]:
    """Ensure FSS InjectDatabaseUrl sync can authenticate to harness DatabaseServer.

    Official FDB.3–6 FSS servers use HTTP Basic Auth with fixed lab credentials.
    Non-empty operator overrides are preserved. Production must set real secrets.
    """
    out = dict(env)
    if not str(out.get("DB_SYNC_USERNAME") or "").strip():
        out["DB_SYNC_USERNAME"] = _WINNFORUM_DB_SERVER_USER
    if not str(out.get("DB_SYNC_PASSWORD") or "").strip():
        out["DB_SYNC_PASSWORD"] = _WINNFORUM_DB_SERVER_PASSWORD
    return out


@dataclass
class RunnerConfig:
    repo_root: Path
    artifacts_root: Path
    harness_dir: Path | None
    harness_repo: str = DEFAULT_HARNESS_REPO
    harness_ref: str | None = None
    families: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    host: str = "localhost"
    rsa_port: int = 9000
    ecc_port: int = 9001
    certs_dir: Path | None = None
    client_cert: Path | None = None
    client_key: Path | None = None
    ca_certs: Path | None = None
    admin_id: str = "sas_admin_id"
    cbsd_sas_version: str = "v1.2"
    sas_sas_version: str = "v1.3"
    maximum_batch_size: int = 100
    start_uut: bool = False
    dry_run: bool = False
    skip_healthcheck: bool = False
    health_timeout_seconds: float = 60.0
    sas_profile: str = "cbrs_winnforum"
    python_executable: str = sys.executable
    # When set with --harness-dir, allow git checkout of harness_ref (destructive).
    update_harness_ref: bool = False
    docker_harness: bool = False
    docker_image: str = "winnforum-sas-harness:928c3150"


@dataclass
class RunArtifacts:
    directory: Path
    environment_path: Path
    sas_cfg_path: Path
    uut_log_path: Path
    harness_log_path: Path
    results_path: Path
    junit_path: Path
    summary_path: Path


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_rev(cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def prepare_artifact_dir(artifacts_root: Path, stamp: str | None = None) -> RunArtifacts:
    """Create a unique artifact directory for one harness run.

    Same-second collisions (parallel/manual re-runs) get a numeric suffix instead
    of raising ``FileExistsError``.
    """
    base = stamp or _utc_stamp()
    directory = artifacts_root / base
    if directory.exists():
        n = 1
        while True:
            candidate = artifacts_root / f"{base}_{n}"
            if not candidate.exists():
                directory = candidate
                break
            n += 1
    directory.mkdir(parents=True, exist_ok=False)
    return RunArtifacts(
        directory=directory,
        environment_path=directory / "environment.json",
        sas_cfg_path=directory / "sas.cfg",
        uut_log_path=directory / "uut.log",
        harness_log_path=directory / "harness.log",
        results_path=directory / "results.json",
        junit_path=directory / "junit.xml",
        summary_path=directory / "summary.md",
    )


def _path_from_env(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(raw).expanduser()


def resolve_cert_paths(
    cfg: RunnerConfig,
) -> tuple[Path | None, Path | None, Path | None, list[str]]:
    """Resolve client/CA paths from CLI or env only (no hardcoded basenames).

    Official harness leaf files live under ``<harness>/src/harness/certs/``
    (not ``testdata/certs``). Callers must pass absolute paths via CLI/env;
    preflight discovers and prints suggested ``WINNFORUM_*`` values.
    """
    notes: list[str] = []
    client_cert = cfg.client_cert or _path_from_env(_ENV_CLIENT_CERT)
    client_key = cfg.client_key or _path_from_env(_ENV_CLIENT_KEY)
    ca_certs = cfg.ca_certs or _path_from_env(_ENV_CA_CERTS)
    if client_cert is None:
        notes.append(
            f"client cert required via --client-cert or {_ENV_CLIENT_CERT} "
            "(typically under <harness>/src/harness/certs/)"
        )
    if client_key is None:
        notes.append(
            f"client key required via --client-key or {_ENV_CLIENT_KEY} "
            "(typically under <harness>/src/harness/certs/)"
        )
    if ca_certs is None:
        notes.append(
            f"CA bundle required via --ca-certs or {_ENV_CA_CERTS} "
            "(typically under <harness>/src/harness/certs/)"
        )
    return client_cert, client_key, ca_certs, notes


def ensure_harness_checkout(cfg: RunnerConfig, log: list[str]) -> Path:
    if cfg.harness_dir is not None:
        path = cfg.harness_dir.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"harness dir not found: {path}")
        log.append(f"using existing harness dir {path}")
        if cfg.harness_ref:
            if not cfg.update_harness_ref:
                raise RuntimeError(
                    "refusing to git checkout inside --harness-dir without "
                    "--update-harness-ref (destructive). Pass a matching checkout "
                    "or clone via --harness-ref alone."
                )
            subprocess.run(
                ["git", "fetch", "--tags", "--force"],
                cwd=str(path),
                check=False,
                capture_output=True,
                text=True,
            )
            checkout = subprocess.run(
                ["git", "checkout", "--force", cfg.harness_ref],
                cwd=str(path),
                check=False,
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"git checkout {cfg.harness_ref!r} failed: {checkout.stderr}"
                )
            log.append(f"checked out harness ref {cfg.harness_ref} (--update-harness-ref)")
        return path

    if not cfg.harness_ref:
        raise ValueError("either --harness-dir or --harness-ref is required")

    cache = cfg.repo_root / ".cache" / "winnforum-harness"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / ".git").is_dir():
        log.append(f"cloning {cfg.harness_repo} → {cache}")
        clone = subprocess.run(
            ["git", "clone", cfg.harness_repo, str(cache)],
            check=False,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed: {clone.stderr}")
    else:
        subprocess.run(
            ["git", "fetch", "--tags", "--force"],
            cwd=str(cache),
            check=False,
            capture_output=True,
            text=True,
        )
    checkout = subprocess.run(
        ["git", "checkout", "--force", cfg.harness_ref],
        cwd=str(cache),
        check=False,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        raise RuntimeError(
            f"git checkout {cfg.harness_ref!r} failed: {checkout.stderr}"
        )
    log.append(f"harness at {cache}@{cfg.harness_ref}")
    return cache


def harness_workdir(harness_root: Path) -> Path:
    candidate = harness_root / "src" / "harness"
    return candidate if candidate.is_dir() else harness_root


@contextmanager
def install_sas_cfg(workdir: Path, sas_cfg: Path) -> Iterator[None]:
    """Install sas.cfg for the harness CWD, restoring any previous file afterward."""
    target = workdir / "sas.cfg"
    backup = workdir / "sas.cfg.sas-core-backup"
    had_existing = target.exists()
    if had_existing:
        backup.write_bytes(target.read_bytes())
    try:
        target.write_text(sas_cfg.read_text(encoding="utf-8"), encoding="utf-8")
        yield
    finally:
        if had_existing and backup.exists():
            target.write_bytes(backup.read_bytes())
            backup.unlink(missing_ok=True)
        elif target.exists() and not had_existing:
            target.unlink()
            backup.unlink(missing_ok=True)


def write_environment(
    path: Path,
    *,
    cfg: RunnerConfig,
    harness_root: Path | None,
    uut_commit: str | None,
    harness_commit: str | None,
    targets: Sequence[UnittestTarget],
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "uut_commit": uut_commit,
        "harness_commit": harness_commit,
        "harness_ref": cfg.harness_ref,
        "harness_repo": cfg.harness_repo,
        "harness_root": str(harness_root) if harness_root else None,
        "sas_profile": cfg.sas_profile,
        "datasets": {"note": "dataset versions recorded when models are versioned"},
        "targets": [t.label() for t in targets],
        "host": cfg.host,
        "rsa_port": cfg.rsa_port,
        "ecc_port": cfg.ecc_port,
        "dry_run": cfg.dry_run,
        "start_uut": cfg.start_uut,
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_summary(
    path: Path,
    *,
    artifacts: RunArtifacts,
    results: dict[str, Any],
    notes: list[str],
) -> None:
    lines = [
        "# WInnForum harness run summary",
        "",
        f"- artifacts: `{artifacts.directory}`",
        f"- tests_run: {results.get('tests_run')}",
        f"- passed: {results.get('passed')}",
        f"- failed: {results.get('failed')}",
        f"- error: {results.get('error')}",
        f"- raw_ok: {results.get('raw_ok')}",
        "",
        "## Notes",
        "",
    ]
    lines.extend(f"- {n}" for n in notes)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_harness_unittest(
    *,
    workdir: Path,
    sas_cfg: Path,
    targets: Sequence[UnittestTarget],
    python_executable: str,
    log_path: Path,
    repo_root: Path | None = None,
    docker_harness: bool = False,
    docker_image: str = "winnforum-sas-harness:928c3150",
    artifacts_dir: Path | None = None,
    harness_pki_dir: Path | None = None,
) -> tuple[int, str]:
    payload = [{"module": t.module, "method": t.method} for t in targets]
    if docker_harness:
        from tools.winnforum.docker_runner import DockerHarnessConfig, docker_harness_run_exec

        run_dir = artifacts_dir or sas_cfg.parent
        # Ensure sas.cfg is visible inside the container working directory.
        (run_dir / "sas.cfg").write_text(sas_cfg.read_text(encoding="utf-8"), encoding="utf-8")
        cfg = DockerHarnessConfig(
            image=docker_image,
            sas_repo_root=repo_root,
            artifacts_dir=run_dir,
            harness_pki_dir=harness_pki_dir,
        )
        return docker_harness_run_exec(
            targets_payload=payload,
            cfg=cfg,
            log_path=log_path,
        )
    # Run exec_unittest with harness on sys.path via cwd.
    cmd = [
        python_executable,
        "-c",
        (
            "import runpy, sys;\n"
            "sys.path.insert(0, '.');\n"
            "from tools.winnforum.exec_unittest import main as _main;\n"
            "raise SystemExit(_main([sys.argv[1]]))\n"
        ),
        json.dumps(payload),
    ]
    # tools package must be importable: add repo root to PYTHONPATH.
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = (
        str(repo_root)
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    with install_sas_cfg(workdir, sas_cfg):
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    combined = (proc.stdout or "") + ("\n" if proc.stderr else "") + (proc.stderr or "")
    log_path.write_text(combined, encoding="utf-8")
    return proc.returncode, combined


def _write_blocked(
    *,
    artifacts: RunArtifacts,
    cfg: RunnerConfig,
    uut_commit: str | None,
    targets: Sequence[UnittestTarget],
    notes: list[str],
    status: str,
    exit_code: int,
) -> int:
    write_environment(
        artifacts.environment_path,
        cfg=cfg,
        harness_root=None,
        uut_commit=uut_commit,
        harness_commit=None,
        targets=targets,
        extra={"status": status, "notes": notes},
    )
    write_summary(
        artifacts.summary_path,
        artifacts=artifacts,
        results={
            "tests_run": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "raw_ok": False,
        },
        notes=notes,
    )
    artifacts.results_path.write_text(
        json.dumps({"status": status, "targets": [t.label() for t in targets], "notes": notes}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    artifacts.harness_log_path.write_text(f"blocked: {status}\n", encoding="utf-8")
    artifacts.uut_log_path.write_text("", encoding="utf-8")
    write_junit_xml(
        parse_unittest_output(""),
        artifacts.junit_path,
        suite_name=f"winnforum-{status}",
    )
    return exit_code


def run(cfg: RunnerConfig) -> int:
    notes: list[str] = []
    log_lines: list[str] = []
    artifacts = prepare_artifact_dir(cfg.artifacts_root)
    uut_commit = _git_rev(cfg.repo_root)
    targets = resolve_unittest_targets(cfg.families, cfg.cases)
    if cfg.docker_harness and not cfg.dry_run:
        from tools.winnforum.discover_tests import reexpand_targets_for_docker

        targets = reexpand_targets_for_docker(
            targets,
            image=cfg.docker_image,
            repo_root=cfg.repo_root,
            harness_pki_dir=cfg.certs_dir,
        )

    params = default_sas_cfg_params(
        host=cfg.host,
        rsa_port=cfg.rsa_port,
        ecc_port=cfg.ecc_port,
        cbsd_sas_version=cfg.cbsd_sas_version,
        sas_sas_version=cfg.sas_sas_version,
        admin_id=cfg.admin_id,
        maximum_batch_size=cfg.maximum_batch_size,
    )
    write_sas_cfg(artifacts.sas_cfg_path, params)

    harness_root: Path | None = None
    harness_commit: str | None = None
    uut_proc: subprocess.Popen[str] | None = None
    uut_log_fh: TextIO | None = None

    try:
        if cfg.dry_run:
            notes.append("dry-run: UUT/harness execution skipped; artifacts prepared only")
            write_environment(
                artifacts.environment_path,
                cfg=cfg,
                harness_root=None,
                uut_commit=uut_commit,
                harness_commit=None,
                targets=targets,
                extra={"status": "dry_run"},
            )
            empty = parse_unittest_output("")
            artifacts.results_path.write_text(
                json.dumps(
                    {
                        **empty.to_dict(),
                        "status": "dry_run",
                        "targets": [t.label() for t in targets],
                        "notes": notes,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            write_junit_xml(empty, artifacts.junit_path, suite_name="winnforum-dry-run")
            artifacts.uut_log_path.write_text("", encoding="utf-8")
            artifacts.harness_log_path.write_text(
                "dry-run: harness not executed\n", encoding="utf-8"
            )
            write_summary(
                artifacts.summary_path,
                artifacts=artifacts,
                results={"tests_run": 0, "passed": 0, "failed": 0, "error": 0, "raw_ok": None},
                notes=notes,
            )
            return 0

        client_cert, client_key, ca_certs, cert_notes = resolve_cert_paths(cfg)
        if client_cert is None or client_key is None or ca_certs is None:
            return _write_blocked(
                artifacts=artifacts,
                cfg=cfg,
                uut_commit=uut_commit,
                targets=targets,
                notes=notes + cert_notes,
                status="blocked_missing_certs",
                exit_code=2,
            )
        missing_files = [
            f"{label}={path}"
            for label, path in (
                ("client_cert", client_cert),
                ("client_key", client_key),
                ("ca_certs", ca_certs),
            )
            if not path.is_file()
        ]
        if missing_files:
            return _write_blocked(
                artifacts=artifacts,
                cfg=cfg,
                uut_commit=uut_commit,
                targets=targets,
                notes=notes + [f"missing file {m}" for m in missing_files],
                status="blocked_missing_certs",
                exit_code=2,
            )

        if cfg.docker_harness:
            from tools.winnforum.preflight import resolve_harness_pki_dir

            clean = cfg.repo_root / ".cache" / "winnforum-harness-src"
            pki = resolve_harness_pki_dir(cfg.repo_root, explicit=cfg.certs_dir)
            if pki is not None:
                cfg.certs_dir = pki
                notes.append(f"docker harness: UUT PKI from {pki}")
            harness_root = clean if clean.is_dir() else None
            harness_commit = os.environ.get(
                "HARNESS_COMMIT", "928c3150adf7b31e53a96b695bf1fbdd3284ecb2"
            )
            workdir = clean / "src" / "harness" if harness_root else cfg.repo_root
            notes.append(f"docker harness image {cfg.docker_image}")
        else:
            harness_root = ensure_harness_checkout(cfg, log_lines)
            harness_commit = _git_rev(harness_root)
            workdir = harness_workdir(harness_root)

        if cfg.start_uut:
            uut_env = apply_certification_fss_db_sync_auth(
                {
                    **os.environ,
                    "SAS_EXECUTION_MODE": os.environ.get(
                        "SAS_EXECUTION_MODE", "certification"
                    ),
                    "CERTS_DIR": str(cfg.certs_dir or (cfg.repo_root / "certs")),
                    # Cat A outdoor HAAT (REG.7): USGS NED 1″ GridFloat tiles.
                    "SAS_TERRAIN_DIR": os.environ.get(
                        "SAS_TERRAIN_DIR",
                        str(cfg.repo_root / "data" / "geo" / "ned"),
                    ),
                    # PAL licenseAreaIdentifier → county GeoJSON (PCR/WDB PPA).
                    "SAS_COUNTY_DIR": os.environ.get(
                        "SAS_COUNTY_DIR",
                        str(cfg.repo_root / "data" / "geo" / "county"),
                    ),
                    # NLCD land-cover for PPA RF region type (reference_models).
                    "SAS_NLCD_DIR": os.environ.get(
                        "SAS_NLCD_DIR",
                        str(cfg.repo_root / "data" / "geo" / "nlcd"),
                    ),
                }
            )
            # Prefer campaign harness pin for reference_models (PPA RF / ITM).
            if not str(uut_env.get("SAS_HARNESS_DIR") or "").strip():
                cache_harness = (
                    cfg.repo_root / ".cache" / "winnforum-harness-src" / "src" / "harness"
                )
                if cache_harness.is_dir():
                    uut_env["SAS_HARNESS_DIR"] = str(cache_harness)
            notes.append("certification FSS DB sync Basic Auth credentials configured")
            county_dir = Path(uut_env["SAS_COUNTY_DIR"])
            if county_dir.is_dir() and any(county_dir.glob("*.json")):
                notes.append(f"UUT county GeoJSON dir={county_dir}")
            else:
                notes.append(
                    "UUT county GeoJSON missing; run "
                    "tools/winnforum/sync_uut_county_geometries.sh for PCR/WDB PPA"
                )
            # Authorize the harness Admin client fingerprint without hardcoding
            # basenames — derived from the resolved --client-cert / env path.
            if client_cert is not None and client_cert.is_file():
                try:
                    from services.certificate_policy import fingerprint_from_cert_pem

                    uut_env["SAS_ADMIN_CERT_SHA1"] = fingerprint_from_cert_pem(
                        client_cert
                    )
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"admin fingerprint not set: {exc}")
            # UUT must use the sas-core interpreter; harness may use another
            # (e.g. harness .venv with pyOpenSSL).
            uut_python = sys.executable
            uut_log_fh = open(artifacts.uut_log_path, "w", encoding="utf-8")
            uut_proc = subprocess.Popen(
                [uut_python, "main.py"],
                cwd=str(cfg.repo_root),
                stdout=uut_log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                env=uut_env,
            )
            notes.append(f"started UUT pid={uut_proc.pid} python={uut_python}")
        else:
            artifacts.uut_log_path.write_text(
                "UUT not started by runner (--start-uut not set)\n", encoding="utf-8"
            )
            notes.append("UUT assumed already running")

        if not cfg.skip_healthcheck:
            health = wait_for_mtls_admin(
                base_url=f"https://{cfg.host}:{cfg.rsa_port}",
                ca_certs=ca_certs,
                client_cert=client_cert,
                client_key=client_key,
                timeout_seconds=cfg.health_timeout_seconds,
            )
            notes.append(f"healthcheck: ok={health.ok} detail={health.detail}")
            if not health.ok:
                write_environment(
                    artifacts.environment_path,
                    cfg=cfg,
                    harness_root=harness_root,
                    uut_commit=uut_commit,
                    harness_commit=harness_commit,
                    targets=targets,
                    extra={"status": "healthcheck_failed", "notes": notes + log_lines},
                )
                write_summary(
                    artifacts.summary_path,
                    artifacts=artifacts,
                    results={
                        "tests_run": 0,
                        "passed": 0,
                        "failed": 0,
                        "error": 0,
                        "raw_ok": False,
                    },
                    notes=notes + log_lines,
                )
                artifacts.results_path.write_text(
                    json.dumps(
                        {
                            "status": "healthcheck_failed",
                            "targets": [t.label() for t in targets],
                            "notes": notes + log_lines,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                artifacts.harness_log_path.write_text(
                    "harness not started: healthcheck failed\n", encoding="utf-8"
                )
                write_junit_xml(
                    parse_unittest_output(""),
                    artifacts.junit_path,
                    suite_name="winnforum-healthcheck-failed",
                )
                return 3

        rc, output = run_harness_unittest(
            workdir=workdir,
            sas_cfg=artifacts.sas_cfg_path,
            targets=targets,
            python_executable=cfg.python_executable,
            log_path=artifacts.harness_log_path,
            repo_root=cfg.repo_root,
            docker_harness=cfg.docker_harness,
            docker_image=cfg.docker_image,
            artifacts_dir=artifacts.directory,
            harness_pki_dir=cfg.certs_dir,
        )
        parsed = parse_unittest_output(output)
        from tools.winnforum.failure_dump import write_failure_dumps

        write_failure_dumps(
            artifacts.directory,
            parsed,
            harness_log_text=output,
        )
        results = {
            **parsed.to_dict(),
            "status": "completed",
            "unittest_exit_code": rc,
            "targets": [t.label() for t in targets],
            "notes": notes + log_lines,
        }
        artifacts.results_path.write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )
        write_junit_xml(parsed, artifacts.junit_path, suite_name="winnforum-harness")
        write_environment(
            artifacts.environment_path,
            cfg=cfg,
            harness_root=harness_root,
            uut_commit=uut_commit,
            harness_commit=harness_commit,
            targets=targets,
            extra={"status": "completed", "unittest_exit_code": rc},
        )
        write_summary(
            artifacts.summary_path,
            artifacts=artifacts,
            results=results,
            notes=notes + log_lines,
        )
        return 0 if rc == 0 and parsed.raw_ok else 1
    finally:
        if uut_proc is not None:
            uut_proc.terminate()
            try:
                uut_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                uut_proc.kill()
                uut_proc.wait(timeout=5)
            notes.append("UUT process terminated")
        if uut_log_fh is not None:
            uut_log_fh.close()
