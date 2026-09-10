"""SQLAlchemy models for the SAS MVP."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base
from models.types import UtcDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FccIdRecord(Base):
    __tablename__ = "fcc_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fcc_max_eirp: Mapped[float] = mapped_column(Float, default=47.0)


class UserIdRecord(Base):
    __tablename__ = "user_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)


class BlacklistedFccId(Base):
    __tablename__ = "blacklisted_fcc_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)


class BlacklistedFccIdSerial(Base):
    """Per-CBSD blacklist (fccId + cbsdSerialNumber), Admin BlacklistByFccIdAndSerialNumber."""

    __tablename__ = "blacklisted_fcc_id_serials"
    __table_args__ = (
        UniqueConstraint(
            "fcc_id", "cbsd_serial_number", name="uq_blacklist_fcc_serial"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), index=True)
    cbsd_serial_number: Mapped[str] = mapped_column(String(128), index=True)


class ConditionalRegistration(Base):
    __tablename__ = "conditional_registrations"
    __table_args__ = (
        UniqueConstraint("fcc_id", "cbsd_serial_number", name="uq_cond_fcc_serial"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), index=True)
    cbsd_serial_number: Mapped[str] = mapped_column(String(128), index=True)
    data_json: Mapped[str] = mapped_column(Text)


class CpiUser(Base):
    __tablename__ = "cpi_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cpi_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    cpi_name: Mapped[str] = mapped_column(String(256), default="")
    cpi_public_key: Mapped[str] = mapped_column(Text, default="")


class Cbsd(Base):
    __tablename__ = "cbsds"
    __table_args__ = (
        UniqueConstraint("fcc_id", "cbsd_serial_number", name="uq_cbsd_fcc_serial"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cbsd_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    fcc_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(256))
    cbsd_serial_number: Mapped[str] = mapped_column(String(128))
    cbsd_category: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # SHA-1 fingerprint of the registering client cert (``AA:BB:...``), for mTLS binding.
    certificate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # CBSD lifecycle: UNREGISTERED | REGISTERED | DEREGISTERED (persisted while row exists).
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), default="REGISTERED", nullable=False, index=True
    )
    registration_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_utc_now, onupdate=_utc_now
    )
    grants: Mapped[list[Grant]] = relationship(
        "Grant", back_populates="cbsd", cascade="all, delete-orphan"
    )


class Grant(Base):
    __tablename__ = "grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grant_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    cbsd_pk: Mapped[int] = mapped_column(ForeignKey("cbsds.id"), index=True)
    cbsd_id: Mapped[str] = mapped_column(String(256), index=True)
    channel_type: Mapped[str] = mapped_column(String(16), default="GAA")
    low_frequency: Mapped[int] = mapped_column(BigInteger, default=0)
    high_frequency: Mapped[int] = mapped_column(BigInteger, default=0)
    max_eirp: Mapped[float | None] = mapped_column(Float, nullable=True)
    grant_expire_time: Mapped[datetime] = mapped_column(UtcDateTime)
    heartbeat_interval: Mapped[int] = mapped_column(Integer, default=60)
    transmit_expire_time: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True
    )
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    meas_report_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    terminated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Grant lifecycle: GRANTED | AUTHORIZED | SUSPENDED | TERMINATED | EXPIRED | RELINQUISHED
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), default="GRANTED", nullable=False, index=True
    )
    grant_json: Mapped[str] = mapped_column(Text, default="{}")
    cbsd: Mapped[Cbsd] = relationship("Cbsd", back_populates="grants")


class PalRecord(Base):
    """Priority Access License injected by the marketplace via /admin/injectdata/pal_database_record."""

    __tablename__ = "pal_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pal_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    low_frequency: Mapped[int] = mapped_column(BigInteger)
    high_frequency: Mapped[int] = mapped_column(BigInteger)
    license_status: Mapped[str] = mapped_column(String(16), default="VALID")
    license_expiration: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record_json: Mapped[str] = mapped_column(Text, default="{}")


class AdminInjectedData(Base):
    """Stores admin-injected incumbents / PAL / PPA for Spectrum Inquiry simulation."""

    __tablename__ = "admin_injected_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # fss|wisp|pal|zone
    data_json: Mapped[str] = mapped_column(Text)


class PeerSas(Base):
    """Peer SAS authorized for SAS?SAS (FAD) access."""

    __tablename__ = "peer_sas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    certificate_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    # Last successfully applied FullActivityDump.generationDateTime ().
    last_fad_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EscSensor(Base):
    """ESC sensor records injected via admin API for FAD export."""

    __tablename__ = "esc_sensors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    data_json: Mapped[str] = mapped_column(Text)


class PeerFadRecord(Base):
    """Records imported from a peer SAS Full Activity Dump (UUT as client)."""

    __tablename__ = "peer_fad_records"
    __table_args__ = (
        UniqueConstraint(
            "peer_sas_id", "record_type", "record_id", name="uq_peer_fad_record"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    peer_sas_id: Mapped[int] = mapped_column(ForeignKey("peer_sas.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(32), index=True)
    record_id: Mapped[str] = mapped_column(String(256), index=True)
    data_json: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class FadDump(Base):
    """Local Full Activity Dump generation (UUT as SAS?SAS server).

 Semantics:

 - ``ready``: snapshot generation completed; historical ready dumps may coexist.
 - ``published``: exactly one current dump served via SAS?SAS ``GET /dump``.
 """

    __tablename__ = "fad_dumps"
    __table_args__ = (
        Index(
            "uq_fad_dumps_one_published",
            "published",
            unique=True,
            postgresql_where=text("published IS TRUE"),
            sqlite_where=text("published IS TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generation_datetime: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(String(256), default="Full activity dump files")
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    files: Mapped[list[FadFile]] = relationship(
        "FadFile", back_populates="dump", cascade="all, delete-orphan"
    )


class FadFile(Base):
    """Activity dump file content belonging to a FadDump."""

    __tablename__ = "fad_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dump_id: Mapped[int] = mapped_column(ForeignKey("fad_dumps.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(32), index=True)
    url_path: Mapped[str] = mapped_column(String(512), index=True)
    checksum: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_json: Mapped[str] = mapped_column(Text)
    dump: Mapped[FadDump] = relationship("FadDump", back_populates="files")
