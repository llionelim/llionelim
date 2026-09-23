"""The index calculation. Pure functions, no I/O, no data-dependent branches.

Given a frozen version spec and the observation history, the index for day t is:

    R_m(t) = mean of metric m's daily observations in [t - W + 1, t]
    r_m(t) = R_m(t) / B_m                      (B_m frozen at lock time)
    I(t)   = L * sum_m w_m * r_m(t)            (weighted_arithmetic)
           = L * prod_m r_m(t) ** w_m          (weighted_geometric)

The only branch is the uniform publication rule: if any metric has fewer than
its min_observations inside the window, the day is not published. Weights are
never redistributed and values are never capped, smoothed or clipped.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from .config import source_key

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
LEVEL_DECIMALS = 6


def window_dates(day: date, window_days: int) -> list[date]:
    return [day - timedelta(days=i) for i in range(window_days - 1, -1, -1)]


def rolling_mean(series: dict[date, float], day: date, window_days: int) -> tuple[float | None, int]:
    values = [series[d] for d in window_dates(day, window_days) if d in series]
    if not values:
        return None, 0
    return math.fsum(values) / len(values), len(values)


def base_values(definition: dict, observations: dict, start: date, end: date) -> dict[str, dict]:
    """Mean of each metric's observations over the base period [start, end].

    `observations` maps (metric_id, source_key) -> {date: value}.
    Returns {metric_id: {"value": float|None, "count": int}}.
    """
    out = {}
    n_days = (end - start).days + 1
    for m in definition["metrics"]:
        series = observations.get((m["id"], source_key(m["source"])), {})
        mean, count = rolling_mean(series, end, n_days)
        out[m["id"]] = {"value": mean, "count": count}
    return out


def compute_day(spec: dict, observations: dict, day: date) -> dict:
    """Compute the index for one day under a frozen version spec.

    spec = {"definition": {...}, "base": {"values": {metric_id: B_m}, ...}}
    """
    definition = spec["definition"]
    bases = spec["base"]["values"]
    window = definition["window_days"]

    components = {}
    missing = []
    for m in definition["metrics"]:
        series = observations.get((m["id"], source_key(m["source"])), {})
        rolling, count = rolling_mean(series, day, window)
        comp = {
            "weight": m["weight"],
            "observations_in_window": count,
            "rolling_value": rolling,
            "base_value": bases[m["id"]],
            "ratio": None,
        }
        if count < m["min_observations"]:
            missing.append(m["id"])
        else:
            comp["ratio"] = rolling / bases[m["id"]]
        components[m["id"]] = comp

    if missing:
        return {"status": STATUS_INSUFFICIENT, "index_level": None,
                "components": components, "missing_metrics": missing}

    ids = sorted(components)
    if definition["combination"] == "weighted_arithmetic":
        combined = math.fsum(components[i]["weight"] * components[i]["ratio"] for i in ids)
    elif definition["combination"] == "weighted_geometric":
        if any(components[i]["ratio"] <= 0 for i in ids):
            combined = 0.0
        else:
            combined = math.exp(math.fsum(
                components[i]["weight"] * math.log(components[i]["ratio"]) for i in ids))
    else:  # pragma: no cover - rejected by config validation
        raise ValueError(definition["combination"])

    level = round(definition["base_level"] * combined, LEVEL_DECIMALS)
    return {"status": STATUS_OK, "index_level": level,
            "components": components, "missing_metrics": []}
