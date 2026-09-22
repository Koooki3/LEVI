# 本地 Ollama 与外部教师监督

[English guide](OLLAMA.md)

此实现将 Ollama 原生接口接入现有的计划、证据、标注校验、人工审核与提交流程。
它不会训练 Qwen 权重，不提供自动晋级，也没有替换数据转换系统。通用 DAG 执行与
其余 0.4.0 架构仍在迁移，真实完成范围见[实施状态](architecture/IMPLEMENTATION_STATUS.md)。

## 1. 准备 LEVI

在克隆的仓库目录中依次运行：

```bash
export LEVI_WORKSPACE="$PWD/.state"   # 也可使用已有工作区
uv sync --locked --extra agent
uv run levi setup
uv run levi build
uv run levi
```

打开启动器打印的 Web 地址，不要访问后端端口。按照
[Ollama 官方说明](https://docs.ollama.com/download)另行安装 Ollama。
打开模型设置页不会自动安装程序、下载模型、探测加速器或执行推理。

## 2. 选择服务归属

**已有服务：** Agent 工作台 → 模型设置，选择“Ollama · 本地服务”，设置连接名、
`http://127.0.0.1:11434` 和 `qwen3.5:4b`，明确允许回环地址并保存。无需 API Key。
模型目录由外部服务管理，LEVI 不猜测其 checkpoint 位置。此适配器暂不支持非回环地址。

**专用实例：** 账号与连接 → LEVI 专用 Ollama 服务，检查安装，选择空闲端口
（默认 11435），授权并启动。真实服务启动可能初始化硬件。模型位于
`$LEVI_WORKSPACE/checkpoints/ollama`，进程记录与日志位于工作区内的
`outputs/LEVI/workbench/models/ollama`。点击“配置 Qwen 连接”后，在连接卡片中检查模型。
复用连接名称前请检查已有配置，避免覆盖仍需保留的配置。

专用实例设置 `OLLAMA_NO_CLOUD=1`、单请求并发、单模型驻留；这不等于操作系统网络隔离，
也不等于取得 GPU 独占租约。停止服务会核验启动标识并使用 Linux pidfd，避免误停 PID
被复用后的其他进程。有活动 LEVI 任务或下载时，界面拒绝停止。服务可跨前端/核心重启
保留并重新连接；不用时请在此面板明确停止。

## 3. 检查、下载、绑定

1. 点击“检查本地模型”，只读取清单、版本和详情。
2. 模型不存在时展开“手动下载模型”，检查许可证和磁盘空间，授权后开始。
   Qwen 许可见[官方模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)。
3. 进度显示当前层的真实字节数，不伪造整体百分比。关闭页面不会取消持久化后台作业。
4. 需要时明确取消；其他 Ollama 客户端共享的下载可能继续。失败或中断可用新请求重试，
   已完成的缓存块保留在 Ollama 中。
5. 下载完成后再次检查，确认结构化 JSON 能力，按服务声明选择图像输入，再绑定模型。
6. 绑定记录完整 digest、模板摘要、版本与声明能力，不代表通过了模型质量验收。
   模型或配置变更后需新建计划。执行过程中不会隐式下载缺失模型。
7. 可选加载/卸载模型内存需要单独硬件授权。这会影响服务级模型驻留，可能影响其他客户端。

只有 Ollama 内部的内容寻址 blob 保留引擎原生命名；LEVI 业务产物继续使用数据集名称
和可读任务 ID。下载大小不等于显存需求。本轮没有做真实硬件或质量验收。

## 4. 计划、试标、审核、发布

新建任务时选择该配置，明确少量 episode、相机与证据共享授权。批准固定计划、执行
试标、核对实际证据并修改草稿。试标验收和最终提交仍由人工分别批准。本地适配器发送
带时间信息的证据图片和有界结构化上下文，不假设原生视频输入。现有 schema、范围和证据
校验会拒绝无效结果，不会静默切换云服务。

供应商未报告用量时按预留量保守计费并明确标记，不显示成“实测零消耗”。分词与视觉
token 成本无法预先精确确定；调用前预留和调用后检查不等于供应商级输入 token 硬上限。

## 5. 指定 Codex / Claude 教师

先按 [Pilot 指南](PILOT.zh-CN.md)创建限定数据集的连接，例如先查看：

```bash
uv run levi agent connect --client codex --help
# 按提示检查项目级配置差异并显式应用。
```

新任务的“外部教师监督”选择该连接。证据共享授权同时覆盖学生与教师。当前“影子对照”
与“监督标注”均在每个阶段等待反馈；模式用于记录和评估，不自动扩大权限。

学生运行后，教师通过 `supervision.pending`（MCP 名称 `supervision__pending`）查询
任务阶段，通过 `evidence.read` 获取真实图像，再提交反馈：

```json
{
  "name": "supervision.feedback",
  "arguments": {
    "run_id": "review-20260922T1000",
    "teaching_id": "review-20260922T1000:0:coarse",
    "revision": 0,
    "decision": "accept",
    "note": "引用帧支持该时间区间的建议。"
  }
}
```

`revise` 需同时提供完整且校验通过的 `ModelOutput`，放在 `output` 字段；`reject`
保持阻塞。反馈绑定阶段版本与输入摘要，仅指定连接或人工控制入口可以提交。
重复相同反馈返回既有记录；修改已接受反馈会报告冲突。教师不能通过其他草稿工具
绕过此门槛，也不能批准计划、试标或发布。

接受反馈后，在原任务中恢复试标/执行；已结算的模型阶段复用缓存，即使原调用已耗尽
调用预算也不重复推理。教师断开或过期时任务阻塞。界面沿用 SSE 日志更新；教学记录
保存在同一工作区 SQLite 中，未单独报告的教师用量明确为未知。不会自动训练、发布技能、
晋级能力或进行隐藏的教师推理。

## 开发验证

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_execution_contracts.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
bun run format && bun run validate && bun run build
# 可选：使用已安装 Chromium；模型 API 使用固定替身，Chromium 禁用 GPU。
export LEVI_BROWSER_TESTS=1
export LEVI_CHROMIUM_EXECUTABLE=/path/to/installed/chromium
uv run pytest tests/test_ollama_browser.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-browser"
```

推理、下载和进程测试均使用替身。浏览器测试仅启动 Next.js，并拦截模型 API。
真实 Ollama/Qwen 推理、Codex/Claude 教学质量、GPU 竞争和 OS 隔离离线包仍需独立验收，
不能以这些自动测试代替。
