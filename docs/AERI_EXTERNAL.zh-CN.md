# AERI 外部智能体桥接

[English](AERI_EXTERNAL.md) · [AERI 提供者](AERI_PROVIDERS.zh-CN.md)

AERI 长期保留本地 vLLM 与独立运行的外部智能体两种模式。
`levi.automatic.external_bridge` 提供 `VllmSubtaskVlm` 使用的 HTTP 接口，
不运行模型、不启动智能体、不连接机器人或策略服务、不执行答案、不写操作员标签。
本地模式启动配置仍然有效；外部模式是显式启动选择，不是 GPU 不足时的自动回退。
此模块属于软件候选；加入代码不代表已部署或通过真机验证。

调用示例（仅文档；启动仍需正常服务归属，真机会话仍需人工授权）：

```sh
python -m levi.automatic.external_bridge serve frozen-model \
  --host 127.0.0.1 --port 18991 --served-model-name external-agent \
  --queue /path/to/owned/external-queue \
  --max-model-len 4096 --gpu-memory-utilization 0.6
```

实际参数必须与调用方冻结的模型、服务名、端口和 vLLM 参数一致。未知参数作为兼容
参数忽略，并向 stderr 提示；它们不能改变已识别的 `--host`、`--port`，也不支持
参数缩写。`localhost` 固定绑定 `127.0.0.1`，不查询 DNS；只允许 IPv4 回环地址。
拒绝端口 5000、5001、5100、7470、8000，以及特权或非法端口。
必须显式提供操作员拥有的队列路径，不得写入源数据或让不可信写入者共用。
这是可信本地文件接口，不是带认证的多用户服务。队列保留供审计，由操作员管理
保留期限；桥不自动删除文件。

## HTTP 接口与容量

- `GET /health` 返回 `{ "ok": true }`。
- `GET /v1/models` 返回包含服务名的 OpenAI 模型列表。
- `POST /sleep?level=N`、`POST /wake_up` 设置睡眠状态；
  `GET /is_sleeping` 查询状态。桥没有需要卸载的 GPU 模型。
- `POST /v1/chat/completions` 返回非流式完成消息，助手内容就是 `answer.json` 文本。
  完全省略 `usage`：未知 token 或费用不等于零。真实外部成本另行报告，并关联请求目录
  或 completion ID。

睡眠后拒绝新请求（503），已准入请求继续等待。默认同时等待一个请求；超额并发返回
429，不发布目录。`--max-pending` 允许 1–16 个并发请求，各自按目录 ID 作答，不保证
FIFO 顺序。最多 32 个 HTTP 连接线程，超额连接关闭；响应后关闭连接。请求体读取有
总截止时间，请求行和请求头共用独立的绝对截止时间。客户端断开不会取消已准入请求。

| 参数 | 默认 | 上限 |
| --- | --- | --- |
| `--max-request-bytes` | 32 MiB | 128 MiB |
| `--max-answer-bytes` | 1 MiB | 8 MiB |
| `--answer-timeout-s` | 30 秒 | 3600 秒 |
| `--request-timeout-s` | 60 秒 | 7200 秒 |
| `--read-timeout-s` | 10 秒 | 60 秒 |

所有限额必须为正且有限。请求过大返回 413，请求无效返回 400，答案超时返回 504，
已发布答案无效或不可读返回 502。超时进入调用方的答案不可用路径，不代表视觉判断。
不支持 HTTP 流式输出、远程图片获取或分块请求传输。

## 调用方超时绑定

启动强制校验 `answer_timeout_s + 2 * read_timeout_s < request_timeout_s`，
为请求头、请求体各保留读取预算，并留出响应余量。默认答案等待 30 秒、读取 10 秒、
调用方 60 秒，与 AERI 默认匹配。`--request-timeout-s` 声明真实调用方预算，不会修改
调用方。新任务必须将同值冻结在 `plan.subtask.vlm.request_timeout_s`，不能把该字段
放在 binding 块里。桥无法读取远端调用方配置；不能用旧冻结任务搭配新的超时，也不
修改既有实验配置。

长等待外部会话应新建任务，VLM plan 片段例如：

```json
{"provider_mode":"external","port":18991,"model":"frozen-model","served_name":"external-agent",
 "min_model_len":4096,"max_model_len":4096,"gpu_memory_utilization":0.6,"request_timeout_s":600}
```

`subtask.vlm.provider_mode` 可显式写为 `external` 或 `local`，只声明配置的作答方式。
它进入新任务的冻结计划和计划 SHA；省略或 null 不改变旧计划表示。运行页从摘要校验
通过的计划读取并显示“配置的作答方式”；旧记录、缺失或无效字段，以及旧服务未提供
该快照字段时，均显示“未知”。此声明不选择后端，也不验证实际作答服务身份；不改变
模型、GPU 预算或服务生命周期。不会根据模型名称、命令行或 usage 推断。页面仍只读，
作答方的判断不等于操作员标签。任务 schema 的 minor 3 增加此可选字段。


对应 binding 的 `subtask_vlm` 块示例如下。路径与资源数值仅用于说明；实际必须保留
已审核的策略显存需求与共享 GPU 锁，并使用已安装 LEVI 的绝对解释器路径。示例不授权
启动或真机操作。

```json
{
  "subtask_vlm": {
    "base_url": "http://127.0.0.1:18991",
    "model": "external-agent",
    "service": {
      "unit": "levi-aeri-vlm-external",
      "argv": ["/path/to/venv/bin/python", "-m", "levi.automatic.external_bridge",
        "serve", "frozen-model", "--host", "127.0.0.1", "--port", "18991",
        "--served-model-name", "external-agent", "--queue", "/path/to/owned/queue",
        "--max-model-len", "4096", "--gpu-memory-utilization", "0.6",
        "--request-timeout-s", "600", "--answer-timeout-s", "540"],
      "gpu_lock": "/path/to/owned/gpu.lock",
      "need_policy_mib": 12000,
      "ready_timeout_s": 1800
    }
  }
}
```

540 秒等待必须显式增大调用方预算（如 600 秒），否则启动拒绝。等待文件期间不能
可靠观测客户端断开，槽位最多占用到答案截止时间。`answer_ready` 同时记录
`consumption: "unknown"`，只表示答案已准备，不代表客户端收到或执行。迟到答案保持
超时审计状态。文件系统或操作系统阻塞不属于硬实时保证。

## 文件协议

每个已准入的有效请求创建新的 `request-*` 目录，重启不重置名字，也不复用历史目录
或答案。桥写入 `request.json`（图片 URL 改为相对文件名）、`prompt.txt`、
`schema.json`（完整 `response_format`）及依序排列的 `img_1.png`、`img_2.jpg` 等。
只接收严格 base64 编码的 `data:image/png`、`image/jpeg`、`image/webp`；保存图片字节，
不解释图像。

**消费者必须忽略没有 `READY` 的目录。** 所有请求文件关闭后才原子发布这个 JSON
标记。写入失败可能留下没有标记的目录，不能把它当成待答请求。此协议保证可见性，
不保证断电后的持久性。

独立获授权的智能体读取已就绪请求及其 schema，在同目录写临时答案，再原子重命名
为 `answer.json`。该文件直接包含助手答案，不包 OpenAI completion 外壳。只发布一次，
内容为 UTF-8 JSON，不加 Markdown 围栏，不含重复键、NaN 或 Infinity。
桥拒绝超限文件、符号链接、非普通文件和无效 JSON。任务语义仍由 AERI 的 schema、
白名单、指令模板与人工放行守卫校验。不要修改已发布请求，也不要替换目录。

可写时，`result.json` 记录 `answer_ready`、`timeout` 或 `invalid_answer`。消费者不能认领
已有结果标记的请求。超时后迟到的答案只保留审计，不能影响其他请求。旧版 lab `wait_request` 工具依赖序号目录，不能直接用于新协议；
消费者必须支持 READY。现有 lab 工具及冻结实验配置不作修改。重启不会恢复
旧等待：分配新工作前，应核对旧会话遗留的 READY 目录。多个外部作答者需自行协调
独占认领，桥不分配或派生它们。答案不能当成操作员成功/失败标签。

## 离线验证

`tests/automatic/test_external_bridge.py` 覆盖发布边界、重启唯一目录、内联图片校验、
普通文件读取上限、可注入时钟截止时间、容量/睡眠、usage 缺失、命令行边界和内存流
HTTP 契约。测试不监听端口、不导入 GPU 框架；执行仍受仓库测试资源和机器人安全门禁
约束。

每题还生成 `BRIEF.md`，在发布 READY 前写完关闭。已获授权的派题方可引用该文件，
不必重复通用任务书。它标识当前请求并指向原始消息、schema 与图片，不新增任务特定
成功标准或机器人权限。本候选减少需要重复编写的派题文本；尚未测量 token、费用
或作答质量变化，也不会自行启动作答者。

## 可选自报证据（本片仅持久化）

作答者可在当前请求目录先原子发布固定文件 `report.json`，最后原子发布
`answer.json`；都先写临时文件再重命名。只写答案的旧消费者仍兼容。
报告只是可选证据；缺失、非法或错配不能改变业务答案、操作员标签或控制状态。
不搜索或导入旧会话、proxy 日志、`agent_notes.txt`。

```json
{
  "schema": "levi.aeri.external_report.v1",
  "request_id": "request-example",
  "answer_sha256": "<answer.json 原始 UTF-8 字节的 SHA-256>",
  "agent_id": "worker-7",
  "agent_notes": "当前可见物体已放在目标上，且离开夹爪。",
  "elapsed_ms": 18400,
  "tokens": {"input_new": 1200, "output": 240, "cache_read": null, "cache_write": null}
}
```

必填仅 schema、请求 ID 与答案摘要。其他字段可以缺失或 null；但 `tokens` 若出现
必须为对象，其四项可各自缺失或 null。未知键使整份报告无效。请求 ID 必须等于
当前请求目录/completion ID。摘要覆盖原始字节，包括空白和换行，不得 strip、Unicode
归一化或解析后重新序列化。桥将原始 UTF-8 解码后原样传入 content，客户端对完整
content 重编码验证。因此只证明请求与内容对应，不认证身份、计费、语义正确性或
人工批准。

限额：整份报告 16 KiB；`agent_notes` 4096 UTF-8 字节；`agent_id` 为 1–64 个
ASCII 字母、数字或 `_ . : -`；`elapsed_ms` 为 0–86400000 的整数；四项 token
分别为 0–9007199254740991 的整数。拒绝 bool、数字字符串、小数、负数、非有限数、
重复 JSON 键和非法 UTF-8。只读取有界普通文件，不跟随符号链接，也不读取报告指定的
路径或远程资源。依据只能是作答者已公开、面向操作员的简短观察/说明，不能采集私有
内部推理。身份、耗时和四项 token 均属自报；缺失保持未知，真实零保留零，不擅自对
缓存维度求和、不推算其他计数或价格。

每轮答案轮询先取报告快照，再检查答案；接受答案所用快照即为最终版本，不等待报告、
不刷新已完成响应、不补录迟到报告。快照后才发布的报告保持未知。先报告后答案是
写入者协议，不把文件时间戳当作真实发布先后的认证。超时请求的报告和答案不能移入
后续请求。

completion 增加独立 `levi_external`，包含 schema
`levi.aeri.external_evidence.v1`、`bridge_wait_ms`、`report_status`、
`report_source: "external_self_reported"` 与规范化 `report` 或 null。
状态为 `reported / unavailable / invalid / mismatch / unsupported_version`。
`bridge_wait_ms` 是 READY 发布完成后至接受答案的单调时钟观测，区别于自报
`elapsed_ms` 和客户端 HTTP `latency_ms`。标准 OpenAI `usage` 继续省略，不把自报
计数灌入标准输入/输出 token 字段。本地/proxy 缺报告仍未知；尚未实现 proxy-log 导入。

客户端对扩展重新验证，完整答案摘要/请求 ID 匹配后记录 `binding_verified`，只代表
内容关联。扩展非法时丢弃扩展，不改变答案处理。`VlmReply` 将证据传到运行目录
`subtasks/<sequence>-request-evidence.json`，旧请求记录只保存 `request_evidence`
文件/摘要引用。新文件不以 `-detail.json` 结尾，现有公开 note 自动展开不会读取它；
现有 plan 投影也不返回该请求证据。**本片只新增持久化，不增加 API/UI 自由文本展示。**

独立证据文件保存实际客户端提示及答案最多 20000 字符的展示前缀，同时记录完整
UTF-8 字节数、SHA-256 与 `prompt_text_truncated` / `raw_content_truncated`。
截断前缀不能独立重验完整内容摘要。这些有界文本加报告及正常 AERI 帧引用保持在
现有 1 MiB sidecar 预算内。报告不能覆盖真实提示。

`evidence_context` 由宿主编排器生成：inventory 的轮次/片段 ID 强制 null；方向计划
使用当前权威 `round_id`；check 额外使用当前片段 ID，并核对本地请求记录和循环方向。
缺失或冲突时记录 null，不从轮次整数或提供者文本猜测。历史记录不回填。后续只读 UI
必须先实现按 round label/member 精确判断可见性，才可展示该新文件；本片不改变现有
API 或历史可见性行为。

### 只读请求证据

运行窗口仅在服务端核对宿主生成的精确 round ID、journal 和本轮最新人工
`success` / `failure` 标签后，投影私有请求证据。检查还须匹配 started episode
和该轮正向/逆向成员；逆向成员补写前，逆向检查保持隐藏。inventory、discarded、
只有成员没有标签、缺少准确上下文的旧记录，以及损坏或冲突关联均隐藏。后来改为
丢弃会隐藏后续响应，但不能撤回已经看过的内容。轮次数字分组和自报身份不授予权限。

投影复用已有 subtask 响应，不增加请求或写操作。隐藏行不包含新增自由文本或私有
侧车引用。标签和证据使用有界、禁止符号链接的读取器。每次最多展示最近 16 题，
累计读取最多 4 MiB（每次读取前预留单文件额度），自由文本最多 64,000 字符；
截断标记说明省略项。完整 journal 的关联验证先于展示裁剪，不改变旧侧车上限。

作答者 ID 和备注均为自报，不是认证身份或操作员标签。新增输入、输出、缓存读取、
缓存写入四项 token 独立保留：未知不变成零，显式零不丢失。客户端请求耗时、桥等待
耗时和作答者自报耗时覆盖不同区间，不推算账单。提示词和答案以普通文本渲染；
字节数和 SHA-256 指完整内容截断前的值，显示前缀（或经过路径隐藏的文本）不能
独立核验全文摘要。


## 显式导入已脱敏的本地 agent 导出

`python -m levi.automatic.local_log_import` 将**显式准备的一份已脱敏导出**转换为
已有可选 `external_report.v1` 对象。这是 LEVI 自定义交换格式，不是 Codex/Claude
原生会话、JSONL transcript、工具日志或账单解析器。不会自动发现会话、扫描目录、
发布队列、回填证据或写人工标签；原始私有会话不是合法输入。

由用户准备一个独立、本人拥有的暂存目录，输入仅为以下固定文件（其他文件忽略，不扫描）：

- `agent-export.json`：严格 UTF-8 JSON 对象，字段恰为 `schema`、`source`、
  `redacted`、`report`。schema 为 `levi.aeri.agent_log_export.v1`；source 仅可为
  `codex`、`claude`、`other`、`synthetic`，由导出者声明，不是认证来源；redacted
  必须为字面值 `true`。report 遵守前述可选报告契约，包括精确请求 ID 与答案摘要。
- `answer.json`：桥所用的原始 assistant-content JSON，**不是 OpenAI completion
  响应封套**或 Markdown。必须保留所有 UTF-8 字节、空白和换行；导入器不 strip 或
  重序列化答案。对象不得含 `choices` 或 `usage`。这里只检查 JSON 传输边界；
  inventory/plan/check 的题目事实、白名单等语义仍由正常 provider 校验。

准备者必须事先移除所有导出字段中的凭据、私有路径、无关对话、工具输出与私有推理。
agent_notes 只能放有意公开的摘要。`redacted: true` 是声明，**不是脱敏成功证明**；
命令不能识别所有秘密，也不会打开 JSON 内提及的路径。如需审计声明来源，应另行保留
审阅后的导出；来源/脱敏声明不会新增到既有 report schema。

```sh
python -m levi.automatic.local_log_import \
  --export-dir /absolute/owned/redacted-export \
  --request-id request-example
```

请求 ID 必须独立取自预期桥请求。成功时 stdout 仅输出规范报告 JSON；失败退出码为 2，
stderr 仅含固定错误码，不输出报告。不创建输出文件。结果可经正常审阅与发布流程用于
已有可选 `report.json` 协议；命令不代发布，也不会令迟到报告显示。精确轮次及人工标签
可见性规则不变，不修改 provider 标准 usage、答案、时间或控制决策。

目录必须是绝对路径，不含隐藏组件，所有层级均不得是软链接。只打开固定文件名，拒绝
软链接；文件须为当前用户拥有、单一硬链接的普通文件。导出上限 20 KiB，答案上限
1 MiB，JSON 深度上限 16，输出报告上限 16 KiB。重复键、非有限数、未知导出/报告字段、
缺脱敏声明、ID/摘要不符与非法报告均拒绝。各 token 的缺失/null 与合法零独立保留，
bool 不作整数。身份、耗时与 token 都是自报，不是认证或计费事实；不从模型名或会话推算。

完全合成样本位于 `tests/automatic/fixtures_local_import/local_agent_export_v1.json`，对应完整答案
字节由 `tests/automatic/test_local_log_import.py` 的 `ANSWER` 给出。它只是格式示例与
回归夹具，不是实测外部 agent 证据。
