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

首个接口用于已有 campaign 报告。campaign/live 的自动登记与独立实时会话分析导出仍需接线；索引本身不启用这些生产者，也不能证明 GPU 或真机效果。
