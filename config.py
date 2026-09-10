"""Centralized application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent
_DEFAULT_CERTS = _ROOT / "certs"


class Settings(BaseSettings):
    """Single source of truth for connections, mTLS paths and runtime knobs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

 # Persistence
    database_url: str = Field(
        default=f"sqlite:///{_ROOT / 'sas_mvp.db'}",
        description="SQLAlchemy database URL (PostgreSQL in production).",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_pool_pre_ping: bool = True
    db_echo: bool = False

 # Message broker / Celery
    rabbitmq_url: str = "amqp://sas:sas@localhost:5672//"
    celery_broker_url: Optional[str] = None
    celery_result_backend: Optional[str] = None
    celery_task_acks_late: bool = True
    celery_worker_prefetch_multiplier: int = 1
    celery_task_default_queue: str = "sas"

 # Spectrum / runtime profile
    sas_profile: str = "cbrs_winnforum"
    sas_deployment_config: Optional[Path] = Field(
        default=None,
        description=(
            "Path to DeploymentConfig YAML (SAS_DEPLOYMENT_CONFIG). "
            "When unset, uses deployments/reference.yaml."
        ),
    )
    sas_admin_id: str = "sas_admin_id"
    fad_public_base: str = "https://localhost:9000"
    sas_sas_version: str = "v1.3"
 # Peer FAD client: TLS hostname check (default on). Set false only for
 # lab peers whose leaf CN/SAN cannot match the injected URL host.
    sas_fad_client_check_hostname: bool = True
    http_timeout_seconds: float = 30.0
    max_batch_size: int = 100

 # operational security
    sas_max_request_body_bytes: int = Field(
        default=16 * 1024 * 1024,
        description="Reject HTTP bodies larger than this (Content-Length). 0 disables.",
    )
    sas_rate_limit_enabled: bool = Field(
        default=False,
        description=(
            "Enable per-client token-bucket rate limiting. Forced off when "
            "SAS_EXECUTION_MODE=certification."
        ),
    )
    sas_rate_limit_per_second: float = Field(
        default=50.0,
        description="Steady-state requests/second per client when rate limiting is on.",
    )
    sas_rate_limit_burst: float = Field(
        default=200.0,
        description="Burst capacity per client when rate limiting is on.",
    )
    sas_ssl_ocsp_mode: Literal["disabled", "soft", "strict"] = Field(
        default="disabled",
        description=(
            "OCSP mode. WInnForum CRL-based target keeps disabled; soft/strict "
            "reserved for deployments that provision OCSP responders."
        ),
    )
    sas_fad_max_file_bytes: int = Field(
        default=8 * 1024 * 1024,
        description="Absolute cap on a single peer FAD activity file body.",
    )
    sas_ssrf_allow_lab_private: bool = Field(
        default=False,
        description=(
            "Allow loopback/RFC1918 egress targets for Admin/WDB pulls. "
            "Forced on when SAS_EXECUTION_MODE=certification; keep false in production."
        ),
    )

 # API listeners
    api_host: str = "0.0.0.0"
    rsa_port: int = 9000
    ecc_port: int = 9001

 # mTLS / certificates — canonical root is <repo>/certs (override with CERTS_DIR).
    certs_dir: Path = Field(
        default=_DEFAULT_CERTS,
        description="Canonical certificate directory (CERTS_DIR).",
    )
    ssl_certfile: Optional[Path] = None
    ssl_keyfile: Optional[Path] = None
    ssl_ecc_certfile: Optional[Path] = None
    ssl_ecc_keyfile: Optional[Path] = None
    ssl_ca_certs: Optional[Path] = None
    ssl_crl_dir: Optional[Path] = None
    client_certfile: Optional[Path] = None
    client_keyfile: Optional[Path] = None

 # Execution mode: production uses Celery; certification runs CPAS inline;
 # test allows in-process TestClient without TLS (explicit only — fail-closed otherwise).
    sas_execution_mode: Literal["production", "certification", "test"] = Field(
        default="production",
        description="SAS_EXECUTION_MODE=production|certification|test",
    )

 # Admin API: extra SHA-1 fingerprints (AA:BB:..., comma-separated) authorized
 # in addition to ROLE_SAS. Used when the harness admin leaf lacks ROLE_SAS.
    sas_admin_cert_sha1: str = Field(
        default="",
        description="SAS_ADMIN_CERT_SHA1 comma-separated admin client fingerprints.",
    )

 # External federal / marketplace DB basic auth.
 # Production: inject via environment / secret mounts — never commit real values.
 # Empty password disables basic-auth until configured (see db_sync_basic_auth).
    db_sync_username: str = ""
    db_sync_password: str = ""

 # USGS NED 1″ GridFloat directory for Cat A outdoor HAAT (SAS_TERRAIN_DIR).
    sas_terrain_dir: Optional[Path] = Field(
        default=None,
        description="Override path to NED GridFloat tiles (SAS_TERRAIN_DIR).",
    )
    sas_county_dir: Optional[Path] = Field(
        default=None,
        description="Override path to county GeoJSON by FIPS (SAS_COUNTY_DIR).",
    )
 # Protection / RF dataset packaging.
    sas_protection_data_bundle: str = Field(
        default="cbrs_winnforum_protection",
        description="Manifest id under protection_data/manifests/.",
    )
    sas_protection_data_root: Optional[Path] = Field(
        default=None,
        description="Override data root (default: <repo>/data). SAS_PROTECTION_DATA_ROOT.",
    )
    sas_protection_data_strict: bool = Field(
        default=False,
        description=(
            "When true, required payload globs (NED .flt, DPA .kml, …) must be present; "
            "doctor/startup fail otherwise. SAS_PROTECTION_DATA_STRICT."
        ),
    )
 # BPR Arrangement R path loss: ITM (default) or explicit free_space lab profile.
 # Free Space is never a silent substitute when ITM/reference_models are missing.
    sas_bpr_path_loss_model: Literal["itm", "free_space"] = Field(
        default="itm",
        description=(
            "OBSOLETE for operational services after RuntimeComposition. "
            "Was SAS_BPR_PATH_LOSS_MODEL=itm|free_space. Prefer DeploymentConfig.rf "
            "and composed RfPort; kept for config migration diagnostics only."
        ),
    )
 # CPAS IAP production wiring.
    sas_iap_enabled: bool = Field(
        default=True,
        description=(
            "Operational toggle for IAP execution. When false, IAP is skipped only "
            "if the selected Spectrum Profile does not require mechanism "
            "aggregate_linear_power; otherwise startup/composition fails closed. "
            "SAS_IAP_ENABLED (default true when unset)."
        ),
    )
    sas_iap_path_loss_model: Literal["itm", "free_space"] = Field(
        default="itm",
        description=(
            "OBSOLETE for operational services after RuntimeComposition. "
            "Was SAS_IAP_PATH_LOSS_MODEL=itm|free_space. Prefer DeploymentConfig.rf "
            "and composed RfPort; kept for config migration diagnostics only."
        ),
    )

    @field_validator("sas_execution_mode", mode="before")
    @classmethod
    def _normalize_execution_mode(cls, value: object) -> object:
        if value is None or value == "":
            return "production"
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("sas_rate_limit_enabled", mode="before")
    @classmethod
    def _coerce_rate_limit_enabled(cls, value: object) -> object:
        if value is None or value == "":
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @field_validator("sas_ssrf_allow_lab_private", mode="before")
    @classmethod
    def _coerce_ssrf_allow_lab_private(cls, value: object) -> object:
        if value is None or value == "":
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @field_validator("sas_ssl_ocsp_mode", mode="before")
    @classmethod
    def _normalize_ocsp_mode(cls, value: object) -> object:
        if value is None or value == "":
            return "disabled"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("soft", "strict", "disabled"):
                return normalized
            return "disabled"
        return value

    @field_validator("sas_fad_client_check_hostname", mode="before")
    @classmethod
    def _coerce_fad_check_hostname(cls, value: object) -> object:
        if value is None or value == "":
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @field_validator("certs_dir", mode="before")
    @classmethod
    def _coerce_certs_dir(cls, value: object) -> object:
        if value is None or value == "":
            return _DEFAULT_CERTS
        return Path(str(value))

    @field_validator("sas_terrain_dir", "sas_county_dir", "sas_deployment_config", mode="before")
    @classmethod
    def _coerce_terrain_dir(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return Path(str(value))

    @field_validator("sas_protection_data_root", mode="before")
    @classmethod
    def _coerce_protection_data_root(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return Path(str(value))

    @field_validator("sas_protection_data_strict", mode="before")
    @classmethod
    def _coerce_protection_data_strict(cls, value: object) -> object:
        if value is None or value == "":
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @field_validator("sas_bpr_path_loss_model", mode="before")
    @classmethod
    def _normalize_bpr_path_loss_model(cls, value: object) -> object:
        if value is None or value == "":
            return "itm"
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized in ("free_space", "fs", "freespace"):
                return "free_space"
            return "itm"
        return value

    @field_validator("sas_iap_path_loss_model", mode="before")
    @classmethod
    def _normalize_iap_path_loss_model(cls, value: object) -> object:
        if value is None or value == "":
            return "itm"
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized in ("free_space", "fs", "freespace"):
                return "free_space"
            return "itm"
        return value

    @field_validator("sas_iap_enabled", mode="before")
    @classmethod
    def _coerce_iap_enabled(cls, value: object) -> object:
        if value is None or value == "":
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @field_validator(
        "ssl_certfile",
        "ssl_keyfile",
        "ssl_ecc_certfile",
        "ssl_ecc_keyfile",
        "ssl_ca_certs",
        "ssl_crl_dir",
        "client_certfile",
        "client_keyfile",
        mode="before",
    )
    @classmethod
    def _coerce_optional_path(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, Path):
            return value
        return Path(str(value))

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.rabbitmq_url

    @property
    def result_backend(self) -> Optional[str]:
        """Optional Celery result backend; None disables result persistence."""
        return self.celery_result_backend

    @property
    def resolved_protection_data_root(self) -> Path:
        from protection_data.loader import DEFAULT_DATA_ROOT

        return (self.sas_protection_data_root or DEFAULT_DATA_ROOT).resolve()

    @property
    def resolved_ssl_certfile(self) -> Path:
        return self.ssl_certfile or (self.certs_dir / "server.cert")

    @property
    def resolved_ssl_keyfile(self) -> Path:
        return self.ssl_keyfile or (self.certs_dir / "server.key")

    @property
    def resolved_ssl_ecc_certfile(self) -> Path:
        return self.ssl_ecc_certfile or (self.certs_dir / "server-ecc.cert")

    @property
    def resolved_ssl_ecc_keyfile(self) -> Path:
        return self.ssl_ecc_keyfile or (self.certs_dir / "server-ecc.key")

    @property
    def resolved_ssl_ca_certs(self) -> Path:
        return self.ssl_ca_certs or (self.certs_dir / "ca.cert")

    @property
    def resolved_ssl_crl_dir(self) -> Path:
        return self.ssl_crl_dir or (self.certs_dir / "crl")

    @property
    def resolved_client_certfile(self) -> Path:
        return self.client_certfile or self.resolved_ssl_certfile

    @property
    def resolved_client_keyfile(self) -> Path:
        return self.client_keyfile or self.resolved_ssl_keyfile

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def db_sync_basic_auth(self) -> tuple[str, str]:
        return (self.db_sync_username, self.db_sync_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
