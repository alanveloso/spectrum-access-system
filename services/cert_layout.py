"""Canonical mTLS certificate layout under CERTS_DIR."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import Settings

# Filenames relative to CERTS_DIR. Keep harness-agnostic: no fixture IDs.
REQUIRED_FILES: tuple[str, ...] = (
    "server.cert",
    "server.key",
    "ca.cert",
    "server-ecc.cert",
    "server-ecc.key",
)
REQUIRED_DIRS: tuple[str, ...] = ("crl",)
_PEM_MARKER = "-----BEGIN "


@dataclass(frozen=True)
class CertPaths:
    """Resolved absolute paths for the SAS mTLS material."""

    certs_dir: Path
    server_cert: Path
    server_key: Path
    ca_cert: Path
    ecc_cert: Path
    ecc_key: Path
    crl_dir: Path


@dataclass
class CertCheckResult:
    ok: bool
    certs_dir: Path
    missing_files: list[str] = field(default_factory=list)
    missing_dirs: list[str] = field(default_factory=list)
    invalid_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [*self.missing_files, *[f"{name}/" for name in self.missing_dirs]]


def _looks_like_pem(path: Path) -> bool:
    """Reject empty/garbage files that would only fail later inside OpenSSL."""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    return _PEM_MARKER in head


def resolve_cert_paths(settings: Settings) -> CertPaths:
    certs_dir = Path(settings.certs_dir).expanduser().resolve()
    return CertPaths(
        certs_dir=certs_dir,
        server_cert=settings.resolved_ssl_certfile.expanduser().resolve(),
        server_key=settings.resolved_ssl_keyfile.expanduser().resolve(),
        ca_cert=settings.resolved_ssl_ca_certs.expanduser().resolve(),
        ecc_cert=settings.resolved_ssl_ecc_certfile.expanduser().resolve(),
        ecc_key=settings.resolved_ssl_ecc_keyfile.expanduser().resolve(),
        crl_dir=settings.resolved_ssl_crl_dir.expanduser().resolve(),
    )


def validate_certificate_layout(settings: Settings) -> CertCheckResult:
    """Validate RSA, ECC, CA and CRL material required for dual mTLS listeners."""
    paths = resolve_cert_paths(settings)
    missing_files: list[str] = []
    missing_dirs: list[str] = []
    invalid_files: list[str] = []
    notes: list[str] = []

    required_path_by_name = {
        "server.cert": paths.server_cert,
        "server.key": paths.server_key,
        "ca.cert": paths.ca_cert,
        "server-ecc.cert": paths.ecc_cert,
        "server-ecc.key": paths.ecc_key,
    }
    for name in REQUIRED_FILES:
        path = required_path_by_name[name]
        if not path.is_file():
            missing_files.append(name)
        elif not _looks_like_pem(path):
            invalid_files.append(name)

    if not paths.crl_dir.is_dir():
        missing_dirs.append("crl")
    else:
        crls = sorted(paths.crl_dir.glob("*.crl.pem"))
        if not crls:
            missing_files.append("crl/*.crl.pem")
        else:
            for crl in crls:
                if not _looks_like_pem(crl):
                    invalid_files.append(f"crl/{crl.name}")

    if not paths.certs_dir.is_dir():
        notes.append(f"CERTS_DIR '{paths.certs_dir}' does not exist or is not a directory")

    return CertCheckResult(
        ok=not missing_files and not missing_dirs and not invalid_files,
        certs_dir=paths.certs_dir,
        missing_files=missing_files,
        missing_dirs=missing_dirs,
        invalid_files=invalid_files,
        notes=notes,
    )


def format_certificate_error(result: CertCheckResult) -> str:
    parts: list[str] = []
    if result.missing:
        parts.append(f"missing={', '.join(result.missing)}")
    if result.invalid_files:
        parts.append(f"invalid_pem={', '.join(result.invalid_files)}")
    detail = "; ".join(parts) if parts else "(unknown)"
    return (
        f"Certificados TLS incompletos em CERTS_DIR={result.certs_dir}: {detail}. "
        "Provisionar ./certs (ou definir CERTS_DIR) com server.cert/server.key, "
        "server-ecc.cert/server-ecc.key, ca.cert e crl/*.crl.pem em PEM. "
        "Gerar material de teste com o script oficial do harness WInnForum "
        "(generate_fake_certs.sh) e copiar para CERTS_DIR."
    )
