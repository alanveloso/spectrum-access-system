"""Initial SAS schema.

Revision ID: 20260808_0001
Revises:
Create Date: 2026-08-08

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fcc_ids",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fcc_id", sa.String(length=64), nullable=False),
        sa.Column("fcc_max_eirp", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fcc_id"),
    )
    op.create_index("ix_fcc_ids_fcc_id", "fcc_ids", ["fcc_id"])

    op.create_table(
        "user_ids",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_user_ids_user_id", "user_ids", ["user_id"])

    op.create_table(
        "blacklisted_fcc_ids",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fcc_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fcc_id"),
    )
    op.create_index(
        "ix_blacklisted_fcc_ids_fcc_id", "blacklisted_fcc_ids", ["fcc_id"]
    )

    op.create_table(
        "blacklisted_fcc_id_serials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fcc_id", sa.String(length=64), nullable=False),
        sa.Column("cbsd_serial_number", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fcc_id", "cbsd_serial_number", name="uq_blacklist_fcc_serial"
        ),
    )
    op.create_index(
        "ix_blacklisted_fcc_id_serials_fcc_id",
        "blacklisted_fcc_id_serials",
        ["fcc_id"],
    )
    op.create_index(
        "ix_blacklisted_fcc_id_serials_cbsd_serial_number",
        "blacklisted_fcc_id_serials",
        ["cbsd_serial_number"],
    )

    op.create_table(
        "conditional_registrations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fcc_id", sa.String(length=64), nullable=False),
        sa.Column("cbsd_serial_number", sa.String(length=128), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fcc_id", "cbsd_serial_number", name="uq_cond_fcc_serial"
        ),
    )
    op.create_index(
        "ix_conditional_registrations_fcc_id",
        "conditional_registrations",
        ["fcc_id"],
    )
    op.create_index(
        "ix_conditional_registrations_cbsd_serial_number",
        "conditional_registrations",
        ["cbsd_serial_number"],
    )

    op.create_table(
        "cpi_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cpi_id", sa.String(length=128), nullable=False),
        sa.Column("cpi_name", sa.String(length=256), nullable=False),
        sa.Column("cpi_public_key", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cpi_id"),
    )
    op.create_index("ix_cpi_users_cpi_id", "cpi_users", ["cpi_id"])

    op.create_table(
        "cbsds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cbsd_id", sa.String(length=256), nullable=False),
        sa.Column("fcc_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("cbsd_serial_number", sa.String(length=128), nullable=False),
        sa.Column("cbsd_category", sa.String(length=8), nullable=True),
        sa.Column("certificate_hash", sa.String(length=128), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("registration_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cbsd_id"),
        sa.UniqueConstraint(
            "fcc_id", "cbsd_serial_number", name="uq_cbsd_fcc_serial"
        ),
    )
    op.create_index("ix_cbsds_cbsd_id", "cbsds", ["cbsd_id"])
    op.create_index("ix_cbsds_fcc_id", "cbsds", ["fcc_id"])
    op.create_index("ix_cbsds_certificate_hash", "cbsds", ["certificate_hash"])
    op.create_index("ix_cbsds_lifecycle_state", "cbsds", ["lifecycle_state"])

    op.create_table(
        "grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("grant_id", sa.String(length=256), nullable=False),
        sa.Column("cbsd_pk", sa.Integer(), nullable=False),
        sa.Column("cbsd_id", sa.String(length=256), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("low_frequency", sa.BigInteger(), nullable=False),
        sa.Column("high_frequency", sa.BigInteger(), nullable=False),
        sa.Column("max_eirp", sa.Float(), nullable=True),
        sa.Column("grant_expire_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_interval", sa.Integer(), nullable=False),
        sa.Column(
            "transmit_expire_time", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("meas_report_requested", sa.Boolean(), nullable=False),
        sa.Column("terminated", sa.Boolean(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("grant_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["cbsd_pk"], ["cbsds.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grant_id"),
    )
    op.create_index("ix_grants_grant_id", "grants", ["grant_id"])
    op.create_index("ix_grants_cbsd_pk", "grants", ["cbsd_pk"])
    op.create_index("ix_grants_cbsd_id", "grants", ["cbsd_id"])
    op.create_index("ix_grants_lifecycle_state", "grants", ["lifecycle_state"])

    op.create_table(
        "pal_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pal_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("low_frequency", sa.BigInteger(), nullable=False),
        sa.Column("high_frequency", sa.BigInteger(), nullable=False),
        sa.Column("license_status", sa.String(length=16), nullable=False),
        sa.Column("license_expiration", sa.String(length=32), nullable=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pal_id"),
    )
    op.create_index("ix_pal_records_pal_id", "pal_records", ["pal_id"])
    op.create_index("ix_pal_records_user_id", "pal_records", ["user_id"])

    op.create_table(
        "admin_injected_data",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_injected_data_kind", "admin_injected_data", ["kind"])

    op.create_table(
        "peer_sas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("certificate_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fad_generation", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("certificate_hash"),
    )
    op.create_index(
        "ix_peer_sas_certificate_hash", "peer_sas", ["certificate_hash"]
    )

    op.create_table(
        "esc_sensors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.String(length=256), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id"),
    )
    op.create_index("ix_esc_sensors_record_id", "esc_sensors", ["record_id"])

    op.create_table(
        "peer_fad_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("peer_sas_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("record_id", sa.String(length=256), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["peer_sas_id"], ["peer_sas.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "peer_sas_id",
            "record_type",
            "record_id",
            name="uq_peer_fad_record",
        ),
    )
    op.create_index(
        "ix_peer_fad_records_peer_sas_id", "peer_fad_records", ["peer_sas_id"]
    )
    op.create_index(
        "ix_peer_fad_records_record_type", "peer_fad_records", ["record_type"]
    )
    op.create_index(
        "ix_peer_fad_records_record_id", "peer_fad_records", ["record_id"]
    )

    op.create_table(
        "fad_dumps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("generation_datetime", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fad_dumps_generation_datetime",
        "fad_dumps",
        ["generation_datetime"],
    )
    op.create_index("ix_fad_dumps_published", "fad_dumps", ["published"])
    op.create_index(
        "uq_fad_dumps_one_published",
        "fad_dumps",
        ["published"],
        unique=True,
        postgresql_where=sa.text("published IS TRUE"),
        sqlite_where=sa.text("published IS TRUE"),
    )

    op.create_table(
        "fad_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dump_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("url_path", sa.String(length=512), nullable=False),
        sa.Column("checksum", sa.String(length=40), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["dump_id"], ["fad_dumps.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fad_files_dump_id", "fad_files", ["dump_id"])
    op.create_index("ix_fad_files_record_type", "fad_files", ["record_type"])
    op.create_index("ix_fad_files_url_path", "fad_files", ["url_path"])


def downgrade() -> None:
    op.drop_table("fad_files")
    op.drop_table("fad_dumps")
    op.drop_table("peer_fad_records")
    op.drop_table("esc_sensors")
    op.drop_table("peer_sas")
    op.drop_table("admin_injected_data")
    op.drop_table("pal_records")
    op.drop_table("grants")
    op.drop_table("cbsds")
    op.drop_table("cpi_users")
    op.drop_table("conditional_registrations")
    op.drop_table("blacklisted_fcc_id_serials")
    op.drop_table("blacklisted_fcc_ids")
    op.drop_table("user_ids")
    op.drop_table("fcc_ids")
