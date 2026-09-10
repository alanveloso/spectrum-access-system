"""Typed preflight for official WInnForum harness execution (P2-HARNESS)."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class PreflightCode(str, Enum):
    OK = "OK"
    MISSING_CHECKOUT = "MISSING_CHECKOUT"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    MISSING_CERTIFICATE = "MISSING_CERTIFICATE"
    INVALID_CERTIFICATE = "INVALID_CERTIFICATE"
    TLS_FAILURE = "TLS_FAILURE"
    UUT_UNREACHABLE = "UUT_UNREACHABLE"
    ADMIN_API_FAILURE = "ADMIN_API_FAILURE"
    HARNESS_CONFIGURATION_FAILURE = "HARNESS_CONFIGURATION_FAILURE"


@dataclass
class CheckResult:
    name: str
    code: PreflightCode
    ok: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    checks: list[CheckResult]
    harness_root: str | None
    harness_commit: str | None
    python_version: str
    suggested_env: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def ok_for_reg(self) -> bool:
        """REG-only readiness: ignore optional GDAL dependency."""
        return all(
            c.ok for c in self.checks if c.name != "dependencies_gdal"
        )

    def blocking_codes(self) -> list[str]:
        return sorted({c.code.value for c in self.checks if not c.ok})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ok_for_reg": self.ok_for_reg,
            "blocking_codes": self.blocking_codes(),
            "harness_root": self.harness_root,
            "harness_commit": self.harness_commit,
            "python_version": self.python_version,
            "suggested_env": self.suggested_env,
            "checks": [asdict(c) for c in self.checks],
        }


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def harness_certs_dir(harness_root: Path) -> Path:
    """Official layout: ``src/harness/certs`` (not testdata/certs)."""
    candidate = harness_root / "src" / "harness" / "certs"
    if candidate.is_dir():
        return candidate
    legacy = harness_root / "src" / "harness" / "testcases" / "testdata" / "certs"
    if legacy.is_dir():
        return legacy
    return candidate


def resolve_harness_pki_dir(
    repo_root: Path, *, explicit: Path | None = None
) -> Path | None:
    """Locate official harness PKI on the host (not shipped in git archive)."""
    if explicit is not None:
        explicit = explicit.expanduser().resolve()
        if (explicit / "ca.cert").is_file() and (explicit / "admin.cert").is_file():
            return explicit
    for candidate in (
        repo_root / ".cache" / "winnforum-harness-pki",
        repo_root / ".cache" / "winnforum-harness-src" / "src" / "harness" / "certs",
        repo_root.parent / "Spectrum-Access-System" / "src" / "harness" / "certs",
    ):
        if (candidate / "ca.cert").is_file() and (candidate / "admin.cert").is_file():
            return candidate.resolve()
    return None


def discover_certificate_roles(certs: Path) -> dict[str, str]:
    """Map role → relative filename when the file exists (no content)."""
    roles = {
        "ca_bundle": "ca.cert",
        "admin_client_cert": "admin.cert",
        "admin_client_key": "admin.key",
        "uut_server_rsa_cert": "server.cert",
        "uut_server_rsa_key": "server.key",
        "uut_server_ecc_cert": "server-ecc.cert",
        "uut_server_ecc_key": "server-ecc.key",
        "cbsd_device_a_cert": "device_a.cert",
        "cbsd_device_a_key": "device_a.key",
        "domain_proxy_cert": "domain_proxy.cert",
        "domain_proxy_key": "domain_proxy.key",
        "root_ca": "root_ca.cert",
        "sas_ca": "sas_ca.cert",
        "cbsd_ca": "cbsd_ca.cert",
        "proxy_ca": "proxy_ca.cert",
        "root_ecc_ca": "root-ecc_ca.cert",
        "sas_ecc_ca": "sas-ecc_ca.cert",
    }
    return {role: name for role, name in roles.items() if (certs / name).is_file()}


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _key_matches_cert(cert_path: Path, key_path: Path) -> tuple[bool, str]:
    try:
        cert = _load_cert(cert_path)
        key = load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001 — preflight detail
        return False, f"{type(exc).__name__}: {exc}"
    pub = cert.public_key()
    if isinstance(key, rsa.RSAPrivateKey) and isinstance(pub, rsa.RSAPublicKey):
        return key.public_key().public_numbers() == pub.public_numbers(), "rsa match check"
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(
        pub, ec.EllipticCurvePublicKey
    ):
        return (
            key.public_key().public_numbers() == pub.public_numbers(),
            "ec match check",
        )
    return False, "key type does not match certificate public key"


def _verify_chain(leaf: Path, ca_bundle: Path) -> tuple[bool, str]:
    try:
        _ = _load_cert(leaf)
        proc = subprocess.run(
            ["openssl", "verify", "-CAfile", str(ca_bundle), str(leaf)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, (proc.stdout or "OK").strip()
        return False, (proc.stderr or proc.stdout or "openssl verify failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _probe_https(
    *,
    url: str,
    ca_certs: Path | None,
    client_cert: Path | None,
    client_key: Path | None,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[bool, str, int | None]:
    try:
        if ca_certs is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context(cafile=str(ca_certs))
            if client_cert and client_key:
                ctx.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
        req = Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urlopen(req, context=ctx, timeout=5) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return True, f"HTTP {code}", int(code)
    except HTTPError as exc:
        return True, f"HTTP {exc.code}", int(exc.code)
    except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}", None


def run_preflight(
    *,
    harness_dir: Path,
    expected_commit: str | None = None,
    python_executable: str | None = None,
    client_cert: Path | None = None,
    client_key: Path | None = None,
    ca_certs: Path | None = None,
    certs_dir: Path | None = None,
    host: str = "localhost",
    rsa_port: int = 9000,
    skip_uut: bool = False,
) -> PreflightReport:
    checks: list[CheckResult] = []
    py = python_executable or sys.executable
    py_ver = subprocess.run(
        [py, "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True,
        text=True,
        check=False,
    )
    python_version = (py_ver.stdout or "").strip() or sys.version.split()[0]

    root = harness_dir.expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        checks.append(
            CheckResult(
                "checkout",
                PreflightCode.MISSING_CHECKOUT,
                False,
                f"harness checkout missing or not a git repo: {root}",
            )
        )
        return PreflightReport(
            checks=checks,
            harness_root=str(root),
            harness_commit=None,
            python_version=python_version,
        )

    remote = _git(root, "remote", "get-url", "origin")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "-sb")
    checks.append(
        CheckResult(
            "checkout",
            PreflightCode.OK,
            True,
            "checkout present",
            {
                "remote": remote,
                "branch": branch,
                "commit": commit,
                "status": status,
            },
        )
    )
    if expected_commit and commit and not commit.startswith(expected_commit):
        checks.append(
            CheckResult(
                "commit_pin",
                PreflightCode.HARNESS_CONFIGURATION_FAILURE,
                False,
                f"commit {commit} does not match expected {expected_commit}",
            )
        )
    else:
        checks.append(
            CheckResult(
                "commit_pin",
                PreflightCode.OK,
                True,
                f"commit recorded: {commit}",
                {"commit": commit},
            )
        )

 # Python compatibility (official README: 3.11; 3.12+ untested).
    major_minor = ".".join(python_version.split(".")[:2])
    if major_minor != "3.11":
        checks.append(
            CheckResult(
                "python",
                PreflightCode.MISSING_DEPENDENCY,
                False,
                f"Python {python_version} is not 3.11 (official harness requirement)",
            )
        )
    else:
        checks.append(
            CheckResult(
                "python",
                PreflightCode.OK,
                True,
                f"Python {python_version}",
            )
        )

    required_mods = [
        "jsonschema",
        "OpenSSL",
        "jwt",
        "cryptography",
        "numpy",
        "lxml",
        "psutil",
        "portpicker",
    ]
    missing_mods: list[str] = []
    for mod in required_mods:
        name = "OpenSSL" if mod == "OpenSSL" else mod
        probe = subprocess.run(
            [py, "-c", f"import {name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            missing_mods.append(mod)
 # GDAL is required by full requirements but often optional for REG-only.
    gdal_probe = subprocess.run(
        [py, "-c", "from osgeo import gdal"],
        capture_output=True,
        text=True,
        check=False,
    )
    if missing_mods:
        checks.append(
            CheckResult(
                "dependencies_core",
                PreflightCode.MISSING_DEPENDENCY,
                False,
                f"missing imports: {missing_mods}",
            )
        )
    else:
        checks.append(
            CheckResult(
                "dependencies_core",
                PreflightCode.OK,
                True,
                "core harness imports OK",
            )
        )
    checks.append(
        CheckResult(
            "dependencies_gdal",
            PreflightCode.OK
            if gdal_probe.returncode == 0
            else PreflightCode.MISSING_DEPENDENCY,
            gdal_probe.returncode == 0,
            "GDAL available"
            if gdal_probe.returncode == 0
            else "GDAL/osgeo missing (geo/RF families may fail; REG may still run)",
        )
    )

    certs = (certs_dir or harness_certs_dir(root)).resolve()
    roles = discover_certificate_roles(certs)
    required = [
        "ca_bundle",
        "admin_client_cert",
        "admin_client_key",
        "uut_server_rsa_cert",
        "uut_server_rsa_key",
    ]
    missing_roles = [r for r in required if r not in roles]
    if missing_roles:
        checks.append(
            CheckResult(
                "certificates_present",
                PreflightCode.MISSING_CERTIFICATE,
                False,
                f"missing roles in {certs}: {missing_roles}; "
                "generate via src/harness/certs/generate_fake_certs.sh",
                {"found": roles, "certs_dir": str(certs)},
            )
        )
    else:
        checks.append(
            CheckResult(
                "certificates_present",
                PreflightCode.OK,
                True,
                f"required certificate files present under {certs}",
                {"found": roles, "certs_dir": str(certs)},
            )
        )

 # Resolve client/CA paths (CLI/env override discovery).
    env_client = Path(os.environ["WINNFORUM_CLIENT_CERT"]).expanduser() if os.environ.get("WINNFORUM_CLIENT_CERT") else None
    env_key = Path(os.environ["WINNFORUM_CLIENT_KEY"]).expanduser() if os.environ.get("WINNFORUM_CLIENT_KEY") else None
    env_ca = Path(os.environ["WINNFORUM_CA_CERTS"]).expanduser() if os.environ.get("WINNFORUM_CA_CERTS") else None
    resolved_client = client_cert or env_client or (certs / "admin.cert")
    resolved_key = client_key or env_key or (certs / "admin.key")
    resolved_ca = ca_certs or env_ca or (certs / "ca.cert")

    suggested = {
        "WINNFORUM_CLIENT_CERT": str(resolved_client),
        "WINNFORUM_CLIENT_KEY": str(resolved_key),
        "WINNFORUM_CA_CERTS": str(resolved_ca),
        "CERTS_DIR": str(certs),
    }

    if all(p.is_file() for p in (resolved_client, resolved_key, resolved_ca)):
        ok_match, match_detail = _key_matches_cert(resolved_client, resolved_key)
        checks.append(
            CheckResult(
                "certificate_key_match",
                PreflightCode.OK if ok_match else PreflightCode.INVALID_CERTIFICATE,
                ok_match,
                match_detail,
            )
        )
        ok_chain, chain_detail = _verify_chain(resolved_client, resolved_ca)
        checks.append(
            CheckResult(
                "certificate_chain",
                PreflightCode.OK if ok_chain else PreflightCode.INVALID_CERTIFICATE,
                ok_chain,
                chain_detail,
            )
        )
        server = certs / "server.cert"
        if server.is_file():
            ok_s, s_detail = _verify_chain(server, resolved_ca)
            checks.append(
                CheckResult(
                    "server_certificate_chain",
                    PreflightCode.OK if ok_s else PreflightCode.INVALID_CERTIFICATE,
                    ok_s,
                    s_detail,
                )
            )
    else:
        checks.append(
            CheckResult(
                "certificate_key_match",
                PreflightCode.MISSING_CERTIFICATE,
                False,
                "cannot validate key/chain; required PEM files missing",
            )
        )

    if skip_uut:
        checks.append(
            CheckResult(
                "uut",
                PreflightCode.OK,
                True,
                "UUT probes skipped (--skip-uut)",
            )
        )
    else:
        base = f"https://{host}:{rsa_port}"
 # No client cert should fail TLS (UUT requires mTLS).
        ok_anon, anon_detail, _ = _probe_https(
            url=f"{base}/",
            ca_certs=resolved_ca if resolved_ca.is_file() else None,
            client_cert=None,
            client_key=None,
        )
        if ok_anon:
            checks.append(
                CheckResult(
                    "tls_reject_anonymous",
                    PreflightCode.TLS_FAILURE,
                    False,
                    f"anonymous TLS unexpectedly succeeded: {anon_detail}",
                )
            )
        else:
            checks.append(
                CheckResult(
                    "tls_reject_anonymous",
                    PreflightCode.OK,
                    True,
                    f"anonymous connection rejected as expected: {anon_detail}",
                )
            )

        if all(p.is_file() for p in (resolved_client, resolved_key, resolved_ca)):
            ok_admin, admin_detail, admin_code = _probe_https(
                url=f"{base}/admin/get_daily_activities_status",
                ca_certs=resolved_ca,
                client_cert=resolved_client,
                client_key=resolved_key,
                method="POST",
                body=b"{}",
            )
            if not ok_admin:
 # Distinguish unreachable vs TLS vs admin
                code = (
                    PreflightCode.TLS_FAILURE
                    if "SSL" in admin_detail
                    else PreflightCode.UUT_UNREACHABLE
                )
                checks.append(
                    CheckResult("admin_api", code, False, admin_detail)
                )
            else:
                checks.append(
                    CheckResult(
                        "admin_api",
                        PreflightCode.OK,
                        True,
                        admin_detail,
                        {"http_code": admin_code},
                    )
                )

            ok_cbsd, cbsd_detail, cbsd_code = _probe_https(
                url=f"{base}/v1.2/registration",
                ca_certs=resolved_ca,
                client_cert=resolved_client,
                client_key=resolved_key,
                method="POST",
                body=b"{}",
            )
            if not ok_cbsd:
                code = (
                    PreflightCode.TLS_FAILURE
                    if "SSL" in cbsd_detail
                    else PreflightCode.UUT_UNREACHABLE
                )
                checks.append(CheckResult("cbsd_endpoint", code, False, cbsd_detail))
            else:
                checks.append(
                    CheckResult(
                        "cbsd_endpoint",
                        PreflightCode.OK,
                        True,
                        cbsd_detail,
                        {"http_code": cbsd_code},
                    )
                )
        else:
            checks.append(
                CheckResult(
                    "admin_api",
                    PreflightCode.MISSING_CERTIFICATE,
                    False,
                    "skipped admin probe; certificates incomplete",
                )
            )

 # sas.cfg presence in harness workdir (template)
    sas_cfg = root / "src" / "harness" / "sas.cfg"
    if not sas_cfg.is_file():
        checks.append(
            CheckResult(
                "sas_cfg_template",
                PreflightCode.HARNESS_CONFIGURATION_FAILURE,
                False,
                f"missing template {sas_cfg}",
            )
        )
    else:
        checks.append(
            CheckResult(
                "sas_cfg_template",
                PreflightCode.OK,
                True,
                f"found {sas_cfg}",
            )
        )

    return PreflightReport(
        checks=checks,
        harness_root=str(root),
        harness_commit=commit,
        python_version=python_version,
        suggested_env=suggested,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="WInnForum harness preflight (typed blockers)")
    p.add_argument("--harness-dir", type=Path, required=True)
    p.add_argument("--expected-commit", default=None)
    p.add_argument("--python", dest="python_executable", default=None)
    p.add_argument("--client-cert", type=Path, default=None)
    p.add_argument("--client-key", type=Path, default=None)
    p.add_argument("--ca-certs", type=Path, default=None)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--host", default="localhost")
    p.add_argument("--rsa-port", type=int, default=9000)
    p.add_argument("--skip-uut", action="store_true")
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args(argv)

    report = run_preflight(
        harness_dir=args.harness_dir,
        expected_commit=args.expected_commit,
        python_executable=args.python_executable,
        client_cert=args.client_cert,
        client_key=args.client_key,
        ca_certs=args.ca_certs,
        certs_dir=args.certs_dir,
        host=args.host,
        rsa_port=args.rsa_port,
        skip_uut=args.skip_uut,
    )
    text = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.ok_for_reg else 2


if __name__ == "__main__":
    raise SystemExit(main())
