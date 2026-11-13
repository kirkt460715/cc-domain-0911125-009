# 试验数据版本与批准发布

内容寻址的数据对象与目录版本独立保存。发布计划需要用途和批准；补丁、模式迁移及保留期审查各自留痕。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q trialvault
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m trialvault demo
python3 -m trialvault --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。

## 预先登记的中期分析（成组序贯）

面向按材料批次随机分到两组、连续终点的研究，避免“每批结果都看一次、看到好结果就停、报告却按只检验一次写”的一类错误膨胀。分析单位始终是随机化批次：批内试片按登记规则（有效试片均值）先汇总，批次汇总量进入统计量。

设计在试验开始前一次性登记并不可变保存：检验方向（单/双侧）、总体 alpha、每组最大批次数、计划查看的信息比例、每组最小有效批次数、结果锁定规则（批次以数据版本锁定、缺失值剔除、每批最少试片数）、先导试验冻结的共同标准差及其来源版本、适用假设与等比例分组日程。设计保存后若要改计划，必须用新设计标识另立试验，已消耗的错误额度不能沿用。

查看（`interim_look`）从固定数据版本提取当次截止前已锁定的批次，缺失整批与缺失试片分别展示。信息比例按预登记的方差口径（冻结尺度、等比例批次计数）计算；实际查看可偏离计划比例，但只按增加的信息量消耗额度，信息未增加或不足最小有效批次数时不产生新正式结论。边界用 O’Brien–Fleming 型 alpha spending 构造，按相关序贯统计量的累计越界概率校准（Brownian 协方差递推数值积分），不把每次累计花费当独立 p 值阈值；每次查看返回统计量、边界、累计与增量花费、求解误差及继续、获益停止或达到终点的决定。停止后到达的批次在报告中单列，不能重开同一试验挑结果。

校验（`interim_validate`）用固定种子在零效应下独立模拟整个多次查看过程，给出整体一类错误的蒙特卡洛区间，并用正效应案例展示提前停止。边界求解为确定性求积、不含任何模拟样本，校验流按设计摘要与种子独立派生，不存在与调参样本的自证重叠。数据版本被撤回后，历史决定保留，但不再继续发布有效结论。

动作示例：

```json
{"action": "interim_design", "design_id": "study", "dataset": "trial", "schema_version": 1,
 "fields": {"batch": "batch", "specimen": "specimen", "value": "strength"},
 "schedule": [{"batch": "A1", "group": "A"}, {"batch": "B1", "group": "B"}],
 "benefit_direction": "A_minus_B", "sidedness": "two", "alpha": "0.05",
 "max_batches_per_group": 1, "planned_fractions": ["1"], "min_batches_per_group": 1,
 "specimens_per_batch": 2,
 "lock_rule": {"batch_lock": "dataset_version", "min_specimens_per_batch": 2, "missing_value": "exclude"},
 "scale": {"common_sd": "1.2", "source": {"dataset": "pilot-run", "version": 1}},
 "assumptions": ["Batch summaries approximately normal", "Batches independent"]}
{"action": "interim_look", "design_id": "study", "look_id": "look-1", "dataset_version": 1}
{"action": "interim_report", "design_id": "study", "dataset_version": 1}
{"action": "interim_validate", "design_id": "study", "seed": "review", "effect": "1.5", "replications": 10000}
```

试片数据集的主键必须是（批次， 试片），终点字段为数值；先导尺度来源必须是独立数据集的已激活版本。`python3 -m trialvault check` 的完整性核查覆盖设计与查看记录的摘要链。
