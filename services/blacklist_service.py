"""CBSD blacklist checks shared by registration, inquiry, grant and heartbeat."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.models import BlacklistedFccId, BlacklistedFccIdSerial


def is_fcc_id_blacklisted(db: Session, fcc_id: str) -> bool:
    return (
        db.query(BlacklistedFccId).filter_by(fcc_id=fcc_id).first() is not None
    )


def is_cbsd_blacklisted(db: Session, fcc_id: str, cbsd_serial_number: str) -> bool:
    """True if FCC ID is blacklisted or the specific (fccId, serial) pair is."""
    if is_fcc_id_blacklisted(db, fcc_id):
        return True
    return (
        db.query(BlacklistedFccIdSerial)
        .filter_by(fcc_id=fcc_id, cbsd_serial_number=cbsd_serial_number)
        .first()
        is not None
    )


def add_fcc_id_blacklist(db: Session, fcc_id: str) -> None:
    if is_fcc_id_blacklisted(db, fcc_id):
        return
    db.add(BlacklistedFccId(fcc_id=fcc_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def add_fcc_id_serial_blacklist(
    db: Session, fcc_id: str, cbsd_serial_number: str
) -> None:
    existing = (
        db.query(BlacklistedFccIdSerial)
        .filter_by(fcc_id=fcc_id, cbsd_serial_number=cbsd_serial_number)
        .first()
    )
    if existing is not None:
        return
    db.add(
        BlacklistedFccIdSerial(
            fcc_id=fcc_id, cbsd_serial_number=cbsd_serial_number
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
