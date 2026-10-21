"""Run with: python -m unittest discover tests"""
import datetime as dt
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model  # noqa: E402

UTC = dt.timezone.utc


def simulate(days=5, hot_hours=(4, 10), seed=1):
    rng = random.Random(seed)
    start = dt.datetime(2026, 10, 21, tzinfo=UTC)
    polls, sightings = [], []
    t = start
    while t < start + dt.timedelta(days=days):
        polls.append({"t": t.isoformat(), "ok": True})
        t += dt.timedelta(minutes=5)
    for d in range(days):
        for h in range(24):
            n = 4 if h in hot_hours else (1 if rng.random() < 0.3 else 0)
            for i in range(n):
                seen = start + dt.timedelta(days=d, hours=h, minutes=rng.randrange(60))
                typ = rng.choice(["F-1", "F-1", "F-2", "J-1", "J-2"])
                city = rng.choice(["MUMBAI", "NEW DELHI", "CHENNAI"])
                sightings.append({"type": typ, "location": city, "city": city, "kind": "interview",
                                  "earliest": "2026-11-12", "seen_utc": seen.isoformat(), "src": "phone"})
    return polls, sightings, start + dt.timedelta(days=days)


class ModelTests(unittest.TestCase):
    def test_finds_hot_hours_and_is_ready(self):
        polls, sightings, now = simulate()
        m, d = model.build(sightings, polls, now)
        self.assertTrue(m["ready"])
        hours = {w["start_utc_hour"] for w in m["windows"]}
        self.assertTrue({4, 10} <= hours, hours)
        self.assertGreater(max(w["lift"] for w in m["windows"]), 1.5)
        self.assertEqual(d["meta"]["covered_hours"], 120)

    def test_downtime_does_not_count_as_quiet(self):
        polls, sightings, now = simulate()
        # drop all coverage for UTC hour 4 -> its sightings are not "timed"
        polls = [p for p in polls if dt.datetime.fromisoformat(p["t"]).hour not in (4, 5)]
        m, d = model.build(sightings, polls, now)
        self.assertEqual(d["cover_hour"][4], 0)
        self.assertEqual(d["by_hour"]["FJ"]["n"][4], 0)

    def test_no_odds_without_coverage(self):
        m, d = model.build([], [], dt.datetime(2026, 10, 21, tzinfo=UTC))
        self.assertFalse(m["ready"])
        self.assertTrue(all(o["j2_p7"] is None for o in d["odds"]))
        self.assertEqual(m["windows"], [])

    def test_odds_increase_with_later_targets(self):
        polls, sightings, now = simulate()
        _, d = model.build(sightings, polls, now)
        ps = [o["j2_p7"] for o in d["odds"]]
        self.assertTrue(all(p is not None for p in ps))
        self.assertEqual(ps, sorted(ps))


if __name__ == "__main__":
    unittest.main()
