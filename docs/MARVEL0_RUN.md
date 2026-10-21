# The evaluation run buttons of the marvel_workstation page

English first, Chinese after the line below. Backend: `levi/marvel0/run.py` (state rules, keys, supervisor),
`run_api.py` (routes), `hostchild.py` (the launcher). Tests: `tests/marvel0/test_marvel0_run*.py`, the fake client
`tests/marvel0/fake_client.py` and the drift check `scripts/check_client_prompts.py`. Design record:
`LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md` (section 2.10-2.12, 5.3) and the client-hosting
review next to it, whose adversarial part wins where the pieces differ.

## What the buttons do

The page can start the evaluation client (terminal E) that a person starts in a terminal today, press the keys that
person presses, freeze it and end it. It hosts **`--levi-mode off`, the manual start gate, and nothing else**: not the
unattended mode, not the dual-label mode, no automatic next episode, no automatic label, no step-by-step. The command is
the one the command card renders (`command.render` is the only producer of the client's argv); the page adds
`--no-display-images` and nothing else.

- **Start** (`POST /run/start`). The page asks for a token first (`POST /run/challenge`, purpose `start`, with the form):
  the answer carries every start check and the command's warnings; a token is only issued when all checks pass. The start
  then needs the token, the phrase `start client <rollout group>` (`confirm`: the page's confirm window sends it for the
  person, with the checklist ticked there; a script sends it itself), the checklist (`attest`: scene reset, area clear, a
  hand on the physical e-stop) and a fresh `request_id`. The client runs through `hostchild.py` (a launcher that gives it
  a keyboard, a pty) inside its own `systemd-run --user --scope` (`env -i` with the allowlist in front of the launcher; `systemd-run` itself gets
  only the user bus variables), so a restart of the product does not signal it. The core keeps the pty's master. Output goes to a private log (0600) in the workspace.
- **Keys.** Only these five bytes are ever written to the client's keyboard: Enter, `s`, `f`, `d`, `q`. Which of them
  the page offers depends on the state the client is in, read from its session file, its log and the pty:

  | State | The client | Keys offered |
  | --- | --- | --- |
  | `starting` | connecting, warming up the cameras | none (hold, end) |
  | `at_gate` | waits at the start gate | **Enter** (the arm goes home without any limit check, then the policy runs; once per gate), **q** (leave; no motion) |
  | `homing` | the arm goes home | none (hold) |
  | `running` | the policy runs | **s** success, **f** failure (one label per episode), **q** "exit and go home" (blind; its own phrase `exit and go home`, sent by its confirm window, which has tick boxes: hand on the e-stop, area clear) |
  | `awaiting_label` | the step limit is reached, the client waits | **s**, **f**, **d** discard (one label) |
  | `finishing` | saving the episode, closing | none (hold) |
  | `held`, `lost` | frozen, or not supervisable | none (end; hold in `lost`) |
  | `done`, `failed` | over | none (start a new run) |

  **There is no `s`, `p`, `i` or digit at the start gate.** There `s` and `p` choose *another task and start it*
  (`TASK_KEYS`): on 2026-10-20 a stray `s` ran the screws task twice. A label is sent once per episode: a second click while
  the client still saves the episode would wait in the keyboard and become the gate's `s`; so every key after the first is
  refused until the episode number changes. Enter is sent once per gate and never again; `q` is refused behind an Enter
  that has not been taken (the running loop reads that `q` as "quit"). A key is a request with its own token (30 s,
  bound to the run, the session, the episode number and the state; used once) and a `request_id`; one byte, one request at
  a time; the byte is written only after "sent" was synced, so a crash loses a key and never doubles it.
- **Hold** (`POST /run/hold`). SIGSTOP to the client's whole process group: no new target is sent, the arm stays at the
  last one. One click, no phrase, no token, always available while a client exists, idempotent, never waits for a lock.
  **Nothing resumes it**: afterwards the only action is end. (A resumed step would send one stale action, and a hold at
  the step-limit prompt would turn into a blind homing.)
- **End** (`POST /run/end`, phrase `end client run`, sent by the page's confirm window). `q` at the gate (a key); for a held, lost or still starting client
  SIGKILL of its group. Never SIGTERM + SIGCONT. A killed episode leaves its folder without `stopped_at` (the client's
  off mode does not tidy it): the page says so after an end; look at it before using the data.
- **Holds on its own** (it never sends anything else on its own): the health monitor shows red on **two distinct writes**
  while the arm may move; the session file stops updating for 10 s; the session file shows another task (a key reached the
  gate); a second connection is accepted by the policy server; a client is found alive after the service restarted
  (it has no keyboard any more). A monitor that is not fresh and green only **warns and blocks the next Enter**; red also
  blocks every key that makes the arm move. A client that does not reach the gate within 180 s is ended (nothing has
  moved before the gate).
- **Start checks (all passive).** Hosted runs enabled in the control file (`[client] enabled`, `LEVI_MARVEL0_CONTROL`, off
  when missing); the product LEVI; no client running; the policy server **started by this page** is ready, for the form's
  model and RTC setting; nobody is connected to it; no evaluation session is active (an ended one, or the one left by this
  page's own killed client, is not); the arm lock is free (the page then holds it for the whole run: a deploy and an AERI
  run refuse meanwhile); the health monitor is fresh and green; no automatic evaluation run is alive; the rollout folder is
  writable; and the z floor: `--min-z` below the client's floor for the **screws (0.075) and plates (0.05325) tasks is
  refused** (the client lets an explicit `--min-z` beat its task table, risk RIG-08); other tasks only get the warning.
  A robot server that is not listening is a hint, not a refusal.
- **Records.** `<workspace>/marvel0/run/`: `record.json`, `requests/` (answers kept for replay), `history.jsonl` (time,
  person, key name, request id, episode number; no transcript, no path) and `logs/` (the client's output, 0600). The page
  shows the last lines of the log without paths (`GET /run/log`).

## What the gates are worth, and what nothing here is

**The gates stop mistakes. They do not stop another program of the same account**: anything that runs as this user can send
the same requests (ask for a token, then use it), forge the page's headers, write to the client's keyboard or signal its
process. The tokens, phrases and checklist are clicks that are recorded, not proofs. **Nothing is typed any more**: every
start and stop of the page (the client, the arm stack, the policy server, the deploy) is one confirm window with its hazard
sentences and tick boxes, and the page sends the phrase itself; the backend still checks it. The two overrides that cut
something that is in use stay typed fields: stopping the robot server C with a client connected, and stopping the policy
server with a client connected. The real barriers are Franka Desk and
the **physical emergency stop; keep a hand on it**. **Nothing on this page is an emergency stop**: hold freezes the client,
it does not stop the arm, which keeps the last target it was sent; success, failure, discard, exit and Enter send the arm
home without any limit check. Agent sessions never call the `/run` or `/stack` write routes (a rule, not a mechanism, on a
shared account). Writes act only inside the product LEVI (`levi-product.service` from the main checkout); another instance
shows the run and refuses (`not_product`). If the core ends, the pty's master closes and the kernel hangs the client up.

Not hosted, by decision: the client in the unattended or dual mode (dual needs `levi-live`, which is not running), the next
episode starting by itself, labels by anything but the person, the step-by-step mode (hold is its substitute) and
anything typed into the client. A browser that closes does not stop the client: the current episode runs to its step
limit and waits; hold and end stay available.

## Wiring, and what is still for the owners of other files

Wired: the routes are in `levi/marvel0/api.py` (and listed in `tests/marvel0/test_marvel0_routes_mounted.py`, which also checks
that every write refuses a Bearer credential); `run_api.py` registers `run_api.run_active` with
`stack_act.register_run_probe` when it is imported, so the arm stack's start and stop refuse while a client runs, and a
client that outlived a restart is taken over (frozen) by that very question, before anyone opens the run card.

Not wired, for whoever owns those files: `run_api.active_job()` gives the running client as a job row (`kind marvel0-client`)
for `levi stop` (`levi/activity.py`) and for the policy deploy's stop (`levi/marvel0/deploy.py` should refuse while a client
runs and not honour its override phrase); `run_api.adopt_on_start()` could be called from the service's lifespan
(`levi/service.py`, next to `stack_api.start_auto()`); nothing calls either today.

## What was run, and what was not

Run on the development machine, with the arm off, never against a robot port, never the real client: the unit tests of the
state rules (every state, text the client words differently, a client that is gone), what is offered, the token, the
environment and command; the supervisor on a scripted client (start and every refusal, keys and the bytes that reach the
keyboard, hold, end, the watcher's own holds, take-over after a restart, replay); the launcher and the fake client under a
real pty (a whole run, a stray `s`, two label clicks during a slow save, a hold and an end, a client whose core died);
mutation checks (the code broken on purpose, the named tests fail); and the drift check against the client's source.
The five test files hold 260 tests (96 + 81 + 25 + 50 + 8, the 25 with real processes take about 25 s); the mutation sweep
`tests/marvel0/mutate_run.py` (it refuses to run while a robot process is alive; `--check` only validates the list) breaks
the code 98 ways, in a scratch copy, one test file per breakage: the first sweep let 7
through (a pid check after a restart, the launcher's own kill of the group, the exit record's mode, the token list after a
run, the same monitor write counted twice, the arm lock row, the z floor row), each got a test and is now caught, so all 98 are;
`scripts/check_client_prompts.py` found all 49 strings in place on 2026-10-21.
Regenerate what the front end reads with `uv run --no-sync python tests/marvel0/regen_run_sample.py`
(`marvel0-run.samples.json`: `GET /run` in every state; `marvel0-run.answers.json`: the other answers and the refusals).

**Not verified on the real machine:** `systemd-run --user --scope` itself (that the pid is the launcher's, that the group
survives a restart of the product, the scope's stop behaviour); the real client under the launcher (`uv` offline with the
environment allowlist, the `uv` chain's signal handling, SIGHUP reaching `python`, `--no-display-images` without a display);
SIGSTOP for minutes (cameras, the policy server's websocket, the controller holding the last target); the real timing of
the saving, homing and the 1 s session heartbeat (the page's thresholds are guesses until measured); the real monitor's
write rate for the two-writes rule; the real client's words (checked only against the source files:
`python3 scripts/check_client_prompts.py` before each day's first run); how a hung-up pty looks to the real client
(Linux answers EIO to a read; the fake takes the worse reading and goes on). The first real use goes in the order of the
review: start to the gate and `q` (no motion), one episode with Enter and `s`, `f`, hold and end, `d` at the step-limit prompt,
with the owner at the arm and a hand on the emergency stop.

---

# 评测运行按键

上文是英文，下文是中文。后端：`levi/marvel0/run.py`（状态规则、按键、监管者）、`run_api.py`（路由）、`hostchild.py`（启动器）。
测试：`tests/marvel0/test_marvel0_run*.py`，假客户端 `tests/marvel0/fake_client.py`，漂移检查 `scripts/check_client_prompts.py`。
设计记录：`LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md`（2.10-2.12 与 5.3）以及旁边的客户端托管复审，
两者不一致时以复审中的对抗部分为准。

## 按键做什么

页面可以启动人今天在终端里启动的评测客户端（终端 E），按下人会按的键，冻结它，结束它。它托管的**只有 `--levi-mode off`
和手动开始闸门**：不托管无人值守模式，不托管双标签模式，没有自动开始下一个片段，没有自动标签，没有逐步确认。命令就是命令卡片渲染出来的那一条
（客户端的 argv 只由 `command.render` 产生）；页面只多加 `--no-display-images`，别的都不加。

- **开始**（`POST /run/start`）。页面先要一个令牌（`POST /run/challenge`，用途 `start`，带上表单）：回答里有全部开始检查和命令的警告；
  只有全部检查通过才会发令牌。开始还需要：令牌、短语 `start client <rollout group>`（`confirm`：由页面的确认弹窗替人发送，清单在弹窗里勾选；脚本自己发）、清单（`attest`：场景已复位、区域已清空、手在物理急停上）
  和新的 `request_id`。客户端通过 `hostchild.py`（给它一个键盘，即伪终端的启动器）运行在它自己的 `systemd-run --user --scope` 里（启动器前面是带白名单的 `env -i`；`systemd-run` 自己只拿到用户总线变量），所以产品重启不会
  给它发信号。核心保留伪终端的主端。输出写入工作区里的私有日志（0600）。
- **按键。** 写给客户端键盘的字节只有这五个：回车、`s`、`f`、`d`、`q`。页面提供哪些，取决于客户端所处的状态，状态由它的会话文件、日志和
  伪终端读出：

  | 状态 | 客户端 | 提供的键 |
  | --- | --- | --- |
  | `starting` | 正在连接、预热相机 | 无（保持、结束） |
  | `at_gate` | 停在开始闸门 | **回车**（机械臂不经任何限位检查地回位，然后运行策略；每个闸门一次）、**q**（离开；不动） |
  | `homing` | 机械臂回位 | 无（保持） |
  | `running` | 策略在运行 | **s** 成功、**f** 失败（每个片段一个标签）、**q** “退出并回位”（盲走；有自己的短语 `exit and go home`，由它的确认弹窗发送，弹窗里有勾选项：手在急停上、区域已清空） |
  | `awaiting_label` | 到达步数上限，客户端在等标签 | **s**、**f**、**d** 作废（一个标签） |
  | `finishing` | 正在保存片段、正在关闭 | 无（保持） |
  | `held`、`lost` | 已冻结，或无法监管 | 无（结束；`lost` 时还可保持） |
  | `done`、`failed` | 已结束 | 无（开始新的一次运行） |

  **开始闸门处没有 `s`、`p`、`i` 和数字。** 在那里 `s` 和 `p` 的意思是“选另一个任务并直接开始”（`TASK_KEYS`）：2026-10-20 一次误按的 `s` 把螺丝任务
  跑了两次。标签每个片段只发一次：客户端还在保存片段时的第二次点击会留在键盘缓冲里，变成下一个闸门的 `s`；所以第一个键之后的所有键都被拒绝，
  直到片段号变化。回车每个闸门只发一次，不再发第二次；回车还没被客户端取走时，`q` 也被拒绝（运行循环会把那个 `q` 读成“退出”）。每个键都是一次带自己令牌的请求
  （30 秒，绑定运行、会话、片段号和状态，只能用一次）加 `request_id`；一次一个字节、一次一个请求；字节在“已发送”记录落盘之后才写入，所以崩溃只会丢掉
  一个键，不会重复一个键。
- **保持**（`POST /run/hold`）。对客户端的整个进程组发 SIGSTOP：不再发出新目标，机械臂停在最后一个目标上。一次点击，不要短语，不要令牌，只要客户端存在就始终可用，
  可重复，从不等锁。**没有任何东西会让它恢复**：之后唯一的动作是结束。（恢复会多发一个过期动作，在步数上限提示处的保持恢复后还会变成盲走回位。）
- **结束**（`POST /run/end`，短语 `end client run`，由页面的确认弹窗发送）。闸门处是 `q`（一个键）；已冻结、失联或仍在启动的客户端则对其进程组发 SIGKILL。绝不用 SIGTERM 加
  SIGCONT。被杀死的片段留下没有 `stopped_at` 的文件夹（客户端的 off 模式不会整理它）：结束后页面会提示；用这份数据之前先看一眼。
- **自动保持**（页面自己不会发别的东西）：健康监视器在**两次不同的写入**里都显示红灯，且机械臂可能在动；会话文件 10 秒不更新；会话文件显示换了任务（有键到了
  闸门）；策略服务器接受了第二个连接；服务重启后发现客户端还活着（它已经没有键盘）。监视器不新鲜或不绿时**只警告并拦住下一次回车**；红灯还会拦住所有会让机械臂动的键。
  180 秒内没到闸门的客户端会被结束（闸门之前什么都没有动过）。
- **开始检查（全部被动）。** 控制文件里打开了托管（`[client] enabled`，`LEVI_MARVEL0_CONTROL`，缺失即关闭）；是产品 LEVI；没有客户端在运行；**由本页启动的**策略服务器
  已就绪，且模型和 RTC 设置与表单一致；没有人连在它上面；没有活动的评测会话（已结束的、或本页自己被杀死的客户端留下的那一个不算）；手臂锁空闲（页面随后在整个运行期间持有它：
  部署和 AERI 运行因此被拒绝）；健康监视器新鲜且为绿；没有自动评测运行；采集目录可写；以及 z 下限：**螺丝（0.075）和盘子（0.05325）任务的 `--min-z` 低于客户端的下限会被拒绝**
  （客户端让显式的 `--min-z` 压过它的任务表，风险 RIG-08）；其他任务只给警告。机器人服务器没有在监听只是提示，不拒绝。
- **记录。** `<工作区>/marvel0/run/`：`record.json`、`requests/`（保留回答以供重放）、`history.jsonl`（时间、人、按键名、请求号、片段号；没有记录文本，没有路径）和
  `logs/`（客户端的输出，0600）。页面显示日志的末尾几行，路径已去掉（`GET /run/log`）。

## 这些门的分量，以及这里什么都不是

**这些门只防误操作，防不了同一账号下的其他程序**：任何以这个用户运行的程序都能发出同样的请求（先要令牌再用令牌）、伪造页面的请求头、直接写客户端的键盘或给它的进程发信号。
令牌、短语和清单是被记录下来的点击，不是证明。**现在不需要再输入任何东西**：页面上每一次启动和停止（客户端、机械臂栈、策略服务器、部署）都是一个带危险提示和勾选项的确认弹窗，
短语由页面自己发送，后端仍然核对它。仍然要手输的只有两个会切断正在使用的东西的覆盖：C 上有客户端连接时停止机器人服务器，以及有客户端连接时停止策略服务器。真正的屏障是 Franka Desk 和**物理急停；手要放在上面**。**页面上没有任何东西是急停**：保持只冻结客户端，不会让机械臂停下，
机械臂保持最后收到的目标；成功、失败、作废、退出和回车都会让机械臂不受任何限位检查地回位。agent 会话从不调用 `/run` 和 `/stack` 的写路由（在共享账号上这是规则，不是机制）。
写操作只在产品 LEVI（主检出里的 `levi-product.service`）中生效；其他实例只显示运行状态并拒绝写操作（`not_product`）。核心结束时伪终端的主端关闭，内核会让客户端挂断。

按决定不托管：无人值守或双标签模式的客户端（双标签需要 `levi-live`，它现在没有运行）、自己开始的下一个片段、由人以外的任何东西打的标签、逐步确认模式（保持是它的替代）、
任何输入到客户端里的文字。浏览器关闭不会让客户端停下：当前片段会跑到步数上限然后等待；保持和结束仍然可用。

## 已经接上的，以及仍交给其他文件所有者的

已接上：路由在 `levi/marvel0/api.py` 里（并列在 `tests/marvel0/test_marvel0_routes_mounted.py` 中，该测试还检查每个写路由都拒绝 Bearer 凭据）；
`run_api.py` 在被导入时把 `run_api.run_active` 注册给 `stack_act.register_run_probe`，所以有客户端在运行时，机械臂栈的启动和停止会被拒绝；
服务重启后仍活着的客户端，也会在这个询问发生时被接管（冻结），不必等有人打开运行卡片。

没有接上，交给拥有那些文件的人：`run_api.active_job()` 把正在运行的客户端给成一行作业（`kind marvel0-client`），供 `levi stop`（`levi/activity.py`）和策略部署的停止使用
（`levi/marvel0/deploy.py` 应在有客户端运行时拒绝停止，且不再接受覆盖短语）；`run_api.adopt_on_start()` 可以在服务的 lifespan 里调用
（`levi/service.py`，紧挨 `stack_api.start_auto()`）；目前没有任何地方调用这两个函数。

## 已经运行过的，和没有运行过的

在开发机上、机械臂关闭时运行，从未连接任何机器人端口，从未运行真正的客户端：状态规则的单元测试（每个状态、客户端换了说法的文字、已经消失的客户端）、提供哪些动作、令牌、环境和命令；
脚本化客户端上的监管者（开始及每一种拒绝、按键以及到达键盘的字节、保持、结束、监视器的自动保持、重启后的接管、重放）；真实伪终端下的启动器和假客户端（完整的一次运行、误按的 `s`、
慢速保存期间的两次标签点击、保持与结束、核心已死的客户端）；变异检查（故意改坏代码，指定的测试必须失败）；以及对客户端源码的漂移检查。五个测试文件共 260 个测试（96 + 81 + 25 + 50 + 8，其中 25 个用真实进程，约 25 秒）；变异检查脚本 `tests/marvel0/mutate_run.py`（有机器人进程在运行时拒绝运行；`--check` 只校验清单）在临时副本里用 98 种方式改坏代码，每种只跑一个测试文件：第一轮有 7 种没被抓到（重启后的进程号检查、启动器自己对进程组的 SIGKILL、退出记录的权限、运行结束后的令牌、同一次监视器写入被数两次、手臂锁检查行、z 下限检查行），每种都补了测试，现在 98 种全部被抓到；
`scripts/check_client_prompts.py` 在 2026-10-21 找到了全部 49 条字符串。重新生成前端读取的文件：`uv run --no-sync python tests/marvel0/regen_run_sample.py`
（`marvel0-run.samples.json`：各个状态下的 `GET /run`；`marvel0-run.answers.json`：其他路由的回答和各种拒绝）。

**没有在真机上验证的：** `systemd-run --user --scope` 本身（pid 是否就是启动器的、进程组是否在产品重启后存活、scope 的停止行为）；启动器下的真实客户端（环境白名单下 `uv` 的离线运行、
`uv` 链上的信号处理、SIGHUP 能否到达 `python`、没有显示器时的 `--no-display-images`）；SIGSTOP 持续几分钟的后果（相机、策略服务器的 websocket、控制器保持最后目标）；
保存、回位的真实耗时和 1 秒的会话心跳（页面的阈值在测量之前只是猜测）；“两次写入”规则所依赖的真实监视器写入频率；真实客户端的措辞
（只对照过源文件：每天第一次运行前执行 `python3 scripts/check_client_prompts.py`）；挂断的伪终端在真实客户端看来是什么样（Linux 对读取返回 EIO；假客户端取更坏的读法，继续运行）。
第一次真实使用按复审的顺序：先开始到闸门再按 `q`（不动），再一个片段（回车和 `s`），再 `f`，再保持与结束，再在步数上限提示处按 `d`，全程所有者在机械臂旁、手放在急停上。
