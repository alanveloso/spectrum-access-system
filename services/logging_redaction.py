"""Log redaction helpers — keep secrets out of logs and audit JSON."""

from __future__ import annotations

import logging
import re
from typing import Any

# Keys matched case-insensitively (substring).
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "privatekey",
    "private_key",
    "cpiPrivateKey",
    "cpiprivatekey",
    "pem",
    "certificate",
    "clientkey",
    "sslkey",
)

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----",
    re.DOTALL,
)


def _key_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "").replace("_", "")
    for part in _SENSITIVE_KEY_PARTS:
        needle = part.lower().replace("-", "").replace("_", "")
        if needle in lowered:
            return True
    return False


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        if "BEGIN " in value and "PRIVATE" in value.upper():
            return "[REDACTED_PEM]"
        if _PEM_RE.search(value):
            return _PEM_RE.sub("[REDACTED_PEM]", value)
        if len(value) > 4096:
            return value[:256] + "…[truncated]"
        return value
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _key_sensitive(str(key)):
            out[key] = "[REDACTED]"
        else:
            out[key] = redact_value(value)
    return out


class RedactingFilter(logging.Filter):
    """Best-effort redaction of ``record.msg`` / ``args`` string forms."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and (
                "BEGIN " in record.msg or "password" in record.msg.lower()
            ):
                record.msg = _PEM_RE.sub("[REDACTED_PEM]", record.msg)
 # Avoid logging obvious password assignments.
                if "password" in record.msg.lower():
                    record.msg = re.sub(
                        r"(password\s*[=:]\s*)\S+",
                        r"\1[REDACTED]",
                        record.msg,
                        flags=re.IGNORECASE,
                    )
        except Exception:  # noqa: BLE001 — never break logging
            return True
        return True


def configure_safe_logging(*, level: int = logging.INFO) -> None:
    """Attach redacting filter to the root logger (idempotent)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    root.setLevel(level)
    marker = "sas_redacting_filter"
    for handler in root.handlers:
        if any(getattr(f, "name", None) == marker for f in handler.filters):
            continue
        f = RedactingFilter()
        f.name = marker  # type: ignore[attr-defined]
        handler.addFilter(f)
