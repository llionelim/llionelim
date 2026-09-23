"""Config loading, validation and canonical hashing of the formula definition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

COMBINATIONS = ("weighted_arithmetic", "weighted_geometric")
WEIGHT_TOLERANCE = 1e-9


class ConfigError(Exception):
    pass


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace variance."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("ascii")).hexdigest()


def source_key(source: dict) -> str:
    """Fingerprint of a metric's proxy source.

    Every observation is stored with the source_key it was collected under, and
    the calculation only reads observations whose source_key matches the locked
    spec. A swapped proxy therefore can never silently mix into an old series.
    """
    return sha256(source)[:12]


def normalize_definition(raw: Any) -> dict:
    """Validate the `formula:` section and return it in canonical form."""
    if not isinstance(raw, dict):
        raise ConfigError("`formula` section missing or not a mapping")

    for key in ("base_level", "window_days", "combination", "metrics"):
        if key not in raw:
            raise ConfigError(f"formula.{key} is required")
    unknown = set(raw) - {"base_level", "window_days", "combination", "metrics"}
    if unknown:
        raise ConfigError(f"unknown formula keys: {sorted(unknown)}")

    base_level = float(raw["base_level"])
    if base_level <= 0:
        raise ConfigError("formula.base_level must be > 0")

    window = raw["window_days"]
    if not isinstance(window, int) or window < 1:
        raise ConfigError("formula.window_days must be a positive integer")

    combination = raw["combination"]
    if combination not in COMBINATIONS:
        raise ConfigError(f"formula.combination must be one of {COMBINATIONS}")

    metrics_raw = raw["metrics"]
    if not isinstance(metrics_raw, list) or not metrics_raw:
        raise ConfigError("formula.metrics must be a non-empty list")

    metrics = []
    seen = set()
    for m in metrics_raw:
        for key in ("id", "weight", "min_observations", "source"):
            if key not in m:
                raise ConfigError(f"metric {m.get('id', '?')}: `{key}` is required")
        unknown = set(m) - {"id", "weight", "min_observations", "source"}
        if unknown:
            raise ConfigError(f"metric {m['id']}: unknown keys {sorted(unknown)}")
        mid = str(m["id"])
        if mid in seen:
            raise ConfigError(f"duplicate metric id {mid}")
        seen.add(mid)
        weight = float(m["weight"])
        if weight <= 0:
            raise ConfigError(f"metric {mid}: weight must be > 0")
        min_obs = m["min_observations"]
        if not isinstance(min_obs, int) or not 1 <= min_obs <= window:
            raise ConfigError(f"metric {mid}: min_observations must be an int in 1..window_days")
        source = m["source"]
        if not isinstance(source, dict) or "type" not in source:
            raise ConfigError(f"metric {mid}: source must be a mapping with a `type`")
        metrics.append(
            {"id": mid, "weight": weight, "min_observations": min_obs, "source": dict(source)}
        )

    total = sum(m["weight"] for m in metrics)
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise ConfigError(f"metric weights must sum to 1.0 (got {total!r})")

    metrics.sort(key=lambda m: m["id"])
    return {
        "base_level": base_level,
        "window_days": window,
        "combination": combination,
        "metrics": metrics,
    }


@dataclass
class Config:
    root: Path
    definition: dict
    collection: dict
    paths: dict

    def path(self, name: str) -> Path:
        return self.root / self.paths[name]


DEFAULT_PATHS = {
    "observations": "data/observations.csv",
    "lock": "formula/lock.json",
    "changelog_md": "formula/CHANGELOG.md",
    "output_csv": "output/hype_index.csv",
    "output_json": "output/hype_index.json",
}


def load_config(path: Path) -> Config:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config(
        root=path.parent,
        definition=normalize_definition(raw.get("formula")),
        collection=raw.get("collection") or {},
        paths={**DEFAULT_PATHS, **(raw.get("paths") or {})},
    )
