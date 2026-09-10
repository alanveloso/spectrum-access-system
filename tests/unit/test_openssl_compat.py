"""OpenSSL 3.x harness version decoder compatibility ()."""

from __future__ import annotations

import sys
import types

from tools.winnforum.openssl_compat import (
    decode_openssl_version_string,
    patch_harness_openssl_version_decoder,
)


def test_decode_openssl_1_1_1_letter_suffix():
    assert decode_openssl_version_string("OpenSSL 1.1.1j  16 Feb 2021") == 111


def test_decode_openssl_3_2_1_without_letter():
    assert decode_openssl_version_string("OpenSSL 3.2.1 30 Jan 2024") == 321


def test_decode_unknown_returns_minus_one():
    assert decode_openssl_version_string("BoringSSL") == -1


def test_patch_applies_decoder_to_harness_util(monkeypatch):
    fake = types.ModuleType("util")
    fake._decode_openssl_version = lambda _version: -1
    monkeypatch.setitem(sys.modules, "util", fake)
    assert patch_harness_openssl_version_decoder() is True
    assert fake._decode_openssl_version("OpenSSL 3.2.1 30 Jan 2024") == 321
