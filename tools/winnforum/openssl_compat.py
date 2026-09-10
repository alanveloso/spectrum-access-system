"""Harness OpenSSL version decode compatibility (OpenSSL 3.x).

The official WInnForum harness strips TLS 1.3 cipher names from
``get_cipher_list()`` only when ``util.get_openssl_version() >= 111``.
Its decoder requires a letter after the patch digit (``1.1.1j``), so
strings like ``OpenSSL 3.2.1 30 Jan 2024`` decode to ``-1`` and every
SCS/SDS cipher assertion fails on the client side.

This module patches the decoder in-process without modifying harness
sources or official expectations.
"""

from __future__ import annotations

import re
from typing import Callable


_OPENSSL_3_RE = re.compile(r"^OpenSSL (\d+)\.(\d+)\.(\d+)")


def decode_openssl_version_string(version: str) -> int:
    """Decode an OpenSSL version banner to the harness integer form (e.g. 321).

 Prefer the official harness letter-suffix pattern when it matches; otherwise
 accept OpenSSL 3.x banners without a letter suffix.
 """
    # Official harness pattern: 'OpenSSL 1.1.1j  16 Feb 2021'
    legacy = re.search(r"^OpenSSL (\d)\.(\d)\.(\d)\w .*", version)
    if legacy is not None:
        return (
            int(legacy.group(1)) * 100
            + int(legacy.group(2)) * 10
            + int(legacy.group(3))
        )
    modern = _OPENSSL_3_RE.match(version)
    if modern is None:
        return -1
    return (
        int(modern.group(1)) * 100
        + int(modern.group(2)) * 10
        + int(modern.group(3))
    )


def patch_harness_openssl_version_decoder(
    decode: Callable[[str], int] | None = None,
) -> bool:
    """Patch ``util._decode_openssl_version`` when the harness util is importable.

 Returns True when the patch was applied.
 """
    try:
        import util as harness_util  # type: ignore[import-not-found]
    except ImportError:
        return False

    decoder = decode or decode_openssl_version_string
    harness_util._decode_openssl_version = decoder  # type: ignore[attr-defined]
    return True
