"""货架期证据与运输条件建议。

口径要点：
- 年龄一律从实际到货日 ``arrived_on`` 起算，与登记时间 ``recorded_at`` 无关。
- 先正常、隔天才发现不合格时，只形成区间删失证据 ``(最后正常年龄, 发现年龄]``，
  不声称知道具体变质时刻；右删失（仍在销售、失联、未观察）不得从证据中消失。
- 整箱退货耗尽整箱质量；部分退货与抽检不合格只让涉及质量进入失效区间，
  其余质量继续作为右删失留在风险集中。
- 拼箱按血缘份额把每条证据拆给各来源，全局质量守恒：同一物理质量只出现一次，
  同一来源在不同路线上分别归入不同 (route, root) 层，不重复制造证据。
- 同一采收批次（root lot）拆出的多箱不独立，使用 Kish 有效样本量折减。

本模块只依赖标准库，输出全部为 JSON 可表示的值，且不修改输入。
"""
import json
import math
from datetime import date

from journeys import arrivals
from records import fingerprint, latest, quantity

KINDS = {"inspection", "partial_return", "full_return", "followup"}
STATES = {"pass", "fail", "unknown"}
# 右删失原因：观察正常后继续在架、失联、失效事件后仍有残值在架、从未随访。
CENSOR_REASONS = {"observed_ok", "lost_contact", "still_selling", "unobserved"}

DEFAULT_POLICY = {
    "policy_id": "default-2026-11",
    "acceptable_spoilage_risk": 0.10,
    "confidence_level": 0.95,
    "min_distinct_root_lots": 3,
    "min_effective_cases": 8.0,
    "reference_case_kg": 40.0,
    "min_observed_age_days": 5,
}

_TURNBULL_ITERATIONS = 10000
_TURNBULL_TOLERANCE = 1e-12


def default_policy():
    return dict(DEFAULT_POLICY)


def _age(observed_on, arrived_on):
    return (date.fromisoformat(observed_on) - date.fromisoformat(arrived_on)).days


def _validate_policy(policy):
    required = set(DEFAULT_POLICY)
    missing = required.difference(policy)
    if missing:
        raise ValueError(f"货架期政策缺少字段: {sorted(missing)}")
    if not 0.0 < policy["acceptable_spoilage_risk"] < 1.0:
        raise ValueError("可接受变质风险应在 (0, 1) 之间")
    if not 0.5 < policy["confidence_level"] < 1.0:
        raise ValueError("置信水平应在 (0.5, 1) 之间")
    if policy["min_distinct_root_lots"] < 1:
        raise ValueError("最低来源批次数应为正整数")
    if policy["min_effective_cases"] <= 0 or policy["reference_case_kg"] <= 0:
        raise ValueError("最低证据量与基准箱重应为正数")
    if policy["min_observed_age_days"] < 1:
        raise ValueError("最低观察年龄应为正整数")


def build_evidence(data, as_of):
    """把到货、血缘与售后检查展开为区间删失/右删失证据组件。

    每个组件带质量（kg），拼箱按来源份额拆分；单个 shipment 的失效质量与
    删失质量之和恒等于 shipment 重量（按来源份额展开后同样守恒）。
    """
    shipments = arrivals(data, as_of)
    checks_by_shipment = {identity: [] for identity in shipments}
    for row in latest(data["checks"], "check_id", as_of):
        if row["shipment_id"] in checks_by_shipment:
            checks_by_shipment[row["shipment_id"]].append(row)

    components = []
    for shipment_id in sorted(shipments):
        shipment = shipments[shipment_id]
        remaining = quantity(shipment["kg"])
        last_pass_age = 0
        last_contact_age = 0
        last_state = None
        # 同一天的合格观察必须先于失效观察处理，才能正确形成 (l, r] 区间。
        state_order = {"pass": 0, "unknown": 1, "fail": 2}
        for row in sorted(checks_by_shipment[shipment_id],
                          key=lambda item: (item["observed_on"],
                                            state_order[item["state"]],
                                            item["check_id"])):
            if row["kind"] not in KINDS:
                raise ValueError(f"未知售后观察类型: {row['kind']}")
            if row["state"] not in STATES:
                raise ValueError(f"未知质量检验状态: {row['state']}")
            age = _age(row["observed_on"], shipment["arrived_on"])
            if age < 0:
                raise ValueError("售后观察早于到货")
            check_kg = quantity(row["kg"])
            if check_kg > shipment["kg"]:
                raise ValueError("售后观察重量超出运输重量")

            if row["state"] == "pass":
                if row["kind"] == "full_return":
                    raise ValueError("整箱退货不能是合格状态")
                last_pass_age = max(last_pass_age, age)
                last_contact_age = max(last_contact_age, age)
                last_state = "pass"
                continue
            if row["state"] == "unknown":
                if row["kind"] == "full_return":
                    raise ValueError("整箱退货必须有明确不合格状态")
                last_contact_age = max(last_contact_age, age)
                last_state = "unknown"
                continue

            # state == fail：整箱退货作用于整箱，其余类型只作用于登记质量。
            if row["kind"] == "full_return":
                if check_kg != shipment["kg"]:
                    raise ValueError("整箱退货重量必须等于整箱运输重量")
                fail_kg = check_kg
            else:
                fail_kg = check_kg
            if fail_kg <= 0:
                raise ValueError("失效质量必须为正数")
            if fail_kg > remaining + 1e-9:
                raise ValueError("累计失效质量超过运输重量")
            for root, share in shipment["sources"].items():
                components.append({
                    "type": "failure",
                    "route": shipment["route"],
                    "root_lot": root,
                    "shipment_id": shipment_id,
                    "check_kind": row["kind"],
                    "l_days": last_pass_age,
                    "r_days": age,
                    "kg": fail_kg * share,
                })
            remaining -= fail_kg
            last_contact_age = max(last_contact_age, age)
            last_state = "fail"

        if remaining > 1e-9:
            if last_state is None:
                reason, censor_age = "unobserved", 0
            elif last_state == "unknown":
                reason, censor_age = "lost_contact", last_contact_age
            elif last_state == "pass":
                reason, censor_age = "observed_ok", last_contact_age
            else:
                reason, censor_age = "still_selling", last_contact_age
            for root, share in shipment["sources"].items():
                components.append({
                    "type": "censor",
                    "route": shipment["route"],
                    "root_lot": root,
                    "shipment_id": shipment_id,
                    "reason": reason,
                    "age_days": censor_age,
                    "kg": remaining * share,
                })

    _assert_conservation(components, shipments)
    return {"as_of": as_of, "components": components}


def _assert_conservation(components, shipments):
    expected = sum(quantity(row["kg"]) for row in shipments.values())
    observed = sum(component["kg"] for component in components)
    if abs(observed - expected) > 1e-6:
        raise ValueError("证据质量不守恒，拼箱来源暴露丢失或重复")
    per_shipment = {}
    for component in components:
        key = (component["shipment_id"], component["route"])
        per_shipment[key] = per_shipment.get(key, 0.0) + component["kg"]
    for shipment_id, shipment in shipments.items():
        mass = per_shipment.get((shipment_id, shipment["route"]), 0.0)
        if abs(mass - shipment["kg"]) > 1e-6:
            raise ValueError(f"运输 {shipment_id} 的证据质量无法对账")


def evidence_fingerprint(components, route=None):
    rows = components if route is None else [c for c in components if c["route"] == route]
    ordered = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    return fingerprint(ordered)


def turnbull(components):
    """区间删失数据的 Turnbull 自洽估计（NPMLE），确定性迭代到固定点。

    失效组件给区间 ``(l, r]``，删失组件给 ``(l, +∞)``。返回网格点、各点质量
    与阶梯生存函数；没有任何失效区间时生存曲线恒为 1。
    """
    intervals = [(c["l_days"], c["r_days"], c["kg"])
                 for c in components if c["type"] == "failure"]
    censors = [(c["age_days"], c["kg"]) for c in components if c["type"] == "censor"]
    grid = sorted({r for _, r, _ in intervals if r > 0})
    if not grid:
        return {"grid": [], "mass": [], "steps": []}

    def support(left, right):
        indexes = [j for j, t in enumerate(grid) if left < t <= right]
        if not indexes:  # left == right：同日观察到合格与不合格，退化为点质量
            indexes = [j for j, t in enumerate(grid) if t == right]
        if not indexes:
            raise ValueError("区间删失证据找不到可落点的网格时间")
        return indexes

    interval_support = [(w, support(left, right)) for left, right, w in intervals]
    censor_support = [(w, [j for j, t in enumerate(grid) if t > left])
                      for left, w in censors]
    censor_support = [(w, idx) for w, idx in censor_support if idx]

    mass = [1.0 / len(grid)] * len(grid)
    for _ in range(_TURNBULL_ITERATIONS):
        added = [0.0] * len(grid)
        for weight, indexes in interval_support:
            total = sum(mass[j] for j in indexes)
            for j in indexes:
                added[j] += weight * mass[j] / total
        for weight, indexes in censor_support:
            total = sum(mass[j] for j in indexes)
            for j in indexes:
                added[j] += weight * mass[j] / total
        grand_total = sum(added)
        updated = [value / grand_total for value in added]
        if max(abs(a - b) for a, b in zip(updated, mass)) < _TURNBULL_TOLERANCE:
            mass = updated
            break
        mass = updated

    steps = []
    for j, age in enumerate(grid):
        survival = sum(mass[k] for k in range(j + 1, len(grid)))
        steps.append({"age_days": age, "survival": survival})
    return {"grid": grid, "mass": mass, "steps": steps}


def survival_at(curve, day):
    """S(day)=P(T>day)：网格点 t 的失效质量落在 (l, t]，承诺到 day 天时，
    恰好在 day 处的质量已经变质，因此生存只保留网格中严格晚于 day 的质量。"""
    return sum(mass for age, mass in zip(curve["grid"], curve["mass"]) if age > day)


def normal_ppf(probability):
    """标准正态分位数（Acklam 算法），只依赖标准库。"""
    if not 0.0 < probability < 1.0:
        raise ValueError("分位数概率应在 (0, 1) 之间")
    # Acklam 常量：a/b 用于中心段有理逼近，c/d 用于尾部有理逼近。
    a = [
        -3.969683028665376e+01, 2.209460984245205e+02,
        -2.759285104469687e+02, 1.383577518672690e+02,
        -3.066479806614716e+01, 2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01, 1.615858368580409e+02,
        -1.556989798598866e+02, 6.680131188771972e+01,
        -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01,
        -2.400758277161838e+00, -2.549732539343734e+00,
        4.374664141464968e+00, 2.938163982698783e+00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01,
        2.445134137142996e+00, 3.754408661907416e+00,
    ]
    low, high = 0.02425, 1 - 0.02425
    if probability < low:
        q = math.sqrt(-2 * math.log(probability))
        numerator = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        denominator = ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        return numerator / denominator
    if probability > high:
        q = math.sqrt(-2 * math.log(1 - probability))
        numerator = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        denominator = ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        return -numerator / denominator
    q = probability - 0.5
    r = q * q
    numerator = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    denominator = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    return numerator / denominator


def wilson_upper(failures, n, confidence_level):
    """单边 Wilson 上界；n 为聚类折减后的有效样本量。"""
    if n <= 0:
        return 1.0
    z = normal_ppf(confidence_level)
    p = failures / n
    z2 = z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z / (1 + z2 / n) * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return min(1.0, center + half)


def _conservative_failure_kg(components, horizon):
    """ horizon 处对上界风险最保守的失效质量计数。

    区间完全落在 horizon 内计失效；跨越 horizon 的区间按失效计（不知道坏的确切时刻）；
    已知存活过 horizon 的不计；早于 horizon 删失的单位只进试验数不进失效数。
    """
    failure_kg = 0.0
    for component in components:
        if component["type"] == "failure":
            # 区间 (l, r]：l < horizon 说明失效可能已在承诺年龄前发生，按最保守计入；
            # l == horizon 的区间严格晚于承诺年龄，不计。右删失组件只进试验数。
            if component["l_days"] < horizon:
                failure_kg += component["kg"]
    return failure_kg


def _route_scope(route, components, shipments):
    ids = sorted({c["shipment_id"] for c in components})
    arrived = [shipments[i]["arrived_on"] for i in ids]
    root_kg = {}
    for component in components:
        root_kg[component["root_lot"]] = root_kg.get(component["root_lot"], 0.0) + component["kg"]
    ages = [c["r_days"] for c in components if c["type"] == "failure"]
    ages += [c["age_days"] for c in components if c["type"] == "censor"]
    return {
        "route": route,
        "shipment_ids": ids,
        "arrival_range": [min(arrived), max(arrived)] if arrived else [],
        "root_lots": sorted(root_kg),
        "root_kg": {root: root_kg[root] for root in sorted(root_kg)},
        "evidence_age_range": [min(ages), max(ages)] if ages else [0, 0],
    }


def _evaluate_route(route, components, policy, shipments):
    reference_kg = policy["reference_case_kg"]
    total_kg = sum(c["kg"] for c in components)
    root_kg = {}
    for component in components:
        root_kg[component["root_lot"]] = root_kg.get(component["root_lot"], 0.0) + component["kg"]
    distinct_roots = len(root_kg)
    raw_cases = total_kg / reference_kg
    root_cases = [mass / reference_kg for mass in root_kg.values()]
    # Kish 有效样本量：同一 root lot 拆出的多箱只贡献一份独立证据。
    effective_cases = (sum(root_cases) ** 2 / sum(value ** 2 for value in root_cases)
                       if root_cases else 0.0)

    curve = turnbull(components)
    ages = [c["r_days"] for c in components if c["type"] == "failure"]
    ages += [c["age_days"] for c in components if c["type"] == "censor"]
    evidence_cap = max(ages) if ages else 0

    failures = [c for c in components if c["type"] == "failure"]
    censors = [c for c in components if c["type"] == "censor"]
    root_detail = {}
    for root in sorted(root_kg):
        sub = [c for c in components if c["root_lot"] == root]
        sub_curve = turnbull(sub)
        root_detail[root] = {
            "total_kg": root_kg[root],
            "failure_components": sum(1 for c in sub if c["type"] == "failure"),
            "censor_components": sum(1 for c in sub if c["type"] == "censor"),
            "max_age_days": max([c["r_days"] for c in sub if c["type"] == "failure"]
                                + [c["age_days"] for c in sub if c["type"] == "censor"],
                                default=0),
            "survival_curve": sub_curve["steps"],
        }

    reasons = []
    if distinct_roots < policy["min_distinct_root_lots"]:
        reasons.append("min_distinct_root_lots")
    if effective_cases < policy["min_effective_cases"]:
        reasons.append("min_effective_cases")
    if evidence_cap < policy["min_observed_age_days"]:
        reasons.append("min_observed_age_days")

    horizon = None
    point_risk = None
    upper_risk = None
    if not reasons:
        scale = effective_cases / raw_cases if raw_cases else 0.0
        for candidate in range(evidence_cap, 0, -1):  # 不向外推超过证据覆盖年龄
            fail_kg = _conservative_failure_kg(components, candidate)
            effective_failures = (fail_kg / reference_kg) * scale
            wilson_bound = wilson_upper(effective_failures, effective_cases,
                                        policy["confidence_level"])
            point = 1.0 - survival_at(curve, candidate)
            # 保守上界在任何情况下都不得低于点估计，避免对客户给出自相矛盾的风险。
            bound = max(wilson_bound, point)
            if bound <= policy["acceptable_spoilage_risk"]:
                horizon = candidate
                point_risk = point
                upper_risk = bound
                break
        if horizon is None:
            reasons.append("risk_not_acceptable")

    if reasons and horizon is None:
        if reasons == ["risk_not_acceptable"]:
            status = "do_not_recommend"
        else:
            status = "insufficient_evidence"
    else:
        status = "recommended"

    return {
        "status": status,
        "recommendation_reasons": reasons,
        "commitment": None if horizon is None else {
            "shelf_life_days": horizon,
            "point_spoilage_risk": point_risk,
            "upper_spoilage_risk": upper_risk,
            "confidence_level": policy["confidence_level"],
        },
        "scope": _route_scope(route, components, shipments),
        "evidence": {
            "total_kg": total_kg,
            "distinct_root_lots": distinct_roots,
            "raw_cases": raw_cases,
            "effective_cases": effective_cases,
            "failure_components": len(failures),
            "censor_components": len(censors),
            "censor_reasons": sorted({c["reason"] for c in censors}),
            "max_evidence_age_days": evidence_cap,
            "check_kinds": sorted({c["check_kind"] for c in failures}),
        },
        "uncertainty": {
            "method": "Turnbull NPMLE + clustered one-sided Wilson upper bound",
            "censoring": "interval-censored failures and right-censored survivors; "
                         "exact failure times are never assumed",
            "cluster_adjustment": "Kish effective sample size over root harvest lots",
            "extrapolation_limit_days": evidence_cap,
        },
        "survival_curve": curve["steps"],
        "root_lot_strata": root_detail,
    }


def evaluate(data, policy, as_of):
    """按运输条件（route）给出货架期建议；证据不足时明确不推荐并说明原因。"""
    _validate_policy(policy)
    evidence = build_evidence(data, as_of)
    shipments = arrivals(data, as_of)
    components = evidence["components"]
    routes = sorted({c["route"] for c in components})
    result = {
        "policy_id": policy["policy_id"],
        "policy": dict(policy),
        "as_of": as_of,
        "routes": {},
        "evidence_fingerprints": {
            route: evidence_fingerprint(components, route) for route in routes
        },
    }
    for route in routes:
        route_components = [c for c in components if c["route"] == route]
        result["routes"][route] = _evaluate_route(route, route_components, policy, shipments)
    return result
