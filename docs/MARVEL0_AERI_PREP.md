# The AERI preparation card of the marvel0 page

English first, Chinese after the line below. Backend: `levi/marvel0/aeri_prep.py` (the versioned port of `new_run.py`, the code
pin, the templates, run names, the check), `aeri_ready.py` (the passive readiness rows and the document), `aeri_leftovers.py` (what
an earlier run leaves behind, the two stops), `aeri_api.py` (routes), `site.py` and `control_site.py` (the `[aeri]` sections).
Tests: `tests/marvel0/test_marvel0_aeri_{site,prep,ready,leftovers,route,ready_sample}.py` on the made-up machine
`tests/marvel0/aeri_world.py`; samples `tests/marvel0/regen_aeri_ready_sample.py`; mutation sweep
`tests/marvel0/mutate_aeri_prep.py`. Design record:
`LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md` (section 7, 8, 11). The hosted terminal D (keys, state,
Stop) is `docs/MARVEL0_AERI_RUN.md`; the whole step is `docs/MARVEL0_AUTONOMY.md`.

## What the card does

It is the part of Step 5 (Autonomy system) that gets an AERI run ready, **before** any terminal D exists. It starts nothing that
moves: no D, no client, no policy server. It reads the machine passively, writes one run folder, and offers two stops with a
typed phrase. Nothing here is an emergency stop; the gates stop mistakes, not another program of the same account; the barriers
are Franka Desk and the physical emergency stop.

- **Off by default.** `lab/marvel0/control.toml` needs `[aeri] enabled = true` (the control file of `docs/MARVEL0_RUN.md`). A missing
  section is off; a malformed one (not a table, an unknown key, a value that is not a bool) is off **with the errors listed**, and
  never switches the `[stack]` or `[client]` switches off. A missing, unreadable or invalid control file is off.
- **Site section.** `lab/marvel0/site.toml` may hold `[aeri]` (`site.AERI_TEMPLATE` is the text to start from). A missing section is
  "not configured". A malformed one is "invalid" with the errors, and the rest of the site file stays valid. Keys: `profile` (default
  `harvest-5bc750ca`, the code that ran on the real robot on 18 Oct), `pin_sha` (8 to 40 hex digits; the worktree's HEAD must start
  with them), `worktree`, `runs_root`, `templates_root`, `rollout_root`, `openpi_root`, `levi_python`, `levi_bin`, and optional
  `console_lang` (`zh`), `uv_offline` (true), `tools_root` (`<runs_root>/../tools`), `local_model` (local mode is offered only
  with it). Every path is absolute, normalized and only of the characters `shlex.quote` leaves alone, so the command the page prints
  for a terminal is the very text it hosts.
- **Profile.** One profile per known AERI version (`aeri_state.PROFILES`: `harvest-5bc750ca`, `bfdee2cf`, `main-297a74c3`). An
  unknown profile, or a worktree whose four source files differ from the ones the profile was written from
  (`aeri_state.profile_matches`), is refused. A version is never guessed.

### `GET /api/levi/marvel0/aeri` (`levi.marvel0.aeri.v1`, cached three seconds)

`enabled`, `control`, `site`, `profile`, `profiles`, `limits`, `readiness`, `ready`, `readiness_sha256`, `prepared`, `live_service`,
`leftovers`, `confirm`, `notices`. Every row is `{code, level, text, params}`, level `ok`, `warn`, `refuse` or `unknown`;
**`unknown` refuses like `refuse`**; the card is `ready` only with rows and none refusing. Every row is read from a file, `/proc`,
`nvidia-smi` (query options), `systemctl --user show/list-units`, `git` (no optional lock) or a read-only view of another part of
the page. A source that fails makes its row `unknown`; it never fails the request.

| Row | Passes when |
| --- | --- |
| `aeri_enabled`, `product`, `aeri_site` | the control file switches AERI on; this is the product LEVI; the site section is valid |
| `code_pin` | the worktree is a git top folder, HEAD starts with `pin_sha`, `git status --porcelain` is empty |
| `profile_known`, `prompts_checked` | the profile is in the table; its four source files are the recorded ones |
| `templates_sha` | `job.base.yaml` and `binding.base.json` hash to their lines in `SOURCES.sha256` |
| `prepared`, `run_name_free` | a run prepared by this page is intact (below); its name is used by no rollout folder or AERI registry entry |
| `cameras_config` | the binding's `cameras.side` and `cameras.wrist` equal the site file's `[cameras]` view and wrist |
| `gpu_free`, `mem_available` | free GPU memory at least 9 GiB (external) or 19 GiB (local); `MemAvailable` at least 28 GiB |
| `gpu_lock_free`, `arm_lock_free` | nobody holds the GPU lock or the arm lock (`/proc/locks`; never taken) |
| `ports_free`, `live_service` | nothing listens on 8000, 8100, 8110; the live annotation service is not running |
| `no_e_run`, `no_policy_deploy`, `no_other_aeri` | no hosted evaluation client or session; this page has no policy server unit; no AERI run is alive |
| `no_aeri_unit_left`, `swap_logs_clean` | no `levi-aeri-vlm-*` or `levi-aeri-policy-*` unit exists; no policy-switch log is unfinished |
| `monitor_green`, `stack_ready` | the health file is fresh and not red; the arm and the robot server look up (the gripper stack and the controller cannot be observed, so this row shows `warn` at best) |

The arm's pose is **not** read (it needs the robot server's port): "the arm is at home" stays a tick of the person's and the check
of AERI's own start gate. `readiness_sha256` covers each row's code and level, the profile, the AERI HEAD and the prepared run's
digests, **not** the numbers in `params`; the hosted run asks `aeri_ready.snapshot()` (always a fresh read) at its challenge and at its
start and needs the two to be equal.

### `POST .../aeri/prepare` (phrase `prepare aeri run`)

Body `{request_id, mode, task, rounds, steps, gripper_confirm_s, start_home, confirm}`. It needs: the switch on, the product LEVI, a valid site
section, a known profile, the pinned clean code, unchanged templates and a valid form (rounds 1 to 50, steps 1 to 3000,
gripper confirmation 1.0 to 10.0 s or null, task `[a-z0-9 ,.'-]{3,120}` with no edge space and a letter in it, `start_home` only for a
profile that has startup homing, local mode only with `local_model`). Hardware rows do not gate preparing; they gate the start.

It is `new_run.py` as a function (same substitutions, same files, same `execute` string for the same worktree; the first line of
`job.yaml` names the page instead of the script). It writes **only** `<runs_root>/<name>/` (`job.yaml`, `binding.json`, `check.json`,
`RUN.json`, `queue/` in external mode) and the page's folder `<workspace>/marvel0/aeri_prep/` (`record.json`, `requests/`,
`history.jsonl`). The name is `aeri-<date>-ext|loc-rNN`, the first number used in none of: the runs folder, `<rollout_root>/.aeri/runs`,
`~/.levi-aeri/runs`, this page's record; the folder is made with `mkdir` (a name taken in between is skipped). It then runs
`aeri.py check` of the pinned worktree (`nice -n 19`, 60 s, an environment of `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`, `PATH`
and nothing else) and needs `plan_sha256`, `binding_sha256`, `reset_strategy == "subtask_loop"`, `hardware_verified == false`; on any failure
the folder is removed. `RUN.json` is written last and adds to `new_run.py`'s keys: `profile`, `start_home`, `gripper_confirm_s`,
`prepared_at`, `prepared_by`, `files_sha256` and `templates_sha256`. `prepared` in the document reads it again each time and lists
`problems` (`run_json_missing`, `files_changed`, `code_changed`, `profile_changed`, `digest_missing`); only an intact run can be started.
Preparing does not take the machine-wide run lock (a running client's keys must not wait a minute for a check).

### The two stops

`POST .../aeri/live/stop` (phrase `stop live service`) runs `levi live stop` of the product checkout (no `--force`; environment
`PATH`, `HOME`, `USER`, `LANG`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` only, no `LEVI_*`), 120 s, and then **reads the unit again**:
it succeeds only if the service is no longer running, whatever the exit code said; a refusal of the command comes back as it is.
`POST .../aeri/leftovers/stop` (phrase `stop leftover aeri units`) runs `systemctl --user stop` for each `levi-aeri-vlm-*` and
`levi-aeri-policy-*` **service**, never a `levi-aeri-policy-manual-*` unit (a policy server started by hand may be serving a robot), never a
scope, never the evaluation client's or the hosted terminal's. Both hold the machine-wide run lock for their short time, and both
refuse while an AERI run is alive, a hosted evaluation client or session is on, the arm lock is held (the leftovers also while the
GPU lock is held), or any of that cannot be read. A stop that fails is not remembered: the same request may be sent again.

### Writes

All three are a person's (`person_only`, a Bearer credential is refused; an agent never calls them), closed strict bodies, a
`request_id` that is answered again and never acted on twice (also after a restart), and one write at a time (`busy`). Errors have the
shape of `errors.fail`: `aeri_disabled`, `aeri_unconfigured`, `not_product`, `profile_unknown`, `code_pin_mismatch`, `templates_changed`,
`run_name_taken`, `check_failed`, `form_invalid`, `start_home_unsupported`, `confirm_required`, `request_id_invalid`, `request_id_used`,
`busy`, `lock_unusable`, `state_dir_unusable`, `d_active`, `e_active`, `arm_in_use`, `gpu_in_use`, `state_unreadable`,
`live_stop_failed`, `stop_failed`.

## Checks and what is not verified

`uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py` rewrites the two sample files; their test fails and says so when they
differ. `uv run --no-sync python tests/marvel0/mutate_aeri_prep.py` removes one gate at a time in a copy and needs every mutation killed.
One test runs the real `new_run.py` of the workspace next to the port on the same inputs (skipped when the file is absent).
**Not verified on the real machine:** what `levi live stop` does when called this way (no run of it was made); `nvidia-smi`, `systemctl`
and `git` outputs on other machines; `stack_ready` against a running stack. The first real use is staged with the owner present.

---

# marvel0 页面的 AERI 准备卡

先英文，后中文。后端：`levi/marvel0/aeri_prep.py`（`new_run.py` 的带版本移植、代码固定、模板、运行名、只读检查）、
`aeri_ready.py`（被动就绪行与文档）、`aeri_leftovers.py`（上次运行的残留与两个停止动作）、`aeri_api.py`（路由）、`site.py` 与
`control_site.py`（`[aeri]` 两个段）。测试、样本与变异检查的位置见上文。设计记录：
`LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md`（第 7、8、11 节）。托管终端 D（按键、状态、停止）见
`docs/MARVEL0_AERI_RUN.md`，整个步骤见 `docs/MARVEL0_AUTONOMY.md`。

## 这张卡做什么

它是第 5 步（自主系统）里把一次 AERI 运行准备好的部分，发生在任何终端 D 出现**之前**。它不启动任何会动的东西：不启动 D、客户端或策略服务。
它被动读取机器状态，写一个运行文件夹，并提供两个需要键入短语的停止动作。这里没有任何东西是急停；这些门只防误操作，防不了同一账号下的其他程序；
真正的屏障是 Franka Desk 和物理急停。

- **默认关闭。** 控制文件 `lab/marvel0/control.toml` 需要 `[aeri] enabled = true`。没有这一段就是关；这一段写坏了（不是表、有未知键、值不是布尔）也是关，
  并列出错误，且不会连带关掉 `[stack]` 或 `[client]` 的开关。控制文件缺失、读不了或无效，都是关。
- **站点文件的段。** `lab/marvel0/site.toml` 可以有 `[aeri]`（起步文本是 `site.AERI_TEMPLATE`）。没有这一段是“未配置”；写坏了是“无效”并列出错误，
  站点文件其余部分仍然有效。键：`profile`（默认 `harvest-5bc750ca`，即 10 月 18 日在真机上跑过的代码）、`pin_sha`（8 到 40 位十六进制，工作树的 HEAD 必须以它开头）、
  `worktree`、`runs_root`、`templates_root`、`rollout_root`、`openpi_root`、`levi_python`、`levi_bin`，以及可选的 `console_lang`（默认 `zh`）、`uv_offline`（默认真）、
  `tools_root`（默认 `<runs_root>/../tools`）、`local_model`（有它才提供本地模式）。所有路径都必须是绝对、规范化、且只含 `shlex.quote` 不会改动的字符，
  这样页面给终端打印的指令和它托管的是同一段文字。
- **提示词档案。** 每个已知的 AERI 版本一个档案（`aeri_state.PROFILES`：`harvest-5bc750ca`、`bfdee2cf`、`main-297a74c3`）。档案未知，或工作树的四个源文件与档案所依据的不一致
  （`aeri_state.profile_matches`），一律拒绝。版本从不靠猜。

### `GET /api/levi/marvel0/aeri`（`levi.marvel0.aeri.v1`，缓存三秒）

字段见上文。每一行是 `{code, level, text, params}`，级别为 `ok`、`warn`、`refuse`、`unknown`；**`unknown` 与 `refuse` 一样拒绝**；只有有行且没有一行拒绝时，整张卡才是 `ready`。
每一行都来自文件、`/proc`、`nvidia-smi`（只用查询选项）、`systemctl --user show/list-units`、`git`（不取可选锁）或页面其他部分的只读视图；某个来源失败只会让对应行变成 `unknown`，
不会让请求失败。各行的通过条件见上面的表。

机械臂位姿**不读**（那需要连机器人服务端口）：“臂在 home”仍由人勾选，再由 AERI 自己的起始门兜底。`readiness_sha256` 覆盖每行的 code 与 level、档案、AERI 的 HEAD 和已准备运行的摘要，
**不含** `params` 里的数字；托管运行在挑战和开始时各调一次 `aeri_ready.snapshot()`（总是新读），两次必须相等。

### `POST .../aeri/prepare`（短语 `prepare aeri run`）

需要：开关已开、产品 LEVI、有效的站点段、已知档案、固定且干净的代码、未被改动的模板、有效的表单（轮数 1 到 50，步数 1 到 3000，夹爪确认 1.0 到 10.0 秒或空，
任务文本 `[a-z0-9 ,.'-]{3,120}` 且首尾无空格并含字母，`start_home` 只对有开跑回位的档案开放，本地模式只在有 `local_model` 时开放）。硬件行不限制准备，只限制开始。

它就是写成函数的 `new_run.py`（同样的替换、同样的文件、同一工作树下同样的 `execute` 字符串；`job.yaml` 第一行写的是页面而不是脚本）。只写 `<runs_root>/<名>/`
（`job.yaml`、`binding.json`、`check.json`、`RUN.json`，外部模式还有 `queue/`）和页面自己的文件夹 `<工作区>/marvel0/aeri_prep/`。名字是 `aeri-<日期>-ext|loc-rNN`，取在
运行文件夹、`<rollout_root>/.aeri/runs`、`~/.levi-aeri/runs` 和本页记录四处都没用过的第一个编号；用 `mkdir` 创建，中途被占用就跳过。随后用固定工作树运行只读的 `aeri.py check`
（`nice -n 19`、60 秒、环境只有三个变量），要求 `plan_sha256`、`binding_sha256`、`reset_strategy == "subtask_loop"`、`hardware_verified == false`；任何失败都会删掉刚建的文件夹。
`RUN.json` 最后写，并在 `new_run.py` 的键之外增加 `profile`、`start_home`、`gripper_confirm_s`、`prepared_at`、`prepared_by`、`files_sha256`、`templates_sha256`。文档里的 `prepared`
每次都重新读取并列出 `problems`；只有完好的运行才能开始。准备不占用全机运行锁（正在跑的客户端的按键不该为一次检查等一分钟）。

### 两个停止动作

`POST .../aeri/live/stop`（短语 `stop live service`）在产品检出里运行 `levi live stop`（不加 `--force`；环境只有 `PATH`、`HOME`、`USER`、`LANG`、`XDG_RUNTIME_DIR`、`DBUS_SESSION_BUS_ADDRESS`，没有任何 `LEVI_*`），
超时 120 秒，然后**重新读取单元状态**：不管退出码是什么，只有服务确实不再运行才算成功；命令的拒绝原样带回。
`POST .../aeri/leftovers/stop`（短语 `stop leftover aeri units`）对每个 `levi-aeri-vlm-*` 和 `levi-aeri-policy-*` **服务**执行 `systemctl --user stop`，**绝不**碰
`levi-aeri-policy-manual-*`（手工起的策略服务可能正在服务机器人）、绝不碰 scope、绝不碰评测客户端或托管终端的单元。两者都在短时间内持全机运行锁，
并在以下任一情况拒绝：AERI 运行存活、托管评测客户端或会话在用、臂锁被持有（残留清理还要求 GPU 锁空闲），或这些读不出来。失败的停止不会被记成答案，可以用同一请求重发。

### 写入规则

三个写路由都是人的操作（`person_only`，Bearer 凭据一律拒绝；agent 从不调用）、闭合的严格请求体、`request_id` 重放（同一请求只回答不重做，重启后也一样）、同一时刻只处理一个写请求（`busy`）。
错误码见上文英文列表。

## 检查与尚未验证

`uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py` 重写两份样本，不一致时对应测试失败并写明这条命令。`uv run --no-sync python tests/marvel0/mutate_aeri_prep.py`
在副本里每次拿掉一道门，要求每个变异都被测试抓住。有一个测试把工作区里真正的 `new_run.py` 与移植版放在同样输入上对比（文件不存在时跳过）。
**未在真机上验证：** 这样调用 `levi live stop` 的实际效果（没有真跑过）；其他机器上 `nvidia-smi`、`systemctl`、`git` 的输出；`stack_ready` 在栈运行时的表现。首次真机使用在用户在场时分阶段进行。
