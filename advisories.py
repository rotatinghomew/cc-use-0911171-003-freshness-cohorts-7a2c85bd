"""可对客户发出的到货建议：发布、留存、依赖定位与重新评估。

设计约束：
- 已经对客户发出的版本和当时看到的证据不可改写：``advisories/v-*.json`` 只追加。
- 每个建议记录其政策、证据指纹、全部依赖实体（批次/运输/检查）与完整分析快照。
- 后续新增回访或改正拆箱关系时，``affected_advisories`` 可按实体与证据指纹定位
  仍在使用的建议；``issue_advisories`` 重新评估，内容变化才产生新版本。
- 撤回只改索引状态，不删除历史文件。
"""
import json
from pathlib import Path

from records import fingerprint, latest, save
from shelflife import evaluate


def _dependency_index(data, as_of):
    """构造实体 -> 指纹的依赖索引，覆盖批次血缘、到货与全部已知检查。"""
    dependencies = {}
    for lot in data["lots"]:
        dependencies[f"lot:{lot['lot_id']}"] = fingerprint(lot)
    for shipment in latest(data["shipments"], "shipment_id", as_of):
        dependencies[f"shipment:{shipment['shipment_id']}"] = fingerprint(shipment)
    for check in latest(data["checks"], "check_id", as_of):
        dependencies[f"check:{check['check_id']}"] = fingerprint(check)
    return dependencies


def _lineage_closure(data, lot_id):
    """返回批次及其全部祖先批次编号（含中间拼箱批，血缘改正时可被定位）。"""
    nodes = {row["lot_id"]: row for row in data["lots"]}
    seen = set()
    stack = [lot_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for parent in nodes[current]["parents"]:
            stack.append(parent["lot_id"])
    return seen


def _series_id(policy_id, route):
    seed = f"{policy_id}|{route}"
    return "ADV-" + fingerprint(seed)[:12].upper()


def _store_paths(store_dir):
    directory = Path(store_dir)
    return directory, directory / "index.json"


def load_index(store_dir):
    _, index_path = _store_paths(store_dir)
    if not index_path.exists():
        return {"advisories": []}
    return json.loads(index_path.read_text(encoding="utf-8"))


def _write_index(store_dir, index):
    _, index_path = _store_paths(store_dir)
    save(index_path, index)


def _write_advisory(store_dir, record):
    directory, _ = _store_paths(store_dir)
    path = directory / "advisories" / f"{record['series_id']}-v{record['version']}.json"
    if path.exists():  # 不可变：同文件已存在时禁止覆盖
        raise ValueError("建议版本文件已存在，禁止改写已发出版本")
    save(path, record)
    return str(path)


def issue_advisories(store_dir, data, policy, as_of):
    """按当前证据重新评估各运输条件，并发布或续版客户建议。

    - 同一系列（政策+路线）证据指纹与分析结果均未变化时不产生新版本；
    - 证据或结论变化时追加新版本，旧版本文件保留；
    - 证据不足时发布状态为 ``insufficient_evidence``/``do_not_recommend`` 的记录，
      其中 ``commitment`` 为 null，客户承诺字段不得臆造。
    """
    analysis = evaluate(data, policy, as_of)
    dependencies = _dependency_index(data, as_of)
    index = load_index(store_dir)
    latest_by_series = {}
    for entry in index["advisories"]:
        latest_by_series[entry["series_id"]] = entry

    issued = []
    for route in sorted(analysis["routes"]):
        series_id = _series_id(policy["policy_id"], route)
        route_view = analysis["routes"][route]
        evidence_fp = analysis["evidence_fingerprints"][route]
        analysis_fp = fingerprint(route_view)
        scope_ids = route_view["scope"]["shipment_ids"]
        shipment_rows = {row["shipment_id"]: row
                         for row in latest(data["shipments"], "shipment_id", as_of)}
        route_lots = set()
        for shipment_id in scope_ids:
            route_lots |= _lineage_closure(data, shipment_rows[shipment_id]["lot_id"])
        check_ids = {check for shipment_id in scope_ids
                     for check in _checks_of(data, as_of, shipment_id)}
        route_dependencies = {
            key: value for key, value in dependencies.items()
            if (key.startswith("lot:") and key.split(":", 1)[1] in route_lots)
            or (key.startswith("shipment:") and key.split(":", 1)[1] in scope_ids)
            or (key.startswith("check:") and key.split(":", 1)[1] in check_ids)
        }
        previous = latest_by_series.get(series_id)
        if (previous is not None and previous["evidence_fingerprint"] == evidence_fp
                and previous["analysis_fingerprint"] == analysis_fp
                and previous["status"] != "withdrawn"):
            continue
        version = 1 if previous is None else previous["version"] + 1
        record = {
            "series_id": series_id,
            "version": version,
            "policy_id": policy["policy_id"],
            "route": route,
            "issued_as_of": as_of,
            "status": route_view["status"],
            "commitment": route_view["commitment"],
            "recommendation_reasons": route_view["recommendation_reasons"],
            "scope": route_view["scope"],
            "evidence_fingerprint": evidence_fp,
            "analysis_fingerprint": analysis_fp,
            "dependency_fingerprints": route_dependencies,
            "analysis": route_view,
        }
        path = _write_advisory(store_dir, record)
        record["path"] = path
        issued.append(record)
        index["advisories"].append({k: record[k] for k in (
            "series_id", "version", "policy_id", "route", "issued_as_of", "status",
            "commitment", "recommendation_reasons", "scope", "evidence_fingerprint",
            "analysis_fingerprint", "dependency_fingerprints", "path")})
    _write_index(store_dir, index)
    return issued


def _checks_of(data, as_of, shipment_id):
    return [row["check_id"] for row in latest(data["checks"], "check_id", as_of)
            if row["shipment_id"] == shipment_id]


def affected_advisories(store_dir, data, as_of):
    """对照当前实体指纹，定位仍在使用但依赖证据已经变化的建议。

    返回每个受影响系列的现行索引项、变化实体清单与当前重新评估结果；
    已撤回的系列不再视为“仍在使用”。新出现的实体不会触发误报。
    """
    index = load_index(store_dir)
    current = _dependency_index(data, as_of)
    latest_by_series = {}
    for entry in index["advisories"]:
        latest_by_series[entry["series_id"]] = entry

    findings = []
    for entry in latest_by_series.values():
        if entry["status"] == "withdrawn":
            continue
        old_dependencies = entry["dependency_fingerprints"]
        changed = [entity for entity, old_fp in old_dependencies.items()
                   if entity in current and current[entity] != old_fp]
        missing = [entity for entity in old_dependencies if entity not in current]
        # 发布后新增、且落在该建议适用范围内的实体（如新增回访）。
        scope_ids = set(entry["scope"]["shipment_ids"])
        added = [entity for entity in current
                 if entity not in old_dependencies
                 and ((entity.startswith(("shipment:", "check:"))
                       and _entity_shipment(data, as_of, entity) in scope_ids)
                      or (entity.startswith("lot:")
                          and entity.split(":", 1)[1] in set(entry["scope"]["root_lots"])))]
        if changed or missing or added:
            findings.append({
                "index_entry": entry,
                "changed_entities": sorted(changed),
                "withdrawn_entities": sorted(missing),
                "added_entities": sorted(added),
            })
    return findings


def _entity_shipment(data, as_of, entity):
    kind, identity = entity.split(":", 1)
    if kind == "shipment":
        return identity
    if kind == "check":
        for row in latest(data["checks"], "check_id", as_of):
            if row["check_id"] == identity:
                return row["shipment_id"]
    return None


def advisory_history(store_dir, series_id):
    """读取一个系列的全部已发出版本（按版本号排序，文件内容不可变）。"""
    directory, _ = _store_paths(store_dir)
    paths = sorted((directory / "advisories").glob(f"{series_id}-v*.json"),
                   key=lambda path: int(path.stem.split("-v")[-1]))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def withdraw_advisory(store_dir, series_id, as_of):
    """撤回某系列的现行建议；历史版本文件保留，索引标记为 withdrawn。"""
    index = load_index(store_dir)
    target = None
    for entry in index["advisories"]:
        if entry["series_id"] == series_id:
            target = entry
    if target is None:
        raise ValueError("找不到建议系列")
    target["status"] = "withdrawn"
    target["withdrawn_as_of"] = as_of
    _write_index(store_dir, index)
    return target
