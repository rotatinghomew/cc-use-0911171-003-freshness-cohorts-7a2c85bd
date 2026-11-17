import copy
import json
import tempfile
import unittest
from pathlib import Path

from advisories import (advisory_history, affected_advisories,
                        issue_advisories, load_index, withdraw_advisory)
from records import load
from shelflife import DEFAULT_POLICY

FIXTURE = Path(__file__).parents[1] / "fixtures/data.json"
DAY = "2026-11-10"
LATER = "2026-11-12"


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.data = load(FIXTURE)
        self.policy = copy.deepcopy(DEFAULT_POLICY)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_creates_series_and_reissue_is_idempotent(self):
        first = issue_advisories(self.store, self.data, self.policy, DAY)
        self.assertTrue(first)
        routes = {row["route"] for row in first}
        self.assertEqual(routes, {"short", "long"})
        self.assertTrue(all(row["version"] == 1 for row in first))
        files = list((self.store / "advisories").glob("*.json"))
        self.assertEqual(len(files), len(first))

        again = issue_advisories(self.store, self.data, self.policy, DAY)
        self.assertEqual(again, [])
        self.assertEqual(list((self.store / "advisories").glob("*.json")), files)

    def test_new_followup_triggers_new_version_but_keeps_history(self):
        issue_advisories(self.store, self.data, self.policy, DAY)
        index_before = load_index(self.store)
        series_before = {e["series_id"]: e for e in index_before["advisories"]}

        self.data["checks"].append({
            "check_id": "LATE1", "shipment_id": "S3", "observed_on": "2026-11-11",
            "recorded_at": "2026-11-11", "kind": "followup", "state": "fail", "kg": 10})
        short_series = next(entry for entry in series_before.values()
                            if entry["route"] == "short")
        issued = issue_advisories(self.store, self.data, self.policy, LATER)
        self.assertEqual([row["route"] for row in issued], ["short"])
        self.assertEqual(issued[0]["version"], short_series["version"] + 1)

        history = advisory_history(self.store, short_series["series_id"])
        self.assertEqual([row["version"] for row in history], [1, 2])
        # v1 文件保留发布当时看到的证据，不被改写。
        self.assertEqual(history[0]["issued_as_of"], DAY)
        self.assertNotIn("LATE1", json.dumps(history[0], ensure_ascii=False))
        self.assertIn("LATE1", json.dumps(history[1], ensure_ascii=False))

    def test_affected_advisories_detects_lineage_correction(self):
        issue_advisories(self.store, self.data, self.policy, DAY)
        self.assertEqual(affected_advisories(self.store, self.data, DAY), [])

        # 改正拆箱关系：MIX 里 ROOT-B 的投入改为 30kg（同时 A1 改为 50 保持守恒）。
        mix = next(lot for lot in self.data["lots"] if lot["lot_id"] == "MIX")
        mix["parents"] = [{"lot_id": "A1", "kg": 50}, {"lot_id": "ROOT-B", "kg": 30}]
        a1 = next(lot for lot in self.data["lots"] if lot["lot_id"] == "A1")
        a1["kg"] = 50
        a1["parents"] = [{"lot_id": "ROOT-A", "kg": 50}]

        findings = affected_advisories(self.store, self.data, DAY)
        affected_routes = {f["index_entry"]["route"] for f in findings}
        self.assertIn("long", affected_routes)  # S1(MIX) 走 long 路线
        changed = {entity for f in findings for entity in f["changed_entities"]}
        self.assertIn("lot:MIX", changed)
        self.assertIn("lot:A1", changed)
        # 只改正拆箱关系：没有检查记录被改动。
        self.assertFalse(any(entity.startswith("check:") for entity in changed))

        # 重新评估后长路线建议续版，旧版仍可追溯。
        reissued = issue_advisories(self.store, self.data, self.policy, DAY)
        self.assertTrue(any(row["route"] == "long" and row["version"] == 2
                            for row in reissued))

    def test_withdrawal_marks_index_but_keeps_files(self):
        issued = issue_advisories(self.store, self.data, self.policy, DAY)
        series_id = issued[0]["series_id"]
        files_before = list((self.store / "advisories").glob("*.json"))
        withdraw_advisory(self.store, series_id, LATER)
        entry = next(e for e in load_index(self.store)["advisories"]
                     if e["series_id"] == series_id)
        self.assertEqual(entry["status"], "withdrawn")
        self.assertEqual(list((self.store / "advisories").glob("*.json")), files_before)
        # 已撤回的系列不再作为“仍在使用”受影响建议出现。
        self.assertEqual(affected_advisories(self.store, self.data, DAY), [])

    def test_point_in_time_ignores_future_records(self):
        issue_advisories(self.store, self.data, self.policy, DAY)
        future_data = copy.deepcopy(self.data)
        future_data["checks"].append({
            "check_id": "FUTURE", "shipment_id": "S3", "observed_on": "2026-11-11",
            "recorded_at": "2026-11-11", "kind": "followup", "state": "fail", "kg": 5})
        # 在旧时点重新评估：未来记录不可见，不产生新版本。
        self.assertEqual(issue_advisories(self.store, future_data, self.policy, DAY), [])
        # 到新时点后才定位到变化。
        findings = affected_advisories(self.store, future_data, LATER)
        # 新检查在发布时点尚不存在：以 added_entities 形式暴露证据增量，
        # 业务据此知道需要重新评估并补发新版本。
        self.assertTrue(any("check:FUTURE" in f["added_entities"]
                            for f in findings))

    def test_immutable_file_cannot_be_overwritten(self):
        # 已发出版本由建议层保护：同系列同版本再次落盘直接拒绝。
        issued = issue_advisories(self.store, self.data, self.policy, DAY)
        with self.assertRaises(ValueError):
            from advisories import _write_advisory
            _write_advisory(self.store, issued[0])
        target = self.store / "advisories" / f"{issued[0]['series_id']}-v1.json"
        self.assertTrue(target.exists())

    def test_snapshot_contains_scope_and_dependencies(self):
        issued = issue_advisories(self.store, self.data, self.policy, DAY)
        record = issued[0]
        self.assertTrue(record["scope"]["shipment_ids"])
        self.assertTrue(record["dependency_fingerprints"])
        self.assertIn("analysis", record)
        self.assertTrue(all(key.startswith(("lot:", "shipment:", "check:"))
                            for key in record["dependency_fingerprints"]))


if __name__ == "__main__":
    unittest.main()
