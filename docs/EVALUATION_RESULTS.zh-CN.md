# 评测结果目录

用同一份索引按 rollout 的模型、任务目录原名查找已有报告。索引复用现有统计，不移动采集数据，也不复制视频。多模型对照仍保留一份 campaign 报告，各模型、任务、会话分别关联到它。

```text
evaluation-results/
  INDEX.md
  index.json
  models/<模型>/<任务>/sessions/<会话>/reference.json
  models/<模型>/<任务>/sessions/<会话>/versions/<摘要>.json
  campaigns/<评测计划>/<标签依据>/reference.json
```

为已经完整生成的报告登记冻结配置中的准确对应关系：

```sh
levi results register --root lab/evaluation-results \
  --report <工作区>/campaigns/<评测计划>/report/operator_label \
  --rollout-root /path/online_rollout_data/models \
  --rollout-dir /path/online_rollout_data/models/<模型>/<任务> \
  --model <模型> --task <任务> --session <会话>
levi results list --root lab/evaluation-results
```

rollout 目录必须与 root/model/task 完全对应。不能通过检查点文件名猜模型名称；对应关系标为调用者显式提供，应来自冻结配置。原报告和采集数据留在原处，索引核对每个产物的哈希，保留引用版本，并在同一个目录锁下原子发布当前指针。INDEX.md 可直接打开来源报告目录，查看已有数据、表格和图表。

每条记录包括标签依据、评测状态、来源报告摘要、产物哈希与 rollout 位置。人工、自动和裁决标签分别登记。进行中或盲评报告仅登记评测计划进度引用，暂不登记模型/任务/会话对应关系、组别元数据或分析路径。来源缺失、变化、格式不完整或涉及符号链接时拒绝登记，等待报告发布完整后重试。

首个接口用于已有 campaign 报告。campaign自动登记仍需接线；独立实时会话导出与后台结束会话登记已实现。索引本身不启用这些生产者，也不能证明 GPU 或真机效果。

## 实时会话导出

命令：`levi results live-export --root lab/evaluation-results --live-dir <实时工作区>/live --rollout-root /path/online_rollout_data/models --session-file /path/online_rollout_data/models/.eval_sessions/<会话>.json`。只导出明确标识且已经结束的会话。数据集来源与每个片段的run_id必须一致；未知、进行中、盲评或绑定campaign的会话拒绝直接导出，应走评测报告门禁。复用现有统计生成data/analysis.json、data/records.json、双语摘要与成功率SVG，人工和自动标签分开展示。仅统计已提交标注的片段，历史缺失覆盖明确标为未知。保留版本与来源，不复制rollout；后台静默分析钩子会在<实时工作区>/outputs/LEVI/evaluation-results登记结束会话，未知或活动来源跳过。手工CLI可选择lab/evaluation-results。各会话的reports/<标签依据>分别保留引用。

AERI 会话缺少明确公开可见性记录时不会自动导出；其已发布分析使用受 campaign 状态和盲法保护的报告入口。源关联相同但来自不同 rollout root 的会话身份碰撞会拒绝，不能静默覆盖。
