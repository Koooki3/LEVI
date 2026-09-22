# 本地模型（Ollama）

[English](OLLAMA.md)

LEVI 可以通过 [Ollama](https://ollama.com) 在本机运行视觉语言模型。默认档案为 `qwen3.5:4b`（Qwen3.5-4B，Q4_K_M，约 3.4 GB，支持图像输入）。本地模型和其他 agent 一样，走已批准的计划、证据校验、人工审核与提交：它只提出建议，发布由人决定。不需要 API key，也不会回退到云端。LEVI 会计量每一次请求。

本地模型有两个用途：

- **标注**：在已规划任务中完成粗采样与细化阶段，作为在线运行的模型。
- **自然语言任务**：把一句话需求解析成经过校验的任务规格（`levi agent task new "…"`，或工作台里的任务控制台），见 [Agents → 自然语言任务](AGENTS.md#natural-language-tasks)。

## 1. 安装 Ollama

LEVI 不会自行安装二进制或下载模型。请按[官方说明](https://docs.ollama.com/download)安装；没有 root 权限时，可以把发行包解压到自己的目录，再把 `ollama` 放进 `PATH`：

```bash
mkdir -p ~/tools/ollama && cd ~/tools/ollama
curl -L -o ollama.tar.zst https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst
tar --use-compress-program=unzstd -xf ollama.tar.zst
ln -sf "$PWD/bin/ollama" ~/.local/bin/ollama
ollama --version
```

## 2. 选择由谁运行服务

**LEVI 自管服务（推荐）**：Agent Workbench → Accounts & connections → *LEVI-owned Ollama service*，检查安装、选择端口（默认 11435）、授权并启动。也可以用操作员凭据在脚本里启动：

```bash
curl -X POST -H "x-levi-ui-token: $(cat "$LEVI_WORKSPACE/outputs/LEVI/workbench/agent/core/human.key")" \
  -H "Content-Type: application/json" -d '{"port": 11435, "approve_start": true}' \
  http://127.0.0.1:7861/api/levi/agent/v1/ollama/runtime/start
```

模型存放在 `$LEVI_WORKSPACE/checkpoints/ollama`，进程记录与日志在 `outputs/LEVI/workbench/models/ollama/`。服务以 `OLLAMA_NO_CLOUD=1`、单并发、单模型常驻运行；LEVI 重启后仍可接管，需要显式停止（`POST …/ollama/runtime/stop`）。

**自己运行的服务**：Model settings → *Ollama · local service*，地址 `http://127.0.0.1:11434`，模型 `qwen3.5:4b`，勾选允许本机回环地址。暂不支持非回环的远程 Ollama。

## 3. 下载并绑定模型

1. **Inspect local models** 只读取服务的模型清单与声明能力，不做推理。
2. 缺少模型时，先阅读许可（[Qwen3.5-4B 模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)），再用 **Download model explicitly** 下载；进度按层显示，关闭页面不会取消。也可以对同一服务、同一模型目录执行 `ollama pull qwen3.5:4b`。
3. **Bind installed model** 记录确切的 digest、模板、版本与能力（需确认结构化输出和图像输入）。模型或档案变化后需要新建任务计划；任务执行中缺失的模型不会被自动拉取。

## 4. 共享 GPU：守护进程

在别人训练或运行真机的共享 GPU 上，静默共用会拖慢他们的推理与控制循环，甚至导致显存不足。LEVI 的 GPU 守护会让路，并学习"让多久才够"：

- **每次本地模型请求前**（以及启动 LEVI 自管服务前）采样 `nvidia-smi`，给出 `free` / `shared` / `cooling` / `busy` 判决。被挡的 run 在该边界停下，原因里写明判决与预计等待，标记 `blocked_by: gpu`。
- **按工作负载的策略**（`workbench/models/gpu-policy.json`）：`protect`（默认，完全让路）、`share`（显存与利用率有余量时共存）、`ignore`（如显示服务）。Ollama 自己的 `llama-server` 按安装路径与父进程识别，绝不按名字。
- **学习的静默窗口**：按"重启后仍不变"的签名记录每个负载的出现与离开，离开后等待重启间隔 90 分位的 1.5 倍（60–1800 s；样本不足时用默认 `LEVI_GPU_QUIET_SECONDS=300`）。
- **服务运行期间**，守护每 15 s 采样一次（模型驻留时 2 s），受保护负载一出现就卸载模型——被这样打断的请求记为"抢占"而非模型失败——窗口过后**自动恢复**所有被 GPU 挡住的 run。
- `uv run levi agent gpu`（或 `gpu.status` 能力）显示判决、原因、见过的负载及其学到的窗口。
- 只有在约定共享 GPU 时才设置 `LEVI_GPU_SHARING=allow` 关闭守护。

## 5. 运行任务

在新任务中选择该档案（或给 `levi agent task new` 传 `--provider qwen-local`），选择较小的集和相机范围，并明确同意发送证据。批准计划、跑试点、审核试点，再执行其余集。模型收到带时间标注的证据图像和有界的上下文；结构与范围校验会拒绝格式错误或越界的输出。token 与耗时按阶段计量；每个阶段的结果和成本在老师审核前就已结算，恢复执行不会重复计费。

模型被卸载后，下一次请求会包含加载时间（`qwen3.5:4b` 在消费级 GPU 上约 20 秒）。结构化输出的调用默认关闭"思考"模式，因为它耗费的 token 和时间对结构化答案帮助很小。

## Harness 为小模型兜住什么

4B 模型对指令的遵循很松散，LEVI 不依赖它自觉。每次请求的输出 schema 都被收窄，使畸形答案根本无法被解码（Ollama 在解码时强制 schema）：

- 只允许计划允许的标注类型（有子任务定义的计划只要区间）、计划中的子任务 id、以及本次提供或草稿已引用的证据 id；
- 每个区间必须有 end、outcome 和一句证据说明；proposals 先于 summary 生成且 summary 很短，答案不会被叙述耗尽；
- 精修（refine）逐一返回草稿的区间并保持其子任务；移动超出计划边界窗口（即超出所给帧）的边界保留草稿值，区间保持顺序、相邻区间共享边界；
- 输出额度随预期区间数增长。

请求按模型量体裁衣：只发送紧凑证据行（id、图片序号、时间）；精修携带的图片数、每图与每字符的 token 成本都从本模型自己的计量请求中学习（`workbench/models/request-cost/<provider>.json`，首次用两次单 token 调用标定）。长 episode 分批精修，每批都在上下文与计划帧上限之内。仍然失败的答案保留其有效部分：有老师时连同原因交给老师；无老师时丢弃无效条目并记录（`answer_salvaged`）。无论答案是否有效，token 都会结算。

## 6. 模型从哪里学习

本地模型在任务之间唯一的学习途径是提示词。LEVI 会在每次请求中放入一份有长度上限的**简报**，内容在计划时冻结：

- 已提交工作沉淀下来的数据集概况、子任务词表和反复出现的不确定性；
- 老师针对该数据集和工作流写的规则，以及关于 LEVI 用法的规则；
- 来自**其他**集的示例，从不包含本次要标注的集；
- 已发布的 harness 改进经验；
- LEVI 内置的通用标注规则（`levi/knowledge/annotation.md`，见[内置知识](KNOWLEDGE.md)）。

模型权重不会被修改。

## 7. 老师参与的学习循环

任务可以在每个学生阶段前设置一位有范围限制的外部 agent 作为老师（Codex、Claude Code）：

```bash
uv run levi agent connect --client claude --project /path/to/teacher/project --dataset local/my_dataset --apply
uv run levi agent task new "…" --provider qwen-local --supervision supervised --teacher <grant-id>
```

在工作台中，于 **External teacher supervision** 选择该连接。老师用 `supervision.pending` 读取待审阶段，用 `evidence.read` 查看真实图像，然后通过 `supervision.feedback` 给出 `accept`（保留学生输出）、`revise`（给出完整且通过校验的修订输出）或 `reject`（该阶段保持阻塞）。反馈与该阶段的确切版本和输入绑定；只有指定连接或人可以提交；反馈永远不能批准计划、试点或发布。

每条反馈都会保存为文件（`datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json`），其中的批注会进入该数据集的记忆，供下一个任务使用。老师也可以修正自然语言任务的解析结果（`tasks.feedback`）：修正后的规格成为示例，批注成为后续解析的经验。老师已提交的结果可以固化为参考答案，用来给之后的学生 run 打分（`teaching/reference-<task>.json`、`teaching/grades.json`）。

## 开发检查

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_gpu_guard.py tests/test_teaching.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
```

这些测试使用模拟的模型响应、模拟的老师和模拟的 `nvidia-smi`，不衡量模型质量。

## 限制

- 小型本地模型的标注质量尚未确立。依赖它之前，请在自己的数据上用老师和参考答案实测。
- `OLLAMA_NO_CLOUD` 不等于网络隔离：没有操作系统级的离线沙箱。
- GPU 守卫通过 `nvidia-smi` 识别进程，它是礼让策略，不是 GPU 调度器或资源租约。
- 不训练权重，不自动发布技能，不自动晋级能力档案。
