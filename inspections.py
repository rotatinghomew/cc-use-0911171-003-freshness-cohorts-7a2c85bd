from datetime import date
from journeys import arrivals
from records import latest, quantity


def observations(data, as_of):
    shipments = arrivals(data, as_of)
    result = []
    for row in latest(data["checks"], "check_id", as_of):
        if row["shipment_id"] not in shipments or row["observed_on"] > as_of:
            continue
        shipment = shipments[row["shipment_id"]]
        days = (date.fromisoformat(row["observed_on"]) - date.fromisoformat(shipment["arrived_on"])).days
        if days < 0 or quantity(row["kg"]) > shipment["kg"]:
            raise ValueError("售后观察与到货不一致")
        if row["state"] not in {"pass", "fail", "unknown"}:
            raise ValueError("未知质量检验状态")
        result.append({**row, "age_days": days, "route": shipment["route"], "sources": shipment["sources"]})
    return sorted(result, key=lambda row: (row["shipment_id"], row["age_days"]))


def followup_status(data, as_of):
    grouped = {identity: [] for identity in arrivals(data, as_of)}
    for row in observations(data, as_of):
        grouped[row["shipment_id"]].append(row)
    return {identity: {"last_age_days": rows[-1]["age_days"] if rows else None,
                       "last_state": rows[-1]["state"] if rows else "unknown",
                       "checks": len(rows)} for identity, rows in grouped.items()}
