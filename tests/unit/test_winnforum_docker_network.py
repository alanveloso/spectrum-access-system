"""Harness Docker network topology: host mode keeps injected localhost reachable.

Official cases inject ``https://localhost:<port>/…`` for CPI/PAL databases and
for ``SasTestHarnessServer`` peer FAD (GRA.5 / GRA.6 / FAD.2). Bridge mode
breaks UUT→peer pull; host mode matches the official inject hostname.
"""

from __future__ import annotations

from pathlib import Path

from tools.winnforum.docker_runner import (
    DockerHarnessConfig,
    _docker_run_base,
    resolve_harness_network_mode,
)


def test_default_network_mode_is_host(monkeypatch):
    monkeypatch.delenv("WINNFORUM_HARNESS_NETWORK_MODE", raising=False)
    assert resolve_harness_network_mode(None) == "host"


def test_env_overrides_default_network_mode(monkeypatch):
    monkeypatch.setenv("WINNFORUM_HARNESS_NETWORK_MODE", "bridge")
    assert resolve_harness_network_mode(None) == "bridge"


def test_host_mode_omits_extra_hosts(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("WINNFORUM_HARNESS_NETWORK_MODE", raising=False)
    # Minimal mounts so _docker_run_base does not require real Common-Data.
    ned = tmp_path / "ned"
    ned.mkdir()
    (ned / "dummy.flt").write_bytes(b"x")
    common = tmp_path / "data"
    common.mkdir()
    (common / "ned").mkdir()
    (common / "ned" / "dummy.flt").write_bytes(b"x")
    cfg = DockerHarnessConfig(
        sas_repo_root=tmp_path,
        common_data_dir=common,
        harness_pki_dir=None,
        network_mode="host",
        county_dir=None,
    )
    cmd = _docker_run_base(cfg)
    assert "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == "host"
    assert "--add-host" not in cmd


def test_bridge_mode_adds_localhost_host_gateway(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("WINNFORUM_HARNESS_NETWORK_MODE", raising=False)
    common = tmp_path / "data"
    (common / "ned").mkdir(parents=True)
    (common / "ned" / "dummy.flt").write_bytes(b"x")
    cfg = DockerHarnessConfig(
        sas_repo_root=tmp_path,
        common_data_dir=common,
        harness_pki_dir=None,
        network_mode="bridge",
        county_dir=None,
    )
    cmd = _docker_run_base(cfg)
    assert cmd[cmd.index("--network") + 1] == "bridge"
    assert "--add-host" in cmd
    assert "localhost:host-gateway" in cmd


def test_pki_mount_is_writable_staged_copy(monkeypatch, tmp_path: Path):
    """SCS/SDS short-lived certs require a writable certs tree (not :ro)."""
    monkeypatch.delenv("WINNFORUM_HARNESS_NETWORK_MODE", raising=False)
    common = tmp_path / "data"
    (common / "ned").mkdir(parents=True)
    (common / "ned" / "dummy.flt").write_bytes(b"x")
    pki = tmp_path / "pki-src"
    pki.mkdir()
    (pki / "ca.cert").write_text("dummy\n", encoding="utf-8")
    arts = tmp_path / "arts"
    cfg = DockerHarnessConfig(
        sas_repo_root=tmp_path,
        common_data_dir=common,
        harness_pki_dir=pki,
        artifacts_dir=arts,
        network_mode="host",
        county_dir=None,
    )
    cmd = _docker_run_base(cfg)
    mounts = [cmd[i + 1] for i, t in enumerate(cmd) if t == "-v"]
    cert_mount = next(m for m in mounts if "/opt/winnforum-harness/src/harness/certs" in m)
    assert "harness-certs-rw" in cert_mount
    assert not cert_mount.endswith(":ro")
    staged = Path(cert_mount.rsplit(":", 1)[0])
    assert staged.is_dir()
    assert (staged / "ca.cert").is_file()
    assert staged.resolve() != pki.resolve()
