import copy
import json
import unittest
from pathlib import Path
from app import inspect
from records import load


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(load(Path(__file__).parents[1] / "fixtures/data.json"))
        self.day = "2026-11-10"

    def test_end_to_end_inspection_is_json_and_repeatable(self):
        before = copy.deepcopy(self.data)
        first = inspect(self.data, self.day)
        self.assertEqual(first, inspect(self.data, self.day))
        self.assertEqual(before, self.data)
        self.assertTrue(json.dumps(first, ensure_ascii=False, allow_nan=False))

    def test_mixed_lineage_conserves_share(self):
        from lineage import sources
        self.assertEqual(sources(self.data, "MIX"), {"ROOT-A": .75, "ROOT-B": .25})

    def test_common_source_is_not_lost(self):
        from journeys import route_exposure
        rows = route_exposure(self.data, self.day)
        self.assertIn("ROOT-A", rows["short"]["root_kg"])
        self.assertIn("ROOT-A", rows["long"]["root_kg"])

    def test_age_uses_arrival_not_recording(self):
        from inspections import observations
        rows = {row["check_id"]: row for row in observations(self.data, self.day)}
        self.assertEqual(rows["I2"]["age_days"], 5)

    def test_last_followup_is_not_repeated(self):
        from inspections import followup_status
        self.assertEqual(followup_status(self.data, self.day)["S2"]["last_age_days"], 3)

    def test_mass_violation_rejected(self):
        from lineage import sources
        self.data["lots"][4]["kg"] = 81
        with self.assertRaises(ValueError):
            sources(self.data, "MIX")

    def test_cycle_rejected(self):
        from lineage import sources
        self.data["lots"][0]["parents"] = [{"lot_id": "ROOT-A", "kg": 100}]
        with self.assertRaises(ValueError):
            sources(self.data, "ROOT-A")
