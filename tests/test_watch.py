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

# Mirrors the live checkvisaslots table layout (columns and date formats as of Oct 2026).
FIXTURE = """
<html><body>
<p>Generated 15 Oct 2026</p>
<table>
  <tr><th>Visa Location</th><th>Visa Type</th><th>Earliest Date</th><th>Slots on Earliest Date</th>
      <th>Total Dates Available</th><th>Last Seen At</th><th>Relative Time</th></tr>
  <tr><td>MUMBAI</td><td>J-2 (Regular)</td><td><a href="#">20 Nov, 26</a></td><td>3</td><td>2</td>
      <td>13 Oct 2026, 03:04 PM</td><td>2 hrs 10 mins ago</td></tr>
  <tr><td>MUMBAI VAC</td><td>J-2 (Regular)</td><td><a href="#">03 Nov, 26</a></td><td>5</td><td>8</td>
      <td>13 Oct 2026, 11:55 AM</td><td>5 hrs ago</td></tr>
  <tr><td>HYDERABAD</td><td>J-2 (Regular)</td><td><a href="#">03 Dec, 26</a></td><td>1</td><td>2</td>
      <td>08 Oct 2026, 07:25 PM</td><td>6 hrs ago</td></tr>
  <tr><td>KOLKATA VAC</td><td>J-2 (Regular)</td><td><a href="#">14 Oct, 26</a></td><td>1</td><td>1</td>
      <td>12 Oct 2026, 07:05 AM</td><td>3 hrs ago</td></tr>
  <tr><td>NEW DELHI</td><td>J-2 (Regular)</td><td><a href="#">10 Nov, 26</a></td><td>1</td><td>1</td>
      <td>22 Sep 2026, 06:13 AM</td><td>3 wks ago</td></tr>
</table></body></html>
"""

TODAY = dt.date(2026, 10, 15)


def load(env: dict):
    with mock.patch.dict(os.environ, env, clear=False):
        import watch
        return importlib.reload(watch)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.w = load({"CITIES": "", "CUTOFF_DATE": "2026-11-20"})

    def test_parses_all_rows(self):
        rows = self.w.parse_page(FIXTURE)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["location"], "MUMBAI")
        self.assertEqual(rows[0]["earliest"], dt.date(2026, 11, 20))
        self.assertEqual(rows[0]["total"], 2)
        self.assertAlmostEqual(rows[0]["age_hours"], 2 + 10 / 60)

    def test_date_formats(self):
        for text, want in [("03 Dec, 26", dt.date(2026, 12, 3)), ("03 Dec 2026", dt.date(2026, 12, 3)),
                           ("5 Jan, 27", dt.date(2027, 1, 5)), ("nothing", None)]:
            self.assertEqual(self.w.parse_date(text), want, text)

    def test_age_parsing(self):
        self.assertEqual(self.w.parse_age_hours("1 wk ago"), 168)
        self.assertEqual(self.w.parse_age_hours("1 day ago"), 24)
        self.assertEqual(self.w.parse_age_hours("just now"), 0)
        self.assertIsNone(self.w.parse_age_hours(""))

    def test_filtering(self):
        rows = {r["location"]: r for r in self.w.parse_page(FIXTURE)}
        self.assertTrue(self.w.matches(rows["MUMBAI"], TODAY))       # before cutoff, fresh
        self.assertTrue(self.w.matches(rows["MUMBAI VAC"], TODAY))
        self.assertFalse(self.w.matches(rows["HYDERABAD"], TODAY))   # after cutoff
        self.assertFalse(self.w.matches(rows["KOLKATA VAC"], TODAY)) # date already passed
        self.assertFalse(self.w.matches(rows["NEW DELHI"], TODAY))   # sighting 3 weeks old

    def test_no_cutoff_by_default(self):
        w = load({"CUTOFF_DATE": ""})
        rows = {r["location"]: r for r in w.parse_page(FIXTURE)}
        self.assertIsNone(w.CUTOFF_DATE)
        self.assertTrue(w.matches(rows["HYDERABAD"], TODAY))     # Dec date now allowed
        self.assertFalse(w.matches(rows["KOLKATA VAC"], TODAY))  # past dates still skipped

    def test_city_filter(self):
        w = load({"CITIES": "Chennai, Hyderabad"})
        rows = {r["location"]: r for r in w.parse_page(FIXTURE)}
        self.assertFalse(w.matches(rows["MUMBAI"], TODAY))


class RunTests(unittest.TestCase):
    def test_alerts_once_then_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            w = load({"STATE_FILE": str(state), "VISA_TYPES": "j-2-regular", "CITIES": "",
                      "CUTOFF_DATE": "2026-11-20", "NTFY_TOPIC": "x", "DRY_RUN": "false"})
            sent = []
            with mock.patch.object(w, "fetch", return_value=FIXTURE), \
                 mock.patch.object(w, "notify", side_effect=lambda *a: sent.append(a)):
                w.run_once(TODAY)
                self.assertEqual(len(sent), 1)
                title, body, priority, _ = sent[0]
                self.assertEqual(priority, 5)
                self.assertIn("Mumbai interview", body)
                self.assertIn("Mumbai biometrics", body)
                self.assertNotIn("Hyderabad", body)
                w.run_once(TODAY)  # same data again -> no second alert
                self.assertEqual(len(sent), 1)
            self.assertEqual(len(json.loads(state.read_text())["seen"]), 2)

    def test_health_warning_after_repeated_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = load({"STATE_FILE": str(Path(tmp) / "s.json"), "VISA_TYPES": "j-2-regular",
                      "HEALTH_ALERT_AFTER": "3", "NTFY_TOPIC": "x"})
            sent = []
            with mock.patch.object(w, "fetch", side_effect=RuntimeError("blocked")), \
                 mock.patch.object(w, "notify", side_effect=lambda *a: sent.append(a)):
                for _ in range(4):
                    w.run_once()
            self.assertEqual([s[0] for s in sent], ["Slot watcher is NOT working"])


if __name__ == "__main__":
    unittest.main()
