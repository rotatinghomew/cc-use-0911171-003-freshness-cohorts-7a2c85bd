import copy
import unittest
from pathlib import Path

from records import load
from shelflife import (DEFAULT_POLICY, build_evidence, evaluate, normal_ppf,
                       survival_at, turnbull, wilson_upper)

FIXTURE = Path(__file__).parents[1] / "fixtures/data.json"
DAY = "2026-11-10"


def policy(**overrides):
    value = copy.deepcopy(DEFAULT_POLICY)
    value.update(overrides)
    return value


def component_map(evidence, shipment_id):
    return [c for c in evidence["components"] if c["shipment_id"] == shipment_id]


class ShelfLifeTests(unittest.TestCase):
    def setUp(self):
        self.data = load(FIXTURE)

    def test_age_starts_at_arrival_not_recording(self):
        # I2 观察日 11-08、登记日 11-09；S1 到货 11-03，年龄必须为 5。
        evidence = build_evidence(self.data, DAY)
        failure = next(c for c in evidence["components"]
                       if c["shipment_id"] == "S1" and c["type"] == "failure")
        self.assertEqual((failure["l_days"], failure["r_days"]), (2, 5))

    def test_failure_observed_later_is_interval_censored(self):
        # S1：11-05 抽检合格（年龄2），11-08 部分退货（年龄5）。
        # 失效只能落在 (2, 5]，不得假装知道具体坏掉时刻。
        evidence = build_evidence(self.data, DAY)
        failures = [c for c in component_map(evidence, "S1") if c["type"] == "failure"]
        self.assertTrue(failures)
        for row in failures:
            self.assertEqual(row["l_days"], 2)
            self.assertEqual(row["r_days"], 5)

    def test_partial_return_keeps_residual_at_risk(self):
        # S7 共 37kg：合格随访后部分退货 7kg，其余 30kg 右删失仍在销售。
        evidence = build_evidence(self.data, DAY)
        rows = component_map(evidence, "S7")
        fail_kg = sum(c["kg"] for c in rows if c["type"] == "failure")
        censor_kg = sum(c["kg"] for c in rows if c["type"] == "censor")
        self.assertAlmostEqual(fail_kg, 7.0)
        self.assertAlmostEqual(censor_kg, 30.0)
        self.assertEqual({c["reason"] for c in rows if c["type"] == "censor"},
                         {"still_selling"})

    def test_full_return_exhausts_shipment(self):
        # S5 整箱退货 35kg：全部质量进入失效区间，没有右删失残值。
        evidence = build_evidence(self.data, DAY)
        rows = component_map(evidence, "S5")
        self.assertTrue(rows)
        self.assertEqual({c["type"] for c in rows}, {"failure"})
        self.assertAlmostEqual(sum(c["kg"] for c in rows), 35.0)
        # 先有 11-05 合格（年龄2），11-09 整箱退货（年龄6）：区间 (2, 6]。
        self.assertEqual({(c["l_days"], c["r_days"]) for c in rows}, {(2, 6)})

    def test_lost_contact_is_right_censored_not_failure(self):
        # S9 最后一条为 unknown 随访：失联不等于失效，证据是右删失。
        evidence = build_evidence(self.data, DAY)
        rows = component_map(evidence, "S9")
        self.assertTrue(rows)
        self.assertEqual({c["type"] for c in rows}, {"censor"})
        self.assertEqual({c["reason"] for c in rows}, {"lost_contact"})

    def test_unobserved_shipment_is_censored_at_zero(self):
        # 合成数据：一个到货后从未随访的运输，仍在风险集中，年龄 0 右删失。
        data = {
            "lots": [{"lot_id": "U1", "kg": 12, "parents": []}],
            "shipments": [{"shipment_id": "SU1", "lot_id": "U1", "kg": 12,
                           "route": "short", "arrived_on": "2026-11-03",
                           "recorded_at": "2026-11-03"}],
            "checks": [],
        }
        evidence = build_evidence(data, DAY)
        rows = evidence["components"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "censor")
        self.assertEqual(rows[0]["reason"], "unobserved")
        self.assertEqual(rows[0]["age_days"], 0)
        self.assertAlmostEqual(rows[0]["kg"], 12.0)

    def test_mixed_shipment_exposure_is_split_and_conserved(self):
        # S1 = MIX：ROOT-A 75% / ROOT-B 25%，每条证据按份额拆分且不重不漏。
        evidence = build_evidence(self.data, DAY)
        rows = component_map(evidence, "S1")
        by_root = {}
        for row in rows:
            by_root.setdefault(row["root_lot"], 0.0)
            by_root[row["root_lot"]] += row["kg"]
        self.assertAlmostEqual(by_root["ROOT-A"], 60.0)
        self.assertAlmostEqual(by_root["ROOT-B"], 20.0)
        # 全局守恒：全部证据质量等于全部到货质量。
        from journeys import arrivals
        expected = sum(row["kg"] for row in arrivals(self.data, DAY).values())
        self.assertAlmostEqual(sum(c["kg"] for c in evidence["components"]), expected)

    def test_same_source_on_two_routes_is_separate_not_duplicated(self):
        evidence = build_evidence(self.data, DAY)
        root_a = [c for c in evidence["components"] if c["root_lot"] == "ROOT-A"]
        routes = {c["route"] for c in root_a}
        self.assertEqual(routes, {"short", "long"})
        # ROOT-A 总暴露不超过其采收重量 100kg（A1 60 + A2 40）。
        self.assertLessEqual(sum(c["kg"] for c in root_a), 100.0 + 1e-9)
        # 没有同一物理质量被记两次：每个 (shipment, type) 对 ROOT-A 质量之和可对账。

    def test_turnbull_matches_known_step_solution(self):
        # 区间 (0,2] 1 单位 + (2,4] 1 单位：质量应分别落在网格 2 和 4。
        components = [
            {"type": "failure", "l_days": 0, "r_days": 2, "kg": 1.0},
            {"type": "failure", "l_days": 2, "r_days": 4, "kg": 1.0},
        ]
        curve = turnbull(components)
        self.assertEqual(curve["grid"], [2, 4])
        self.assertAlmostEqual(curve["mass"][0], 0.5)
        self.assertAlmostEqual(curve["mass"][1], 0.5)
        self.assertAlmostEqual(survival_at(curve, 2), 0.5)
        self.assertAlmostEqual(survival_at(curve, 3), 0.5)
        self.assertAlmostEqual(survival_at(curve, 4), 0.0)

    def test_turnbull_with_right_censoring(self):
        # 失效 (0,4] 一个；在年龄 2 右删失一个：年龄 4 处保留失效质量。
        components = [
            {"type": "failure", "l_days": 0, "r_days": 4, "kg": 1.0},
            {"type": "censor", "age_days": 2, "kg": 1.0},
        ]
        curve = turnbull(components)
        self.assertEqual(curve["grid"], [4])
        self.assertAlmostEqual(curve["mass"][0], 1.0)

    def test_wilson_and_normal_ppf(self):
        self.assertAlmostEqual(normal_ppf(0.975), 1.959964, places=5)
        # 0 失效、10 个有效样本：上界仍为正数，不会承诺零风险。
        bound = wilson_upper(0.0, 10.0, 0.95)
        self.assertGreater(bound, 0.0)
        self.assertLessEqual(bound, 1.0)
        self.assertGreater(wilson_upper(3.0, 10.0, 0.95), 0.3)

    def test_cluster_adjustment_reduces_effective_sample_size(self):
        # 三个 root lot 质量 10/10/100kg：Kish ESS 明显小于原始样本量。
        data = copy.deepcopy(self.data)
        data["lots"] = [
            {"lot_id": "X1", "kg": 10, "parents": []},
            {"lot_id": "X2", "kg": 10, "parents": []},
            {"lot_id": "X3", "kg": 100, "parents": []},
        ]
        data["shipments"] = [
            {"shipment_id": f"T{i}", "lot_id": f"X{i}", "kg": lot["kg"],
             "route": "r", "arrived_on": "2026-11-01", "recorded_at": "2026-11-01"}
            for i, lot in enumerate(data["lots"], start=1)
        ]
        data["checks"] = [
            {"check_id": f"P{i}", "shipment_id": f"T{i}", "observed_on": "2026-11-08",
             "recorded_at": "2026-11-08", "kind": "followup", "state": "pass",
             "kg": lot["kg"]}
            for i, lot in enumerate(data["lots"], start=1)
        ]
        report = evaluate(data, policy(), "2026-11-10")
        evidence = report["routes"]["r"]["evidence"]
        self.assertAlmostEqual(evidence["raw_cases"], 3.0)
        self.assertLess(evidence["effective_cases"], evidence["raw_cases"])
        # Kish ESS = 3^2 / (0.25^2+0.25^2+2.5^2)，与基准箱重的单位选择无关。
        self.assertAlmostEqual(evidence["effective_cases"], 9.0 / 6.375)

    def test_route_recommendation_has_scope_uncertainty_and_cap(self):
        report = evaluate(self.data, policy(), DAY)
        for route, view in report["routes"].items():
            if view["status"] == "recommended":
                commitment = view["commitment"]
                self.assertLessEqual(commitment["shelf_life_days"],
                                     view["evidence"]["max_evidence_age_days"])
                self.assertLessEqual(commitment["upper_spoilage_risk"],
                                     DEFAULT_POLICY["acceptable_spoilage_risk"])
                self.assertIn("method", view["uncertainty"])
                self.assertIn("root_lot_strata", view)
            else:
                self.assertIsNone(view["commitment"])
                self.assertTrue(view["recommendation_reasons"])

    def test_insufficient_evidence_does_not_recommend(self):
        custom = policy(min_distinct_root_lots=99, min_effective_cases=999.0)
        report = evaluate(self.data, custom, DAY)
        for view in report["routes"].values():
            self.assertEqual(view["status"], "insufficient_evidence")
            self.assertIsNone(view["commitment"])

    def test_risk_gate_can_force_do_not_recommend(self):
        # 把可接受风险压到极低：有充分证据但任何年龄都过不了风险门槛。
        report = evaluate(self.data, policy(acceptable_spoilage_risk=1e-6), DAY)
        statuses = {view["status"] for view in report["routes"].values()}
        self.assertIn("do_not_recommend", statuses)

    def test_deterministic_and_input_untouched(self):
        snapshot = copy.deepcopy(self.data)
        first = evaluate(self.data, policy(), DAY)
        second = evaluate(self.data, policy(), DAY)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, self.data)

    def test_bad_policy_and_bad_full_return_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(self.data, policy(acceptable_spoilage_risk=1.5), DAY)
        broken = copy.deepcopy(self.data)
        broken["checks"].append({
            "check_id": "BAD", "shipment_id": "S3", "observed_on": "2026-11-08",
            "recorded_at": "2026-11-08", "kind": "full_return", "state": "fail", "kg": 30})
        with self.assertRaises(ValueError):
            build_evidence(broken, DAY)

    def test_cumulative_partial_returns_beyond_shipment_rejected(self):
        broken = copy.deepcopy(self.data)
        broken["checks"].append({
            "check_id": "OVER", "shipment_id": "S7", "observed_on": "2026-11-10",
            "recorded_at": "2026-11-10", "kind": "partial_return", "state": "fail",
            "kg": 31})  # S7 已退 7kg，再退 31kg 超过 37kg 总量
        with self.assertRaises(ValueError):
            build_evidence(broken, DAY)

    def test_same_day_pass_and_fail_falls_back_to_point_interval(self):
        data = {
            "lots": [{"lot_id": "D1", "kg": 20, "parents": []}],
            "shipments": [{"shipment_id": "SD1", "lot_id": "D1", "kg": 20,
                           "route": "short", "arrived_on": "2026-11-01",
                           "recorded_at": "2026-11-01"}],
            "checks": [
                {"check_id": "DP", "shipment_id": "SD1", "observed_on": "2026-11-04",
                 "recorded_at": "2026-11-04", "kind": "inspection", "state": "pass",
                 "kg": 2},
                {"check_id": "DF", "shipment_id": "SD1", "observed_on": "2026-11-04",
                 "recorded_at": "2026-11-05", "kind": "partial_return", "state": "fail",
                 "kg": 5},
            ],
        }
        evidence = build_evidence(data, "2026-11-10")
        failure = next(c for c in evidence["components"] if c["type"] == "failure")
        self.assertEqual((failure["l_days"], failure["r_days"]), (3, 3))
        curve = turnbull(evidence["components"])
        self.assertEqual(curve["grid"], [3])

    def test_split_boxes_from_one_root_count_as_one_cluster(self):
        # 同一 root lot 拆成 4 箱：失败 2 箱不等于 2 次独立成功/失效，
        # 折减后该层只有 1 个独立来源，过不了 min_distinct_root_lots 门槛。
        data = {
            "lots": [{"lot_id": "CL1", "kg": 40, "parents": []}],
            "shipments": [
                {"shipment_id": f"K{i}", "lot_id": "CL1", "kg": 10, "route": "r",
                 "arrived_on": "2026-11-01", "recorded_at": "2026-11-01"}
                for i in range(1, 5)],
            "checks": [
                {"check_id": f"K{i}P", "shipment_id": f"K{i}",
                 "observed_on": "2026-11-07", "recorded_at": "2026-11-07",
                 "kind": "followup", "state": "pass", "kg": 10}
                for i in range(1, 5)],
        }
        report = evaluate(data, policy(min_distinct_root_lots=2), "2026-11-10")
        view = report["routes"]["r"]
        self.assertEqual(view["evidence"]["distinct_root_lots"], 1)
        self.assertEqual(view["status"], "insufficient_evidence")
        self.assertIn("min_distinct_root_lots", view["recommendation_reasons"])
        self.assertAlmostEqual(view["evidence"]["effective_cases"],
                               view["evidence"]["raw_cases"])

    def test_record_revision_respects_known_time(self):
        data = {
            "lots": [{"lot_id": "V1", "kg": 10, "parents": []}],
            "shipments": [{"shipment_id": "SV1", "lot_id": "V1", "kg": 10,
                           "route": "short", "arrived_on": "2026-11-01",
                           "recorded_at": "2026-11-01"}],
            "checks": [
                {"check_id": "VV", "shipment_id": "SV1", "observed_on": "2026-11-05",
                 "recorded_at": "2026-11-05", "kind": "followup", "state": "pass",
                 "kg": 10},
                {"check_id": "VV", "shipment_id": "SV1", "observed_on": "2026-11-05",
                 "recorded_at": "2026-11-09", "kind": "partial_return", "state": "fail",
                 "kg": 10, "revision": 2},
            ],
        }
        early = build_evidence(data, "2026-11-06")["components"]
        self.assertEqual({c["type"] for c in early}, {"censor"})  # 修订尚不可见
        later = build_evidence(data, "2026-11-10")["components"]
        self.assertEqual({c["type"] for c in later}, {"failure"})  # 修订生效


if __name__ == "__main__":
    unittest.main()
