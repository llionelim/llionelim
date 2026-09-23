"""Observation storage (raw inputs) and index output (CSV + JSON)."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))

OBS_FIELDS = ["date", "metric_id", "source_key", "value", "collected_at", "note"]
OUT_FIELDS = ["date", "index_level", "status", "formula_version", "formula_hash",
              "computed_at", "missing_metrics", "components_json"]


def now_sgt() -> datetime:
    return datetime.now(SGT).replace(microsecond=0)


def today_sgt() -> date:
    return now_sgt().date()


# ---------------------------------------------------------------- observations

def read_observations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def observation_index(rows: list[dict]) -> dict:
    """(metric_id, source_key) -> {date: float}"""
    out: dict = {}
    for r in rows:
        key = (r["metric_id"], r["source_key"])
        out.setdefault(key, {})[date.fromisoformat(r["date"])] = float(r["value"])
    return out


def add_observations(path: Path, new: list[dict], replace: bool = False) -> tuple[int, int]:
    """Append observations. One value per (date, metric_id, source_key).

    Existing values are kept unless replace=True. Returns (written, skipped).
    """
    rows = read_observations(path)
    index = {(r["date"], r["metric_id"], r["source_key"]): i for i, r in enumerate(rows)}
    written = skipped = 0
    for obs in new:
        row = {k: str(obs.get(k, "")) for k in OBS_FIELDS}
        key = (row["date"], row["metric_id"], row["source_key"])
        if key in index:
            if not replace:
                skipped += 1
                continue
            rows[index[key]] = row
        else:
            index[key] = len(rows)
            rows.append(row)
        written += 1
    rows.sort(key=lambda r: (r["date"], r["metric_id"], r["source_key"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return written, skipped


# ---------------------------------------------------------------- index output

def read_output(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_output(csv_path: Path, json_path: Path, rows: list[dict], meta: dict) -> None:
    rows = sorted(rows, key=lambda r: r["date"])
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(rows)

    series = []
    for r in rows:
        series.append({
            "date": r["date"],
            "index_level": float(r["index_level"]) if r["index_level"] else None,
            "status": r["status"],
            "formula_version": int(r["formula_version"]),
            "formula_hash": r["formula_hash"],
            "missing_metrics": [m for m in r["missing_metrics"].split(";") if m],
            "components": json.loads(r["components_json"]),
        })
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "series": series}, f, indent=2, sort_keys=True)
        f.write("\n")
