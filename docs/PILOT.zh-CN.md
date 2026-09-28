# Codex / Claude Code Pilot 顺序指南

状态：实验性。首批支持 Linux、WSL 和 VS Code Remote/SSH。
“离线”指不打开 LEVI 网页（本机后台核心照常运行），不代表模型不用联网。
API 模型与 Codex/Claude Pilot 共用 Harness、计划审批、证据、草稿、验证和提交。
完整接口、限制与来源见 [英文技术指南](PILOT.md)。

## 1. 安装与登录

在 LEVI 仓库根目录执行，所有命令使用同一个 `LEVI_WORKSPACE`（默认是仓库下的 `.state/`）。

```bash
uv sync --locked --extra agent
bun install --frozen-lockfile
# 仅托管模式需要；PATH 中还需要 Node.js >=22
(cd integrations/pilot && bun install --frozen-lockfile)
bun run build
```

可选依赖锁定了 Codex ACP 1.12.0 和 Claude Agent ACP 0.79.0，使用它们自带的运行时。
已有终端/IDE 通过 MCP 接入时，不需要安装这些适配器。
LEVI 复用官方本机登录，不复制账号凭据，不替换用户全局配置：

```bash
codex login
# 或
claude auth login
```

安装成功不等于已登录。无法可靠读取登录状态时显示“未知”；实际连接失败时会明确阻塞。
切换账号前先暂停或断开 Pilot。用官方客户端退出登录可能影响同一台机器上的其他 IDE/CLI；LEVI 的“断开”只撤销 LEVI 连接。
换机器后要重新生成连接配置，不要复制旧的绝对路径、连接密钥或 checkpoint 配置。

## 2. 将已有 Codex / Claude Code 接入 LEVI

先用 LEVI 的数据集管理登记数据集，取得 `local/<目录名>`；两个内置演示数据集的 ID 也可以直接使用。
先查看配置差异，再应用：

```bash
uv run levi agent connect --client codex --project . --dataset local/my_dataset
uv run levi agent connect --client codex --project . --dataset local/my_dataset --apply
# Claude Code 将 --client codex 改成 --client claude
```

只添加项目级 MCP 配置和缺失的 LEVI skill，保留其他条目。
已有 `levi` 条目不会被覆盖：先断开该连接，再移除这一项目条目，重新连接。
在官方客户端中重新加载 MCP，并按客户端的要求信任项目。
在 VS Code Remote/SSH 或 WSL 中，LEVI 和工具进程都运行在数据所在的主机上；需要网页时再转发前端端口。

让 Agent 发现工具、检查数据、澄清缺少的要求，然后创建计划。MCP 工具名用双下划线，例如
`runs__plan`、`runs__prepare`、`media__sample`、`annotations__propose_segments`、
`view__seek`、`runs__finish`、`runs__result`。旧的点号工具名仍可调用。
连接按数据集授权，默认 24 小时有效，有调用次数上限；不授予批准和提交权限。

## 3. 离线人工审批与试标

可以让已接入的 Agent 用工具创建计划，也可以自己写 JSON 文件，填入真实的数据集 ID、相机、目标和预算：

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

托管 Claude 使用 `pilot_runtime: "claude"`。时序子任务要给出可观察的定义，物体标注要给出目标；
详见 [Agents](AGENTS.md)。外发许可只覆盖已选的证据，不等于允许不受限制地上传。

```bash
uv run levi agent plan create plan.json
uv run levi agent plan show <run-id>
uv run levi agent plan approve <run-id>
```

批准命令会显示具体的修订版，并要求在交互式终端中输入 `approve`；不接受通过管道传入的确认。
Agent 不能通过 MCP 自行批准。这是应用层的权限边界，不是能约束同一操作系统用户下 agent 的安全沙箱。

批准后，可以让原终端里的 Agent 执行试标，也可以启动 LEVI 托管会话：

```bash
uv run levi agent pilot start <run-id> --runtime codex
uv run levi agent pilot status
uv run levi agent events <run-id>
uv run levi agent permission list
uv run levi agent permission answer <permission-id> --option <option-id>
```

运行时权限与计划执行、试标验收、最终提交的权限分开处理。通用的终端和文件操作不会因为模型请求而放开。

## 4. 修正、验收、提交与导出

```bash
uv run levi agent changes show <changeset-id>
# 可选：完整编辑后的 proposals JSON 数组
uv run levi agent changes edit <changeset-id> --file proposals.json
uv run levi agent changes validate <changeset-id>
uv run levi agent pilot review <run-id> --text "已核对证据和时间边界"
# 不通过时加 --reject，并说明需要修改的内容
```

掩码必须用现有的播放器/sidecar 审阅；终端里的 RLE 文本不能代替视觉验收，待审核的掩码会阻止批准。
试标通过后，通知外接 Agent 继续；托管会话可以发送消息：

```bash
uv run levi agent pilot message <session-id> --text "继续处理剩余已批准范围"
uv run levi agent changes review <changeset-id>
uv run levi agent changes commit <changeset-id>
uv run levi agent changes export <changeset-id>
uv run levi agent result <run-id>
```

审核、提交、导出分别确认。提交使用与修订版绑定的幂等键。导出复用原生导出器，并重新读取、校验生成的文件；
导出的是完整数据集，未选中的片段保持不变，不会把它说成按范围筛选的导出。

## 5. 在线追踪与恢复

```bash
uv run levi
# 或启动网页并选中已有任务
uv run levi agent open <run-id>
```

前端连接同一个核心，不会重新执行任务。在 Dock 中选择 API / 外部 MCP、Codex Pilot 或 Claude Code Pilot，
查看实时工具调用记录、公开的运行时事件、待批准项和实际产物。
外接会话只完整记录 LEVI 操作，不读取其他聊天内容；托管会话还会记录公开的运行时事件。
证据定位复用播放器；“跟随”默认关闭，有未保存的草稿修改时暂停；切换到其他片段需要先保存再手动打开。

```bash
uv run levi agent pilot pause <session-id>
uv run levi agent pilot resume <session-id>
uv run levi agent pilot cancel <session-id>
uv run levi agent disconnect <connection-id>
uv run levi agent core status
uv run levi agent core stop
```

暂停/取消先禁止新的工具调用，再中断运行时；请求停止不等于进程已经退出。
恢复时会新建对话，复用 LEVI 保存的任务状态、证据和草稿，不保证续接原生聊天记录。
累计的回合/时长限制不会因重连而清零。已取消的任务不能重新启动。未知的 token/费用不会显示为零，也不会当作有保证的计费上限。
关闭浏览器或前端不会停止核心，需显式 `core stop`。所属 run 结束，或超过 `LEVI_PILOT_IDLE_SECONDS`（默认 600 秒）没有新消息时，会话连同其运行时进程一起结束。核心重启后会把旧会话标记为中断、撤销它们的授权，并终止被杀掉的核心遗留的运行时进程，不会自动恢复任务（见 [Workspace](WORKSPACE.md#processes-levi-starts-and-how-they-stop)）。

产物位于 `LEVI_WORKSPACE/outputs/LEVI/workbench/agent/datasets/<数据集名>/runs/<时间戳>/`，
包括证据、物体草稿、公开事件以及 `result.json` / `result.md`；清单中的路径相对于 `path_base` 解析。
正式导出仍放在 `outputs/LEVI/exports/<数据集名>/…`，不加哈希后缀，不改动原始数据集。
不要把运行目录、连接凭据和本机专用配置发布到 GitHub。

自动测试使用模拟模型、协议进程和浏览器 API，不运行 GPU、CUDA 探测、真实推理或 checkpoint 下载。
真实 CLI/IDE 账号、图像理解、标注精度和实际费用仍须单独授权后验收。

托管会话在执行前预留时长预算，并跨恢复累计。核心异常退出后，无法核对的
预留仍保守地计入消耗；预算耗尽时，需修订计划预算并重新批准后才能继续。
