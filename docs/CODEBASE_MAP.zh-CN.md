# 代码地图

原来写在 [AGENTS.md](../AGENTS.md) 里的文件级说明（2026-11-06 迁出）：数据集格式如何解析、每个文件做什么，以及各区域背后的坑。`AGENTS.md` 只留规则和一行一条的不变量；要改哪个区域，就读对应的一节。图示层面的总览见[架构图集](ARCHITECTURE.md)（英文）；设计系统本身的规则见[设计系统](DESIGN.zh-CN.md)。English: [CODEBASE_MAP.md](CODEBASE_MAP.md)。

本文与英文版章节结构相同，英文版为准；标识符、路径、命令保持原样。代码变了就同步改这里：过期的条目会把下一个 agent 带到错误的文件。

## 前端数据层

### 数据集版本支持

支持基于视频的 v2.0、v2.1、v3.0 和 v3.1。版本从 `meta/info.json` → `codebase_version` 检测。

| 版本 | 路径模式 | 片段元数据 | 视频 |
| --- | --- | --- | --- |
| **v2.0** | `data/{episode_chunk:03d}/episode_{episode_index:06d}.parquet` | 有则用 `meta/episodes.jsonl` | 每个片段一个完整文件 |
| **v2.1** | 同 v2.0 | `meta/episodes.jsonl` | 每个片段一个完整文件 |
| **v3.0/v3.1** | `data/chunk-{N:03d}/file-{N:03d}.parquet`（经 `buildV3DataPath`） | `meta/episodes/chunk-{N}/file-{N}.parquet` | 分段（每个片段、每个相机各有一组时间戳） |

### 解析器路由

`src/app/[org]/[dataset]/[episode]/fetch-data.ts` → `getEpisodeData()` 分发给：

- `getEpisodeDataV2()`：v2.0 和 v2.1
- `getEpisodeDataV3()`：v3.0 和 v3.1

### v3.x 的特殊点

- 片段元数据行有命名键（`episode_index`、`data/chunk_index`、`data/file_index`、`dataset_from_index`、`dataset_to_index`、`videos/{key}/chunk_index` 等）。
- parquet 的整数列读出来是 **BigInt**：一律用 `src/utils/typeGuards.ts` 的 `bigIntToNumber()`。
- 行范围选择：`dataset_from_index` / `dataset_to_index` 让你只读共享 parquet 文件里属于该片段的行。
- 拿不到列名时的回退格式使用数字键 `"0"` 到 `"9"`。
- 片段元数据可以跨**多个 chunk**（片段数超过 `chunks_size` 时）。一律通过 `fetch-data.ts` 里的 `iterateEpisodeMetadataFilesV3(repoId, version)` 异步生成器遍历：它从 chunk-000 推进到 chunk-001……遇到第一个不存在的 `file-000` 就停。绝不写死 `chunk-000`。
- 多任务片段：片段元数据行带 `tasks` 字段（`list[str]`），优先用它，不用旧的单个 `task_index` 查找。`EpisodeMetadataV3.tasks?: string[]` 暴露了它。
- `meta/tasks.parquet` 的查找：各行**不是**按 `task_index` 排序的，任务字符串在一个命名的 pandas 索引（`__index_level_0__`）里。一律按 `task_index` **列**过滤（`row.task_index === taskIndexNum`），绝不按行位置取。

### v2.x 的路径构造

```ts
formatStringWithVars(info.data_path, {
  episode_chunk: Math.floor(episodeId / chunkSize)
    .toString()
    .padStart(3, "0"),
  episode_index: episodeId.toString().padStart(6, "0"),
});
// → "data/000/episode_000042.parquet"
```

`formatStringWithVars` 会去掉 `:03d` 这类格式说明符：补零必须由调用方完成。

### 关键文件

| 文件 | 作用 |
| --- | --- |
| `src/app/[org]/[dataset]/[episode]/fetch-data.ts` | 数据加载主入口；v2/v3 解析器；`computeColumnMinMax` |
| `src/utils/versionUtils.ts` | `getDatasetInfo`、`getDatasetVersionAndInfo`、`buildVersionedUrl` |
| `src/utils/stringFormatting.ts` | `buildV3DataPath`、`buildV3VideoPath`、`buildV3EpisodesMetadataPath`、补零辅助函数 |
| `src/utils/parquetUtils.ts` | `fetchParquetFile`、`readParquetAsObjects`、`formatStringWithVars` |
| `src/utils/dataProcessing.ts` | 图表分组流水线：`buildSuffixGroupsMap` → `computeGroupStats` → `groupByScale` → `flattenScaleGroups` → `processChartDataGroups` |
| `src/utils/typeGuards.ts` | `bigIntToNumber`、`isNumeric`、`isValidTaskIndex` 等 |
| `src/utils/constants.ts` | `PADDING`、`EXCLUDED_COLUMNS`、`CHART_CONFIG`、`THRESHOLDS` |
| `src/types/` | TypeScript 类型：`DatasetVersion`、`EpisodeMetadataV3`、`VideoInfo`、`ChartDataGroup` 等 |

### 动作洞察的范围（Action Insights scope）

`loadCrossEpisodeActionVariance(repoId, version, info, fps, options)` 接收一个 `CrossEpisodeRequest`：`scope`（`all` | `range` | `task`）加 `maxEpisodes`，`null` 表示“范围内的每个片段”。范围过滤发生在抽样**之前**，所以一个区间或一个任务能拿到完整的额度。`CROSS_EPISODE_DEFAULTS.sampleSize` 只是面板起始的抽样数；`CROSS_EPISODE_DEFAULTS.ceiling` 是防失控的上限。注意：这个模块里读 `process.env` 的代码在浏览器里不起作用（没有 `NEXT_PUBLIC_` 前缀，`next.config.ts` 里也没有 `env` 块），片段查看器这条路径永远拿到回退值。parquet 读取有并发限制（`mapWithConcurrency`）；`onProgress` 只在客户端调用时触发。

`loadDatasetTaskIndex(repoId, version)` 构建整个数据集的任务 ↔ 片段映射（v2：`meta/tasks.jsonl` 加 `meta/episodes.jsonl` 各行的 `tasks` 字段；v3：片段元数据行上的 `tasks` 列表），并缓存 5 分钟。它同时支撑“按任务”的洞察范围和侧栏的任务筛选，并对任务字符串去重：同一条指令可能被声明在两个 `task_index` 下。

### 图表数据流水线

序列键用 `" | "` 作分隔符（例如 `observation.state | 0`）。`groupRowBySuffix` 按**后缀**分组：两个不同前缀共享后缀 `"0"`（例如 `observation.state | 0` 和 `action | 0`）时，会被合并到 `result["0"] = { "observation.state": ..., "action": ... }` 下。后缀唯一的序列保持扁平，保留完整的原始键。

### URL 结构

远程数据集的 URL（本地数据集走同源的 LEVI 文件服务）：

```
https://huggingface.co/datasets/{org}/{dataset}/resolve/main/{path}
```

由 `buildVersionedUrl(repoId, version, path)` 构造。`version` 参数会被接收，但目前没有用在 URL 里（始终是 `main` 修订）。

### 排除的列（不在图表中显示）

来自 lerobot 的保留/记账列：见 `src/utils/constants.ts` 的 `EXCLUDED_COLUMNS`：

- v2.x：`timestamp`、`frame_index`、`episode_index`、`index`、`task_index`、`next.reward`、`next.done`、`next.truncated`、`is_success`
- v3.0：`index`、`task_index`、`episode_index`、`frame_index`、`next.reward`、`next.done`、`next.truncated`、`subtask_index`

## 3D URDF 查看器

查看器是 `src/components/urdf-viewer.tsx`。

- URDF 和网格托管在 HF bucket `lerobot/robot-urdfs`：基础 URL 是 `https://huggingface.co/buckets/lerobot/robot-urdfs/resolve`（没有 `/main` 段；bucket 没有分支）。本地开发用 `NEXT_PUBLIC_URDF_BASE_URL` 覆盖。
- bucket 下的资源布局：`g1/`、`openarm/`、`so101/`（SO-100 和 SO-101 都在这里）。
- **URDFLoader 的坑**：我们的 `loadMeshCb` 返回之后，`URDFLoader.js` 会执行 `if (obj instanceof THREE.Mesh) obj.material = <urdf-material>`，把我们设的材质覆盖掉。绕法：把加载出的网格包进一个 `THREE.Group`，让 `instanceof Mesh` 判断失败。DAE 返回的本来就是 Group；STL 必须显式包一层。
- **STLLoader 的事件顺序**：`manager.itemEnd(url)` 在用户的 `onLoad` 回调**之前**触发，所以 `manager.onLoad` 可能在网格挂到机器人树之前就触发。加载后的工作（自动取景、阴影标志）用 `setTimeout(..., 0)` 推迟。不要在 `manager.onLoad` 里重建材质：直接在 `loadMeshCb` 里选好原型颜色。
- **OpenArm 的 DAE 文件自带 23 个多余的 `PointLight`**，会淹没场景光照。把 `collada.scene` 里除 `AmbientLight` 之外的灯都去掉，再加进机器人。
- 场景设置：`<Canvas shadows>`，`ACESFilmicToneMapping`（曝光 0.9），三点方向光加环境光，`<Environment preset="studio" background={false} />`，`<color attach="background" args={[MEDIA_BACKGROUND]} />`（黑色，来自 `src/components/viewer/data-palette.ts`）。必须有 `<OrbitControls makeDefault />`，`useThree().controls` 才会暴露控制器，自动取景才能用。

## 设计系统

`src/app/globals.css` 是唯一的全局样式表：页面底色由设计令牌设定（`src/app/levi.css`、旧的变量和 Tailwind 颜色重映射都已删除；Tailwind 的颜色类就是 Tailwind 自己的颜色，有测试拒绝 `src/` 里残留任何一个）。
设计系统：`--ds-*` 令牌在 `src/styles/tokens.css`，`ds-*` 样式在 `src/styles/ds.css`，组件在 `src/components/ds/`，外框（吸顶顶栏、命令面板、toast、错误页）在 `src/components/shell/` 和 `src/styles/shell.css`，唯一的 LEVI 标志在 `src/components/shell/brand.tsx`，开发环境下 `/design` 是样例页。每条路由都使用它。新的 UI 代码用 `ds-*` 组件和令牌，绝不硬编码颜色（ESLint 覆盖整个 `src`；例外及其理由写在 `eslint.config.mjs`）。`title=` 绝不是控件名称、禁用原因或图标含义的唯一载体。详见 docs/DESIGN.md（其“Migration status”一节说明哪些地方仍用 Tailwind 工具类叠在令牌之上排版）。
`src/components/levi-locale.tsx` 和 `src/i18n/` 提供中英文界面；`src/i18n/__tests__/catalog.test.ts` 保证两份目录一致，并能发现缺少键的 `t("…")`。
修改继承来的组件时，要保持明暗两套主题、键盘可达和响应式布局。

## 内置转换与服务

- `levi/conversion/`：模块化转换。`registry.py` 列出输入格式（`inputs/`：`robot_capture`、`image_sequence`、`lerobot`）和导出目标（`outputs/`：`lerobot_v21`、`recap_value`）；`pipeline.py` 是单遍转换器（父进程按 CSV 做计划，spawn 进程池对每个相机各做一次解码加一次编码，`retime` 模式下则是无损重封装），`report.py` 是工作台渲染的检查/兼容性模型。`raw.py`/`media.py`/`dataset.py` 是经过测试的基础件。时间戳永远是 `frame_index / fps`。见 docs/CONVERSION.md 和 docs/RECAP.md。
- `levi/annotations/sidecar.py`：物体标注。parquet 把掩码扁平存储（`rle_size`/`rle_counts`）；`read_annotations`/`read_episode` 返回 `ObjectAnnotation` 和查看器都期望的 `mask_rle` 形状。不要在调用处转换：这种重复曾经让叠加层没有掩码。
- `levi/views.py`：原始采集的浏览视图；`levi/annotations/carryover.py` 按 `source_demo` 把标注带进转换结果；`levi/annotations/outcomes.py` 存人工的成功/失败标签。
- 命名（`levi/naming.py`）：按数据集的产物用目录名，按运行的产物用时间戳，绝不用哈希。`uv run levi migrate` 升级旧工作区。见 `docs/WORKSPACE.md`。
- `levi/sync.py`：后台工作区同步（发现、刷新、重建原始视图、丢弃已移除的数据集）；`levi/revision.py` 是只看 stat 的数据集修订号，标注后端和查看器共用。目录（catalog）写入一律经 `catalog.locked()`（跨进程）。
- `levi/jobs.py`：白名单内的工作进程、不可变的计划、超时与恢复。
- `levi/children.py`：LEVI 启动的每个工作进程组（SAM3、转换、Pilot）都带着所有者身份被追踪；服务停止时终止自己的，启动和 `levi stop` 时回收被杀死服务留下的。任何新的长时间运行的子进程都要在这里登记（`track` / `untrack`）。
- `levi/service.py`：本地 API、目录、诊断和数据集文件服务。
- `src/app/api/levi/` 和 `src/app/api/annotation/`：到环回（loopback）后端的运行时代理。
- `levi/maintenance.py`：有界的缓存清理和孤儿报告；保留已登记的数据集、结果和环境。服务运行时它拒绝执行：先 `levi stop`。
- `levi/agent/usage.py`：按 agent 的成本模型（固定 + 每片段 + 每帧，按读取方式计价，用记录下来的运行校准）。在线运行由 LEVI 自己计量；外部 agent 自报。绝不把估算当作价格呈现。
- `levi/harness/`：任务收尾（每个结束的运行一次，不调用模型）→ `datasets/<name>/tasks/<run>/ledger.json`、`workbench/memory/<name>.json`（只有人确认提交的事实；在下一个任务制定计划时交给它）和 `workbench/improvements/<name>/<slug>.json`（计划 §6.6 的状态机；只有 `PARAMETERS` 能发布，且由人发布）。agent 调用和网页界面调用分开计数；只有 agent 的才算 token。`cost.py` 给每个运行一条 token/时间记录（计量 / 自报 / 送达下限），并给出每个数据集、每个 agent 的成本画像，供分流和 `plans.estimate` 使用。未解决的问题、数据知识和 harness 经验保存在 `.state/memory/` 的本地记忆里（git 忽略，绝不交给学习者）：问题的修复通过测试后就删掉该条。对任何数据集都成立的知识放进 `levi/knowledge/<topic>.md`（内置，经 `harness/knowledge.py` 到达每个模型）；用 `levi agent knowledge promote` 提升，绝不直接粘贴特定数据集的事实。
- `levi/eval/`：子任务标注的评测记录（`<workspace>/eval/`），一个指标模块供人和 agent 共用；整数据集时间轴运行结束时写入 agent 记录。`levi/eval/adapters/` 把 A、B、C 各臂的运行转成给 `compare` 用的 `arm_record.v2` 文件（`python -m levi.eval.adapters`，见[评测](EVALUATION.zh-CN.md)）。
- `levi/agent/anchored.py` 和 `levi/agent/anchored_specs/*.json`：释放锚定复核（`workflow.anchored`）。一份规格（JSON，由计划冻结）声明锚点、每个相机的帧数、问题、答案字段和规则，可选地声明起始检查、否决条件、按语言的 `title` 和 `status`（`stable` 或 `candidate`；候选绝不是默认）。任务专用的行为属于规格，不属于这个模块。结果通过 `anchored.specs` / `anchored.get` 和 `/api/anchored/*` 路由读取。见 docs/ANCHORED_REVIEW.md。
- `levi/segmentation/`、`backend/segmentation.py`、`integrations/segmentation/`：快速实例分割。核心从不导入 Torch：学生模型在自己的工作进程环境里运行（标注和蒸馏作业，每个实时叠加会话一个工作进程，都在 `levi/children.py` 里登记）；结果就是普通的物体标注，用 `SidecarStore.publish_merged` 写入，所以一次运行只替换它覆盖到的（片段，相机）对。`LEVI_GPU_LOCK_FILE` 是可选的 `flock`，只有这些作业会拿。见 docs/SEGMENTATION.md。
- `levi/recap/` 和 `integrations/recap_value/`：LEVI 内的 RECAP 价值模型（检查点库、工作作业、`workbench/recap_values/<name>/` 下按数据集的修订、`levi recap`）。推理在工作进程自己的环境里；`fake` 提供者不需要模型。见 docs/RECAP.md。
- `levi/training_manifest.py` 和 `integrations/training_manifest/levi_manifest_reader.py`：训练清单（`levi export`）。操作与任务无关，只读结局和帧标签。读取器是单个独立文件，训练方直接复制。见 docs/TRAINING_MANIFEST.md。
- `levi/pool/`：训练池（`levi pool`、`/pool`）：对 `LEVI_POOL_ROOTS` 的只读按片段索引、配方和导出，留出的片段会被拒绝。见 docs/TRAINING_POOL.md。
- `levi/report.py`：`/report` 背后的只读技术报告（`LEVI_REPORT_DIR`，限定在该文件夹内）。`levi/samples/`：DROID 测试样例。`catalog.create_namespace` / `levi namespace`：在同一份源数据集上做隔离的实验。
- `levi/docs.py`：`levi docs check|sync`。当文档缺少某个能力、某个 `levi agent` 命令、某个面向用户的 `LEVI_*` 设置、某个链接目标或某个再生成的段落时，测试套件会失败。新增其中任何一项后，在同一个改动里更新文档（或运行 `levi docs sync`）。
- `levi/harness/context.py` 构建 LEVI 运行的模型在提示词里拿到的简报（数据集记忆、教师笔记，只含其他片段的示例）；`teaching.py` 把监督反馈变成 `datasets/<name>/teaching/` 文件和记忆笔记；`tasking.py` 是自然语言任务入口（规格 → 目录检查 → 人工批准 → `advance` 到每个关卡）；`grading.py` 按 `teaching/reference-<task>.json` 给学习者打分。
- `levi/inference/`：本地模型提供者（`provider.LocalProvider`），经 `client_for` 选择两种协议客户端：原生 Ollama（`ollama.py`，加 `runtime.py` 里自有的服务）和本地的 OpenAI 兼容服务器如 vLLM（`openai_local.py`，配置类型 `openai-local`），都走固定的环回传输（`transport.py`）。`gpu.py` 是错峰守卫：另一个进程在 GPU 上计算、或离开 GPU 不足 `LEVI_GPU_QUIET_SECONDS` 时，不调用本地模型也不启动服务；出现这种进程时服务卸载模型；为某个 `openai-local` 配置的端口提供服务的进程（通过其监听 socket 找到）算作该配置自己的模型，而对 Ollama 和其他配置来说则算别人的工作；被挡住的运行在 GPU 对它自己的配置空闲后恢复。测试在 `conftest.py` 里设置 `LEVI_GPU_SHARING=allow`，所以结果不依赖机器上谁在训练。
- `levi/agent/housekeeping.py`：只删除 `runs.prepare` 能重建的东西；已提交的修订、来源记录、逆补丁和未关闭的草稿保留。
- `levi/agent/detection.py`：为物体标注提供不依赖模型的候选区域（颜色量化加连通域）。它只测量，不识别。
- `src/utils/serviceToken.ts`：环回 API 的界面令牌，从服务自己拥有的文件读取，所以 Core 重启不会让前端失联。`authHeaders(url)` 只把它附加到 LEVI 自己的文件服务，绝不附加到 Hugging Face 请求。

## 测试与 CI

- 测试文件放在源码旁边的 `**/__tests__/` 目录里。
- 使用 `bun:test`（内置，无需额外安装）。
- BigInt 字面量（`42n`）需要 `tsconfig.test.json`（target ES2020）：测试文件被排除在 `tsconfig.json` 之外。
- `@types/bun` 作为 devDependency 安装，供 `bun:test` 做类型解析。
- 模拟 fetch：`globalThis.fetch = mock(() => Promise.resolve(new Response(...))) as unknown as typeof fetch`
- Python 测试在 `tests/`；用 `uv run pytest`。一次只跑一套：两次并发运行共用 `--basetemp` 会留下残渣，让后面的运行在无关的地方失败（ffprobe 读到写了一半的夹具、`rm_rf` 删非空目录）。如果一次运行失败得很怪，先 `rm -rf .state/tmp/build/pytest` 再试。
- Python 测试里的墙钟等待要经过 `scaled()`（`tests/conftest.py`；CI 设 `LEVI_TEST_TIME_SCALE=2`）：写 `time.monotonic() + scaled(10)`，不写 `time.monotonic() + 10`，并轮询到条件成立，不写 `for _ in range(100): time.sleep(0.02)`（固定次数乘固定间隔同样不随倍率放大；只识别字面量 `range(N)` 加字面量 `time.sleep(s)` 且 N×s 不少于 1 秒的写法，`range(n)` 用变量的看不到）。`tests/test_no_unscaled_deadlines.py` 扫描 `tests/**/*.py` 的这两类写法，把每个文件的违规数与 `tests/unscaled_deadlines_baseline.json`（计数循环记在 `count_loops` 键下）比较，这是只降不升的棘轮：新增字面量截止时间或计数循环会失败，改好一处后基线没降也会失败，用 `python3 tests/test_no_unscaled_deadlines.py --write-baseline` 调低基线。不是等待的用法在该行加 `# unscaled-ok: <原因>`。
- CI：`.github/workflows/test.yml` 在 push/PR 时跑三个作业：`frontend`（校验和生产构建）、`python (no extras)`（`levi dev check-contracts`、Ruff、`scripts/no_extras_check.py`、`pytest --collect-only`）和 `python (full, agent extra)`（整个套件跑一次，四个 worker）。同一分支上较新的 push 会取消较旧的运行；`main` 上的每次 push 各自保留。挂起的测试会在 `pyproject.toml` 里的单测 `timeout` 后失败，不会拖住整个作业。

## 验证分档的实测依据

[AGENTS.md](../AGENTS.md) 验证分档里那些数字的依据。

- **一次检查的代价。** 在 4 核、nice 19 下实测：整个前端检查约 25 秒；Python 套件用四个 worker（`-n 4 --dist loadfile`）约 9 分钟（2026-10-27 为 527 秒），单进程约 28 分钟。所以前端检查每次提交都完整运行，完整的 Python 套件只在合并前运行。
- **agent 额外依赖。** 合并档在同步了 `agent` 额外依赖的环境里（`scripts/dev-env.sh --agent`）跑完整套件。它是默认安装的超集；默认安装的完整运行只多出 3 个被跳过的测试。
- **测试选择器。** `scripts/select_tests.py` 是启发式，不是证明。在 9702dcc9（“fix(dev): select_tests no longer under-selects; dev-env.sh refuses the main checkout”）上，用 `--eval-history` 回放最近 300 个同时改了代码和测试的提交，它点出了这些提交所改的 533 个测试文件中的 521 个，约 98%。回放不包含被删除和被重命名的文件，且选择器后来的改动之后没有重复这次回放。
