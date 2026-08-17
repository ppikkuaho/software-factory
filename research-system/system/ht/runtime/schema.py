"""Strict JSON and trusted packaged-schema validation."""

from __future__ import annotations

import functools
from datetime import datetime
from importlib import resources
import json
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ht.errors import HtError


_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _real_rfc3339_datetime(value: object) -> bool:
    """Reject impossible calendar/time values even without optional validators."""

    if not isinstance(value, str):
        return True  # JSON Schema's type keyword owns non-string rejection.
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_loads(data: bytes | str, *, label: str = "runtime JSON") -> Any:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HtError(f"invalid strict {label}: {exc} (B1 §5)") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HtError(f"runtime value is not canonical JSON: {exc} (B1 §5)") from exc


@functools.lru_cache(maxsize=None)
def packaged_schema(name: str) -> dict[str, Any]:
    if "/" in name or "\\" in name or not name.endswith(".schema.json"):
        raise HtError(f"invalid packaged runtime schema name {name!r} (B1 §5)")
    item = resources.files("ht.runtime").joinpath("schemas", name)
    if not item.is_file():
        raise HtError(f"missing packaged runtime schema {name!r} (B1 §5)")
    value = strict_loads(item.read_bytes(), label=f"schema {name}")
    if not isinstance(value, dict):
        raise HtError(f"packaged runtime schema {name!r} is not an object (B1 §5)")
    return value


def validate(name: str, value: Any) -> None:
    validator = Draft202012Validator(
        packaged_schema(name),
        format_checker=_FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.path) or "<root>"
        raise HtError(
            f"runtime schema {name} rejects {location}: {error.message} (B1 §5)"
        )
