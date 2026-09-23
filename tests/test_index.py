import copy
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import yaml

from hype_index.cli import main
from hype_index.config import ConfigError, normalize_definition, source_key
from hype_index.formula import STATUS_INSUFFICIENT, STATUS_OK, compute_day
from hype_index.store import add_observations, read_output
from hype_index.versioning import FormulaError, lock_base

D0 = date(2026, 1, 1)
DEF = {
    "base_level": 100,
    "window_days": 7,
    "combination": "weighted_arithmetic",
    "metrics": [
        {"id": "a", "weight": 0.6, "min_observations": 5, "source": {"type": "manual", "n": "a"}},
        {"id": "b", "weight": 0.4, "min_observations": 5, "source": {"type": "manual", "n": "b"}},
    ],
}


def obs_for(definition, values_by_metric):
    out = {}
    for m in normalize_definition(definition)["metrics"]:
        out[(m["id"], source_key(m["source"]))] = values_by_metric[m["id"]]
    return out


def constant(value, days, start=D0):
    return {start + timedelta(days=i): float(value) for i in range(days)}


class FormulaTests(unittest.TestCase):
    def setUp(self):
        self.definition = normalize_definition(DEF)

    def spec(self, obs):
        lock = lock_base(self.definition, obs, D0, D0 + timedelta(days=6), D0 + timedelta(days=6), "t")
        return lock["versions"][0]["spec"]

    def test_launch_day_is_exactly_base(self):
        obs = obs_for(DEF, {"a": constant(10, 7), "b": constant(50, 7)})
        self.assertEqual(compute_day(self.spec(obs), obs, D0 + timedelta(days=6))["index_level"], 100.0)

    def test_unbounded_growth(self):
        a = constant(10, 7)
        a.update(constant(1000, 7, D0 + timedelta(days=7)))   # 100x
        b = constant(50, 14)
        obs = obs_for(DEF, {"a": a, "b": b})
        r = compute_day(self.spec(obs), obs, D0 + timedelta(days=13))
        self.assertEqual(r["status"], STATUS_OK)
        self.assertAlmostEqual(r["index_level"], 100 * (0.6 * 100 + 0.4 * 1), places=6)  # 6040, no cap

    def test_decline_below_base(self):
        a = constant(10, 7)
        a.update(constant(5, 7, D0 + timedelta(days=7)))
        b = constant(50, 7)
        b.update(constant(25, 7, D0 + timedelta(days=7)))
        obs = obs_for(DEF, {"a": a, "b": b})
        self.assertEqual(compute_day(self.spec(obs), obs, D0 + timedelta(days=13))["index_level"], 50.0)

    def test_insufficient_data_is_not_published_and_weights_not_redistributed(self):
        a = constant(10, 14)
        b = constant(50, 7)  # b stops reporting
        obs = obs_for(DEF, {"a": a, "b": b})
        r = compute_day(self.spec(obs), obs, D0 + timedelta(days=13))
        self.assertEqual(r["status"], STATUS_INSUFFICIENT)
        self.assertIsNone(r["index_level"])
        self.assertEqual(r["missing_metrics"], ["b"])

    def test_geometric(self):
        d = dict(DEF, combination="weighted_geometric")
        definition = normalize_definition(d)
        a = constant(10, 7)
        a.update(constant(40, 7, D0 + timedelta(days=7)))
        obs = obs_for(d, {"a": a, "b": constant(50, 14)})
        spec = lock_base(definition, obs, D0, D0 + timedelta(days=6), D0 + timedelta(days=6), "t")["versions"][0]["spec"]
        self.assertAlmostEqual(compute_day(spec, obs, D0 + timedelta(days=13))["index_level"], 100 * 4 ** 0.6, places=5)

    def test_deterministic(self):
        obs = obs_for(DEF, {"a": constant(10, 30), "b": constant(50, 30)})
        spec = self.spec(obs)
        self.assertEqual(compute_day(spec, obs, D0 + timedelta(days=20)),
                         compute_day(copy.deepcopy(spec), copy.deepcopy(obs), D0 + timedelta(days=20)))

    def test_swapped_source_is_not_mixed(self):
        obs = obs_for(DEF, {"a": constant(10, 7), "b": constant(50, 7)})
        swapped = copy.deepcopy(DEF)
        swapped["metrics"][0]["source"] = {"type": "manual", "n": "other"}
        with self.assertRaises(FormulaError):
            lock_base(normalize_definition(swapped), obs, D0, D0 + timedelta(days=6), D0 + timedelta(days=6), "t")

    def test_weights_must_sum_to_one(self):
        bad = copy.deepcopy(DEF)
        bad["metrics"][0]["weight"] = 0.5
        with self.assertRaises(ConfigError):
            normalize_definition(bad)


class CliTests(unittest.TestCase):
    """End-to-end: lock, compute, drift refusal, update-formula (rebase + continuity), verify."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.yaml"
        self.write_config(DEF)
        rows = []
        for mid, val in (("a", 10), ("b", 50), ("c", 7)):
            src = {"type": "manual", "n": mid}
            for i in range(40):
                rows.append({"date": (D0 + timedelta(days=i)).isoformat(), "metric_id": mid,
                             "source_key": source_key(src), "value": val * (1 + i / 100),
                             "collected_at": "x", "note": ""})
        add_observations(self.dir / "data/observations.csv", rows)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_config(self, definition):
        self.cfg_path.write_text(yaml.safe_dump({"formula": definition}))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.cfg_path), *args])
        return code, out.getvalue() + err.getvalue()

    def rows(self):
        return read_output(self.dir / "output/hype_index.csv")

    def test_full_lifecycle(self):
        code, out = self.run_cli("compute", "--date", "2026-01-10")
        self.assertEqual(code, 2, out)
        self.assertIn("not locked", out)

        self.assertEqual(self.run_cli("lock-base", "--base-end", "2026-01-07", "--yes")[0], 0)
        self.assertEqual(self.run_cli("lock-base", "--base-end", "2026-01-07", "--yes")[0], 2)

        code, out = self.run_cli("compute", "--from", "2026-01-07", "--to", "2026-01-20")
        self.assertEqual(code, 0, out)
        rows = self.rows()
        self.assertEqual(float(rows[0]["index_level"]), 100.0)
        self.assertTrue(all(r["formula_version"] == "1" for r in rows))
        self.assertGreater(float(rows[-1]["index_level"]), 100)

        # Silent formula change is refused.
        changed = copy.deepcopy(DEF)
        changed["metrics"][0]["weight"], changed["metrics"][1]["weight"] = 0.5, 0.5
        self.write_config(changed)
        code, out = self.run_cli("compute", "--date", "2026-01-21")
        self.assertEqual(code, 2)
        self.assertIn("a.weight: 0.6 -> 0.5", out)
        self.assertEqual(self.run_cli("verify")[0], 1)

        # Effective date cannot overlap published rows.
        code, out = self.run_cli("update-formula", "--mode", "rebase", "--reason", "r",
                                 "--base-start", "2026-01-14", "--base-end", "2026-01-20",
                                 "--effective-date", "2026-01-20", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("last published day", out)

        # Rebase: new version, new base, 100 again at the new base.
        code, out = self.run_cli("update-formula", "--mode", "rebase", "--reason", "rebalance",
                                 "--base-start", "2026-01-15", "--base-end", "2026-01-21",
                                 "--effective-date", "2026-01-21", "--yes")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("compute", "--from", "2026-01-18", "--to", "2026-01-25")
        self.assertEqual(code, 0, out)
        rows = {r["date"]: r for r in self.rows()}
        self.assertEqual(rows["2026-01-20"]["formula_version"], "1")   # untouched
        self.assertEqual(rows["2026-01-21"]["formula_version"], "2")
        self.assertEqual(float(rows["2026-01-21"]["index_level"]), 100.0)
        self.assertNotEqual(rows["2026-01-20"]["formula_hash"], rows["2026-01-21"]["formula_hash"])

        # Continuity for a non-neutral change is refused without explicit acceptance.
        added = copy.deepcopy(changed)
        added["metrics"] = [
            {"id": "a", "weight": 0.4, "min_observations": 5, "source": {"type": "manual", "n": "a"}},
            {"id": "b", "weight": 0.4, "min_observations": 5, "source": {"type": "manual", "n": "b"}},
            {"id": "c", "weight": 0.2, "min_observations": 5, "source": {"type": "manual", "n": "c"}},
        ]
        self.write_config(added)
        code, out = self.run_cli("update-formula", "--mode", "continuity", "--reason", "add c", "--yes",
                                 "--effective-date", "2026-01-26")
        self.assertEqual(code, 0, out)  # all metrics grow at the same rate -> neutral
        self.assertIn('"neutral": true', out)

        self.assertEqual(self.run_cli("compute", "--from", "2026-01-26", "--to", "2026-01-30")[0], 0)
        code, out = self.run_cli("verify")
        self.assertEqual(code, 0, out)

        lock = json.loads((self.dir / "formula/lock.json").read_text())
        self.assertEqual([v["version"] for v in lock["versions"]], [1, 2, 3])
        self.assertEqual([e["mode"] for e in lock["changelog"]], ["initial", "rebase", "continuity"])
        md = (self.dir / "formula/CHANGELOG.md").read_text()
        self.assertIn("RE-BASE", md)
        self.assertIn("rebalance", md)

        data = json.loads((self.dir / "output/hype_index.json").read_text())
        self.assertEqual(data["meta"]["current_formula_version"], 3)
        self.assertTrue(data["meta"]["versions"][1]["series_break"])

    def test_non_neutral_continuity_refused(self):
        self.run_cli("lock-base", "--base-end", "2026-01-07", "--yes")
        self.run_cli("compute", "--from", "2026-01-07", "--to", "2026-01-30")
        changed = copy.deepcopy(DEF)
        changed["window_days"] = 3   # shorter window reacts faster to growth -> not neutral
        changed["metrics"][0]["min_observations"] = 3
        changed["metrics"][1]["min_observations"] = 3
        self.write_config(changed)
        code, out = self.run_cli("update-formula", "--mode", "continuity", "--reason", "x", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("neutrality check failed", out)

    def test_tampered_observations_detected(self):
        self.run_cli("lock-base", "--base-end", "2026-01-07", "--yes")
        self.run_cli("compute", "--from", "2026-01-07", "--to", "2026-01-12")
        add_observations(self.dir / "data/observations.csv", [{
            "date": "2026-01-10", "metric_id": "a", "source_key": source_key({"type": "manual", "n": "a"}),
            "value": 999, "collected_at": "x", "note": ""}], replace=True)
        code, out = self.run_cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("recomputes to", out)

    def test_edited_lock_detected(self):
        self.run_cli("lock-base", "--base-end", "2026-01-07", "--yes")
        path = self.dir / "formula/lock.json"
        lock = json.loads(path.read_text())
        lock["versions"][0]["spec"]["definition"]["window_days"] = 14
        path.write_text(json.dumps(lock))
        code, out = self.run_cli("compute", "--date", "2026-01-10")
        self.assertEqual(code, 2)
        self.assertIn("edited by hand", out)


if __name__ == "__main__":
    unittest.main()
