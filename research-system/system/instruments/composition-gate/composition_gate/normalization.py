"""Shared normalization and deterministic ordering for composition-gate inputs."""

from __future__ import annotations

import unicodedata


def comparison_key(value: str) -> str:
    """Return the deliberately over-matching semantic/filesystem key.

    NFC(casefold(value)) is the single comparison regime ruled by R-i7-5.
    Exact transcription checks, such as candidate lane equality, remain explicit
    call-site exceptions rather than alternate normalizers.
    """

    return unicodedata.normalize("NFC", value.casefold())


def stable_text_key(value: str) -> tuple[str, bytes]:
    """Sort text by comparison identity, then its exact UTF-8 representation."""

    return comparison_key(value), value.encode("utf-8")


def stable_raw_name_key(value: str, raw: bytes) -> tuple[str, bytes]:
    """Sort a decoded Git local name without losing its physical byte tie-break."""

    return comparison_key(value), raw


def stable_path_key(value: str) -> tuple[tuple[str, bytes], ...]:
    """Sort a slash-joined logical path by each already-decoded local name."""

    return tuple(stable_text_key(part) for part in value.split("/"))


__all__ = [
    "comparison_key",
    "stable_path_key",
    "stable_raw_name_key",
    "stable_text_key",
]
