from records import quantity


def sources(data, lot_id):
    nodes = {row["lot_id"]: row for row in data["lots"]}
    if len(nodes) != len(data["lots"]):
        raise ValueError("批次编号重复")
    visiting = set()
    cache = {}

    def expand(identity):
        if identity in visiting:
            raise ValueError("批次血缘存在环")
        if identity in cache:
            return cache[identity]
        if identity not in nodes:
            raise ValueError("找不到来源批次")
        row = nodes[identity]
        mass = quantity(row["kg"])
        if not mass:
            raise ValueError("批次重量为零")
        if not row["parents"]:
            cache[identity] = {identity: 1.0}
            return cache[identity]
        visiting.add(identity)
        result = {}
        if abs(sum(quantity(parent["kg"]) for parent in row["parents"]) - mass) > 1e-8:
            raise ValueError("拼批质量不守恒")
        for parent in row["parents"]:
            if parent["kg"] > nodes[parent["lot_id"]]["kg"]:
                raise ValueError("使用超过来源批次重量")
            for root, share in expand(parent["lot_id"]).items():
                result[root] = result.get(root, 0) + share * parent["kg"] / mass
        visiting.remove(identity)
        cache[identity] = result
        return result

    return expand(lot_id)
