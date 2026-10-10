# 上机向导：配方与状态探针

[English](SETUP_WIZARD.md) | 中文

LEVI 可以引导操作者准备一次真机会话：下一步运行哪条命令、它会动到什么、它要启动的组件是否已经健康运行。本页说明目前已有的两部分：**配方注册表**（把每一步和本机操作手册对应起来）和 `GET /api/levi/setup/status` 背后的**只读状态探针**。两者都不启动、不停止、不连接任何东西。

## 配方

一条配方对应本机操作手册（一个 Markdown 文件）里的一段摘录，并写明它会动到什么、界面可以怎样提供它。配方文件里有本机的路径、地址和序列号，所以放在仓库之外：用 `LEVI_SETUP_RECIPES` 指向配方文件，用 `LEVI_SETUP_DOC` 指向手册。

```toml
version = 1

[[recipe]]
id = "R-CHK-3"
title = "GPU、进程和端口快照"
risk = 1            # 1 只读诊断，2 LEVI/vLLM/记录器服务，3 机器人控制栈或策略服务，4 会让机器人动
ui = "native"       # native | execute | copy | link
requires = []      # 必须先完成的配方编号
preconditions = ["无"]

[recipe.source]
section = "1"       # 手册的节号
heading = "启动顺序与终端"   # 记录时的节标题（标题改了也算漂移）
block = 1           # 该节的第几个代码块，从 1 开始；不写 = 整节正文
pick = [4, 6]       # 可选：代码块内的行范围
lines = [58, 60]    # 记录时所在行（仅供参考）
sha256 = "<规范化摘录的 64 位十六进制哈希>"
# text = "..."      # 可选：记录时的摘录原文，漂移时用来显示差异

[recipe.touches]
robot = false
gpu = false
moves = false
commands_robot = false
listens = []        # 它会监听的端口
connects = []       # 它会连接的端口
```

**安全规则（违反任何一条，校验器拒绝该配方并把它排除）：**

- ③④类配方永远不是 `execute`，也不是 `native`，只能是 `copy` 或 `link`。
- 唯一例外是只加载模型的③类策略服务（`kind = "policy_server"`）：`touches.robot`、`moves`、`commands_robot` 都为 false，并带有 `confirm = { required = true, decision = "<谁、何时允许>" }` 时，可以标为 `execute`。目前 LEVI 里没有任何东西会执行它。
- `moves = true` 必须是④类；`commands_robot = true` 必须是③或④类。
- 会连接机器人侧端口（5000、5001、5100、7470、8000）的配方只能是 `copy` 或 `link`：LEVI 从不连接这些端口。

**漂移。** 哈希覆盖的是规范化后的摘录：Unicode NFC、去掉行尾空格、去掉首尾空行；其他任何改动，哪怕一个字，都算漂移。检查按节号和代码块序号定位摘录，不按行号：

| 状态 | 含义 |
| --- | --- |
| `ok` | 摘录未变（位置变了时标 `shifted`） |
| `drift` | 文字或节标题变了：有人复制这条命令之前要先复核（配方记录了 `text` 时给出差异） |
| `moved` | 同样的文字在别处（代码块换了顺序、节重新编号）：更新配方的 source |
| `missing` | 节、代码块或所选的行不存在了 |
| `ambiguous` | 同一个节号出现了不止一次 |
| `doc_error` | 手册无法可靠解析（有代码块没有闭合） |

```bash
levi setup recipes check --recipes site/setup-recipes.toml --doc setup.md    # 退出码 0 / 1（有问题或漂移）/ 2（读不了文件）
levi setup recipes check --json
levi setup recipes excerpt --doc setup.md --section 1 --block 1 --pick 4 6  # 打印哈希和行号，用来写配方
```

两条命令都只读这两个文件。漂移后是否更新配方文件由人决定（手册另有维护流程）。
