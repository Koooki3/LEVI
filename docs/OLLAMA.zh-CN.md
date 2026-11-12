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
- **请求之间复用判决**：同一组模型服务的 `free` 判决复用 10 s。`shared`（按策略共存）判决默认**不复用**，因为共享依赖会变化的负载；设置 `LEVI_GPU_SHARED_REUSE_SECONDS`（默认 `0` 即关闭，上限 3 s；更大的值会被截为 3 并给出警告）可开启一个很短的复用窗口，每个请求省两次 `nvidia-smi`。窗口内新出现的繁忙负载最迟在该秒数后被发现；`busy`、`cooling`、`unknown` 永不复用，守护（请求或监视线程）一旦看到其中任何一种，所有复用窗口立即作废。
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

LEVI 不安装、不启动、也不停止该服务。请在单独的环境中运行 vLLM，不要用 LEVI 的 `.venv`（[vLLM](VLLM.zh-CN.md) 说明怎样搭建这个环境，包括没有系统 CUDA 工具包的机器，并附带启动脚本 `scripts/vllm/serve.sh`）。以下命令适用于 vLLM 0.30：

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
- **`max_images`** 即服务 `--limit-mm-per-prompt` 中的图像数。图像更多的请求在发送前就被拒绝，精修批次的大小也按它确定。某个片段按计划的 `coarse_step_seconds` 粗标时，（所有相机合计）图像数超过该值，就只为这个片段把粗标间隔加大，取使均匀分布的帧放得下的最小值（精确到 0.01 s），始终保留首尾帧，运行不再被阻塞；事件 `coarse_step_adjusted` 记录片段号、计划间隔和实际间隔、计划图数和实际图数以及 `max_images`，证据摘要里的 `coarse_step_seconds` 是实际间隔。放得下的片段照计划取帧，不变。只有每个相机的首尾两帧都放不下时才停止，并指出 `max_images`。**`image_max_side`**（可选）把每张图按长边缩到不超过该像素数后再发送；证据文件保持原始尺寸，标定也按缩放后的图像计价。
- **`max_pixels`**（可选，仅 `openai-local` 档案，65536 到 16777216，默认不设）让服务端的图像处理器对每张图最多保留这么多像素：请求体顶层（与 `chat_template_kwargs` 并列）随即带上 `"mm_processor_kwargs": {"max_pixels": N}`。不设时什么都不加，请求与该字段出现之前逐字节相同（有测试比较序列化后的请求体）。`image_max_side` 缩小的是 LEVI 发出的那份副本；`max_pixels` 不动图像，由服务端处理器来缩，每条边向下取整到 32 的倍数，所以长宽比正常时结果不会超过预算。两者都设时 LEVI 先缩。65536 是 Qwen3 处理器的像素下限（更小的图会被放大到它，所以更低的预算自相矛盾），16777216 是它的上限。超出范围的值、非整数，以及在 `ollama` 或 `openai-compatible` 档案上设这个字段，保存档案时就被拒绝；客户端在联系服务之前对坏值再拒绝一次。请用提供方 API 设置：档案表单不显示它，在表单里保存档案会把它重置为不设。请求成本拟合按档案名保存，键含模型、摘要、`image_max_side` 和 `max_pixels`，所以改预算后拟合重新开始，不会混入旧样本；不设 `max_pixels` 时键里不多任何东西，字段出现之前记录的拟合仍然适用。它是做实测实验的手段：实时服务或随附服务用的档案里不要设。能省多少、哪些还没测，见下面的“图像 token 预算”。
- **Logprobs**（`openai-local` 客户端的单次调用选项，`chat(..., top_logprobs=k)`，`k` 取 1 到 20；默认 `None`，没有档案字段）让服务端在每个生成的 token 处返回最好的 `k` 个候选，用来从回答里读置信度（`levi/decision/confidence.py`）。此时请求体顶层多带 `"logprobs": true, "top_logprobs": k`，返回的字典多一个键 `logprobs`：服务端的 `choices[0].logprobs` 原样，`{"content": [{"token", "logprob", "bytes", "top_logprobs": [{"token", "logprob", "bytes"}, ...]}, ...]}`，每个生成的 token 一项（服务端没给则为 `None`）。不传 `top_logprobs` 时请求逐字节与以前相同，返回的字典也没有 `logprobs` 键（测试比较序列化后的请求体）。20 是 vLLM 默认的 `--max-logprobs`；更大的值、0 和非整数在发出任何请求之前就被拒绝。在 vLLM 0.30.0 源码里读到的：字段是 `ChatCompletionRequest.logprobs`/`top_logprobs` 和 `ChatCompletionResponseChoice.logprobs`（`vllm/entrypoints/openai/chat_completion/protocol.py`）；语法掩码先就地作用在 logits 上（`vllm/v1/worker/gpu_model_runner.py` 的 `sample_tokens`、`apply_grammar_bitmask`），之后采样器才算默认的 `raw_logprobs`（`vllm/v1/sample/sampler.py` 的 `forward`），所以 `json_schema` strict 输出下返回的应当是经过 schema 掩码之后的分布，schema 禁止的 token 没有概率质量（读源码得到，没有实测）。没有测的，因为需要运行中的服务：你运行的服务是否确实如此返回、这个选项对时延和输出 token 的影响、概率是否校准。
- **思考模式**默认关闭：每次请求都发送 `chat_template_kwargs: {"enable_thinking": false}`，Qwen3 系模板会遵守，其他模板会忽略。`think: true` 可以打开，但服务端必须配置 `--reasoning-parser` 才有效（schema 在推理结束后才生效），而且推理与答案共用同一份输出额度。如果服务只返回推理而没有答案，请求会失败并给出这个原因。
- **`fold_system: true`** 用于不接受 system 角色、并要求 user/assistant 严格交替的对话模板：系统提示放在第一条用户消息的开头，所有图像都放在文字之前，按证据顺序排列（第 *n* 张图对应编号为 *n* 的证据行）。
- **`prompt_style: "lean"`**（两种本地类型均可）：发送计划的原始指令和帧列表（“image *n* = *t* s”），不再发送 LEVI 的 skills 和整份 JSON 文档；记忆中只保留教师备注、经验、反复出现的不确定性和其他片段的示例（不再重复一遍指令）；只要求模型给出 LEVI 无法自己补上的内容：每个区间的子任务、起点、终点、结局和描述（计划允许多种标注类型时还要给类型）；边界精修时只要求每个草稿区间的起点、终点和结局，子任务和描述沿用草稿。LEVI 补上片段信息，为每个区间引用区间内至多三帧，并用区间描述作为证据说明。本地模型的耗时主要花在输出 token 上（解码每个 token 比读提示慢约 30 倍），所以每少一个字段都能省时间。同一模型用与不用 LEVI 的配对实验显示，完整提示会降低本地模型的时间片段 F1，并使输出 token 约翻倍，因此时序标注（子任务与事件）推荐使用 `lean`。档案的提示风格同样作用于数据集审查，而审查在 `lean` 下实测更差（balanced accuracy 0.590，`full` 为 0.621）：审查请使用 `full` 档案。默认 `full`。
- **`requests_in_flight`**（默认 1）让一个 run 同时处理这么多个片段，每个片段一个请求。会批处理请求的服务（vLLM `--max-num-seqs 2`）可以一边解码一个答案，一边读取另一个提示：在 Qwen3.8-27B INT4 上用 `lean` 做时序标注，并发 2 个请求时，plates 开发集（48 个片段）的耗时从 1119 s 降到 856 s，时间片段 F1 相同。这时贪心解码的答案在不同 run 之间可能在最后几位数字上不同（批处理的算术差异），所以需要各次 run 答案完全一致的配对实验应保持为 1。该值通过 provider API 设置；档案表单保存时会保留它。
- **结构化输出**：vLLM 的默认后端（`auto`）能编译 LEVI 的所有答案 schema；如果请求因结构化输出错误被拒绝，请用 `--structured-outputs-config.backend guidance` 启动服务。
- **采样参数**沿用模型自带的生成配置（vLLM 默认 `--generation-config auto`），LEVI 不发送 temperature。不要加 `--generation-config vllm`，它会用 vLLM 的默认值替换模型推荐的设置。需要贪心（可复现）解码时，用 `--override-generation-config '{"temperature": 0.0}'` 启动服务。
- **密钥**：不需要。如果服务使用了 `--api-key`，请把它设在 `LEVI_LOCAL_MODEL_KEY`（这类档案默认的 `key_env`，这样云端 key 永远不会发到本机端口）或会话凭据中。
- **GPU 守护**：为该档案端口提供服务的进程（在端口上监听的 API 服务及其启动的引擎进程）通过监听套接字被识别为 LEVI 自己的模型，从不按进程名识别，因此不会挡住该档案的请求；其他人的工作仍会挡住。守护无法卸载 vLLM 的模型：服务运行期间一直占用 `--gpu-memory-utilization` 比例的显存。对 Ollama 档案以及其他端口上的档案来说，它就是别人的工作：Ollama 的模型会为它卸载，run 会等它离开；守护只在 GPU 对被挡住的 run 所用的档案而言空闲时，才恢复该 run。运行 Ollama 前请先停掉 vLLM，不要两者同时运行。档案的请求会把在其端口上监听的任何程序都当作它的服务；另一个程序占用该端口期间（openpi 的策略服务默认也用 8000），请停用该档案。加载/卸载控制和下载不适用（HTTP 409）。识别要通过 `/proc` 读取服务的套接字，因此服务必须与 LEVI 以同一用户运行；否则请在 `workbench/models/gpu-policy.json` 中加一条忽略规则，例如 `{"rules": [{"match": "(?i)vllm", "class": "ignore"}]}`。
- **Token**：vLLM 报告完整的提示 token，而 Ollama 不计入已缓存的前缀；跨引擎比较时请看输出 token 和单次调用耗时。Ollama 调用还会在每个阶段的用量中记录 `load_seconds`、`prefill_seconds` 和 `decode_seconds`。

**图像 token 预算。** Qwen3-VL 系模型的一个视觉 token 对应处理后图像的一个 32 x 32 像素块（patch 16，merge 2）。处理器把每条边取整到 32 的倍数（用 Python 的 `round`，恰好一半时取偶数那一侧），图像超过 `max_pixels` 就缩小，低于 65536 的像素下限就放大。`scripts/vllm/image_tokens.py MODEL_DIR` 只读模型目录里的 `processor_config.json` 就能对任意帧尺寸打印这张表：不加载权重，不用 GPU、服务或网络（`--markdown FILE`、`--json FILE` 写文件）。Qwen3.8 27B INT4 处理器（下限 65536，上限 16777216）对一帧 640x480 的结果：

| 设置 | 发给服务的尺寸 | 处理器缩放到 | 视觉 token |
| --- | --- | --- | ---: |
| 两个字段都不设（原生） | 640x480 | 640x480 | 300 |
| `image_max_side` 448 | 448x336 | 448x320 | 140 |
| `image_max_side` 384 | 384x288 | 384x288 | 108 |
| `image_max_side` 320 | 320x240 | 320x256 | 80 |
| `image_max_side` 256 | 256x192 | 320x224（放大到下限） | 70 |
| `max_pixels` 150528 | 640x480 | 448x320 | 140 |
| `max_pixels` 110592 | 640x480 | 384x288 | 108 |
| `max_pixels` 76800 | 640x480 | 320x224 | 70 |

两个字段都不设时，每帧原生图像是 300 个 token。“448 约 154 个 token”的估计来自把 336 / 32 = 10.5 进位到 11；处理器取整到 10，所以 448 的副本是 140。低于约 320 时下限让节省停住：256 的副本被放大到 320x224，仍是 70，`max_pixels` 取 49152（48 个 token）也因同一原因被拒绝。这些行是算术结果。它们与 Hugging Face 图像处理器在 CPU 上的实际输出一致（这九行，外加另外十一组尺寸与预算的组合；transformers 5.17.0，未加载权重；一次性核对，不在测试套件里）。

哪些核实过、哪些没有。在 vLLM 0.30.0 源码里读到的：chat-completions 请求把 `mm_processor_kwargs` 作为顶层字段接收（`vllm/entrypoints/openai/chat_completion/protocol.py`，`ChatCompletionRequest.mm_processor_kwargs`）；在线渲染器把它传下去（`vllm/renderers/online_renderer.py`，`preprocess_chat`）；多模态处理器把它当作处理器的关键字参数接收，并把它算进每张已处理图像的缓存键，所以改了预算不会命中别的预算的缓存（`vllm/renderers/base.py`；`vllm/multimodal/processing/inputs.py`，`get_mm_hashes`）；对 Qwen3-VL，`max_pixels` 会替换图像处理器的 `longest_edge`，既用于 vLLM 自己的 token 计数（`vllm/model_executor/models/qwen3_vl.py`，`_get_vision_info`），也用于 vLLM 构造的 Hugging Face 处理器（`vllm/transformers_utils/processor.py`，`cached_get_processor_without_dynamic_kwargs`：`max_pixels` 不是处理器的逐次调用参数，所以它进入构造函数）。最后这一点很关键：transformers 的 Qwen2-VL 图像处理器会忽略单独逐次传入、没有配 `min_pixels` 的 `max_pixels`（CPU 上核对过：得到 300 个 token 而不是 140），所以逐次传入的版本什么都不会缩。没有核实的，因为需要运行中的服务：你运行的服务设了这个字段后是否真的返回更少的 `prompt_tokens`（用同样的帧分别带与不带该字段发送并比较）、它对首 token 时间和吞吐的影响、它对回答质量的影响。无论用哪个字段降低图像分辨率，正式使用前都要在同样的片段上与未降低的设置做配对质量比较。处理器不认识 `max_pixels` 的服务或模型可能忽略该字段，也可能返回 HTTP 400；那时请取消设置。

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
