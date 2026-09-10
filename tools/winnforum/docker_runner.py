"""Run official WInnForum harness inside the reproducible Docker image."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_IMAGE = "winnforum-sas-harness:928c3150"
DEFAULT_HARNESS_COMMIT = "928c3150adf7b31e53a96b695bf1fbdd3284ecb2"


def default_common_data_dir(repo_root: Path) -> Path:
    """Prefer official Common-Data checkout; fall back to SAS-local geo stubs."""
    repo_root = repo_root.resolve()
    sibling = repo_root.parent / "Common-Data" / "data"
    if sibling.is_dir() and (sibling / "ned").is_dir() and any((sibling / "ned").glob("*.flt")):
        return sibling.resolve()
    env = os.environ.get("WINNFORUM_COMMON_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return (repo_root / "data" / "geo").resolve()


def default_county_dir(repo_root: Path) -> Path | None:
    cached = repo_root / ".cache" / "winnforum-harness-county"
    if cached.is_dir() and any(cached.glob("*.json")):
        return cached.resolve()
    return None


@dataclass(frozen=True)
class DockerHarnessConfig:
    image: str = DEFAULT_IMAGE
    sas_repo_root: Path | None = None
    artifacts_dir: Path | None = None
    common_data_dir: Path | None = None
    county_dir: Path | None = None
    harness_pki_dir: Path | None = None
    sas_host: str = "localhost"
    sas_rsa_port: int = 9000
    sas_ecc_port: int = 9001
    # Host networking preserves official test.cfg hostname=localhost for both
    # UUT↔harness and harness DatabaseServer (CPI/PAL/…) inject URLs. Bridge
    # mode + localhost:host-gateway only makes the UUT reachable FROM the
    # harness; injected https://localhost:<port> then fails from the host UUT.
    # None → WINNFORUM_HARNESS_NETWORK_MODE → host.
    network_mode: str | None = None
    extra_hosts: tuple[str, ...] = ()


def resolve_harness_network_mode(explicit: str | None = None) -> str:
    """Resolve harness Docker network mode (default ``host`` on Linux tooling)."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env = os.environ.get("WINNFORUM_HARNESS_NETWORK_MODE", "").strip()
    if env:
        return env
    return "host"


def _writable_harness_pki(pki: Path, artifacts: Path) -> Path:
    """Stage a writable PKI tree for short-lived cert generation (SCS/SDS 17–19).

    Official harness ``createShortLivedCertificate`` writes under ``certs/``.
    Mounting the host PKI read-only breaks those cases with ``Read-only file system``.
    Staging a per-run copy keeps the source PKI immutable.
    """
    import shutil

    dest = (artifacts / "harness-certs-rw").resolve()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(pki, dest, symlinks=True)
    return dest


def _docker_run_base(cfg: DockerHarnessConfig) -> list[str]:
    repo = (cfg.sas_repo_root or Path(__file__).resolve().parents[2]).resolve()
    common = (cfg.common_data_dir or default_common_data_dir(repo)).resolve()
    county = cfg.county_dir or default_county_dir(repo)
    from tools.winnforum.preflight import resolve_harness_pki_dir

    pki = cfg.harness_pki_dir or resolve_harness_pki_dir(repo)
    artifacts = (cfg.artifacts_dir or (repo / "artifacts" / "winnforum" / "docker-harness")).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    common_commit = ""
    common_git = common.parent / ".git" if common.name == "data" else None
    if common_git and common_git.exists():
        proc = subprocess.run(
            ["git", "-C", str(common.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            common_commit = (proc.stdout or "").strip()

    network_mode = resolve_harness_network_mode(cfg.network_mode)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        network_mode,
        "-e",
        f"HARNESS_COMMIT={DEFAULT_HARNESS_COMMIT}",
        "-e",
        "SAS_REPO_MOUNT=/opt/sas",
        "-e",
        "ARTIFACTS_DIR=/artifacts",
        "-e",
        "COMMON_DATA_DIR=/common-data",
        "-e",
        "COMMON_DATA_COUNTY_DIR=/common-data-county",
        "-v",
        f"{repo}:/opt/sas:ro",
        "-v",
        f"{common}:/common-data:ro",
    ]
    if common_commit:
        cmd.extend(["-e", f"COMMON_DATA_COMMIT={common_commit}"])
    if county is not None:
        cmd.extend(["-v", f"{county}:/common-data-county:ro"])
    if pki is not None:
        writable = _writable_harness_pki(Path(pki), artifacts)
        cmd.extend(
            [
                "-v",
                f"{writable}:/opt/winnforum-harness/src/harness/certs",
            ]
        )
    # Bridge-only: map localhost → host gateway so harness can reach a host UUT.
    # Host networking shares the UUT namespace; extra_hosts would be wrong there.
    extra_hosts = cfg.extra_hosts
    if network_mode == "bridge" and not extra_hosts:
        extra_hosts = ("localhost:host-gateway",)
    if network_mode != "host":
        for host_map in extra_hosts:
            cmd.extend(["--add-host", host_map])
    return cmd


def docker_harness_run_exec(
    *,
    targets_payload: list[dict[str, str | None]],
    cfg: DockerHarnessConfig,
    log_path: Path | None = None,
) -> tuple[int, str]:
    """Execute harness unittest targets inside the official harness container."""
    repo = (cfg.sas_repo_root or Path(__file__).resolve().parents[2]).resolve()
    artifacts = (cfg.artifacts_dir or (repo / "artifacts" / "winnforum" / "docker-harness")).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    cmd = _docker_run_base(cfg)
    cmd.extend(
        [
            "-v",
            f"{artifacts}:/artifacts",
            "-w",
            "/opt/winnforum-harness/src/harness",
            cfg.image,
            "exec",
            json.dumps(targets_payload),
        ]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = (proc.stdout or "") + ("\n" if proc.stderr else "") + (proc.stderr or "")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(combined, encoding="utf-8")
    return proc.returncode, combined


def docker_self_check(cfg: DockerHarnessConfig | None = None) -> tuple[int, str]:
    cfg = cfg or DockerHarnessConfig()
    cmd = _docker_run_base(cfg)
    cmd.extend([cfg.image, "self-check"])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = (proc.stdout or "") + ("\n" if proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, combined


def image_identity(image: str = DEFAULT_IMAGE) -> dict[str, str]:
    proc = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"image": image, "error": proc.stderr.strip()}
    data = json.loads(proc.stdout)
    first = data[0] if isinstance(data, list) else data
    return {
        "image": image,
        "id": first.get("Id", ""),
        "digest": first.get("RepoDigests", [""])[0] if first.get("RepoDigests") else "",
    }
