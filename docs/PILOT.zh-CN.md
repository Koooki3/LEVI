# Codex / Claude Code Pilot 顺序指南

当前功能为实验性，首批支持 Linux、WSL 与 VS Code Remote/SSH。
“离线”指不开 LEVI 网页，允许本机后台核心运行，不代表模型无需联网。
API 模型与 Codex/Claude Pilot 共用 Harness、计划审批、证据、草稿、验证和提交。
完整接口、限制与来源见 [英文技术指南](PILOT.md)。

## 1. 安装与登录

在 LEVI 根目录执行。所有终端使用同一 `LEVI_WORKSPACE`，默认是仓库 `.state/`。

```bash
uv sync --locked --extra agent
bun install --frozen-lockfile
# 仅托管模式需要；PATH 中还需要 Node.js >=22
(cd integrations/pilot && bun install --frozen-lockfile)
bun run build
```

适配器锁定 Codex ACP 1.12.0 与 Claude Agent ACP 0.79.0，使用其配套运行时。
已有终端/IDE 通过 MCP 接入时不要求安装这些适配器。
LEVI 复用官方本机登录，不复制账号凭据，不替换用户全局配置：

```bash
codex login
# 或
claude auth login
```

安装成功不等于已登录。无法可靠读取登录状态时显示“未知”，实际连接失败会明确阻塞。
切换账号前先暂停/断开 Pilot；官方退出可能影响同机其他 IDE/CLI，LEVI 的“断开”只撤销 LEVI 连接。
迁移机器后重新生成连接配置，不复制旧绝对路径、连接密钥或 checkpoint 配置。

## 2. 将已有 Codex / Claude Code 接入 LEVI

通过现有数据集管理注册数据，取得 `local/<目录名>`；内置两个演示集 ID 也可直接使用。
先查看配置差异，再应用：

```bash
uv run levi agent connect --client codex --project . --dataset local/my_dataset
uv run levi agent connect --client codex --project . --dataset local/my_dataset --apply
# Claude Code 将 --client codex 改成 --client claude
```

只添加项目级 MCP 配置和缺失的 LEVI skill，保留其他条目。
已有 `levi` 条目不会被覆盖：先断开该连接，再移除这一项目条目，重新连接。
在官方客户端重新加载 MCP，并按客户端要求信任项目。
VS Code Remote/SSH 中工具进程运行在数据所在主机；需要网页时转发前端端口。

让 Agent 发现工具、检查数据、澄清缺失要求并创建计划。MCP 工具名用双下划线，例如
`runs__plan`、`runs__prepare`、`media__sample`、`annotations__propose_segments`、
`view__seek`、`runs__finish`、`runs__result`。旧点号调用仍兼容。
连接按数据集授权，默认 24 小时有效，具有调用上限；不授予审批和提交权限。

## 3. 离线人工审批与试标

已有 Agent 可通过工具创建计划；也可在 JSON 中填写真实数据 ID、相机、目标和预算：

```json
{
  "provider": "external",
  "pilot_runtime": "codex",
  "repo_id": "local/my_dataset",
  "episodes": [0],
  "cameras": ["observation.images.front"],
  "instruction": "检查本片段，基于证据标记失败和不确定动作。",
  "allow_media_egress": true,
  "workflow": {"kind": "review"},
  "budget": {"max_calls": 8, "max_tokens": 16000, "max_seconds": 300}
}
```

托管 Claude 使用 `pilot_runtime: "claude"`。时序子任务需补充可观察的定义，物体标注需补充目标；
详见 [Harness](HARNESS.md)。外发许可只覆盖已选证据，不代表批准无界限上传。

```bash
uv run levi agent plan create plan.json
uv run levi agent plan show <run-id>
uv run levi agent plan approve <run-id>
```

人工命令显示具体修订，要求在交互终端输入 `approve`；不接受管道确认。
Agent 不能通过 MCP 自行批准。这是应用权限边界，不是针对同一操作系统用户的安全沙箱。

批准后，可让原终端 Agent 执行试标；或启动 LEVI 托管会话：

```bash
uv run levi agent pilot start <run-id> --runtime codex
uv run levi agent pilot status
uv run levi agent events <run-id>
uv run levi agent permission list
uv run levi agent permission answer <permission-id> --option <option-id>
```

运行时权限与执行计划、试标验收、最终提交权限分别处理。通用终端和文件操作不会因为模型请求而放开。

## 4. 修正、验收、提交与导出

```bash
uv run levi agent changes show <changeset-id>
# 可选：完整编辑后的 proposals JSON 数组
uv run levi agent changes edit <changeset-id> --file proposals.json
uv run levi agent changes validate <changeset-id>
uv run levi agent pilot review <run-id> --text "已核对证据和时间边界"
# 不通过时加 --reject，并说明需要修改的内容
```

掩码必须通过现有播放器/sidecar 审阅；终端 RLE 文本不是视觉验收的替代品，待审核掩码会阻止提交。
试标通过后，通知外接 Agent 继续；托管会话可发送：

```bash
uv run levi agent pilot message <session-id> --text "继续处理剩余已批准范围"
uv run levi agent changes review <changeset-id>
uv run levi agent changes commit <changeset-id>
uv run levi agent changes export <changeset-id>
uv run levi agent result <run-id>
```

审核、提交、导出各自确认。提交使用修订专属幂等键。导出复用原生导出和重读校验，
默认导出完整数据集，未选片段保持不变，不将其宣称为范围筛选导出。

## 5. 在线追踪与恢复

```bash
uv run levi
# 或启动网页并选中已有任务
uv run levi agent open <run-id>
```

前端连接同一个核心，不重新执行任务。Dock 中选择 API / 外部 MCP、Codex Pilot 或 Claude Code Pilot，
查看实时工具流水、公开运行时事件、待审批项和实际产物。
外接会话只完整记录 LEVI 操作，不读取其他聊天；托管会话额外记录运行时公开事件。
证据定位复用播放器，“跟随”默认关闭，未保存草稿时暂停，跨片段需要保存后手动切换。

```bash
uv run levi agent pilot pause <session-id>
uv run levi agent pilot resume <session-id>
uv run levi agent pilot cancel <session-id>
uv run levi agent disconnect <connection-id>
uv run levi agent core status
uv run levi agent core stop
```

暂停/取消先撤销新工具调用，再中断运行时；请求停止不等于进程已经退出。
恢复会新建对话并复用任务、证据和草稿，不承诺原生聊天记录续接。
累计回合/时长预算不因重连清零；未知 token/费用不能显示为零或硬账单保证。
关闭浏览器或前端不会停止核心，需显式 `core stop`。核心重启后标记旧会话中断，不自动重新推理。

产物位于 `LEVI_WORKSPACE/outputs/LEVI/workbench/agent/datasets/<数据集名>/runs/<时间戳>/`，
包括证据、对象草稿、公开事件以及 `result.json` / `result.md`；清单路径按 `path_base` 解析。
正式导出沿用 `outputs/LEVI/exports/<数据集名>/…`，不引入哈希后缀，不改动原始数据集。
不要将运行目录、连接凭据和机器专属配置发布到 GitHub。

自动测试使用模拟模型、协议进程和浏览器 API，不运行 GPU、CUDA 探测、真实推理或 checkpoint 下载。
真实 CLI/IDE 账号、图像理解、标注精度和实际费用仍须单独授权后验收。

托管会话在执行前预留时长预算，并跨恢复累计。核心异常退出后，无法核对的
预留仍保守计入消耗；若预算耗尽，需修订计划预算并重新批准后继续。
