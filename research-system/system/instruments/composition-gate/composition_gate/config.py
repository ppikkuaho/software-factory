"""Versioned, hashed configuration for the stage-1 composition screen."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).with_name("screen-config.v1.json")
_THRESHOLD_NAMES = (
    "k_last_merged",
    "surface_budget_max_diff_lines",
    "surface_budget_max_cumulative_directives",
    "queue_adjacency_min_pending",
)


@dataclass(frozen=True)
class ScreenConfig:
    path: Path
    sha256: str
    version: str
    thresholds: dict[str, int]

    def threshold(self, name: str) -> int:
        return self.thresholds[name]


def load_config(path: str | Path | None = None) -> ScreenConfig:
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    raw = config_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("screen config must be a JSON object")
    if set(document) != {"version", "thresholds"}:
        raise ValueError("screen config requires exactly version and thresholds")
    version = document.get("version")
    if version != "screen-config.v1":
        raise ValueError(f"unsupported screen config version {version!r}")
    thresholds = document.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != set(_THRESHOLD_NAMES):
        raise ValueError(
            "screen config thresholds must be exactly " + ", ".join(_THRESHOLD_NAMES)
        )
    normalized: dict[str, int] = {}
    for name in _THRESHOLD_NAMES:
        value = thresholds[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"screen config threshold {name} must be a positive integer")
        normalized[name] = value
    return ScreenConfig(config_path, digest, version, normalized)
