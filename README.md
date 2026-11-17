# freshness-cohorts

鲜蔬拆并批次、运输与售后观察的追溯核对。

## 当前产品

这是可独立运行的本地分析应用，提供业务记录核对和确定性基线结果。各业务职责可直接作为 Python 接口调用，`app.py` 负责组合一次只读业务检查。结果可保存为 JSON，不依赖远端服务。

- `lineage.py`：拆并关系和来源质量份额。
- `journeys.py`：到货起点与路线暴露。
- `inspections.py`：抽检/退货/回访形成的可观察区间。

## 运行

使用 Python 3.11.14，无需安装第三方包。

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 app.py demo
```

`inspect` 与 `demo` 使用相同业务入口；`--as-of` 选择已知日期，`--data` 可选本地数据文件，`--output` 可将本次业务核对结果写入文件。默认数据位于 `fixtures/data.json`，默认日期为 `2026-11-10`。这些命令仅展示当前已经存在的能力。

## 容器

`Dockerfile` 固定到 Python 基础镜像内容摘要。构建后运行不联网：

```bash
docker build -t freshness-cohorts:local .
docker run --rm --network none freshness-cohorts:local python -m unittest discover -s tests -v
docker run --rm --network none freshness-cohorts:local
```

## 数据与业务契约

样例记录全部为本地合成数据，不代表真实农户、市场成交或实际试验结果。日期采用 `YYYY-MM-DD`，有记录时点的条目以 `recorded_at` 表示系统已知日期；同一业务键存在多个修订时选已知范围内最新记录，同日由 `revision` 区分，`withdrawn` 表示撤回。可修改样例验证业务边界，不能将演示数据的统计方向视为真实结论。

lots.parents记录投入质量，子批重量等于父来源投入之和；来源比例递归守恒。shipment以到货日期起算。checks描述被观察或退回的质量及状态，不声称是全箱失效时间。基线可追溯来源与最近随访状态，不提供货架期估计或客户承诺。

接口细节见 `docs/contracts.md`。测试使用各自的内存副本和临时目录，不修改原始数据或依赖固定端口。
