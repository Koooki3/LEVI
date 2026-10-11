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


## 只读实时结果 API

结果目录与精确会话页面分别使用 `GET /api/levi/live/results` 和 `GET /api/levi/live/results/session?root=<已配置根>&group=<模型>&task=<任务>&session=<运行或会话ID>`。会话请求还接受 `session_id`、`run_id` 做一致性核对。根目录必须与服务端配置完全一致，身份字段不能选择任意文件路径。目录可按 `dataset`（规范数据集或已核实查看器别名）和 `run_id`（真实时间片段标注或审核运行，不是评测运行）筛选。跨根同名记录、同一任务的不同评测运行分别保留。

JSON schema 为 `levi.live.result-index.v1`，每项含 `identity`、`status`、`result_visibility`、`analysis`、`records`、`runs`、`viewers`、`reports`、`policy`、`source`、`comparison`。`analysis` 复用既有实时汇总，人工与自动标签分别统计。`result_visibility` 为 `visible`、`withheld` 或 `unknown`；结局统计须同时满足 `visible` 和 `analysis` 非空。活动、盲态、身份未知、来源重绑定或发生变化时，不返回分析、记录、标注运行链接或报告文件。即使旧会话或状态文件没有 AERI 信息，也核对真实采集头的 `eval.aeri` 与片段角色。可读且无 AERI 标志的旧采集头可按原规则展示，缺失或不可读的采集头不能放行分析。

状态包含 `active`、`waiting_annotation`、`partial`、`reported`、`stale`、`missing_source`、`archived`、`rebound`、`unknown`、`blocked`，表达可用性和来源状态，不代表模型质量。请求期间来源或标签被替换会使响应失效。`/api/levi/live/results/session/download` 只接受相同身份及 `format=json|markdown`、`lang=en|zh`，现场生成带摘要值的当前只读快照，不发布或自动导出。目录报告保持独立的标签依据与版本，并明确其记录的来源哈希是否与当前快照一致；不跟随外部报告路径，也不把它们变成下载地址。历史规范会话的可见性无法核实时，仍保留目录身份和未核实的版本引用，关闭分析内容。

查看器链接沿已核实的原生与产品目录映射，使用既有 `episode_<index>` 路由。标注链接还须在只读运行库中核实运行确实绑定原生数据集，并确认对应实时工作区界面可用。产品查看器别名不代表产品运行库中存在实时标注运行；缺少关联时仅展示文字引用。

多轮评测复用原有终态与盲性报告门，不把多轮分析冒充实时会话汇总。比较须有明确的多轮评测、精确子运行与组映射，以及一致的冻结策略和设置哈希。重置、标签依据、早停规则、覆盖率、运行提交和环境版本来自既有报告；存在差异或未核实条件时不能判为可比。模型名称和时间不能证明可比。历史不完整或丢弃片段无法精确归入会话时，其覆盖率保持未知。

单次请求共享来源读取和解析索引，结束时核对固定的来源快照，不保留跨请求缓存。每个配置根最多扫描 1,000 份会话，全目录最多扫描 1,000 份数据集状态、1,000 份报告引用，每份文件最多 16 MiB。目录返回 `complete`、`truncated`、`limits`；发生截断时关闭派生结果，不声称范围完整。这些接口不启动作业、不写源采集数据、不登记导出、不修改工作区状态。
