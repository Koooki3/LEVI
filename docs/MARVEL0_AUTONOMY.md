# The autonomy system of the marvel0 page (Step 5)

English first, Chinese after the line below. This page is the whole-feature overview and the front end. The two backend
halves have their own documents: `docs/MARVEL0_AERI_PREP.md` (readiness rows, preparing a run, the site and control
file sections, the live service and leftovers buttons) and `docs/MARVEL0_AERI_RUN.md` (hosting terminal D, the state
machine, the keys, Stop, End, adoption after a restart). Front end: `src/components/marvel0/autonomy-*.ts(x)` and
`use-autonomy.ts`, mounted by one line in `src/app/marvel_workstation/page.tsx`. Design record (binding, with its adversarial
review): `LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md`.

## What it is, and what it is not

AERI is a loop: a vision model plans subtasks, the policy executes them, a judge decides whether each one worked, and a
reverse loop resets the scene, round after round. Until now a person prepared a run in a terminal, started **terminal D**
(`scripts/aeri.py execute`) there, and pressed its keys there. Step 5 does the same from the page:

1. it shows the passive **readiness** rows and prepares a run (task, mode, rounds, steps, the gripper wait, whether D
   homes the arm first);
2. it starts terminal D **inside the page** (a pty the page owns) up to D's confirmation prompt;
3. it shows what D is doing and waits for, and **a click on a button stands for pressing the same key in terminal D**.

The honest wording is on the page, always shown, not folded: _this is not an emergency stop; keep a hand on the physical
e-stop; a click here stands for the same key in terminal D and is your decision; Stop is Ctrl+C: the arm stays where it
is and does not go home; the checks only prevent mistakes and cannot stop another program of the same account._ Franka
Desk and the physical emergency stop are the barrier. Terminal D stays a valid way to run AERI (`AERI.md`); a run started
in a terminal is not hosted by the page, and a hosted run cannot be taken over by a terminal.

**Only a person clicks.** The write routes behind the buttons refuse any Bearer credential, and agents never call them.
In external mode the agent's only job is to answer the vision model's queue files; the page shows the queue and never
answers for it. **Nothing starts by default**: `control.toml` needs `[aeri] enabled = true` (a missing section is off,
a malformed one is off and says so, neither can switch hosting on). The switch is read when a run is prepared or
started. **Turning it off while a terminal D runs does not stop D**: the backend refuses the keys that move the arm from
then on (`aeri_disabled`), the page greys them out and says why (and a key that moves the arm stays off on the page even
if a document still offered it), and Stop and Force-end stay available. No new run can be prepared or started until the
switch is on again. The first real use is staged with the owner present and a hand on the e-stop (design section 12).

## What the card shows

The workstation has five in-page navigation links. They scroll to the existing
steps and do not start a service. The run card adds a compact operational
overview below Stop: the task, round, current subtask step and cap, completed
and discarded rounds, and the loop stages. Only a fresh, explicit state
highlights a stage. Waiting, closing, unknown and stale readings infer no
stage; earlier stages are never marked complete. Reverse reset returns to
round release. Unknown counters stay blank rather than becoming zero.
Human labels remain on the existing keys, and the detailed run page remains
available only after terminal D ends.

Top to bottom, in the order of a run. While a hosted D lives the prepare part is hidden; once it has ended the run's
summary stays and the next run can be prepared.

- **Readiness** (`GET /aeri`): one row per check, what refuses first (`Not met:`), warnings, and what is met folded. An
  `unknown` row refuses like a refusal. The backend checks every row again at the start. A full read costs the backend
  about six processes (git, nvidia-smi, systemctl), so the page reads it every 3 s **only while there is something to
  prepare**: when hosting is off in the control file or the site file has no usable `[aeri]` section it reads once and then
  every 30 s (the backend answers with the switch and the site's facts only), and while an evaluation client is live it
  reads once and then not at all until the client ends (the card says so; the arm controller is up then, and the
  backend's read is a light one). D's own document is read every second while D lives, otherwise every 3 s (every 30 s
  when hosting is off or a client is live).
- **Prepare**: first the three stages of a first use (1: inventory only, no motion; 2: one round; 3: several rounds), then
  the task (the new_run.py pattern, sent to the policy word for word; the example in the field is "pick the watermelon
  into the blue plate", no longer the worn-out pepper), the mode (external: an agent
  answers, about 9 GB of GPU; local: a small vision model, about 19 GB, not yet run on the real robot), rounds, steps per
  subtask, the gripper wait, and a switch "home the arm first" that exists only for code versions that have the three
  homing confirmations. The limits are the backend's (`limits` in the document), checked again there. The defaults here
  (5 rounds, 200 steps per subtask) are the cautious ones for the first runs; the page's 3000 steps and 50 episodes belong
  to the evaluation client of Step 4, not to this step.
- **The prepared run**: its facts, the plan and binding digests, and **the command of the terminal fallback**, which is the
  very command the hosted D runs. **Start terminal D** is off while a row refuses (or is unknown), the prepared files
  changed, a D or the evaluation client of Step 4 is live, or the run document says a start is not offered; the reasons
  are listed under it.
- **The live service and leftovers**: when the live annotation service holds :8100, or an earlier D left AERI units
  behind, a button with a confirm window stops exactly those (nothing hand-started), with the backend's reasons when it
  refuses.
- **The hosted terminal D**: the state in words and a sentence on what it waits for, the **line D printed** quoted
  from its screen, how far the rounds are (from D's journal) and, in the forward loop, the subtask's step; the objects
  the vision model listed (a table that becomes stacked rows on a phone; **confirm it only against the table**); what a
  WAIT needs, with the reminder to restore the scene by hand before a resume; the keys; the agent's queue (external mode);
  D's process, keyboard, heartbeat, locks and ports; D's screen (folded; open when its wording drifted); the **clicks made
  on this page** (folded); the backend's notices; after the exit, the exit in words, what is left behind, and the way to
  the run's own page.
- **Labels are the person's.** `s`, `f` and `d` are the operator's labels, one per round; the page never labels. The
  link to `/automatic/runs/<name>` appears only **after** the run ended, because that page shows the vision model's
  judgements, which would sway the person's own label.

## The keys

A key is a button with the byte it stands for (`Enter`, `y`, `s`...), what it does in words, and a line under it. The
backend decides which keys exist in a state and why one is off (`keys[]` of `GET /aeri/run`); the page adds only checks
that are about this browser. The page labels each key from its own closed table and **refuses a document whose byte
differs from it** (a test compares the table with the backend's).

| Level | Keys                                                                                     | What the page asks                                                                                                                                                                                                                                                                                                                                                                |
| ----- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A     | `n`, `q`, cancel of a resume                                                             | one click                                                                                                                                                                                                                                                                                                                                                                         |
| B     | `y` (list), Enter (release, resume), `o`, `p`, `h`, `s`, `f`, `d`, `y` (resume)          | the key is off for **1.5 s** after it appears (a countdown on the button) and for **3 s** after the previous key; then one click opens a **confirm window** (Cancel has the initial focus; Escape and the backdrop cancel) and its Confirm sends the key                                                                                                                          |
| C     | `execute <name>`, the long variant after an unfinished policy switch, and the forced end | the same, with **one tick for every check terminal D itself asks a person to make before it takes the phrase**, then the three checks of the arm stack's windows (a hand on the e-stop, hands and objects clear, no evaluation client); Confirm stays off until every box is ticked. **The page sends the phrase itself, the document's own word for word, and nothing is typed** |

- `o`, `p` and `h` are three separate clicks in their own states, each with its own warning; the later keys of a pair
  (`p`, `h`, `s`/`f`/`d`, the resume `y`) sit in **another column** (on a phone, under a line "next step"), so one hand
  cannot press both in the same place.
- **One key per prompt.** After a click the button says it was sent and is **never sent again**; every other key of the
  prompt is off. If the page does not see the key taken after 5 s it says so; it does not resend. A request that got no
  answer is treated as possibly sent. There is **no optimistic state**: what the card shows next is what the next
  document says.
- **What the windows tick.** The release of a round (the Enter at D's `sub_release`: "when the scene is ready press Enter")
  has **one tick**, `scene_ready`: the scene is set, objects placed, the area clear, a hand on the physical e-stop; its
  Confirm stays off until it is set. `execute`: D's five (the reviewed scene, limits, checkpoints, active providers and
  hardware readiness: what its `run_confirm_warning` names) + the three. `execute_variant`: the four of D's policy
  switch check (each leftover unit with `systemctl --user status`, the policy ports with `ss -ltnp`, `nvidia-smi`, the
  arm at its home pose) + D's five + the three. The forced end: the three (D asks nothing there). The lists are written
  from the profile's own texts (`levi/marvel0/aeri_prompts.json`) and a test fails when a text names an item that has
  no tick or the other way round; the window also quotes the line D is showing, so the person confirms that prompt. The
  two windows that answer the phrase prompt (`execute`, `execute_variant`) also **quote what D printed above it**, verbatim:
  the last 30 lines of its screen, without the prompt's own line. D prints the run's reviewed summary there and, when an
  earlier policy switch did not end cleanly, the logs to check. The document has no separate fields for those lines (the
  screen tail is the only place they are), so the page quotes the screen and picks nothing out of it. A level C key whose
  document gives no phrase is never sent (the page does not make one up; it says so as its own mistake).
- **What a click sends as `attest`.** A key that has ticks (`execute`, `execute_variant`, `release_round`) and the forced
  end send the codes of the ticks that were set, in the window's order, in the `attest` field of the request: exactly the
  codes of that key's required ticks, never fewer (the page refuses to build a request with fewer, `attest_incomplete`).
  The backend holds the same table (`aeri_state.KEY_ATTEST`) and refuses any other list. This stops a mistake of the page
  or of a click; it is no barrier against another program of the same account, which can send the same list. A test reads
  the backend's table and compares it with the page's.
- **The Enter that asks to resume** is level B for both kinds of wait (the backend cannot be sure which kind a wait is, so
  the asking Enter has the delay, the gap and the presence check too). Its window says "step 1 of 2: D should ask again; if
  it resumes at once instead, the arm may move" for the wait that asks twice, and "D resumes at once" for the other.
- A window whose prompt moved on while it was open (another run, another state counter, another prompt id, or a prompt
  of another kind than the key's) closes by itself and sends nothing; `pressKey` itself also refuses a key for a prompt
  other than the one D shows.
- A click carries what the page was looking at (`seen`: run id, state counter, prompt id), the key's one-time token and
  this tab's id; the backend compares `seen` with its own state and sends nothing when they differ. A read older than 2 s
  switches every key off. The delay the page takes from the backend's `not_before` is bounded by 3 s, so a browser clock
  that is minutes off cannot lock a key (the backend still enforces its own). A visible tab in focus tells the backend it is there every 5 s (a key that moves the arm needs a
  beat from the tab that clicks within 15 s).
- **There is no keyboard shortcut and no `accesskey`**: a stray key in the browser cannot release a round.
- **Stop** (Ctrl+C) is a full-width block, 64 px high, in a place that does not move, there while D lives and **not
  waiting for a fresh read** (a D that is not read is the case in which it matters most). It is its own path: no seen, no
  token, it never waits for a key on its way. Once per run; the backend opens one retry if D did not acknowledge it. **What
  the page says after a Stop depends on what D has said back:** until D acknowledges it (stop phase `sent`,
  `unconfirmed`, `retried`) it says "sent, not confirmed; the arm may still be moving" and that the physical e-stop is
  the emergency stop; only an acknowledged stop (`acked`, whoever sent it) is "D is winding down, the arm stays where it
  is". The same for the restart orphan and a stuck wrap-up, and the card's edge is red while a Stop is unacknowledged (the
  arm may still be moving; a green edge would say the opposite). **The Stop frame's mode comes from the stop phase and
  from whether the backend offers a Stop now, together** (a phase alone said "one more try is offered" next to a button
  that was off, for an adopted D whose retry the backend had not opened). `retry` is shown only while a retry is offered;
  a Stop that D did not answer and that is not offered again is `unoffered` ("D has not confirmed the Stop ... and this
  page is offered no further Stop", the reason, the physical e-stop and `scripts/aeri_page_stop.py --again`); a Stop that
  is not offered and was never sent is `off`. The reason is the document's `stop.why_not` in the Stop's own words
  (`stop_spent`: a Stop was already sent and none is offered from this page now; `driven_elsewhere`: another LEVI core
  owns the run; `d_gone`), else the document's wording; the dock at the foot of the window says the same. The sentence under the
  badge is told apart the same way: a stopping D whose retry is not offered does not read "one more try is offered", and
  `no_keyboard` is two situations, read from the document's word on Stop: a run another core of LEVI owns (Stop refused with
  `driven_elsewhere`: "only watches it") and a D the page cannot follow (an older record whose site file does not read, Stop
  offered and sent to D's own process: "Stop is still offered"). A run another core of LEVI owns (`no_keyboard`, Stop refused
  with `driven_elsewhere`) is said to be only watched: no key, no Stop from this page. A
  dock repeats it at the foot of the window when the card is scrolled out of view. Nothing here is an emergency stop and
  the caption beside it says so. If the page itself hangs, `scripts/aeri_page_stop.py` (named on the card) sends D one
  Ctrl+C.
- **Force-end** is folded and offered only when D's wrap-up shows no progress; a window with a danger button and the three
  ticks; the units may stay behind.
- **The chain at the top of the page** follows D: a D that lives is a run (its node is ready, working, a problem, or needs a
  look, by D's state, and stale when it was not read lately). The backend counts D as an active run (`run_active`), which
  is why the arm stack's stop and start (blocker `run_active`), a policy-server deploy (`aeri_run_live`) and the evaluation
  client's start (`no_aeri`) are refused while D lives; the page shows the backend's blocker.
- **Loaded on demand.** Step 5 is a separate piece of code the page fetches after its first paint, behind a boundary: if
  it cannot load, or throws while it renders, one note replaces it and the rest of the page stays. The note is drawn by
  the wrapper that is always loaded (it imports nothing from the step's code), and it names what works without the page:
  `python3 -I scripts/aeri_page_stop.py`, run on the LEVI machine in the LEVI checkout the page is served from; it sends
  D one Ctrl+C, like the Stop button; it needs the run record the page wrote (`outputs/LEVI/workbench/marvel0/aeri/
  record.json` in the LEVI workspace, `--workspace DIR` names another) and a D that is still running; `--again` only
  when D did not answer a first Stop within 5 s (a second Ctrl+C cuts its wrap-up short); if the script says it cannot
  read the record, run it again with `--scope`: it finds D by its systemd scope without the record (the page will not know of that Stop and nothing guards a second one). The physical e-stop is always there; this script is not an emergency stop.

## Default off, and the files

| What                                         | Where                                                                                                                                                                                                                                                                       |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| switch (default off)                         | `control.toml` `[aeri] enabled = false`                                                                                                                                                                                                                                     |
| which AERI code, which prompt profile, paths | `site.toml` `[aeri]` (profile `harvest-5bc750ca`, validated on the real robot on 18 Oct, is the default; `bfdee2cf` has no startup homing; an unknown version is refused, never guessed)                                                                                    |
| readiness, preparing                         | `levi/marvel0/aeri_ready.py`, `aeri_prep.py`, `aeri_leftovers.py`, `aeri_api.py`                                                                                                                                                                                            |
| hosting, state, keys                         | `levi/marvel0/aeri_run.py`, `aeri_state.py`, `aeri_screen.py`, `aeri_journal.py`, `aeri_run_api.py`, `hostchild.py` (`mode: "aeri"`)                                                                                                                                        |
| front end                                    | `autonomy-doc.ts` (documents, closed sets), `autonomy-logic.ts` (what a key may do), `autonomy-api.ts`, `use-autonomy.ts`, `autonomy-keys.tsx`, `autonomy-run-card.tsx`, `autonomy-prepare.tsx`, `autonomy-step.tsx`, `autonomy-styles.css`, the words `marvel0.autonomy.*` |

## Tests

The tests read the backend's own samples (`tests/marvel0/fixtures/aeri_run.sample.json`, and
`src/components/marvel0/__tests__/marvel0-aeri.samples.json` generated from the real code by
`tests/marvel0/regen_aeri_ready_sample.py`) and its Python source (routes, the closed body models, the key table, the
constants, the code lists), so a change on either side fails here. Run one file at a time (`bun test <file>`; not while
the arm controller is on, and under `nice -n 19 taskset`):

- `autonomy-doc.test.ts` (documents against the closed sets), `autonomy-logic.test.ts` (gating, with the real functions
  and no injected fake), `autonomy-contract.test.ts` (routes, bodies, constants, key table), `autonomy-catalog.test.ts`
  (every code the backend names has words in both languages; the Chinese rules; nothing calls itself an emergency
  stop), `autonomy-styles.test.ts`;
- `autonomy-review2.test.tsx` and `autonomy-review-docs.json`: the second round of reviews. The json holds three documents
  the reviewers recorded from the real backend functions (an adopted D whose Ctrl+C went unanswered, one read at once,
  and a run whose site file is broken), unedited; the tests run the page on them (Stop, the switch turned off mid-run, the
  release tick, the quote of D's screen);
- DOM: `autonomy-run.test.tsx` (every state of the samples, level A and B keys, one request per click),
  `autonomy-confirm.test.tsx` (the ticks of the level C windows, each one needed; the phrase; the window closing with the
  prompt; `pressKey` on its own), `autonomy-poll.test.tsx` (how often it reads, the dock's watch, the lazy load, the
  boundary), `autonomy-chain.test.ts` (the chain's run node follows D),
  `autonomy-actions.test.tsx` (Stop, the windows of the other keys, the dead and stuck states, presence, switched off,
  Chinese), `autonomy-prepare.test.tsx` (readiness, preparing, starting, the live service and leftovers),
  `autonomy-page.test.tsx` (the mount on the page);
- `tests/marvel0/mutate_autonomy_ui.py` breaks one gate at a time in a temporary state of the front end and requires a
  test to fail for each (116 mutations; `MUTATE_CORES=16-19` picks the cores).

Screenshots of every state (1440 and 420 px, light and dark, English and Chinese) are made with the rig-console harness
(`programs/rig-console/tools/shots/marvel0_shots.py`) on a temporary instance, every request answered from the samples;
nothing reaches a backend route or a robot port.

## Not verified

The first run on the real robot has not happened. Unverified until then: how the page's pty and `uv` carry Ctrl+C and the
hang-up to D, the numbers in the table above (1.5 s, 3 s, 5 s, 2 s, 15 s are guesses to be measured), that the tick
lists match D's texts on a version of the AERI code other than the profiles on record (a test compares them with the
pinned texts of every profile, in both languages), the zh prompt wording of the pinned code on the real robot, the live
service's stop through its unit on the real machine, local mode on the real robot, and that the
AERI log cannot tell a click on this page from a key in the terminal (only the page's own click list can).

---

# 页面上的自主系统（第 5 步）

先英文，后中文。本文是整项功能的概览和前端说明。后端两半各有文档：`docs/MARVEL0_AERI_PREP.md`（就绪检查、准备运行、
站点与控制文件段、实时服务与残留单元按钮）和 `docs/MARVEL0_AERI_RUN.md`（托管终端 D、状态机、按键、停止、强制结束、
重启后接管）。前端：`src/components/marvel0/autonomy-*.ts(x)` 和 `use-autonomy.ts`，由 `src/app/marvel_workstation/page.tsx` 里
的一行挂载。设计记录（有约束力，含对抗审查）：`LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md`。

## 它是什么，不是什么

AERI 是一个闭环：视觉模型规划子任务，策略执行，判定器判断每个子任务是否完成，逆向循环复位场景，一轮一轮地跑。此前由人
在终端里准备运行、启动**终端 D**（`scripts/aeri.py execute`）并在终端里按键。第 5 步在页面上做同样的事：

1. 显示被动的**就绪**检查，并准备一次运行（任务、模式、轮数、步数、夹爪等待、D 是否先回位）；
2. 在**页面内**启动终端 D（页面自己持有的 pty），运行到 D 的确认提示为止；
3. 显示 D 在做什么、在等什么，**点按钮等同于在终端 D 里按同一个键**。

诚实的措辞一直显示在页面上，不折叠：_这不是急停；手要放在物理急停上；这里的点击等同于在终端 D 里按同一个键，每一次都是你的
决定；停止等于 Ctrl+C：手臂停在原处、不会回位；这些检查只防误操作，防不了同一账号下的其他程序。_ 屏障是 Franka Desk 和
物理急停。终端 D 仍然是运行 AERI 的有效方式（`AERI.md`）；在终端里启动的运行不由页面托管，页面托管的运行也不能被终端接管。

**只有人点击。** 按钮背后的写入路由拒绝任何 Bearer 凭据，agent 从不调用。外部模式里 agent 只负责回答视觉模型的队列文件；
页面显示这个队列，从不替它作答。**默认什么都不会启动**：`control.toml` 里要有 `[aeri] enabled = true`（缺这一段视为关闭，
格式有误也视为关闭并说明原因，两者都不会开启托管）。开关在准备或启动一次运行时读取。**终端 D 运行期间把它关掉，并不会停止 D**：
此后后端拒绝会让手臂动的键（`aeri_disabled`），页面把它们置灰并说明原因（即使某份文档仍然提供这样的键，页面上它也保持关闭），
“停止”和“强制结束”仍然可用。开关重新打开之前，不能准备或启动新的运行。第一次真机使用分阶段进行，用户在场、手放急停（设计第 12 节）。

## 卡片显示什么

工作站提供五个页内导航入口，只滚动到既有步骤，不启动服务。运行卡片在停止按钮
下方集中显示任务、轮次、当前子任务步数及上限、完成和丢弃轮次，以及运行流程。
阶段高亮只采用新鲜且明确的状态；等待、收尾、未知或过期记录不推断阶段，也不把
前序步骤标为已完成。反向复位后返回轮次放行。未知计数保留为空，不补成零。
人工标注仍使用原有按键，详细运行页面仍在终端 D 结束后才开放。

自上而下，按一次运行的顺序。托管的 D 活着时，准备部分隐藏；它结束后，这次运行的摘要保留，可以准备下一次。

- **就绪检查**（`GET /aeri`）：每项检查一行，未满足的在前（“未满足：”），然后是提醒，已满足的折叠。“未知”按未满足处理。
  启动时后端会把每一行再查一遍。后端完整读一次要起约六个进程（git、nvidia-smi、systemctl），所以页面**只在有东西可准备时**
  每 3 秒读一次：控制文件里托管关闭、或站点文件没有可用的 `[aeri]` 段时，只读一次，之后每 30 秒读一次（后端此时只回答开关和
  站点的情况）；评测客户端运行期间读一次后就不再读，直到客户端结束（卡片会说明；此时手臂控制器是开着的，后端的读取也是轻量的）。
  终端 D 的文档在 D 活着时每秒读一次，否则每 3 秒一次（托管关闭或客户端运行时每 30 秒一次）。
- **准备**：先是三行首次使用的阶段说明（1：只看清单，手臂不动；2：只跑一轮；3：多轮），然后是任务（new_run.py 的格式，原样发给
  策略；输入框里的示例是“pick the watermelon into the blue plate”，不再是用腻了的辣椒）、模式（外部：由智能体作答，显存约 9 GB；本地：本地小视觉模型，约
  19 GB，还没有在真机上跑过）、轮数、每个子任务的步数、夹爪等待，以及“先让手臂回位”开关（只有带三次回位确认的代码版本才有）。
  上下限来自后端（文档里的 `limits`），后端会再检查一遍。这里的默认值（5 轮、每个子任务 200 步）是首次运行用的保守值；页面上
  的 3000 步和 50 个片段属于第 4 步的评测客户端，不属于这一步。
- **已准备的运行**：它的各项信息、计划和绑定摘要，以及**终端备用的命令**——就是托管的 D 运行的那一条命令。**启动终端 D**
  在这些情况下关闭：有检查未满足（或未知）、准备好的文件被改动、已有 D 或第 4 步的评测客户端在运行、运行文档说现在不提供
  启动；原因列在按钮下面。
- **实时服务和残留单元**：实时标注服务占着 :8100，或上一次的 D 留下了 AERI 单元时，会出现一个带确认窗口的按钮，只停止
  这些（不动手工启动的），被拒绝时显示后端的原因。
- **托管的终端 D**：用文字说出状态和它在等什么；**引用 D 屏幕上打印的那一行**；轮次进度（来自 D 的日志），正向循环里还有子任务
  的步数；视觉模型列出的物体（表格，手机上变成堆叠的行；**只对照桌面核对后再确认**）；人工等待需要做什么，续跑之前先手动
  恢复场景的提醒；按键；智能体的队列（外部模式）；D 的进程、键盘、心跳、锁和端口；D 的屏幕（折叠，措辞漂移时展开）；
  **本页上的点击记录**（折叠）；后端的说明；结束之后：退出情况的文字、留下了什么、通往这次运行自己页面的链接。
- **标签是人的。** `s`、`f`、`d` 是操作者的标签，每轮一次，页面从不打标签。通往 `/automatic/runs/<名>` 的链接**只在运行结束之后**
  给出，因为那个页面会显示视觉模型的判断，会影响人自己的标签。

## 按键

一个键就是一个按钮：左边是它代表的字节（`Enter`、`y`、`s`……），中间是它做什么，下面一行是状态。哪些键在哪个状态下存在、
为什么关着，由后端决定（`GET /aeri/run` 的 `keys[]`）；页面只加与这个浏览器有关的检查。页面用自己的封闭表给每个键写标签，
**文档里的字节与这张表不符就不提供这个键**（有测试把这张表和后端的表对照）。

| 级别 | 键                                                                          | 页面要求                                                                                                                                                                                                                                                              |
| ---- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A    | `n`、`q`、续跑的取消                                                        | 点一下                                                                                                                                                                                                                                                                |
| B    | `y`（清单）、Enter（放行、续跑）、`o`、`p`、`h`、`s`、`f`、`d`、`y`（续跑） | 键出现后 **1.5 秒**内、以及上一个键之后 **3 秒**内保持关闭（按钮上倒计时）；然后点一下打开**确认窗口**（“取消”有初始焦点；Esc 和点背景都是取消），窗口里的“确认”才发送这个键                                                                                          |
| C    | `execute <名>`、上次策略切换未收尾后的长确认语、强制结束                    | 同上，另有**终端 D 自己在接受确认语之前要人检查的每一项，各是一个勾选框**，再加手臂栈各个窗口都要的三项（手在物理急停上、手和物体离开夹爪与手臂的活动范围、没有评测客户端）；全部勾选之前“确认”保持关闭。**确认语由页面自己发送，逐字取自后端文档，不用输入任何东西** |

- `o`、`p`、`h` 是三次独立的点击，各在自己的状态里，各带自己的警告；成对的后一个键（`p`、`h`、`s`/`f`/`d`、续跑的 `y`）放在
  **另一栏**（手机上放在“下一步”一行文字下面），一只手不会在同一个位置连按两下。
- **每个提示只发一个键。** 点击之后按钮显示已发送，**不会重发**；该提示的其他键全部关闭。5 秒内没有看到键被取走，页面
  会说出来，但不重发。没有收到回答的请求按“可能已发出”处理。**没有乐观状态**：接下来卡片显示什么，只看下一份文档。
- **窗口勾选什么。** 放行一轮（终端 D 的 `sub_release` 处的 Enter：“确认场景就绪后按 Enter”）有**一个勾选框**，`scene_ready`：
  场景已摆好，物体已放置，周围无人，手放在物理急停上；勾选之前“确认”保持关闭。`execute`：D 的五项（已核对的场景、限位、检查点、生效的提供方、硬件就绪情况，即它的
  `run_confirm_warning` 点名的东西）+ 那三项。`execute_variant`：D 的策略切换检查的四项（逐个服务单元用 `systemctl --user status`、
  策略端口用 `ss -ltnp`、`nvidia-smi`、机械臂在回位姿态）+ D 的五项 + 那三项。强制结束：那三项（D 在这里不要求任何检查）。
  这些清单照档案自己的文本（`levi/marvel0/aeri_prompts.json`）写成，有测试在文本点名了某一项却没有对应勾选、或反过来时失败；
  窗口里还引用 D 此刻显示的那一行，让人确认的就是这个提示。回答确认语提示的两个窗口（`execute`、`execute_variant`）还会**原样引用
  D 在这个提示上方打印的内容**：它屏幕的最后 30 行，不含提示自己的那一行。D 在那里打印这次运行已核对的摘要；如果上一次策略切换没有干净结束，
  还有需要检查的日志。文档里没有单独的字段放这些行（它们只出现在屏幕尾部），所以页面引用屏幕本身，不从中挑选。文档没有给出确认语的 C 级键绝不发送
  （页面不自己编，并把它当作页面自己的错误说出来）。
- **一次点击把什么作为 `attest` 发出。** 有勾选的键（`execute`、`execute_variant`、`release_round`）和强制结束，会把已勾选项的代码按窗口里的顺序
  放进请求的 `attest` 字段：正好是该键必需的那些代码，不会少（页面拒绝构造少于这些的请求，错误码 `attest_incomplete`）。后端持有同一张表
  （`aeri_state.KEY_ATTEST`），拒绝任何其他清单。这防的是页面或一次点击的失误，防不了同一账号下的其他程序，它们可以发送同样的清单。有测试读取后端的表并与页面的对照。
- **请求续跑的那个 Enter** 对两种等待都是 B 级（后端无法确定一次等待属于哪一种，所以这个询问的 Enter 同样有延迟、间隔和在场检查）。
  对要求确认两次的等待，窗口里写“两步中的第一步：D 应当再问一次；如果它直接续跑，手臂可能会动”；对 D 直接续跑的等待，写“D 立即续跑”。
- 窗口打开期间提示已经变了（运行号、状态计数、提示号变了，或者提示种类不再是这个键对应的），窗口会自己关闭，什么都不发；
  `pressKey` 本身也拒绝发送与 D 当前显示的提示不符的键。
- 一次点击带着页面当时看到的内容（`seen`：运行号、状态计数、提示号）、这个键的一次性令牌和本标签页的编号；后端把 `seen`
  和自己此刻的状态比较，不一致就什么都不发。读取结果超过 2 秒，所有键都关闭。页面从后端的 `not_before` 取的等待最多 3 秒，所以浏览器时钟差几分钟也不会把键锁住（后端仍按自己的时间强制执行）。可见且有焦点的标签页每 5 秒向后端报一次在场
  （会让手臂动的键要求点击的标签页 15 秒内报过在场）。
- **没有任何键盘快捷键，也没有 `accesskey`**：浏览器里误敲一个键不能放行一轮。
- **停止**（Ctrl+C）是整行的块，高 64 px，位置固定，终端 D 活着就在，**不等新的读取**（读不到 D 的时候恰恰最需要它）。
  它走独立的路径：没有 seen、没有令牌，从不等正在发送的键。每次运行一次；D 没有确认时后端再开放一次重试。**停止之后页面怎么说，取决于
  D 回了什么：** 在 D 确认之前（停止阶段 `sent`、`unconfirmed`、`retried`）说的是“已发送，尚未确认，手臂可能仍在运动”，并说急停
  仍由物理急停负责；只有已确认的停止（`acked`，不管是谁发的）才说“D 正在收尾，手臂停在原处”。重启后找回的孤儿和卡住的收尾同理；停止未被确认期间卡片的边缘是红色（手臂可能仍在运动，绿色会说反）。
  **停止框的模式由停止阶段和“后端现在是否提供停止”共同决定**（只看阶段时，一个重试没有开放的、重启后找回的 D 会在一个关闭的按钮旁写着“还可以再试一次”）。
  只有重试确实开放时才显示 `retry`；终端 D 没有回应且不再提供停止时是 `unoffered`（“终端 D 还没有确认停止……本页不再提供停止”，加上原因、物理急停和
  `scripts/aeri_page_stop.py --again`）；没有发过、也不提供停止时是 `off`。原因取自文档的 `stop.why_not`，用停止框自己的话说
  （`stop_spent`：这次运行已发过停止，现在不再从本页提供；`driven_elsewhere`：另一个 LEVI 核心持有这次运行；`d_gone`），其他的用文档自己的措辞；页面底部的停靠条也这样说。
  状态徽章下面的那句话也按同样的方式区分：停止没有回应、重试又不提供时，不再写“还可以再试一次”；`no_keyboard` 有两种情形，由文档对“停止”的说法区分：
  另一个 LEVI 核心持有的运行（停止被 `driven_elsewhere` 拒绝：“只观察”），和本页跟踪不了的活着的 D（旧版本写下的记录加上读不出的站点文件，
  停止仍然提供并发给 D 自己的进程：“停止仍然可用”）。
  由另一个 LEVI 核心持有的运行（`no_keyboard`，停止被 `driven_elsewhere` 拒绝）显示为只观察：本页不发键、不发停止。
  卡片滚出视野时，
  窗口底部的停靠条重复它。页面上的任何东西都不是急停，旁边的说明也这么写。如果页面本身卡住，用卡片上写明的
  `scripts/aeri_page_stop.py` 给 D 发一次 Ctrl+C。
- **强制结束**默认折叠，只在 D 的收尾没有进展时才开放；窗口里是危险色按钮和那三项勾选；单元可能会残留。
- **页面顶部的链条**跟随 D：活着的 D 就是一次运行（它的节点按 D 的状态显示为就绪、进行中、有问题或需要看一眼，很久没读到则显示
  过期）。后端把 D 算作活动运行（`run_active`），所以 D 活着期间手臂栈的停止和启动（阻挡原因 `run_active`）、策略服务器的部署（`aeri_run_live`）、评测客户端的启动（`no_aeri`）
  都被拒绝；页面显示后端给出的阻挡原因。
- **按需加载。** 第 5 步是页面在首屏之后才取的一段独立代码，外面套着一个边界：加载失败或渲染时抛错，就用一条说明替换它，页面其余部分不受影响。这条说明由始终加载的外层画出（它不从第 5 步的代码里导入任何东西），
  并写明不靠页面也能用的办法：在 LEVI 所在的机器上、在本页所属的 LEVI 检出目录里运行 `python3 -I scripts/aeri_page_stop.py`；它和“停止”按钮一样向 D 发一次 Ctrl+C；
  它需要页面写下的运行记录（LEVI 工作区里的 `outputs/LEVI/workbench/marvel0/aeri/record.json`，用 `--workspace DIR` 指定别的工作区）和仍在运行的 D；
  只有在 D 对第一次停止 5 秒内没有回应时才加 `--again`（第二次 Ctrl+C 会打断它的收尾）；如果脚本说读不到这份记录，就加上 `--scope` 再运行：它不靠记录，按 systemd 的 scope 找到 D（页面不会知道这次停止，也没有任何东西防止再发第二次）。物理急停始终可用；这个脚本不是急停。

## 默认关闭，以及相关文件

| 内容                                   | 位置                                                                                                                                                                                                                                                      |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 开关（默认关）                         | `control.toml` 的 `[aeri] enabled = false`                                                                                                                                                                                                                |
| 用哪份 AERI 代码、哪套提示词档案、路径 | `site.toml` 的 `[aeri]`（档案 `harvest-5bc750ca` 已于 10 月 18 日在真机上验证，是默认；`bfdee2cf` 没有开跑前回位；未知版本拒绝托管，绝不猜）                                                                                                              |
| 就绪、准备                             | `levi/marvel0/aeri_ready.py`、`aeri_prep.py`、`aeri_leftovers.py`、`aeri_api.py`                                                                                                                                                                          |
| 托管、状态、按键                       | `levi/marvel0/aeri_run.py`、`aeri_state.py`、`aeri_screen.py`、`aeri_journal.py`、`aeri_run_api.py`、`hostchild.py`（`mode: "aeri"`）                                                                                                                     |
| 前端                                   | `autonomy-doc.ts`（文档与封闭集合）、`autonomy-logic.ts`（键何时可点）、`autonomy-api.ts`、`use-autonomy.ts`、`autonomy-keys.tsx`、`autonomy-run-card.tsx`、`autonomy-prepare.tsx`、`autonomy-step.tsx`、`autonomy-styles.css`，文案 `marvel0.autonomy.*` |

## 测试

测试读取后端自己的样本（`tests/marvel0/fixtures/aeri_run.sample.json`，以及由 `tests/marvel0/regen_aeri_ready_sample.py` 用真实代码生成的
`src/components/marvel0/__tests__/marvel0-aeri.samples.json`）和它的 Python 源码（路由、封闭的请求体模型、键表、常量、代码清单），
所以任何一边改了都会在这里失败。一次只跑一个文件（`bun test <文件>`；手臂控制器开着时不跑，并用 `nice -n 19 taskset`）：

- `autonomy-doc.test.ts`（文档对照封闭集合）、`autonomy-logic.test.ts`（键何时可点，用真函数、不注入替身）、
  `autonomy-contract.test.ts`（路由、请求体、常量、键表）、`autonomy-catalog.test.ts`（后端命名的每个代码在两种语言里都有文案；
  中文规则；没有任何文字自称急停）、`autonomy-styles.test.ts`；
- `autonomy-review2.test.tsx` 和 `autonomy-review-docs.json`：第二轮审查。json 里是审查者用真实后端函数录下的三份文档（重启后找回、Ctrl+C 没有回应的 D；
  刚找回时读到的同一个 D；站点文件损坏的一次运行），未经编辑；测试用它们跑页面（停止、运行中被关掉的开关、放行勾选、D 屏幕的引用）；
- DOM：`autonomy-run.test.tsx`（样本里的每个状态、A 级和 B 级键、一次点击一个请求）、`autonomy-confirm.test.tsx`（C 级窗口的勾选，每
  一项都必需；确认语；窗口随提示一起关闭；`pressKey` 本身）、`autonomy-poll.test.tsx`（读取频率、停靠条的监视、按需加载、边界）、
  `autonomy-chain.test.ts`（链条的运行节点跟随 D）、`autonomy-actions.test.tsx`（停止、其他键的窗口、
  终止和卡住的状态、在场信号、关闭时、中文）、`autonomy-prepare.test.tsx`（就绪、准备、启动、实时服务和残留单元）、
  `autonomy-page.test.tsx`（页面上的挂载）；
- `tests/marvel0/mutate_autonomy_ui.py` 在前端的临时副本里每次破坏一道门，并要求至少有一个测试失败（116 条；`MUTATE_CORES=16-19` 指定可用的核）。

每个状态的截图（1440 和 420 px，浅色和深色，英文和中文）用 rig-console 的截图工具
（`programs/rig-console/tools/shots/marvel0_shots.py`）在临时实例上拍摄，所有请求都由样本回答，不会碰到任何后端路由或机器人端口。

## 未验证

还没有在真机上跑过。在那之前没有验证的有：页面的 pty 和 `uv` 怎样把 Ctrl+C 与挂断传给 D；上表里的数字（1.5 秒、3 秒、5 秒、
2 秒、15 秒都是待测的猜测值）；勾选清单在档案已记录的版本之外的 AERI 代码上是否仍与 D 的文本一致（有测试把它们和每个档案的固定文本逐一对照，中英文都查）；固定代码在真机上的中文提示词措辞；实时服务经它自己的单元停止在真机上的表现；本地模式在真机上；以及 AERI 的日志分不出
这是本页上的点击还是终端里的按键（只有本页自己的点击记录能分出来）。
