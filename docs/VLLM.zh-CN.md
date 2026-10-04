# 给 LEVI 用的 vLLM

LEVI 用两种方式使用 vLLM。[English](VLLM.md)

- **产品 LEVI** 从不启动 vLLM：服务由你自己运行，再把一个 `openai-local` 档案绑定上去（[本地模型](OLLAMA.zh-CN.md#8-本地-openai-兼容服务vllm)）。
- **实时标注服务**（`levi live`，[实时服务](LIVE.zh-CN.md)）通过启动脚本自己启动、休眠、唤醒和停止 vLLM。本页写明这个脚本要遵守的约定、LEVI 自带的启动脚本 `scripts/vllm/serve.sh`，以及怎样为它搭建 vLLM 环境。

## 1. 搭建 vLLM 环境

vLLM 装在单独的环境里，不要装进 LEVI 的 `.venv`（它要钉住自己的 torch 和 CUDA wheel）。

| | 已验证 |
| --- | --- |
| 平台 | Linux x86_64，一块 NVIDIA GPU |
| vLLM | 0.30.0（Python 3.12），torch 2.13.0+cu132，flashinfer-python 0.6.18.post1，xgrammar 0.2.7，transformers 5.17.0 |
| 驱动 | 支持 torch wheel 所用 CUDA 版本的 NVIDIA 驱动（`+cu132` 需要 CUDA 13.2；`nvidia-smi` 显示驱动支持的最高 CUDA 版本） |
| 显卡 | 实测配置（Qwen3.8-27B INT4、49152 token 上下文、128 张图、两个并发序列）需要 32 GB 显卡；显存更小就换小模型或缩短上下文，并重新标定 `[vllm]` 的显存预算参数 |

其他版本也可能可用；LEVI 传的启动参数（第 3 节）按 vLLM 0.30 的参数名写。

```bash
cd /path/to/LEVI
uv venv --python 3.12 .venv-vllm               # 启动脚本默认找这个位置
uv pip install --python .venv-vllm/bin/python "vllm==0.30.0"
scripts/vllm/serve.sh --check                  # 打印解析后的设置；缺 vllm 或没设模型时退出码 1
```

环境放在别处时用 `LEVI_VLLM_VENV` 指过去；没有这个环境时，脚本用 `PATH` 上的 `vllm`。

**没有系统 CUDA 工具包时。** FlashInfer 第一次运行某个算子时现场编译，在 `CUDA_HOME` 下找 `nvcc`；没有工具包，服务会在引擎启动时退出（`Could not find nvcc ... /usr/local/cuda`）。不需要 root：

1. 安装与 torch 的 CUDA 版本一致的 pip CUDA 工具链 wheel（直接 `pip install vllm` 可能装上比运行时和驱动更新的编译器）：`uv pip install --python .venv-vllm/bin/python "cuda-toolkit[nvcc,crt,nvvm,cccl,cudart]==<torch.version.cuda>.*"`。以后任何升级改动了 `nvidia-*` wheel，都要重做这一步。
2. `scripts/vllm/make-cuda-home.sh` 在 `.venv-vllm/cuda-home` 建一个只由符号链接组成的 `CUDA_HOME`，指向这些 wheel（链接期的 `libcuda` 存根指向驱动的 `libcuda.so.1`）。
3. `export LEVI_VLLM_CUDA_HOME=$PWD/.venv-vllm/cuda-home`：启动脚本据此导出 `CUDA_HOME`，并把环境的 `bin`（FlashInfer 要调用 `ninja`）加进 `PATH`。

**权重。** 首次启动前先下载（默认模型约 19 GiB）：`HF_HOME=<缓存目录> hf download RedHatAI/Qwen3.8-27B-INT4`。给服务传同一个 `HF_HOME`（会原样传给启动脚本），再设 `HF_HUB_OFFLINE=1`，这样启动时绝不会去下载。受限模型需要 Hugging Face 令牌（`HF_TOKEN`），由人提供。

## 2. 自带的启动脚本：`scripts/vllm/serve.sh`

它是实时服务 `[vllm] script` 和 `stop_script` 的默认值（`live.toml` 里的相对路径相对 LEVI 检出目录）。脚本里没有本机路径：检出目录由脚本自身位置推出，其余都来自环境变量。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LEVI_VLLM_MODEL` | （`levi live` 按 `[vllm] model` 设置） | 要服务的模型 id 或本地快照；启动时必需 |
| `LEVI_VLLM_SERVED_NAME` | 模型名 | `--served-model-name`（`levi live` 按 `[vllm] served_model` 设置） |
| `PORT` | 8100 | 端口，绑定在 `VLLM_HOST`（默认 127.0.0.1） |
| `GPU_UTIL` | 0.90 | `--gpu-memory-utilization` |
| `MAX_MODEL_LEN` | 未设 | `--max-model-len`（LEVI 改用命令行参数传） |
| `LEVI_VLLM_PID_DIR` | `${LEVI_LIVE_HOME:-~/.levi-live}/vllm` | pid 文件和日志（`levi live` 按 `[vllm] pid_dir` 设置） |
| `LEVI_VLLM_VENV` | `<检出目录>/.venv-vllm` | vLLM 环境 |
| `LEVI_VLLM_CUDA_HOME` | 未设 | 要导出的 `CUDA_HOME`（见上） |
| `VLLM_WAIT_S` | 600 | 等 `GET /health` 的秒数；0 表示立即返回（LEVI 设 0） |
| `SERVE_SKIP_PREFLIGHT`、`SERVE_DRY_RUN` | 0 | 跳过空闲显存检查；只打印命令 |

`HF_HOME`、`HF_HUB_OFFLINE`、`HF_TOKEN`、`CUDA_VISIBLE_DEVICES` 和 `VLLM_*` 原样传过去。`--stop` 只在 pid 文件里是一个正整数、且该进程命令行含 `vllm` 时才发信号（过期 pid 文件的号可能已被别的进程复用：这时不碰它，只删 pid 文件）。进程还在但读不出命令行时同样不发信号，pid 文件保留。启动前，若 `nvidia-smi` 显示的空闲显存不够预算，脚本拒绝启动（退出码 3）并列出占用 GPU 的进程；读不出 `nvidia-smi` 时也以退出码 3 拒绝并说明原因。手动运行：

```bash
LEVI_VLLM_MODEL=RedHatAI/Qwen3.8-27B-INT4 LEVI_VLLM_SERVED_NAME=qwen3.8-27b \
  scripts/vllm/serve.sh --max-model-len 32768 --limit-mm-per-prompt '{"image":64,"video":0}'
scripts/vllm/serve.sh --stop 8100
```

## 3. `levi live` 与启动脚本之间的约定

任何遵守下面约定的脚本都可以替换自带脚本（`live.toml` 的 `[vllm] script`、`stop_script`、`pid_dir`；`levi live doctor` 检查两个脚本存在且可执行、pid 目录可写）。代码在 `levi/live/gpumgr.py`（`Vllm.start`、`Vllm.stop`、`script_env`、`launch_args`）。

**启动。** LEVI 在新会话里运行 `<script> <参数>`，标准输入关闭，输出追加到 `<workspace>/live/logs/vllm-launch.log`，最多等 120 秒让它退出。环境是 LEVI 自己的环境，再加上：

| 变量 | 值 |
| --- | --- |
| `PORT` | `[vllm] port` |
| `GPU_UTIL` | 这次启动选定的显存预算（按空闲显存算出的 `gpu_memory_utilization`） |
| `VLLM_WAIT_S` | `0`：服务进程一跑起来就返回；LEVI 自己轮询 `/health`（最长 `start_timeout_s`） |
| `LEVI_AGENT` | `[gpu] lock_agent` |
| `VLLM_SERVER_DEV_MODE` | 打开 `sleep_mode` 时为 `1`（开启休眠端点） |
| `LEVI_VLLM_PID_DIR`、`LEVI_VLLM_MODEL`、`LEVI_VLLM_SERVED_NAME` | `[vllm] pid_dir`、`model`（设了才传）、`served_model`；服务固定模型的脚本可以不理会 |

参数必须传到 `vllm serve`，并放在脚本自己的参数之后（后出现的参数生效）：

```
--structured-outputs-config {"backend":"xgrammar","disable_any_whitespace":true}
--max-model-len <这次启动选定的上下文>
--limit-mm-per-prompt {"image":<max_images>,"video":0}
--max-num-seqs <max_num_seqs>
--max-num-batched-tokens <max_num_batched_tokens>
--override-generation-config {"temperature": <temperature>}
--enable-sleep-mode                     （打开 sleep_mode 时）
```

服务启动后脚本要以 0 退出，并在退出前把服务的 pid 写进 **`<pid_dir>/vllm_<PORT>.pid`**（一个十进制数；它必须是进程组的组长，用 `setsid` 启动即可，因为 LEVI 对整个进程组发信号）。退出码非 0 或没有 pid 文件，都算启动失败（按退避重试，之后等人处理：`levi live resume`）。持有 GPU 锁时，锁的文件描述符会被脚本和服务继承（`pass_fds`）：服务活多久锁就持有多久，即使实时服务进程退出也一样。脚本不能关闭它。

**服务**必须绑定回环地址，用 `[vllm] served_model` 作为模型名，并响应 `GET /health`（就绪时返回 200）。打开休眠时，LEVI 使用 vLLM 的开发端点 `POST /sleep?level=1`、`POST /wake_up` 和 `GET /is_sleeping`（vLLM 0.30 在 `--enable-sleep-mode` 加 `VLLM_SERVER_DEV_MODE=1` 时提供）。worker 的模型调用走 `/v1/chat/completions`，使用 JSON schema 结构化输出。

**停止。** LEVI 运行 `<stop_script> --stop <PORT>`（同样的环境，超时 120 秒）。脚本应对 pid 文件里的进程组发 TERM，宽限期后发 KILL，并删除 pid 文件；它的退出码不会被使用。之后若记录的进程还活着，LEVI 自己对进程组发 TERM 再发 KILL，并一直持有 GPU 锁，直到这个进程组没有任何进程还在 GPU 上。LEVI 只停止自己启动的服务：pid 及其进程身份（启动时间）记录在 `<workspace>/live/vllm.json`。

**不属于约定的部分**：权重和缓存位置、CUDA 设置、pid 文件以外的日志。脚本可以打印任何内容。

## 4. 检查

- `scripts/vllm/serve.sh --check` 打印将要运行的内容；没有 vLLM 或没设模型时失败。
- `levi live doctor` 报告：脚本缺失或不可执行、pid 目录不可写、GPU 锁文件打不开、监视根目录不存在、路径指向别的用户的家目录。
- `levi doctor`（安装自检，见[安装](../INSTALL.zh-CN.md)）只在回环地址上向 8100 端口（或 `--vllm-ports` 给出的端口；5000 和 8000 一律拒绝）请求 `/health` 和 `/v1/models`，不跟随重定向。它不读实时服务的 `[vllm] port`；改过端口时用 `--vllm-ports` 传入。
- `tests/test_vllm_launcher.py` 用一个替身 `vllm` 可执行文件运行自带启动脚本，不用 GPU 检查整个约定（启动、pid 文件、参数、停止）。
