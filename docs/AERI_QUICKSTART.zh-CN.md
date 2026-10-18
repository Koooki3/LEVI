# AERI 快速上手

[English](AERI_QUICKSTART.md)

AERI 是 LEVI 的自动评测流水线：让策略在机器人上一轮一轮地跑，每轮做判定，并把发生的事记进一份运行日志。这一页带你**不用机器人**、大约五分钟从零跑完一次：空跑只驱动进程内的假件。第一次真机评测之前先走一遍，之后会遇到的文件、命令和结果在这里都能见到。

各部分怎么配合，先看架构图集的[第 6 张](ARCHITECTURE.md#6-automatic-evaluation-three-parts-one-authority)（谁能让机器人动）和[第 8 张](ARCHITECTURE.md#8-an-aeri-run-start-to-finish)（一次运行从头到尾）。完整参考见[自动评测流水线](AUTOMATIC_PIPELINE.zh-CN.md)。

## 1 用假件空跑一次

空跑使用 `integrations/fr3_automatic/fake.py` 里的假机器人：运行期间所有网络连接都被拒绝，没有任何真实部件有运动权限，结果写在临时目录，加 `--keep` 才会保留。

### 两个文件

放在同一个目录里（下面叫 `JOBDIR`）。

`initial-state.yaml`，初始状态契约：一轮开始之前必须满足的条件。

```yaml
initial_state:
  id: stack-plates-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
```

`automatic-eval.yaml`，作业文件（也可以用网页界面里的启动向导生成）。把 `JOBDIR` 换成这个目录的绝对路径。

```yaml
schema_version: levi.aeri.job.v1
experiment:
  name: rehearsal-1
  episodes: 2
  random_seed: 42
  execution_mode: shadow
policies:
  forward:
    max_steps: 30
  reset:
    max_steps: 20
task:
  instruction: stack the plates
  reset_instruction: "Reset: stack the plates"
  initial_state_spec: initial-state.yaml
termination:
  min_steps: 5
  settle_steps: 3
  cooldown_steps: 5
reset:
  strategy: single_reset_policy
  max_attempts: 1
  on_unknown: reset
recording:
  rollout_root: JOBDIR/rollouts
  group: aeri
  forward_folder: stack__rehearsal-1
  reset_folder: reset_stack__rehearsal-1
```

作业文件只接受严格的 YAML 子集：映射要像上面这样分行写（`{ … }` 这种一行的写法会被拒绝），运行名里不能有点号。开始之前先建好 `JOBDIR/rollouts`。

### 六条命令

每条命令都只读，或者只动假件。

| 命令 | 应该看到 | 说明什么 |
| --- | --- | --- |
| `uv run levi automatic doctor --config JOBDIR/automatic-eval.yaml` | 契约快照、假件、作业文件、启动检查、rollout 目录都是 `ok` | 机器和作业可以空跑。每行都是 `ok` 再继续 |
| `uv run levi automatic validate --config JOBDIR/automatic-eval.yaml` | `valid: run rehearsal-1, 2 episodes, reset strategy single_reset_policy, …, plan <short hash>` | 作业被接受；短哈希标识它冻结的计划 |
| `uv run levi automatic plan --config JOBDIR/automatic-eval.yaml --mode dry_run` | `launchable: yes`，外加一条 `W_CONTRACT_DRAFT` 警告：初始状态契约还是草稿 | 空跑可以用草稿契约；真机运行前要由人确认 |
| `uv run levi automatic run --config JOBDIR/automatic-eval.yaml --dry-run --keep JOBDIR/kept` | `dry run (fakes only, no motion authority): COMPLETED, 12 transitions, 2 forward episodes, 0 resets, fake robot motions 24`，约 1 秒 | 每轮循环在假件上完整跑通 |
| `uv run levi automatic status --run-dir JOBDIR/kept/.aeri/runs/rehearsal-1` | `rehearsal-1: COMPLETED, 35 lines, …` | 只读查看运行日志，不写入 |
| `uv run levi automatic report --run-dir JOBDIR/kept/.aeri/runs/rehearsal-1 --config JOBDIR/automatic-eval.yaml --format md` | 一张表：forward episodes 2、autonomous success rate 1.000 (2/2)、early stops 2、saved steps 38 …… | 真机运行会报告的同一组指标 |

上面的输出是 2026-10-15 用这两个文件实测记录的。计划哈希取决于作业内容（包括所在目录），所以你看到的会不同。

### 读懂结果

`JOBDIR/kept/.aeri/runs/rehearsal-1/state_journal.jsonl` 是运行日志。只有让机器人动的那个部分写它，页面和视觉模型只读。报告里的 *autonomous* 比例是运行自己的判定，不是真值：真值是人给的盲标签（`uv run levi automatic label`，在终端里逐条确认），报告拿它来对比。

## 2 第一次真机运行

> **真机运行暂缓**，直到“只认终端”的版本进入 main。之后每一轮只有终端 D 回答场景问题、放行这一轮；页面和视觉模型只显示它们看到的东西。

**状态。** 下面的步骤是软件，已用假件和离线运行测试；硬件验收待做。真机运行需要有人在机器人旁、手放急停，并且从不通过 HTTP 启动。

1. 启动机器人栈（机械臂控制、夹爪、robot server、健康监视器）和实时服务；写好作业文件、binding 和场景契约，并由人确认契约。
2. 运行只读预检 `python scripts/aeri.py check --config JOB --binding BINDING`，它会打印计划和 binding 的摘要。
3. 在一个终端（第 8 张图里的终端 D）运行 `python scripts/aeri.py execute …`，带上第 2 步的摘要，并按提示输入确认短语。只有这个进程发送运动指令。
4. 每一轮：复位场景，然后在终端 D 里回答场景是否就绪。页面会显示这个问题，但放行这一轮的答案要在终端 D 里输入。main 上终端要求键入 `resume`；单键控制台（Enter、`y` / `n` / `u`）在集成分支上。
5. 在页面上看进度、打标签。只想按自己的判定提前结束正在进行的这一轮时，在它运行中于终端 D 按 `s`（成功）、`f`（失败）或 `d`（作废）：机械臂照常保持、回位，接着进行下一轮；作废的一轮不计轮数、自动补跑一轮，补跑最多与运行轮数相同（见 [AUTOMATIC_PIPELINE.zh-CN.md](AUTOMATIC_PIPELINE.zh-CN.md) 的“按人工判定提前结束一轮”）。要停止整次运行：在页面上点停止，或在终端 D 里按 Ctrl+C。出故障时运行会被锁定：在运行日志里看原因，再开一次新的运行。

每一步的细节、出错情况以及停止和恢复的命令见[自动评测流水线](AUTOMATIC_PIPELINE.zh-CN.md)；README 的[运行一次 AERI 评测](../README.zh-CN.md#运行一次-aeri-评测)一节在第 8 张图旁列了同样的五步。
