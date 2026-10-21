# The AERI preparation card of the marvel0 page

English first, Chinese after the line below. Backend: `levi/marvel0/aeri_prep.py` (the versioned port of `new_run.py`, the code
pin, the templates, run names, the check), `aeri_ready.py` (the passive readiness rows and the document), `aeri_leftovers.py` (what
an earlier run leaves behind, the two stops), `aeri_api.py` (routes), `site.py` and `control_site.py` (the `[aeri]` sections),
`aeri_activity.py` (how the rest of the product sees a hosted terminal D, below).
Tests: `tests/marvel0/test_marvel0_aeri_{site,prep,ready,leftovers,route,ready_sample,activity}.py` on the made-up machine
`tests/marvel0/aeri_world.py` (the last on real `hostchild.py` and fake terminal D under real ptys); samples
`tests/marvel0/regen_aeri_ready_sample.py` (also `fixtures/aeri_stack_up_rows.json`); mutation sweeps
`tests/marvel0/mutate_aeri_prep.py` and `tests/marvel0/mutate_aeri_wiring.py`. Design record:
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

### `GET /api/levi/marvel0/aeri` (`levi.marvel0.aeri.v1`, cached one second)

`enabled`, `control`, `site`, `profile`, `profiles`, `limits`, `readiness`, `ready`, `readiness_sha256`, `prepared`, `live_service`,
`leftovers`, `confirm`, `notices`. Every row is `{code, level, text, params}`, level `ok`, `warn`, `refuse` or `unknown`;
**`unknown` refuses like `refuse`**; the card is `ready` only with rows and none refusing. Every row is read from a file, `/proc`,
`nvidia-smi` (query options), `systemctl --user show/list-units`, `git` (no optional lock) or a read-only view of another part of
the page. A source that fails makes its row `unknown`; it never fails the request.

**What a read costs.** A full read is one batch (`git` three times, `nvidia-smi`, `systemctl` twice, the stack and deploy views; every
source asked once) and the page asks every three seconds from every open tab while the arm controller may be running, so the read is
**light** (no process started, no folder scanned) whenever the rest cannot matter: with `[aeri] enabled = false` the document is the
switch and the site's facts (`readiness: []`, `ready: false`, no `prepared`, `live_service` and `leftovers` null, the profile without
`head` and `clean`); and while this page's own terminal D or evaluation client is on, one row says why (`no_other_aeri` or `no_e_run`,
`refuse`) and nothing else is asked, since the arm controller is up then and no start is possible. A probe of the hosted run that cannot
answer is not a light read (a full one; its rows are `unknown`). The two stops use the same light read to refuse (`d_active`,
`e_active`). The cache time (`aeri_ready.CACHE_S`) is one second, well under the page's three-second poll, so two tabs share one batch and
every poll sees what is now. **`warn` is for `aeri_ready.WARN_OK` only** (`stack_ready`, which can only be `warn` when the stack is up:
the gripper stack and the controller cannot be observed): the start gate of the hosted run needs `snapshot()["ready"] is True` and refuses
a `warn` of any other row; `tests/marvel0/fixtures/aeri_stack_up_rows.json` is `snapshot()` on the made-up machine with the stack up,
not read and down, built from the real rows, to test that gate on.

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

`POST .../aeri/live/stop` (phrase `stop live service`) runs `systemctl --user stop` for the unit of the site file's `[live] unit` (only a
`levi-live*.service`, never `levi-product`; environment `PATH`, `HOME`, `USER`, `LANG`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`
only, no `LEVI_*`), 210 s, and then **reads the unit again**: it succeeds only if the service is no longer running, whatever the exit
code said; the words of the command come back as they are. Why a unit and not `levi live stop` (decision O-6, 22 Oct): the product's own
live-service stop (`levi/live/service_control.py`) stops a service its unit runs this way; the unit's `ExecStop` is `levi live stop` with
the live workspace spelt out, and `TimeoutStopSec=180` bounds the wait and ends what is left of its cgroup; the card reads the unit's state
before and after, so the answer is the state observed. `levi live stop` without `--workspace` finds its service by the workspace of the last
one started with the same home: for another it says "not running" and exits 0 with the service still up. `levi_bin` of the site section is
therefore not used by the stop any more (it stays a required key so that existing site files stay valid).
`POST .../aeri/leftovers/stop` (phrase `stop leftover aeri units`) runs `systemctl --user stop` for each `levi-aeri-vlm-*` and
`levi-aeri-policy-*` **service**, never a `levi-aeri-policy-manual-*` unit (a policy server started by hand may be serving a robot), never a
scope, never the evaluation client's or the hosted terminal's. Both hold the machine-wide run lock for their short time, and both
refuse while an AERI run is alive, a hosted evaluation client or session is on, the arm lock is held (the leftovers also while the
GPU lock is held), or any of that cannot be read. A stop that fails is not remembered: the same request may be sent again.

### How the rest of the product sees a hosted terminal D (`aeri_activity.py`)

- **`levi stop`, the core's stop route and the restart by the workspace manual's R6** read `levi.activity.running_jobs()`. It lists
  `marvel0-aeri` (id = the run id, pid = the launcher's, `hosted: true`) from the run record on disk (`<workspace state>/marvel0/aeri/record.json`)
  and the identity (start time and boot) of the launcher's pid, **read only and never through the page's supervisor**: its take-over sends a SIGINT to
  a D that lost its keyboard, which is right in a restarted service and wrong in a `levi stop` process that merely asks. A record that cannot be read
  is listed (`record-unreadable`), not taken for "no run". `levi stop` therefore refuses while a D runs (`--wait` waits for the person to end it), as it does
  for a pool export. The setup guard shows it among "product jobs" while the arm controller is on (information only, nothing gates on it).
- **The service's start** adopts a D that outlived the previous service (`aeri_run_api.adopt_on_start`: one Ctrl+C, then watched). **Its shutdown** (`levi stop
  --force`, `systemctl stop levi-product`, a restart) first sends a hosted D the Stop a person presses (`Supervisor.stop`, once; a D that was stopped already
  is only waited for) and waits up to 130 s for its 7/7 wrap-up; never a bare kill. What is left after the wait is the launcher's hang-up chain (SIGINT, 3 s,
  SIGKILL) when the pty closes with the process. **`levi-product.service` has `TimeoutStopSec=15`**: systemd ends the service after that, so the unit needs
  at least 150 s for the orderly end to be able to finish (a change of the unit file in the lab ledger, not made here).
- **The evaluation client's start check** (`no_aeri`) refuses from the click that starts a hosted D (D takes the arm lock only after its phrase); a probe that
  fails is `unknown`, which refuses. **The policy-server deploy** is refused with `aeri_run_live` (the hosted run's id) while a hosted D is on.
- **The start gate's mechanical check** (`aeri_activity.hosted_visible_to_activity`): before a launch, the real `running_jobs` must list a canary record of this
  process in a throw-away workspace and the record folder must be one it would read; after the launch bookkeeping, `running_jobs` must list this run id. A start
  whose D `levi stop` could not see is refused (or, after the launch, stopped).

### Writes

All three are a person's (`person_only`, a Bearer credential is refused; an agent never calls them), closed strict bodies, a
`request_id` that is answered again and never acted on twice (also after a restart), and one write at a time (`busy`). Errors have the
shape of `errors.fail`: `aeri_disabled`, `aeri_unconfigured`, `not_product`, `profile_unknown`, `code_pin_mismatch`, `templates_changed`,
`run_name_taken`, `check_failed`, `form_invalid`, `start_home_unsupported`, `confirm_required`, `request_id_invalid`, `request_id_used`,
`busy`, `lock_unusable`, `state_dir_unusable`, `d_active`, `e_active`, `arm_in_use`, `gpu_in_use`, `state_unreadable`,
`live_stop_failed`, `stop_failed`.

## Checks and what is not verified

`uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py` rewrites the sample files; their test fails and says so when they
differ. `uv run --no-sync python tests/marvel0/mutate_aeri_prep.py` and `.../mutate_aeri_wiring.py` remove one gate at a time in a copy and need every mutation killed.
One test runs the real `new_run.py` of the workspace next to the port on the same inputs (skipped when the file is absent).
**Not verified on the real machine:** `systemctl --user stop levi-live.service` called from the product (no run of it was made); `levi stop` and the lifespan
shutdown against a real hosted D (the tests use the fake D under real ptys); the 130 s orderly end under the unit's `TimeoutStopSec`; `nvidia-smi`, `systemctl`
and `git` outputs on other machines; `stack_ready` against a running stack. The first real use is staged with the owner present.

---

# marvel0 页面的 AERI 准备卡

先英文，后中文。后端：`levi/marvel0/aeri_prep.py`（`new_run.py` 的带版本移植、代码固定、模板、运行名、只读检查）、
`aeri_ready.py`（被动就绪行与文档）、`aeri_leftovers.py`（上次运行的残留与两个停止动作）、`aeri_api.py`（路由）、`site.py` 与
`control_site.py`（`[aeri]` 两个段）、`aeri_activity.py`（产品其余部分如何看见托管的终端 D，见下）。测试、样本与变异检查的位置见上文（另有
`test_marvel0_aeri_activity.py`、`fixtures/aeri_stack_up_rows.json`、`mutate_aeri_wiring.py`）。设计记录：
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

### `GET /api/levi/marvel0/aeri`（`levi.marvel0.aeri.v1`，缓存一秒）

字段见上文。每一行是 `{code, level, text, params}`，级别为 `ok`、`warn`、`refuse`、`unknown`；**`unknown` 与 `refuse` 一样拒绝**；只有有行且没有一行拒绝时，整张卡才是 `ready`。
每一行都来自文件、`/proc`、`nvidia-smi`（只用查询选项）、`systemctl --user show/list-units`、`git`（不取可选锁）或页面其他部分的只读视图；某个来源失败只会让对应行变成 `unknown`，
不会让请求失败。各行的通过条件见上面的表。

**一次读取的代价。** 完整读取是一批进程（`git` 三次、`nvidia-smi`、`systemctl` 两次，加栈与部署两个视图；每个来源只问一次），而页面在每个打开的标签页里每三秒问一次，
机械臂控制器可能正在运行。所以凡是其余信息无关紧要时，读取都是**轻量**的（不起进程、不扫文件夹）：`[aeri] enabled = false` 时文档只有开关和站点的事实
（`readiness: []`、`ready: false`、没有 `prepared`、`live_service` 与 `leftovers` 为 null、档案里没有 `head` 和 `clean`）；本页自己的终端 D 或评测客户端在用时，只给出一行说明原因
（`no_other_aeri` 或 `no_e_run`，`refuse`），不再问别的，因为那时控制器已经在运行、也不可能开始。托管运行的探针答不出来时不算轻量读取（走完整读取，各行为 `unknown`）。
两个停止动作用同一个轻量读取来拒绝（`d_active`、`e_active`）。缓存时间（`aeri_ready.CACHE_S`）是一秒，远小于页面三秒的轮询，两个标签页共用一批，
每次轮询都看到当下的状态。**`warn` 只属于 `aeri_ready.WARN_OK`**（`stack_ready`，栈起来时它只能是 `warn`：夹爪栈和控制器无法观察）：托管运行的开始门要求
`snapshot()["ready"] is True`，并拒绝其他任何行上的 `warn`；`tests/marvel0/fixtures/aeri_stack_up_rows.json` 是在虚构机器上栈已起、未读到、已停三种情况下的 `snapshot()`，
由真实的行生成，用来测试这道门。

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

`POST .../aeri/live/stop`（短语 `stop live service`）对站点文件 `[live] unit` 的单元执行 `systemctl --user stop`（只接受 `levi-live*.service`，绝不碰 `levi-product`；
环境只有 `PATH`、`HOME`、`USER`、`LANG`、`XDG_RUNTIME_DIR`、`DBUS_SESSION_BUS_ADDRESS`，没有任何 `LEVI_*`），超时 210 秒，然后**重新读取单元状态**：不管退出码是什么，
只有服务确实不再运行才算成功；命令的输出原样带回。为什么用单元而不是 `levi live stop`（决定 O-6，10 月 22 日）：产品自己的实时服务停止（`levi/live/service_control.py`）对单元运行的服务就是这样停的；
单元的 `ExecStop` 就是写明了实时工作区的 `levi live stop`，`TimeoutStopSec=180` 限定等待时间并清掉 cgroup 里剩下的进程；这张卡在前后都读单元状态，所以答案就是观察到的状态。
不带 `--workspace` 的 `levi live stop` 按“用同一个 home 最近启动的那个服务的工作区”找服务：对另一个工作区的服务它会说“没有在运行”并以 0 退出，服务仍然开着。
因此站点段的 `levi_bin` 不再被停止动作使用（仍是必填键，免得已有的站点文件失效）。
`POST .../aeri/leftovers/stop`（短语 `stop leftover aeri units`）对每个 `levi-aeri-vlm-*` 和 `levi-aeri-policy-*` **服务**执行 `systemctl --user stop`，**绝不**碰
`levi-aeri-policy-manual-*`（手工起的策略服务可能正在服务机器人）、绝不碰 scope、绝不碰评测客户端或托管终端的单元。两者都在短时间内持全机运行锁，
并在以下任一情况拒绝：AERI 运行存活、托管评测客户端或会话在用、臂锁被持有（残留清理还要求 GPU 锁空闲），或这些读不出来。失败的停止不会被记成答案，可以用同一请求重发。

### 产品其余部分如何看见托管的终端 D（`aeri_activity.py`）

- **`levi stop`、核心的停止路由和工作区手册 R6 的重启**都读 `levi.activity.running_jobs()`。它从磁盘上的运行记录（`<工作区状态>/marvel0/aeri/record.json`）和启动器 pid 的身份（启动时刻与开机标识）
  列出 `marvel0-aeri`（id 是运行号，pid 是启动器的，`hosted: true`），**只读，绝不经过页面的 supervisor**：它的接管会给失去键盘的 D 发 SIGINT，这在重启后的服务里是对的，
  在只是来问一句的 `levi stop` 进程里是错的。读不出来的记录也会被列出（`record-unreadable`），而不是当作“没有运行”。所以 D 在运行时 `levi stop` 会拒绝（`--wait` 等人结束它），
  和对池导出一样。启动自检的“产品作业”里，在机械臂控制器开着时也会显示它（只是信息，没有任何东西依赖它）。
- **服务启动**时接管上一个服务留下的 D（`aeri_run_api.adopt_on_start`：一次 Ctrl+C，然后只观察）。**服务关停**（`levi stop --force`、`systemctl stop levi-product`、重启）时，
  先给托管的 D 发人按下的那个停止（`Supervisor.stop`，只发一次；已经停止过的 D 只是等待），最多等 130 秒让它跑完 7/7 收尾；绝不直接杀。等待结束后剩下的由启动器的挂断链处理
  （进程退出、pty 关闭时：SIGINT，3 秒后 SIGKILL）。**`levi-product.service` 的 `TimeoutStopSec=15`**：systemd 过了这个时间就结束服务，所以这个单元至少要改到 150 秒，
  有序关停才有机会跑完（单元文件在实验台账里，这里没有改）。
- **评测客户端的开始检查**（`no_aeri`）从托管 D 的开始点击起就拒绝（D 要等键入短语后才持臂锁）；探针答不出来是 `unknown`，同样拒绝。**策略服务部署**在托管 D 运行时以 `aeri_run_live`（运行号是托管运行的）拒绝。
- **开始门的机械检查**（`aeri_activity.hosted_visible_to_activity`）：启动之前，真正的 `running_jobs` 必须能在一个临时工作区里列出一份指向本进程的试验记录，且记录文件夹必须是它会去读的那种；
  启动登记之后，`running_jobs` 必须列出这个运行号。D 若是 `levi stop` 看不见的，就不开始（启动之后发现，则停止它）。

### 写入规则

三个写路由都是人的操作（`person_only`，Bearer 凭据一律拒绝；agent 从不调用）、闭合的严格请求体、`request_id` 重放（同一请求只回答不重做，重启后也一样）、同一时刻只处理一个写请求（`busy`）。
错误码见上文英文列表。

## 检查与尚未验证

`uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py` 重写样本文件，不一致时对应测试失败并写明这条命令。`uv run --no-sync python tests/marvel0/mutate_aeri_prep.py` 和 `.../mutate_aeri_wiring.py`
在副本里每次拿掉一道门，要求每个变异都被测试抓住。有一个测试把工作区里真正的 `new_run.py` 与移植版放在同样输入上对比（文件不存在时跳过）。
**未在真机上验证：** 产品里调用 `systemctl --user stop levi-live.service` 的实际效果（没有真跑过）；`levi stop` 与服务关停面对真正托管的 D 的表现（测试用的是真 pty 下的假 D）；
单元 `TimeoutStopSec` 之下 130 秒有序关停；其他机器上 `nvidia-smi`、`systemctl`、`git` 的输出；`stack_ready` 在栈运行时的表现。首次真机使用在用户在场时分阶段进行。
