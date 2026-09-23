"""Command-line interface: python -m hype_index <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import ConfigError, load_config, source_key
from .formula import STATUS_OK, compute_day
from .sources import COLLECTORS, MANUAL, SourceError
from .store import (add_observations, now_sgt, observation_index, read_observations,
                    read_output, today_sgt, write_output)
from .versioning import (MODE_CONTINUITY, MODE_REBASE, FormulaError, check_config_matches,
                         latest_version, load_lock, lock_base, plan_update, save_lock,
                         version_for_date)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _yesterday() -> date:
    return today_sgt() - timedelta(days=1)


def _require_lock(cfg):
    lock = load_lock(cfg.path("lock"))
    if lock is None:
        raise FormulaError("formula not locked yet: collect base-period data, then run `lock-base`")
    return lock


def _last_published(cfg) -> date | None:
    ok = [r["date"] for r in read_output(cfg.path("output_csv")) if r["status"] == STATUS_OK]
    return _d(max(ok)) if ok else None


def _all_metrics(cfg, lock) -> list[dict]:
    """Metrics in config.yaml plus the latest locked version (deduped by source)."""
    metrics = list(cfg.definition["metrics"])
    if lock:
        metrics += latest_version(lock)["spec"]["definition"]["metrics"]
    seen, out = set(), []
    for m in metrics:
        key = (m["id"], source_key(m["source"]))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# ---------------------------------------------------------------- commands

def cmd_collect(cfg, args) -> int:
    lock = load_lock(cfg.path("lock"))
    day = _d(args.date) if args.date else _yesterday()
    new, failed = [], []
    for m in _all_metrics(cfg, lock):
        stype = m["source"]["type"]
        if stype == MANUAL:
            continue
        collector = COLLECTORS.get(stype)
        if collector is None:
            failed.append(f"{m['id']}: unknown source type {stype}")
            continue
        try:
            points = collector(m["source"], day, cfg.collection)
        except (SourceError, KeyError, ValueError, OSError) as e:
            failed.append(f"{m['id']}: {e}")
            continue
        for d, v in points:
            new.append({"date": d.isoformat(), "metric_id": m["id"], "source_key": source_key(m["source"]),
                        "value": v, "collected_at": now_sgt().isoformat(), "note": stype})
        print(f"  {m['id']}: " + ", ".join(f"{d}={v:g}" for d, v in points))
    written, skipped = add_observations(cfg.path("observations"), new)
    print(f"collected {written} new observation(s) for {day}, {skipped} already present")
    for f in failed:
        print(f"  FAILED {f}", file=sys.stderr)
    return 1 if failed else 0


def cmd_record(cfg, args) -> int:
    lock = load_lock(cfg.path("lock"))
    matches = [m for m in _all_metrics(cfg, lock) if m["id"] == args.metric]
    if not matches:
        raise ConfigError(f"unknown metric {args.metric}")
    m = matches[0]
    if m["source"]["type"] != MANUAL:
        raise ConfigError(f"{args.metric} is collected automatically (source type {m['source']['type']})")
    obs = {"date": _d(args.date).isoformat(), "metric_id": m["id"], "source_key": source_key(m["source"]),
           "value": float(args.value), "collected_at": now_sgt().isoformat(), "note": args.note or "manual"}
    written, skipped = add_observations(cfg.path("observations"), [obs], replace=args.replace)
    if skipped:
        print(f"{args.metric} already has a value for {args.date}; pass --replace to overwrite")
        return 1
    print(f"recorded {args.metric}={args.value} for {args.date}")
    return 0


def cmd_lock_base(cfg, args) -> int:
    if load_lock(cfg.path("lock")) is not None:
        raise FormulaError("formula is already locked. Use `update-formula` to change it.")
    end = _d(args.base_end)
    window = cfg.definition["window_days"]
    start = _d(args.base_start) if args.base_start else end - timedelta(days=window - 1)
    length = (end - start).days + 1
    if length != window:
        print(f"WARNING: base period is {length} days but window_days is {window}; "
              f"launch-day level will not be exactly {cfg.definition['base_level']:g}")
    effective = _d(args.effective_date) if args.effective_date else end
    obs = observation_index(read_observations(cfg.path("observations")))
    lock = lock_base(cfg.definition, obs, start, end, effective, args.reason)
    v1 = lock["versions"][0]
    print(f"base period {start}..{end}, effective from {effective}")
    for mid, b in sorted(v1["spec"]["base"]["values"].items()):
        print(f"  B[{mid}] = {b:.6g}")
    if not args.yes and not _confirm("Lock formula v1? This is permanent."):
        print("aborted")
        return 1
    save_lock(lock, cfg.path("lock"), cfg.path("changelog_md"))
    print(f"locked formula v1 ({v1['spec_hash'][:12]})")
    return 0


def _meta(lock, rows) -> dict:
    latest = latest_version(lock)
    ok = [r for r in rows if r["status"] == STATUS_OK]
    return {
        "name": "Riftbound Hype Index",
        "site": "nexusnights.sg",
        "description": "Unbounded growth index of Riftbound TCG community and market interest. "
                       "Base period = 100.",
        "generated_at": now_sgt().isoformat(),
        "current_formula_version": latest["version"],
        "latest": ({"date": ok[-1]["date"], "index_level": float(ok[-1]["index_level"])} if ok else None),
        "versions": [{
            "version": v["version"], "mode": v["mode"], "effective_from": v["effective_from"],
            "formula_hash": v["spec_hash"], "base_period": [v["spec"]["base"]["start"], v["spec"]["base"]["end"]],
            "base_level": v["spec"]["definition"]["base_level"],
            "series_break": v["mode"] == MODE_REBASE,
        } for v in lock["versions"]],
    }


def _row(day: date, version: dict, result: dict) -> dict:
    return {
        "date": day.isoformat(),
        "index_level": "" if result["index_level"] is None else repr(result["index_level"]),
        "status": result["status"],
        "formula_version": str(version["version"]),
        "formula_hash": version["spec_hash"],
        "computed_at": now_sgt().isoformat(),
        "missing_metrics": ";".join(result["missing_metrics"]),
        "components_json": json.dumps(result["components"], sort_keys=True, separators=(",", ":")),
    }


def cmd_compute(cfg, args) -> int:
    lock = _require_lock(cfg)
    check_config_matches(lock, cfg.definition)
    if args.date:
        days = [_d(args.date)]
    elif args.from_date:
        start, end = _d(args.from_date), _d(args.to_date) if args.to_date else _yesterday()
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        days = [_yesterday()]

    obs = observation_index(read_observations(cfg.path("observations")))
    rows = {r["date"]: r for r in read_output(cfg.path("output_csv"))}
    for day in days:
        version = version_for_date(lock, day)
        if version is None:
            print(f"{day}: before formula v1 effective date, skipped")
            continue
        existing = rows.get(day.isoformat())
        if existing and existing["status"] == STATUS_OK:
            print(f"{day}: already published ({existing['index_level']}, v{existing['formula_version']}), "
                  f"left unchanged")
            continue
        result = compute_day(version["spec"], obs, day)
        rows[day.isoformat()] = _row(day, version, result)
        if result["status"] == STATUS_OK:
            print(f"{day}: {result['index_level']:.2f} (formula v{version['version']})")
        else:
            print(f"{day}: not published, insufficient data for {', '.join(result['missing_metrics'])}")
    all_rows = list(rows.values())
    write_output(cfg.path("output_csv"), cfg.path("output_json"), all_rows, _meta(lock, sorted(
        all_rows, key=lambda r: r["date"])))
    return 0


def cmd_update_formula(cfg, args) -> int:
    lock = _require_lock(cfg)
    last = _last_published(cfg)
    if args.effective_date:
        effective = _d(args.effective_date)
    else:
        effective = max(today_sgt(), (last + timedelta(days=1)) if last else today_sgt())
    obs = observation_index(read_observations(cfg.path("observations")))
    new, entry = plan_update(
        lock, cfg.definition, obs, mode=args.mode, effective_from=effective,
        reason=args.reason, summary=args.summary or "", last_published=last,
        base_start=_d(args.base_start) if args.base_start else None,
        base_end=_d(args.base_end) if args.base_end else None,
        accept_non_neutral=args.accept_non_neutral)

    print(f"formula v{entry['from_version']} -> v{entry['to_version']}  [{entry['mode'].upper()}]")
    print(f"effective from {entry['effective_from']}  (last published day: {last or 'none'})")
    print("changes:")
    for c in entry["changes"]:
        print(f"  - {c}")
    print(f"base period: {entry['base_period']['start']}..{entry['base_period']['end']}")
    for mid, b in sorted(new["spec"]["base"]["values"].items()):
        print(f"  B[{mid}] = {b:.6g}")
    if entry["neutrality_check"]:
        print(f"neutrality check: {json.dumps(entry['neutrality_check'])}")
    print(f"reason: {entry['reason']}")
    if not args.yes and not _confirm(f"Record formula v{entry['to_version']}?"):
        print("aborted")
        return 1
    lock["versions"].append(new)
    lock["changelog"].append(entry)
    save_lock(lock, cfg.path("lock"), cfg.path("changelog_md"))
    print(f"recorded formula v{new['version']} ({new['spec_hash'][:12]}); see {cfg.paths['changelog_md']}")
    return 0


def cmd_verify(cfg, args) -> int:
    """Recompute every published row from raw observations and check it matches."""
    lock = _require_lock(cfg)
    problems = []
    try:
        check_config_matches(lock, cfg.definition)
    except FormulaError as e:
        problems.append(str(e))
    by_number = {v["version"]: v for v in lock["versions"]}
    obs = observation_index(read_observations(cfg.path("observations")))
    rows = read_output(cfg.path("output_csv"))
    for r in rows:
        day = _d(r["date"])
        expected = version_for_date(lock, day)
        v = by_number.get(int(r["formula_version"]))
        if v is None or expected is None or v["version"] != expected["version"]:
            problems.append(f"{day}: row says v{r['formula_version']} but v"
                            f"{expected and expected['version']} is effective on that date")
            continue
        if r["formula_hash"] != v["spec_hash"]:
            problems.append(f"{day}: formula_hash does not match locked v{v['version']}")
            continue
        if r["status"] != STATUS_OK:
            continue
        result = compute_day(v["spec"], obs, day)
        if result["status"] != STATUS_OK or repr(result["index_level"]) != r["index_level"]:
            problems.append(f"{day}: published {r['index_level']} but recomputes to "
                            f"{result['index_level']} (observations changed after publication?)")
    for p in problems:
        print(f"FAIL {p}")
    print(f"verified {len(rows)} row(s) against {len(lock['versions'])} formula version(s): "
          f"{'OK' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


def cmd_status(cfg, args) -> int:
    lock = load_lock(cfg.path("lock"))
    if lock is None:
        print("formula: NOT LOCKED (pre-launch, collecting base-period data)")
    else:
        for v in lock["versions"]:
            print(f"formula v{v['version']} [{v['mode']}] effective {v['effective_from']}, "
                  f"base {v['spec']['base']['start']}..{v['spec']['base']['end']}, hash {v['spec_hash'][:12]}")
        try:
            check_config_matches(lock, cfg.definition)
            print("config.yaml: matches latest locked formula")
        except FormulaError as e:
            print(f"config.yaml: {e}")
    obs = observation_index(read_observations(cfg.path("observations")))
    for m in _all_metrics(cfg, lock):
        series = obs.get((m["id"], source_key(m["source"])), {})
        span = f"{min(series)}..{max(series)}" if series else "-"
        print(f"  {m['id']:<24} {len(series):>4} obs  {span}")
    rows = [r for r in read_output(cfg.path("output_csv")) if r["status"] == STATUS_OK]
    if rows:
        print(f"latest published: {rows[-1]['date']} = {float(rows[-1]['index_level']):.2f} "
              f"(v{rows[-1]['formula_version']})")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hype_index", description="Riftbound Hype Index")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("collect", help="fetch automatic metrics for a day (default: yesterday SGT)")
    s.add_argument("--date")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("record", help="record a manual metric value")
    s.add_argument("--metric", required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--value", required=True, type=float)
    s.add_argument("--note")
    s.add_argument("--replace", action="store_true")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("lock-base", help="freeze base values and lock formula v1 (once)")
    s.add_argument("--base-end", required=True)
    s.add_argument("--base-start", help="default: base-end minus window_days + 1")
    s.add_argument("--effective-date", help="first day the index is published (default: base-end)")
    s.add_argument("--reason", default="Initial launch")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_lock_base)

    s = sub.add_parser("compute", help="compute and publish index rows (default: yesterday SGT)")
    s.add_argument("--date")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")
    s.set_defaults(func=cmd_compute)

    s = sub.add_parser("update-formula", help="record config.yaml formula changes as a new version")
    s.add_argument("--mode", required=True, choices=[MODE_REBASE, MODE_CONTINUITY],
                   help="rebase: new index=100 period going forward; continuity: keep level (neutral changes only)")
    s.add_argument("--reason", required=True, help="why the formula is changing")
    s.add_argument("--summary", help="one-line description of what changed (default: auto diff)")
    s.add_argument("--base-start", help="rebase only: start of the new base period")
    s.add_argument("--base-end", help="rebase only: end of the new base period")
    s.add_argument("--effective-date", help="default: day after the last published row, or today")
    s.add_argument("--accept-non-neutral", action="store_true",
                   help="continuity only: record even though the neutrality check failed")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_update_formula)

    s = sub.add_parser("verify", help="recompute all published rows and check nothing drifted")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("status", help="show formula versions and data coverage")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        return args.func(cfg, args)
    except (ConfigError, FormulaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
