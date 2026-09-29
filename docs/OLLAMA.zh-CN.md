# 本地模型（Ollama 或本地 vLLM 服务）

[English](OLLAMA.md)

LEVI 可以通过 [Ollama](https://ollama.com) 在本机运行视觉语言模型；Ollama 没有的模型，可以通过本地 OpenAI 兼容服务（如 [vLLM](https://docs.vllm.ai)）运行（见 [§8](#8-本地-openai-兼容服务vllm)）。默认档案为 `qwen3.5:4b`（Qwen3.5-4B，Q4_K_M，约 3.4 GB，支持图像输入）。本地模型和其他 agent 一样，要经过已批准的计划、证据校验、人工审核和提交：它只提出建议，由人发布。不需要 API key，也不会回退到云端。LEVI 计量每一次请求。

本地模型有两个用途：

- **标注**：作为在线运行的模型，完成已规划任务的粗扫与精修阶段。
- **自然语言任务**：把一句话解析成经过校验的任务规格（`levi agent task new "…"`，或工作台里的任务控制台），见 [Agents → 自然语言任务](AGENTS.md#natural-language-tasks)。

## 1. 安装 Ollama

LEVI 不会自行安装二进制文件，也不会自行下载模型。请按[官方说明](https://docs.ollama.com/download)安装 Ollama；没有 root 权限时，可以把发行包解压到自己有权限的目录，再把 `ollama` 加入 `PATH`：

```bash
mkdir -p ~/tools/ollama && cd ~/tools/ollama
curl -L -o ollama.tar.zst https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst
tar --use-compress-program=unzstd -xf ollama.tar.zst
ln -sf "$PWD/bin/ollama" ~/.local/bin/ollama
ollama --version
```

## 2. 选择由谁运行服务

**LEVI 专用服务（推荐）**：Agent 工作台 → 账号与连接 → *LEVI 专用 Ollama 服务*，检查安装、选择端口（默认 11435）、授权并启动。也可以在脚本里用操作员凭据启动：

```bash
curl -X POST -H "x-levi-ui-token: $(cat "$LEVI_WORKSPACE/outputs/LEVI/workbench/agent/core/human.key")" \
  -H "Content-Type: application/json" -d '{"port": 11435, "approve_start": true}' \
  http://127.0.0.1:7861/api/levi/agent/v1/ollama/runtime/start
```

模型存放在 `$LEVI_WORKSPACE/checkpoints/ollama`，进程记录与日志在 `outputs/LEVI/workbench/models/ollama/`。服务以 `OLLAMA_NO_CLOUD=1`、单并发、只加载一个模型的方式运行；LEVI 重启后服务继续运行，需要显式停止（`POST …/ollama/runtime/stop`）。

**自己运行的服务**：模型配置 → *Ollama · 本地服务*，地址 `http://127.0.0.1:11434`，模型 `qwen3.5:4b`，并明确勾选允许本机回环地址。不支持非回环地址的远程 Ollama。

## 3. 下载并绑定模型

1. **检查本地模型** 只读取服务的模型清单和声明的能力，不做推理。
2. 缺少模型时，先阅读许可（[Qwen3.5-4B 模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)），再用 **手动下载模型** 下载；进度按层显示，关闭页面不会取消下载。也可以对同一服务、同一模型目录执行 `ollama pull qwen3.5:4b`。
3. **绑定已安装模型** 记录确切的 digest、模板、版本和能力（需确认结构化输出和图像输入）。模型或档案变化后需要新建任务计划；任务执行中不会拉取缺失的模型。

## 4. 共享 GPU：守护进程

如果别人在同一块 GPU 上训练模型或运行机器人，悄悄共用这块 GPU 会拖慢他们的推理和控制循环，甚至导致显存不足。LEVI 的 GPU 守护会让路，并学习让路多久才够：

- **每次本地模型请求前**（以及启动 LEVI 专用服务前）采样 `nvidia-smi`，给出 `free` / `shared` / `cooling` / `busy` 判决。被挡住的 run 在这个边界停下，原因中写明判决和预计等待时间，并标记 `blocked_by: gpu`。
- **按工作负载的策略**（`workbench/models/gpu-policy.json`）：`protect`（默认，完全让路）、`share`（显存与利用率有余量时共存）、`ignore`（如显示服务）。Ollama 自己的 `llama-server` 按安装路径和父进程识别，从不按进程名识别。
- **学到的静默窗口**：用一个在负载换新 checkpoint 重启后仍不变的签名，记录每个负载何时出现、何时离开；负载离开后，等待其重启间隔 90 分位数的 1.5 倍再使用 GPU（60–1800 s；见到的重启次数不够时用默认值 `LEVI_GPU_QUIET_SECONDS=300`）。
- **服务运行期间**，守护每 15 s 采样一次（模型驻留显存时每 2 s），受保护的负载一出现就卸载模型——这样被打断的请求记为抢占，不算模型失败——窗口过后**自动恢复**所有被 GPU 挡住的 run。
- `uv run levi agent gpu`（或 `gpu.status` 能力）显示判决、原因、见过的负载及各自学到的窗口。
- 已与他人约定共享 GPU 时，设置 `LEVI_GPU_SHARING=allow` 关闭守护。
- `LEVI_GPU_LOCK_FILE` 是另一套需要主动开启的机制，只用于快速分割作业：标注和蒸馏运行期间持有这个 `flock` 锁文件，因此会排在同样遵守它的其他工具之后。守护进程不读取它，本地模型请求也不会占用它。见[快速分割](SEGMENTATION.md#settings--设置)。

## 5. 运行任务

在新任务中选择该档案（或给 `levi agent task new` 传 `--provider qwen-local`），选定较小的片段和相机范围，并明确同意发送证据。批准计划、运行试标、审核试标，再执行其余片段。模型收到带时间标注的证据图像和长度有上限的上下文；schema 与范围校验会拒绝格式错误或超出范围的输出。token 和耗时按阶段计量；每个阶段的结果和成本在任何教师审批之前就已结算，恢复执行的 run 不会重复计费。

暂停或取消 run 会切断它正在进行的模型请求，GPU 不再计算没人会用的答案。LEVI 最后一次请求之后，模型在显存中保留 `LEVI_OLLAMA_KEEP_ALIVE`（默认 `2m`，Ollama 自身默认 5 分钟），随后由 Ollama 卸载。LEVI 启动的全部进程及其退出方式见 [Workspace](WORKSPACE.md#processes-levi-starts-and-how-they-stop)。

模型被卸载后，下一次请求的耗时包含加载时间（`qwen3.5:4b` 在消费级 GPU 上约 20 秒）。受 schema 约束的调用会关闭推理（“思考”）模式：它耗费 token 和时间，而结构化答案很少需要它。

## Harness 为小模型兜住什么

4B 模型对指令的遵循很松散，LEVI 不指望它自觉遵守。每次请求的输出 schema 都经过收窄，格式错误的答案根本解码不出来（Ollama 在解码时强制执行 schema）：

- 只允许计划允许的标注类型（带子任务定义的计划只要区间）、计划中的子任务 id，以及本次提供的或草稿已引用的证据 id；
- 每个区间必须有 end 和 outcome，默认的 `full` 提示风格下还要有一句证据说明（`lean` 档案不写证据说明，由 LEVI 用区间描述作为说明）；proposals 排在 summary 之前且 summary 很短，答案额度不会耗在叙述上；
- 精修（refine）逐一返回草稿中的区间，各区间保持原子任务；边界的移动超出计划的边界窗口（即超出所给的帧）时，保留草稿中的边界；区间保持原有顺序，相邻区间共享边界；
- 输出额度随预期区间数增长。

请求大小按模型确定：发送紧凑的证据行（id、图片序号、时间）；精修携带的图片数，以及每张图片和每个字符的 token 成本，都从这个模型自己的计量请求中学到（`workbench/models/request-cost/<provider>.json`，首次用两次单 token 调用标定）。长片段分批精修，每批都在上下文和计划帧上限之内。连分批也放不下时（草稿的边界数超过粗扫后帧上限剩余的容量），无人监督的 run 保留该片段已通过校验的粗扫草稿，记录 `refinement_skipped` 及相关数字后继续；有监督的 run 则停下，由人调整帧上限。仍然校验失败的答案会保留其中有效的部分：有教师时，连同原因一起交给教师；没有教师时，丢弃无效条目并记录（`answer_salvaged`）。某个片段的答案一条有效内容都没有时，这个片段被搁置（记入 `run.failed`，事件 `episode_set_aside`，被拒的答案保存在 run 旁边），run 继续处理其余片段；恢复 run 时不再重试它。试标仍会阻塞；被搁置的片段超过 10%（且至少 3 个）时 run 也会阻塞，因为这说明问题出在配置上，而不是个别片段太难。无论答案是否有效，token 都会结算。

## 6. 模型从哪里学习

本地模型在任务之间只能通过提示词学习，所以 LEVI 在每次请求中放入一份有长度上限的**简报**，内容在制定计划时固定：

- 已提交工作沉淀下来的数据集概况、子任务词表和反复出现的不确定性；
- 教师针对该数据集和工作流写的备注，以及关于 LEVI 用法的备注；
- 来自**其他**片段的示例，从不包含本次要标注的片段；
- 已发布的 harness 改进经验；
- LEVI 内置的通用标注规则（`levi/knowledge/annotation.md`，见[内置知识](KNOWLEDGE.md)）。

模型权重不会被修改。

## 7. 教师参与的学习循环

任务可以在每个学生阶段前安排一个权限受限的外部 agent（Codex、Claude Code）作为教师：

```bash
uv run levi agent connect --client claude --project /path/to/teacher/project --dataset local/my_dataset --apply
uv run levi agent task new "…" --provider qwen-local --supervision supervised --teacher <grant-id>
```

在工作台中，于 **外部教师监督** 选择该连接。教师用 `supervision.pending` 读取待审阶段，用 `evidence.read` 查看真实图像，然后作答：

```json
{
  "name": "supervision.feedback",
  "arguments": {
    "run_id": "temporal-20260922T1303",
    "teaching_id": "temporal-20260922T1303:0:coarse",
    "revision": 0,
    "decision": "revise",
    "note": "A plate released onto a different colour is a failure, not a success.",
    "output": { "summary": "…", "proposals": ["…"], "warnings": [] }
  }
}
```

`accept` 保留学生的输出；`revise` 用一份完整且通过校验的输出替换它；`reject` 让该阶段保持阻塞。反馈绑定到该阶段的确切修订版和输入；只有指定的连接或人可以提交反馈，反馈也永远不能批准计划、试标或发布。

每条反馈都保存为文件（`datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json`），其中的备注进入该数据集的记忆，供下一个任务使用。教师也可以修正自然语言任务的解析结果（`tasks.feedback`）：修正后的规格成为示例，备注成为后续请求的经验。教师已提交的结果可以固定为参考答案，用来给之后的学生 run 打分（`teaching/reference-<task>.json`、`teaching/grades.json`）。

## 8. 本地 OpenAI 兼容服务（vLLM）

`openai-local` 类型的档案运行由 vLLM（或其他本地 OpenAI 兼容服务）提供的模型，走与 Ollama 相同的加固流程：收窄的答案 schema（以 `response_format` `json_schema` 发送，由服务在解码时强制执行）、紧凑证据行、请求成本标定与图像上限、保留无效答案中的有效部分、暂停/取消时切断进行中的请求、GPU 守护，以及自然语言任务。评测记录中它的名称是 `local-vlm`。适用于没有 Ollama 版本的模型，或在两种引擎上对比同一模型。

LEVI 不安装、不启动、也不停止该服务。请在单独的环境中运行 vLLM，不要用 LEVI 的 `.venv`。以下命令适用于 vLLM 0.30：

```bash
# Qwen3.8-27B，4 bit 权重（按 32 GB 显卡配置）
vllm serve RedHatAI/Qwen3.8-27B-INT4 --served-model-name qwen3.8-27b \
  --host 127.0.0.1 --port 8100 --max-model-len 32768 --max-num-seqs 2 \
  --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image": 64, "video": 0}'
```

然后创建档案（工作台 → 模型配置 → *本地 OpenAI 兼容服务（vLLM）*，或通过 REST）：

```bash
curl -X POST -H "x-levi-ui-token: $(cat "$LEVI_WORKSPACE/outputs/LEVI/workbench/agent/core/human.key")" \
  -H "Content-Type: application/json" http://127.0.0.1:7861/api/levi/agent/v1/providers -d '{
  "name": "qwen38-vllm", "kind": "openai-local", "base_url": "http://127.0.0.1:8100",
  "model": "qwen3.8-27b", "context_tokens": 32768, "vision": true,
  "allow_localhost": true, "max_images": 64, "prompt_style": "lean"}'
```

再在“账号与连接”中检查并绑定（`GET …/providers/qwen38-vllm/ollama`，然后用显示的 digest 调 `POST …/ollama/bind`，并带上 `"vision": true, "structured_output": true`）。

- **Base URL** 填服务根地址（`http://127.0.0.1:8100`，不含 `/v1`；LEVI 的示例用 8100，因为 openpi 的策略服务默认用 8000），只允许本机回环地址。
- **`model`** 是 `--served-model-name`。绑定的 digest 标识服务自报的内容：模型 id、启动时加载的权重（`/v1/models` 中的 `root`）和上下文长度。服务换了权重重启后，推理会被阻止，直到重新绑定模型。要固定到确切的版本，请从本地快照目录启动服务：目录路径中带有版本号。
- **`context_tokens`** 由绑定设为服务的 `--max-model-len`（最多 131072，更长的会被拒绝绑定）。服务的上下文在启动时就已固定，请求无法缩小它，所以这就是一次调用可能用掉的量，LEVI 也按这个数预留。
- **`max_images`** 即服务 `--limit-mm-per-prompt` 中的图像数。图像更多的请求在发送前就被拒绝，精修批次的大小也按它确定。**`image_max_side`**（可选）把每张图按长边缩到不超过该像素数后再发送；证据文件保持原始尺寸，标定也按缩放后的图像计价。
- **思考模式**默认关闭：每次请求都发送 `chat_template_kwargs: {"enable_thinking": false}`，Qwen3 系模板会遵守，其他模板会忽略。`think: true` 可以打开，但服务端必须配置 `--reasoning-parser` 才有效（schema 在推理结束后才生效），而且推理与答案共用同一份输出额度。如果服务只返回推理而没有答案，请求会失败并给出这个原因。
- **`fold_system: true`** 用于不接受 system 角色、并要求 user/assistant 严格交替的对话模板：系统提示放在第一条用户消息的开头，所有图像都放在文字之前，按证据顺序排列（第 *n* 张图对应编号为 *n* 的证据行）。
- **`prompt_style: "lean"`**（两种本地类型均可）：发送计划的原始指令和帧列表（“image *n* = *t* s”），不再发送 LEVI 的 skills 和整份 JSON 文档；记忆中只保留教师备注、经验、反复出现的不确定性和其他片段的示例（不再重复一遍指令）；只要求模型给出 LEVI 无法自己补上的内容：每个区间的子任务、起点、终点、结局和描述（计划允许多种标注类型时还要给类型）；边界精修时只要求每个草稿区间的起点、终点和结局，子任务和描述沿用草稿。LEVI 补上片段信息，为每个区间引用区间内至多三帧，并用区间描述作为证据说明。本地模型的耗时主要花在输出 token 上（解码每个 token 比读提示慢约 30 倍），所以每少一个字段都能省时间。同一模型用与不用 LEVI 的配对实验显示，完整提示会降低本地模型的时间片段 F1，并使输出 token 约翻倍，因此时序标注（子任务与事件）推荐使用 `lean`。档案的提示风格同样作用于数据集审查，而审查在 `lean` 下实测更差（balanced accuracy 0.590，`full` 为 0.621）：审查请使用 `full` 档案。默认 `full`。
- **`requests_in_flight`**（默认 1）让一个 run 同时处理这么多个片段，每个片段一个请求。会批处理请求的服务（vLLM `--max-num-seqs 2`）可以一边解码一个答案，一边读取另一个提示：在 Qwen3.8-27B INT4 上用 `lean` 做时序标注，并发 2 个请求时，plates 开发集（48 个片段）的耗时从 1119 s 降到 856 s，时间片段 F1 相同。这时贪心解码的答案在不同 run 之间可能在最后几位数字上不同（批处理的算术差异），所以需要各次 run 答案完全一致的配对实验应保持为 1。该值通过 provider API 设置；档案表单保存时会保留它。
- **结构化输出**：vLLM 的默认后端（`auto`）能编译 LEVI 的所有答案 schema；如果请求因结构化输出错误被拒绝，请用 `--structured-outputs-config.backend guidance` 启动服务。
- **采样参数**沿用模型自带的生成配置（vLLM 默认 `--generation-config auto`），LEVI 不发送 temperature。不要加 `--generation-config vllm`，它会用 vLLM 的默认值替换模型推荐的设置。需要贪心（可复现）解码时，用 `--override-generation-config '{"temperature": 0.0}'` 启动服务。
- **密钥**：不需要。如果服务使用了 `--api-key`，请把它设在 `LEVI_LOCAL_MODEL_KEY`（这类档案默认的 `key_env`，这样云端 key 永远不会发到本机端口）或会话凭据中。
- **GPU 守护**：为该档案端口提供服务的进程（在端口上监听的 API 服务及其启动的引擎进程）通过监听套接字被识别为 LEVI 自己的模型，从不按进程名识别，因此不会挡住该档案的请求；其他人的工作仍会挡住。守护无法卸载 vLLM 的模型：服务运行期间一直占用 `--gpu-memory-utilization` 比例的显存。对 Ollama 档案以及其他端口上的档案来说，它就是别人的工作：Ollama 的模型会为它卸载，run 会等它离开；守护只在 GPU 对被挡住的 run 所用的档案而言空闲时，才恢复该 run。运行 Ollama 前请先停掉 vLLM，不要两者同时运行。档案的请求会把在其端口上监听的任何程序都当作它的服务；另一个程序占用该端口期间（openpi 的策略服务默认也用 8000），请停用该档案。加载/卸载控制和下载不适用（HTTP 409）。识别要通过 `/proc` 读取服务的套接字，因此服务必须与 LEVI 以同一用户运行；否则请在 `workbench/models/gpu-policy.json` 中加一条忽略规则，例如 `{"rules": [{"match": "(?i)vllm", "class": "ignore"}]}`。
- **Token**：vLLM 报告完整的提示 token，而 Ollama 不计入已缓存的前缀；跨引擎比较时请看输出 token 和单次调用耗时。Ollama 调用还会在每个阶段的用量中记录 `load_seconds`、`prefill_seconds` 和 `decode_seconds`。

长时间运行前先跑试标：绑定不做推理，试标才是服务回答的第一个请求。

## 开发检查

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_gpu_guard.py tests/test_teaching.py \
  tests/test_openai_local.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
```

这些测试使用模拟的模型响应、模拟的教师和模拟的 `nvidia-smi`，不衡量模型质量。

## 限制

- 小型本地模型的标注质量尚未得到验证。依赖它之前，请在自己的数据上借助教师和参考答案实测。
- 在两种本地通道上，LEVI 都不发送采样参数（temperature、top-p、seed），答案是否确定由服务端决定：Ollama 取模型自身参数（其 Modelfile），vLLM 取所服务的生成配置（见上文**采样参数**）。
- `OLLAMA_NO_CLOUD` 不等于网络隔离：没有操作系统级的离线沙箱。
- GPU 守护通过 `nvidia-smi` 查看进程，它是一种礼让策略，不是 GPU 调度器或资源租约。它只能识别与 LEVI 以同一用户运行的本地服务进程（通过 `/proc` 读取其套接字）。
- 不训练权重，不自动发布技能，不自动晋级能力档案。
