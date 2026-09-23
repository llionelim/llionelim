"""Formula lock, versions and changelog.

formula/lock.json is the single source of truth for how the index is calculated.
It holds every formula version ever used, each one frozen with its base values
and a SHA-256 hash. config.yaml is only a *proposal*: daily computation refuses
to run if config.yaml's formula differs from the latest locked version.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .config import canonical_json, sha256, source_key
from .formula import STATUS_OK, base_values, compute_day
from .store import now_sgt

MODE_INITIAL = "initial"
MODE_REBASE = "rebase"
MODE_CONTINUITY = "continuity"

NEUTRALITY_TOLERANCE_PCT = 0.5
NEUTRALITY_LOOKBACK_DAYS = 90


class FormulaError(Exception):
    pass


class FormulaDriftError(FormulaError):
    pass


# ---------------------------------------------------------------- lock file io

def load_lock(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        lock = json.load(f)
    for v in lock["versions"]:
        if sha256(v["spec"]) != v["spec_hash"]:
            raise FormulaError(
                f"lock.json formula v{v['version']} has been edited by hand: its content "
                f"no longer matches its recorded hash. Restore it from git.")
    return lock


def save_lock(lock: dict, lock_path: Path, changelog_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2, sort_keys=True)
        f.write("\n")
    changelog_path.write_text(render_changelog(lock), encoding="utf-8")


def latest_version(lock: dict) -> dict:
    return lock["versions"][-1]


def version_for_date(lock: dict, day: date) -> dict | None:
    chosen = None
    for v in lock["versions"]:
        if date.fromisoformat(v["effective_from"]) <= day:
            chosen = v
    return chosen


def check_config_matches(lock: dict, definition: dict) -> None:
    """Refuse to run if config.yaml's formula is not the latest locked one."""
    latest = latest_version(lock)
    if canonical_json(definition) != canonical_json(latest["spec"]["definition"]):
        changes = "\n  - ".join(diff_definitions(latest["spec"]["definition"], definition))
        raise FormulaDriftError(
            f"config.yaml formula differs from locked formula v{latest['version']}:\n"
            f"  - {changes}\n"
            f"The index formula cannot change silently. Either revert config.yaml, "
            f"or run `update-formula` to record this as formula v{latest['version'] + 1}.")


# ---------------------------------------------------------------- diffs

def diff_definitions(old: dict, new: dict) -> list[str]:
    changes = []
    for key in ("base_level", "window_days", "combination"):
        if old[key] != new[key]:
            changes.append(f"{key}: {old[key]!r} -> {new[key]!r}")
    old_m = {m["id"]: m for m in old["metrics"]}
    new_m = {m["id"]: m for m in new["metrics"]}
    for mid in sorted(set(old_m) - set(new_m)):
        changes.append(f"metric removed: {mid} (weight {old_m[mid]['weight']})")
    for mid in sorted(set(new_m) - set(old_m)):
        changes.append(f"metric added: {mid} (weight {new_m[mid]['weight']}, "
                       f"source {canonical_json(new_m[mid]['source'])})")
    for mid in sorted(set(old_m) & set(new_m)):
        a, b = old_m[mid], new_m[mid]
        if a["weight"] != b["weight"]:
            changes.append(f"{mid}.weight: {a['weight']} -> {b['weight']}")
        if a["min_observations"] != b["min_observations"]:
            changes.append(f"{mid}.min_observations: {a['min_observations']} -> {b['min_observations']}")
        if a["source"] != b["source"]:
            changes.append(f"{mid}.source swapped: {canonical_json(a['source'])} -> "
                           f"{canonical_json(b['source'])}")
    return changes


# ---------------------------------------------------------------- building specs

def _frozen_bases(definition: dict, observations: dict, start: date, end: date,
                  reuse: dict | None = None) -> dict:
    """Base value per metric, reusing `reuse` values for metrics whose id and
    source are unchanged (continuity mode)."""
    computed = base_values(definition, observations, start, end)
    values = {}
    problems = []
    for m in definition["metrics"]:
        mid = m["id"]
        if reuse and mid in reuse:
            values[mid] = reuse[mid]
            continue
        b = computed[mid]
        if b["count"] < m["min_observations"]:
            problems.append(f"{mid}: {b['count']} observations in base period "
                            f"{start}..{end}, need >= {m['min_observations']}")
        elif b["value"] <= 0:
            problems.append(f"{mid}: base value is {b['value']} (must be > 0)")
        else:
            values[mid] = b["value"]
    if problems:
        raise FormulaError("cannot freeze base values:\n  - " + "\n  - ".join(problems))
    return values


def _make_version(number: int, definition: dict, start: date, end: date, bases: dict,
                  effective_from: date, mode: str) -> dict:
    spec = {
        "definition": definition,
        "base": {"start": start.isoformat(), "end": end.isoformat(), "values": bases},
    }
    return {
        "version": number,
        "mode": mode,
        "effective_from": effective_from.isoformat(),
        "created_at": now_sgt().isoformat(),
        "spec": spec,
        "spec_hash": sha256(spec),
    }


def lock_base(definition: dict, observations: dict, start: date, end: date,
              effective_from: date, reason: str) -> dict:
    if end < start:
        raise FormulaError("base end is before base start")
    if effective_from < end:
        raise FormulaError("effective date must be on or after the base period end")
    bases = _frozen_bases(definition, observations, start, end)
    v1 = _make_version(1, definition, start, end, bases, effective_from, MODE_INITIAL)
    entry = {
        "date": now_sgt().isoformat(),
        "from_version": None,
        "to_version": 1,
        "mode": MODE_INITIAL,
        "effective_from": v1["effective_from"],
        "changes": ["initial formula lock"],
        "summary": "Initial formula lock",
        "reason": reason,
        "base_period": {"start": start.isoformat(), "end": end.isoformat()},
        "old_hash": None,
        "new_hash": v1["spec_hash"],
        "neutrality_check": None,
    }
    return {"versions": [v1], "changelog": [entry]}


def neutrality_check(old_spec: dict, new_spec: dict, observations: dict, before: date) -> dict:
    """Compare old vs new formula on the most recent day both can compute."""
    for i in range(NEUTRALITY_LOOKBACK_DAYS):
        day = before - timedelta(days=i)
        a = compute_day(old_spec, observations, day)
        b = compute_day(new_spec, observations, day)
        if a["status"] == STATUS_OK and b["status"] == STATUS_OK:
            diff = (b["index_level"] - a["index_level"]) / a["index_level"] * 100 \
                if a["index_level"] else float("inf")
            return {"reference_date": day.isoformat(), "old_level": a["index_level"],
                    "new_level": b["index_level"], "diff_pct": round(diff, 4),
                    "tolerance_pct": NEUTRALITY_TOLERANCE_PCT,
                    "neutral": abs(diff) <= NEUTRALITY_TOLERANCE_PCT}
    return {"reference_date": None, "neutral": False,
            "tolerance_pct": NEUTRALITY_TOLERANCE_PCT,
            "note": f"no day in the last {NEUTRALITY_LOOKBACK_DAYS} days computable under both formulas"}


def plan_update(lock: dict, definition: dict, observations: dict, *, mode: str,
                effective_from: date, reason: str, summary: str,
                last_published: date | None,
                base_start: date | None = None, base_end: date | None = None,
                accept_non_neutral: bool = False) -> tuple[dict, dict]:
    """Build (new_version, changelog_entry) without writing anything."""
    old = latest_version(lock)
    old_def = old["spec"]["definition"]
    changes = diff_definitions(old_def, definition)
    if not changes:
        raise FormulaError("config.yaml formula is identical to the locked version; nothing to update")
    if not reason.strip():
        raise FormulaError("--reason is required")
    if effective_from <= date.fromisoformat(old["effective_from"]):
        raise FormulaError(f"effective date must be after v{old['version']}'s effective date "
                           f"{old['effective_from']}")
    if last_published and effective_from <= last_published:
        raise FormulaError(f"effective date must be after the last published day ({last_published}); "
                           f"published rows are never recalculated under a new formula")

    number = old["version"] + 1
    neutrality = None
    if mode == MODE_REBASE:
        if not (base_start and base_end):
            raise FormulaError("rebase requires --base-start and --base-end (the new index=100 period)")
        if base_end < base_start:
            raise FormulaError("base end is before base start")
        if base_end > effective_from:
            raise FormulaError("new base period must end on or before the effective date")
        bases = _frozen_bases(definition, observations, base_start, base_end)
        new = _make_version(number, definition, base_start, base_end, bases, effective_from, mode)
    elif mode == MODE_CONTINUITY:
        if base_start or base_end:
            raise FormulaError("continuity keeps the existing base period; do not pass --base-start/--base-end")
        start = date.fromisoformat(old["spec"]["base"]["start"])
        end = date.fromisoformat(old["spec"]["base"]["end"])
        old_metrics = {m["id"]: m for m in old_def["metrics"]}
        reuse = {m["id"]: old["spec"]["base"]["values"][m["id"]]
                 for m in definition["metrics"]
                 if m["id"] in old_metrics
                 and source_key(m["source"]) == source_key(old_metrics[m["id"]]["source"])}
        if definition["base_level"] != old_def["base_level"]:
            raise FormulaError("continuity cannot change base_level; use rebase")
        bases = _frozen_bases(definition, observations, start, end, reuse=reuse)
        new = _make_version(number, definition, start, end, bases, effective_from, mode)
        neutrality = neutrality_check(old["spec"], new["spec"], observations,
                                      effective_from - timedelta(days=1))
        if not neutrality["neutral"] and not accept_non_neutral:
            raise FormulaError(
                "continuity is only valid for a neutral change, but the neutrality check failed:\n"
                f"  {json.dumps(neutrality)}\n"
                "Use --mode rebase, or pass --accept-non-neutral to record it anyway (the "
                "result is written into the changelog).")
    else:
        raise FormulaError(f"unknown mode {mode!r}")

    entry = {
        "date": now_sgt().isoformat(),
        "from_version": old["version"],
        "to_version": number,
        "mode": mode,
        "effective_from": effective_from.isoformat(),
        "changes": changes,
        "summary": summary or "; ".join(changes),
        "reason": reason,
        "base_period": {"start": new["spec"]["base"]["start"], "end": new["spec"]["base"]["end"]},
        "old_hash": old["spec_hash"],
        "new_hash": new["spec_hash"],
        "neutrality_check": neutrality,
        "accepted_non_neutral": bool(neutrality and not neutrality["neutral"]),
    }
    return new, entry


# ---------------------------------------------------------------- changelog

def render_changelog(lock: dict) -> str:
    lines = [
        "# Riftbound Hype Index — Formula Changelog",
        "",
        "Generated from `formula/lock.json` by `hype_index`. Do not edit by hand.",
        "Every change to the formula is a new `formula_version`; every output row records",
        "the version and hash that produced it.",
        "",
    ]
    by_version = {v["version"]: v for v in lock["versions"]}
    for e in reversed(lock["changelog"]):
        v = by_version[e["to_version"]]
        d = v["spec"]["definition"]
        title = (f"## v{e['to_version']}" +
                 (f" (from v{e['from_version']})" if e["from_version"] else "") +
                 f" — {e['mode']}")
        lines += [title, ""]
        lines.append(f"- **Recorded:** {e['date']}")
        lines.append(f"- **Effective from:** {e['effective_from']}")
        lines.append(f"- **Decision:** " + {
            MODE_INITIAL: "initial lock; index = base level at the base period",
            MODE_REBASE: "RE-BASE: series restarts at the base level from the new base period "
                         "(not directly comparable across this boundary)",
            MODE_CONTINUITY: "CONTINUITY: level carried over, existing base period kept",
        }[e["mode"]])
        lines.append(f"- **Base period:** {e['base_period']['start']} to {e['base_period']['end']}")
        lines.append(f"- **Summary:** {e['summary']}")
        lines.append(f"- **Reason:** {e['reason']}")
        lines.append(f"- **Hash:** `{e['old_hash'] or '—'}` → `{e['new_hash']}`")
        if e.get("neutrality_check"):
            n = e["neutrality_check"]
            lines.append(f"- **Neutrality check:** {json.dumps(n, sort_keys=True)}"
                         + (" — **accepted despite failing**" if e.get("accepted_non_neutral") else ""))
        lines.append("- **Changes:**")
        lines += [f"  - {c}" for c in e["changes"]]
        lines.append(f"- **Formula:** base_level={d['base_level']}, window_days={d['window_days']}, "
                     f"combination={d['combination']}")
        lines.append("")
        lines.append("  | metric | weight | min obs | source | base value |")
        lines.append("  |---|---|---|---|---|")
        for m in d["metrics"]:
            lines.append(f"  | {m['id']} | {m['weight']} | {m['min_observations']} | "
                         f"`{canonical_json(m['source'])}` | {v['spec']['base']['values'][m['id']]:.6g} |")
        lines.append("")
    return "\n".join(lines)
