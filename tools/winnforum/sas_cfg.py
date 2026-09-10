"""Generate harness ``sas.cfg`` from settings (no fixture hardcodes)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SasCfgParams:
    admin_api_base_url: str
    cbsd_sas_rsa_base_url: str
    cbsd_sas_ec_base_url: str
    sas_sas_rsa_base_url: str
    sas_sas_ec_base_url: str
    cbsd_sas_version: str
    sas_sas_version: str
    admin_id: str
    maximum_batch_size: int


def default_sas_cfg_params(
    *,
    host: str = "localhost",
    rsa_port: int = 9000,
    ecc_port: int = 9001,
    cbsd_sas_version: str = "v1.2",
    sas_sas_version: str = "v1.3",
    admin_id: str = "sas_admin_id",
    maximum_batch_size: int = 100,
) -> SasCfgParams:
    rsa = f"{host}:{rsa_port}"
    ecc = f"{host}:{ecc_port}"
    return SasCfgParams(
        admin_api_base_url=rsa,
        cbsd_sas_rsa_base_url=rsa,
        cbsd_sas_ec_base_url=ecc,
        sas_sas_rsa_base_url=rsa,
        sas_sas_ec_base_url=ecc,
        cbsd_sas_version=cbsd_sas_version,
        sas_sas_version=sas_sas_version,
        admin_id=admin_id,
        maximum_batch_size=maximum_batch_size,
    )


def render_sas_cfg(params: SasCfgParams) -> str:
    return (
        "[SasConfig]\n"
        f"AdminApiBaseUrl: {params.admin_api_base_url}\n"
        f"CbsdSasRsaBaseUrl: {params.cbsd_sas_rsa_base_url}\n"
        f"CbsdSasEcBaseUrl: {params.cbsd_sas_ec_base_url}\n"
        f"SasSasRsaBaseUrl: {params.sas_sas_rsa_base_url}\n"
        f"SasSasEcBaseUrl: {params.sas_sas_ec_base_url}\n"
        f"CbsdSasVersion: {params.cbsd_sas_version}\n"
        f"SasSasVersion: {params.sas_sas_version}\n"
        f"AdminId: {params.admin_id}\n"
        f"MaximumBatchSize: {params.maximum_batch_size}\n"
    )


def write_sas_cfg(path: Path, params: SasCfgParams) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_sas_cfg(params), encoding="utf-8")
