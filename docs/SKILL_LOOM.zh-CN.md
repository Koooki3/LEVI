# Codex 与 Claude Code 使用 Skill Loom

[English](SKILL_LOOM.md)

[Skill Loom](https://github.com/Koooki3/skill-loom) 用任务证据维护 agent 技能，支持审阅变更计划、应用和回退。LEVI 已收录它的 `skill-lifecycle` 技能正文与引用，供开发 LEVI 的 Codex、Claude Code 等使用。版本固定为 **0.1.1**，提交为 `34efa6e539e2d1da829d05652d6c0df1c1433a7a`。上游技能正文保持原样；`LICENSE` 和 `ORIGIN.json` 记录 MIT 许可、来源与文件哈希。

## 技能发现

从 LEVI 检出目录或属于这个 Git 仓库的子目录启动 agent。嵌套的独立 Git 仓库有自己的技能发现边界。

| 宿主                         | 项目入口                          | 显式调用             |
| ---------------------------- | --------------------------------- | -------------------- |
| Codex                        | `.agents/skills/skill-lifecycle`  | `$skill-lifecycle`   |
| Claude Code                  | `.claude/skills/skill-lifecycle/` | `/skill-lifecycle`   |
| 其他支持 Agent Skills 的宿主 | 按宿主规则选择上述技能根目录之一  | 使用该宿主的调用方式 |

Codex 的入口是相对目录符号链接，指向 Claude Code 入口中的实体目录，两者读取同一份文件。Git 检出时需要保留符号链接；若平台把它检出成普通文本文件，先建立宿主支持的目录链接，再验证 Codex 发现。让宿主报告实际加载的 `SKILL.md` 路径；新技能没有出现时用新会话核对。目录规则见 [Codex 官方文档](https://learn.chatgpt.com/docs/build-skills)和 [Claude Code 官方文档](https://code.claude.com/docs/en/skills)。

可以这样发出任务：

> 使用 skill-lifecycle 检查 LEVI 的项目技能。先说明发现的问题和建议改动，任何文件写入都等我批准后再执行。

## 可选的本地 CLI

技能可以用已有文件工具执行维护流程。需要盘点、计划、应用和回退命令时，再安装 Python CLI。它要求 Python 3.10+ 和 uv，使用自己的虚拟环境。以下安装命令需先获批准，在 LEVI 主检出运行，且目标目录应尚不存在：

```bash
git clone --no-checkout https://github.com/Koooki3/skill-loom.git lab/tools/skill-loom
git -C lab/tools/skill-loom checkout --detach 34efa6e539e2d1da829d05652d6c0df1c1433a7a
uv venv --python python3 lab/tools/skill-loom/.venv
uv pip install --python lab/tools/skill-loom/.venv/bin/python --editable ./lab/tools/skill-loom 'PyYAML==6.0.3'
mkdir -p lab/skill-loom/checks lab/skill-loom/candidates lab/skill-loom/journal
cp lab/tools/skill-loom/examples/user-profile.json lab/skill-loom/profile.json
```

按用户的需求、语言、观察范围和预算修改私有 profile。示例里的权重和预算只是起点，没有验证为最优值；初始修复轮数设为一轮。本维护工作区已有 profile，使用中文，要求写入前明确批准，只观察选定的项目技能目录。`lab/` 不进 Git。

在主检出或自己的 feature worktree 中，用下面的命令找到共用的工具环境和状态目录：

```bash
loom_git_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
loom_checkout="$(dirname "$loom_git_dir")"
loom_python="$loom_checkout/lab/tools/skill-loom/.venv/bin/python"
loom_state="$loom_checkout/lab/skill-loom"
"$loom_python" -m skillloom --version
"$loom_python" -m skillloom --help
```

下面两条命令检查 profile 和实体项目技能目录，并写入私有报告。写报告也需要批准。每次检查选一个新的目录，保留原有证据：

```bash
loom_run="$loom_state/checks/run-001"
mkdir "$loom_run"
"$loom_python" -m skillloom doctor --profile "$loom_state/profile.json" --out "$loom_run/doctor.json"
"$loom_python" -m skillloom inventory --root "$PWD/.claude/skills" --out "$loom_run/inventory.json"
```

每次只盘点一个发现入口；同时盘点两者会把同一个技能报成重复项。`doctor` 检查 profile 格式和可执行文件是否存在，`inventory` 检查元数据、引用和脚本语法。宿主是否发现技能、是否实际调用，需要另外验证。

## 审阅变更与回退

把单个候选放入新的私有目录，目录名与目标技能一致。先检查固定提交、许可、依赖、脚本、触发条件和本地定制的差异，运行适合该任务的验证，再生成精确计划。目标选择自己 worktree 中的实体技能根目录：

```bash
"$loom_python" -m skillloom plan \
  --candidate "$loom_state/candidates/run-001/skill-lifecycle" \
  --root "$PWD/.claude/skills" \
  --journal "$loom_state/journal/run-001" \
  --out "$loom_run/plan.json"
```

读完整的计划文件，保存命令打印的 `review_digest`。具体变更获批准后，把这个值原样传给 `apply`：

```bash
"$loom_python" -m skillloom apply --plan "$loom_run/plan.json" --digest '<已审阅摘要>'
"$loom_python" -m skillloom rollback --transaction '<apply 返回的事务目录>'
```

回退同样需要批准。候选、技能根目录和 journal 必须互不包含。Skill Loom 会拒绝修改链接目标或链接根目录；保留 `.agents/skills` 的发现链接。候选或已安装文件发生变化时，重新生成并审阅计划。摘要只绑定计划内容，不代表授权。

事务记录包含目标的绝对路径。需要回退时，在对应 worktree 仍存在时执行；该事务不能撤销已经合并进主检出的文件。经过独立审查和 Git 整合后，事务保留为开发证据；撤销已整合的配置要另做一次经过审阅的 Git 改动。后续每次技能更新都使用新的计划和 journal。

遵守 [AGENTS.md](../AGENTS.md) 和维护工作区手册里的审批、文件所有权、独立审查与 driver 整合规则。维护范围限于获准的项目开发技能及私有 Skill Loom 状态。用户级目录、凭据、宿主会话、托管插件、源数据和 gold 不在该范围内。定时任务或 hooks 作为单独的配置改动审阅和批准。

## 验证

用独立的离线测试目录检查安装、更新、回退和无变化退出。以下命令会写入指定的测试目录和测试临时文件，先获批准；保留结果后清理测试目录：

```bash
"$loom_python" -m skillloom demo --workdir "$loom_run/demo"
(cd "$loom_checkout/lab/tools/skill-loom" && "$loom_python" -m unittest discover -s tests -v)
```

逐个宿主核对：加载了预期入口；技能维护请求能触发；普通编码请求不触发。CLI 测试和静态发现检查不能证明模型任务质量。本地接入的检查记录在 `lab/skill-loom/checks/`；没有可执行文件的宿主，保留为待实际验证。
