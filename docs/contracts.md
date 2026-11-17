# 业务数据契约

lots.parents记录投入质量，子批重量等于父来源投入之和；来源比例递归守恒。shipment以到货日期起算。checks描述被观察或退回的质量及状态，不声称是全箱失效时间。基线可追溯来源与最近随访状态；货架期模块在此基础上提供带删失、聚类折减与版本留存的客户建议。

## 现有组合入口

`app.inspect(data, as_of)` 是只读检查，输入为本地记录集合，输出为 JSON 可表示的字典；不修改输入。领域接口会对不符合业务口径的数据抛出 `ValueError`。缺失、不足和不适用不应伪装成零数量。

## 货架期分析（shelflife.py）

`evaluate(data, policy, as_of)` 按运输条件 `route` 输出建议，输入政策（业务可自行设定）字段：

| 字段 | 含义 | 默认值 |
| --- | --- | --- |
| `acceptable_spoilage_risk` | 可接受的变质风险上界 | 0.10 |
| `confidence_level` | 风险上界的单边置信水平 | 0.95 |
| `min_distinct_root_lots` | 最低独立采收批次（root lot）数 | 3 |
| `min_effective_cases` | 聚类折减后的最低有效箱数 | 8.0 |
| `reference_case_kg` | 一箱基准质量，用于 kg 与箱数换算 | 40.0 |
| `min_observed_age_days` | 证据必须覆盖的最低观察年龄（天） | 5 |

口径约定：

1. **起算点**：所有年龄从实际到货日 `arrived_on` 起算，与 `recorded_at` 无关。
2. **区间删失**：先观察到合格、之后才发现不合格时，失效只知道落在 `(最后合格年龄, 发现年龄]`，不假设具体变质时刻。`turnbull()` 用 Turnbull 自洽 NPMLE 估计阶梯生存曲线。
3. **右删失**：仍在销售、失联（state=unknown）、从未随访的质量保留在风险集中，分别标记 `observed_ok` / `lost_contact` / `unobserved` / `still_selling`，不得当作失效也不得丢弃。
4. **事件语义**：`full_return` 必须等于整箱重量并耗尽整箱；`partial_return` 与不合格 `inspection` 只让登记质量进入失效区间，其余质量继续右删失。
5. **拼箱**：每条证据按 `lineage.sources` 的来源份额拆给各 root lot，证据质量全局守恒；同一来源在不同路线上进入不同 (route, root) 层，分别解释但不重复计数。
6. **聚类**：同一 root lot 拆出的多箱不独立，用 Kish 有效样本量折减；证据门槛按折减后的有效箱数判断。
7. **风险判断**：承诺年龄处同时给出 Turnbull 点风险与单边 Wilson 保守上界（取两者较大值），上界不超过政策风险才可承诺；承诺年龄不超过证据覆盖的最大年龄，不做外推。
8. **三种结论**：`recommended`（附可售天数与风险）、`do_not_recommend`（证据够但风险过不了门槛）、`insufficient_evidence`（证据门槛未满足，原因列在 `recommendation_reasons`）；后两者 `commitment` 为 null。每条建议带适用范围（运输、批次、到货区间、来源份额）与 `uncertainty` 方法说明。

## 建议发布与留存（advisories.py）

- `issue_advisories(store_dir, data, policy, as_of)`：发布或续版。同一系列（政策+路线）证据指纹与分析指纹都没变时不产生新版本；变化时追加新版本，历史文件 `advisories/<series>-v<n>.json` 不可改写。
- 每条建议记录 `dependency_fingerprints`：血缘闭包内全部批次（含中间拼箱批）、范围内运输与检查的实体指纹，以及发布当时的完整分析快照。
- `affected_advisories(store_dir, data, as_of)`：定位仍在使用且依赖已变化的建议，区分 `changed_entities`（内容修订）、`added_entities`（新增回访/运输）、`withdrawn_entities`（依赖实体被撤回）；已撤回系列不再报告。
- `advisory_history(store_dir, series_id)`：读取一个系列的全部已发出版本；`withdraw_advisory()` 只改索引状态，不删除历史。
- 时点语义与记录层一致：`as_of` 之后登记的记录不可见，已发出版本保留当时看到的证据。
