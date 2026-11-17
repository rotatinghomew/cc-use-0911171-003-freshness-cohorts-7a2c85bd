import argparse
import copy
import json
from pathlib import Path
from records import load, save
from shelflife import DEFAULT_POLICY, evaluate


def inspect(data, as_of):
    from journeys import route_exposure
    from inspections import observations, followup_status
    return {"routes": route_exposure(data, as_of), "observations": observations(data, as_of),
            "followup": followup_status(data, as_of)}


def load_policy(path):
    if path is None:
        return copy.deepcopy(DEFAULT_POLICY)
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = copy.deepcopy(DEFAULT_POLICY)
    merged.update(policy)
    return merged


def shelflife_report(data, as_of, policy):
    """只读货架期分析：不写建议存档，结果可重复且不修改输入。"""
    return evaluate(data, policy, as_of)


def main():
    parser = argparse.ArgumentParser(description="鲜蔬拆并批次、运输与售后观察的追溯核对。")
    parser.add_argument("action", choices=["demo", "inspect", "shelflife", "issue", "affected"])
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "fixtures/data.json")
    parser.add_argument("--as-of", default="2026-11-10")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--policy", type=Path,
                        help="JSON 文件，可覆盖默认政策字段（风险门槛、最低证据要求等）")
    parser.add_argument("--store", type=Path, default=Path(__file__).parent / "out/advisories",
                        help="客户建议存档目录；已发出版本不可变")
    args = parser.parse_args()

    data = load(args.data)
    policy = load_policy(args.policy)
    if args.action in {"demo", "inspect"}:
        result = inspect(data, args.as_of)
    elif args.action == "shelflife":
        result = shelflife_report(data, args.as_of, policy)
    elif args.action == "issue":
        from advisories import issue_advisories
        result = {"issued": issue_advisories(args.store, data, policy, args.as_of)}
    else:
        from advisories import affected_advisories
        result = {"affected": affected_advisories(args.store, data, args.as_of)}

    if args.output:
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
