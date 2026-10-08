# Evaluation outputs and logging

默认只保留实验必需的统计，不保存完整 GUI、LLM 对话或探索内存。
结果目录为 `logs/<app>/<run_id>/`，历史结果不改写。

| 默认文件 | 内容 |
| --- | --- |
| `metrics.csv` | 每次运行一行，便于合并多轮实验：覆盖率、任务数量/成功率、时间、调用/token、预算和消融开关 |
| `summary.json` | 相同指标的完整说明，以及模型、参数、分母来源、逐设备覆盖率、运行状态 |
| `task_results.jsonl` | 每个已结束任务一行：目标、结果/原因、耗时、动作、参与设备数、切换数、验证轮数、token、覆盖增量和任务指纹 |
| `coverage_timeline.csv` | 每 60 秒及开始/结束时记录整体与逐设备覆盖率及分子/分母，无控件或方法名称大列表 |

任务结果和时间序列逐次写入，运行汇总每个任务结束时更新。
`status=completed` 表示正常收尾，`interrupted` 表示执行阶段被 Ctrl+C 中断，`error` 表示执行阶段异常。
尚未收尾的文件可能保留 `running`。中断时尚未完成的任务不计入已结束任务统计，比较实验请使用完整运行。

## 与 evaluation 的对应关系

下表列出当前实现输出的实验指标与计算口径。

| 论文指标 | 输出字段与口径 |
| --- | --- |
| Activity coverage | `activity_cov`：已访问且在静态目标集合中的 Activity / 静态目标数；集合来自 APK manifest 或 dumpsys，外部 Activity 不增加分母 |
| Method coverage | `method_cov`：已执行的插桩方法 / APK 中提取到的 AndroidLog 插桩方法 |
| Class coverage | `class_cov`：已执行插桩方法所属类 / 插桩方法所属类集合 |
| Task Count | `task_count`：已结束尝试的去重任务数；`attempt_count` 另存实际执行尝试数 |
| Task Success Rate | `task_success_rate`：至少成功过一次的去重任务数 / 去重任务数；`attempt_success_rate` 另存成功尝试 / 所有已结束尝试 |

覆盖率和成功率均存为 0–1 比例。没有可靠分母时 JSON 为 `null`、CSV 留空、终端显示 `N/A`；测得的零仍为 0。
`unknown_attempts` 单独记录，并保留在尝试成功率的分母中。跨设备的覆盖集合取并集，避免重复计数。
`covered_*` 为命中目标的数量，`out_of_scope_*` 为观察到但不在目标集合内的数量。

当前实现与论文的严格口径存在以下差别，并在 `summary.json.measurement` 中记录：

- 方法/类分母取决于插桩范围，不代表 APK 全部应用方法/类；排除第三方库需由插桩阶段保证。
- 任务去重使用规范化任务指纹的 token cosine >= 0.95，按首次匹配的代表项分组，是轻量文本近似，并非 Sentence-BERT 语义去重。
- RQ3 的真实缺陷需要人工确认；`confirmed_issue_count` 留空，任务失败不自动算作缺陷。
- 三次重复实验需要分别运行，再按同一 app、配置和预算汇总 `metrics.csv`。当前消融开关不自动冠以论文的三种变体名称。

全程调用/token 包含规划；任务内调用/token 从执行开始统计。记录的是 API 实际报告的 token 数，不估算费用。

## 详细日志开关

```powershell
# 默认精简模式
python -m octopus --dip emulator-5554 --app-package org.example --run-seconds 3600

# 需要复现/排查时开启
python -m octopus --dip emulator-5554 --app-package org.example --run-seconds 3600 --detailed-log
```

`--detailed-info` 是同一开关的旧名称；Python API 仍可使用 `run(..., detailed_info=True)`。
开启后额外保存：

- `info.jsonl`：完整运行事件、GUI 上下文、LLM 请求/响应、插桩命中和每个任务的完整证据。
- `exploration_memory.json`：结束时的探索图和状态快照。

不再重复保存 `task_memory.json`，其任务证据已在 `info.jsonl`；也不再生成自定义 XLSX 和覆盖率明细副本。
默认输出包含必要统计，与是否开启详细日志无关。终端显示运行配置、当前任务、动作、任务结果及最终覆盖率/成功率和文件位置。
