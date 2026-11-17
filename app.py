import argparse
import json
from pathlib import Path
from records import load, save


def inspect(data, as_of):
    from journeys import route_exposure
    from inspections import observations, followup_status
    return {"routes": route_exposure(data, as_of), "observations": observations(data, as_of),
            "followup": followup_status(data, as_of)}



def main():
    parser = argparse.ArgumentParser(description="鲜蔬拆并批次、运输与售后观察的追溯核对。")
    parser.add_argument("action", choices=["demo", "inspect"])
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "fixtures/data.json")
    parser.add_argument("--as-of", default="2026-11-10")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(load(args.data), args.as_of)
    if args.output:
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
