from lineage import sources
from records import latest, quantity


def arrivals(data, as_of):
    nodes = {row["lot_id"]: row for row in data["lots"]}
    result = {}
    for row in latest(data["shipments"], "shipment_id", as_of):
        if row["arrived_on"] > as_of:
            continue
        mass = quantity(row["kg"])
        if row["lot_id"] not in nodes or mass > nodes[row["lot_id"]]["kg"]:
            raise ValueError("运输重量超出批次")
        result[row["shipment_id"]] = {**row, "sources": sources(data, row["lot_id"])}
    return result


def route_exposure(data, as_of):
    groups = {}
    for row in arrivals(data, as_of).values():
        group = groups.setdefault(row["route"], {"shipments": 0, "kg": 0, "root_kg": {}})
        group["shipments"] += 1
        group["kg"] += row["kg"]
        for root, share in row["sources"].items():
            group["root_kg"][root] = group["root_kg"].get(root, 0) + share * row["kg"]
    return groups
