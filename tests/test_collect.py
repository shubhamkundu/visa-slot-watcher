"""Run with: python -m unittest discover tests"""
import datetime as dt
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UTC = dt.timezone.utc


def fresh_collect(tmp: str):
    with mock.patch.dict(os.environ, {"DATA_DIR": tmp, "GITHUB_TOKEN": ""}):
        import collect
        return importlib.reload(collect)


def row(location, earliest, last_seen):
    return {"location": location, "earliest": dt.date.fromisoformat(earliest), "slots": 1, "total": 2,
            "last_seen": last_seen, "age_hours": 0.1}


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.c = fresh_collect(self.tmp)

    def test_parse_last_seen_is_utc(self):
        got = self.c.parse_last_seen("16 Oct 2026, 03:56 AM")
        self.assertEqual(got, dt.datetime(2026, 10, 16, 3, 56, tzinfo=UTC))
        self.assertEqual(self.c.parse_last_seen("19 Oct 2026, 05:35 PM").hour, 17)
        self.assertIsNone(self.c.parse_last_seen("N/A"))

    def test_record_dedupes_and_writes_jsonl(self):
        state = {}
        rows = [row("MUMBAI", "2026-10-26", "16 Oct 2026, 03:56 AM"),
                row("MUMBAI VAC", "2026-10-23", "19 Oct 2026, 05:35 PM")]
        first = self.c.record("j-1-regular", rows, state)
        again = self.c.record("j-1-regular", rows, state)
        self.assertEqual(len(first), 2)
        self.assertEqual(again, [])
        lines = [json.loads(l) for l in (Path(self.tmp) / "sightings.jsonl").read_text().splitlines()]
        self.assertEqual(lines[0]["type"], "J-1")
        self.assertEqual(lines[1]["kind"], "vac")
        self.assertEqual(lines[1]["city"], "MUMBAI")

    def _sighting(self, minute, city, typ="F-1"):
        t = dt.datetime(2026, 10, 21, 12, minute, tzinfo=UTC)
        return {"type": typ, "city": city, "kind": "interview", "earliest": "2026-11-02", "seen_utc": t.isoformat()}

    def test_single_reporter_many_cities_is_not_a_burst(self):
        sent, state = [], {}
        now = dt.datetime(2026, 10, 21, 12, 10, tzinfo=UTC)
        batch = [self._sighting(5, c) for c in ("MUMBAI", "HYDERABAD", "NEW DELHI")]
        self.assertFalse(self.c.check_burst(batch, state, lambda *a: sent.append(a), now))
        self.assertEqual(sent, [])

    def test_independent_reports_trigger_once_then_cool_down(self):
        sent, state = [], {}
        now = dt.datetime(2026, 10, 21, 12, 20, tzinfo=UTC)
        self.assertFalse(self.c.check_burst([self._sighting(2, "MUMBAI")], state, lambda *a: sent.append(a), now))
        fired = self.c.check_burst([self._sighting(15, "MUMBAI", "J-2")], state, lambda *a: sent.append(a), now)
        self.assertTrue(fired)
        self.assertIn("Check now", sent[0][0])
        later = now + dt.timedelta(minutes=10)
        self.assertFalse(self.c.check_burst([self._sighting(25, "KOLKATA")], state, lambda *a: sent.append(a), later))
        self.assertEqual(len(sent), 1)

    def test_peak_window_once_per_hour_and_only_when_ready(self):
        sent, state = [], {}
        now = dt.datetime(2026, 10, 21, 14, 3, tzinfo=UTC)
        model = {"ready": False, "windows": [{"start_utc_hour": 14, "label": "10 AM ET"}]}
        self.c.MODEL_FILE = Path(self.tmp) / "model.json"
        self.c.MODEL_FILE.write_text(json.dumps(model))
        self.assertFalse(self.c.check_peak_window(state, lambda *a: sent.append(a), now))
        model["ready"] = True
        self.c.MODEL_FILE.write_text(json.dumps(model))
        self.assertTrue(self.c.check_peak_window(state, lambda *a: sent.append(a), now))
        self.assertFalse(self.c.check_peak_window(state, lambda *a: sent.append(a), now + dt.timedelta(minutes=5)))
        self.assertEqual(len(sent), 1)

    def test_sync_needs_token(self):
        self.assertTrue(self.c.maybe_sync({}).startswith("skipped"))


if __name__ == "__main__":
    unittest.main()
