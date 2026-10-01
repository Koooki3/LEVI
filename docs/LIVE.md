# Live annotation service

`levi live` is a background LEVI that labels robot rollouts while an evaluation is still writing them. It watches the rollout folders, takes each **finished** rollout into its own workspace, and has the local model (Qwen3.8 through vLLM) mark its subtask segments and their outcomes and judge whether the episode succeeded. A person can watch the progress on the live page and review the result in the ordinary LEVI viewer. [中文](LIVE.zh-CN.md)

It is a **separate LEVI instance** (its own workspace, UI port 7880, core port 7881), so the long-running work never shares a process with the LEVI you use every day (ports 7860/7861). It is built to stay out of the way of the robot: it lowers its own priority, bounds its threads, costs next to nothing while idle, and starts the GPU model only when the GPU policy allows it.

What it does **not** do: it never writes to a rollout folder, never connects to a robot or policy-server port, never writes a human success/failure label, and never takes part in building a gold set. Its verdicts are automatic and unreviewed, and are marked that way everywhere.

## Quick start

**The order matters: live service with `--prewarm` first, the policy server second, the evaluation last.** vLLM then loads while the card is empty (a cold start is 45-70 s of heavy GPU load whose effect on the policy's inference latency has not been measured, so the service never does one while a session is running, homing or waiting for the reset) and sleeps until there is work; the policy server (`.22` = 7.6 GB, inference latency the same as before, about 59 ms) then loads beside it:

```bash
# 1. The live service (once; it keeps running). --prewarm brings vLLM up now.
cd ~/work/wenkai/LEVI && uv run levi live start --daemon --auto-approve --prewarm
uv run levi live status        # wait until vLLM is ready (or asleep): about a minute

# 2. The policy server, with MEM_FRACTION=.22 (never .25 or .35 with vLLM resident)
cd ~/work/wenkai/openpi && XLA_PYTHON_CLIENT_MEM_FRACTION=.22 uv run scripts/serve_policy.py --port 8000 policy:checkpoint --policy.config pi05_fr3_all_state --policy.dir checkpoints/pi05_fr3_all_step49999

# 3. The evaluation client; its "use background LEVI annotation?" answer is yes.
```

If the policy server was started first, `--prewarm` cannot start vLLM (nobody vouches for the server, so the gate is shut; the status says `prewarm_waiting_for_policy`) and the cold start happens when the client writes its first session file, 20 s (`gpu.standby_min_s`) after it reached `standby` at the earliest. That wait lowers the chance of overlapping the first episode; it does not remove it. Without `--prewarm` the same applies whenever work arrives at `standby`.

More commands:

```bash
uv run levi live status                          # what it is doing
uv run levi live doctor                          # CPU, memory, GPU, disk, warnings
uv run levi live stop                            # stops only its own processes
```

`levi live once [--fake-vlm]` labels everything that is finished now and exits (with `--fake-vlm` against a stand-in model server, no GPU; it needs a scratch `--workspace`, never the live one). `levi live resume` clears a vLLM start failure the service gave up on. `levi live init` writes `<workspace>/live.toml` with every default.

Without `--auto-approve` the service still mirrors, builds views and **plans**, then waits: a person approves the plan in the LEVI page (the dataset shows `awaiting_approval`). With it, the audited automatic approver (below) takes those gates.

The default workspace is `/home/marvel/work/wenkai/levi-live-ws`; the default watched root is `/home/marvel/work/wenkai/online_rollout_data/models` (the evaluation client's `--rollout-root` default). Status lives in `~/.levi-live/`.

## How it works

```
evaluation client ──writes──▶ <root>/<group>/<task>/demo_NNNN/   (source: read only)
                                 │ finished? (criteria.py)
   supervisor (stdlib, ~25 MiB) ─┤ scans, reads sessions, decides, writes status.json
                                 ▼
   GPU policy ──▶ vLLM (started and stopped by the supervisor)
                                 ▼
   worker (one per batch, one dataset at a time)
      mirror → view → temporal run → commit → release-review run → clean up
                                 ▼
   <workspace>/captures/<group>__<task>/demo_NNNN/    hard links
   LEVI store: committed segments (levi.review = auto) + anchored-review records
```

- **Supervisor** (`levi live start`): a few megabytes and a few `stat` calls while idle. It imports no numerical library. It asks the GPU policy whether the model may run, starts vLLM, starts one **worker** process for one dataset, and stops vLLM when work ends or the GPU is wanted back.
- **Worker** (`python -m levi.live.worker`): does the heavy imports, handles one dataset's batch, exits. Every step is resumable: a crash or preemption is continued by the next worker, nothing is labelled twice.
- **Page and core**: `levi serve` for the live workspace (UI :7880, core :7881), started by `levi live start` (core only, with a notice, when there is no production build; `--no-ui` / `--no-core` skip them).

A dataset is one `<group>/<task_folder>`; its catalog name is `<group>__<task_folder>`. Datasets are processed **one at a time**, longest-waiting first, because LEVI refuses a second writer to a dataset whose annotation baseline moved.

## What counts as finished (interface C1)

The client creates the empty file `.complete` as the very last step of `close()`. A `demo_NNNN` is taken only when

1. `.complete` exists (a rollout from before the marker existed: nothing in it changed for 60 s);
2. `metadata.json` has a non-empty `stopped_at`;
3. `events.csv` has an `episode_end` row;
4. no `*_raw.avi` is left (a capture not yet muxed).

It is **rejected** (finished but unusable, never retried) when `media_storage.video_frames_match_csv` is not true or `cameras.stall_detection.stalled` is not empty. `incomplete_*` and `discarded_*` folders are never taken, only counted; `incomplete_*` with `eval.abort_reason == "fr3_fault"` mark the dataset as **FR3-faulted** on the page. The source's `.eval_sessions/` folder is read, never mirrored.

Rollouts finished before the service's first start (or before the task's evaluation session began) are **backlog**: counted, never touched, unless `watch.backlog = "process"` / `--process-backlog`. A session that answered "no" to background annotation (`levi.enabled` false) is skipped. `eval.outcome == "unlabeled"` demos are the ones waiting for LEVI's verdict; `eval.run_id` is kept per demo.

## The mirror

A finished demo is hard-linked file by file into `<workspace>/captures/<name>/.partial-demo_NNNN` and renamed to `demo_NNNN`, so LEVI never sees a half-mirrored demo and a hard link costs no disk. If linking is impossible (another file system) the files are copied, and `levi live doctor` says so. The source is never written. It happens only **between batches**, so a run never sees its dataset move; if a demo finishes late and sorts before an already-labelled one, the view is rebuilt and LEVI re-keys the annotations by demo, which the service tests.

## Settings (`live.toml`)

`levi live init` writes the file with every default; an unknown key or a wrong type is an error, not a silent default. `--workspace`, `--root`, `--gpu-mode`, `--auto-approve`, `--prewarm`, `--process-backlog`, `--since`, `--vllm-port`, `--ui-port`, `--core-port`, `--home` override it. `--adopt-workspace` is needed to turn an existing LEVI workspace that is not a live one into the live workspace; without it `levi live` refuses any workspace that holds LEVI state but no live marker (the product's `.state` above all: the approver's marker must never land there), and the checkout's own `.state` is always refused. One service runs per home *and* per workspace. Environment: `LEVI_LIVE_WORKSPACE`, `LEVI_LIVE_CONFIG`, `LEVI_LIVE_HOME` (where `status.json` lives; default `~/.levi-live`). `LEVI_LIVE_WORKER=1` is set by the service in its worker processes (do not set it yourself): it exempts the worker from the per-request gate check, because the worker stands down by itself. The service removes it from the environment of everything else it starts, so a shell that set it exempts nothing.

| Table | Key | Default | Meaning |
| --- | --- | --- | --- |
| `service` | `workspace` | `/home/marvel/work/wenkai/levi-live-ws` | the live LEVI workspace |
| | `ui_port`, `core_port`, `host` | 7880, 7881, 127.0.0.1 | never 7860/7861 (product), 5000/8000 (robot) |
| | `poll_idle_s`, `poll_active_s`, `gate_poll_s`, `heartbeat_s` | 15, 3, 0.25, 4 | stat-only rollout scan interval idle/active; how often the gate is re-decided while a worker runs; status heartbeat |
| `watch` | `roots` | `[".../online_rollout_data/models"]` | rollout roots |
| | `include`, `exclude` | all | `fnmatch` over `group/task_folder` |
| | `backlog`, `since` | `skip`, none | see above |
| | `require_session` | false | only tasks with an evaluation session that enabled LEVI |
| | `batch_max_episodes` | 40 | demos per batch |
| | `stuck_s` | 600 | a demo that has not finished and not changed for this long (a leftover raw capture, a client that died mid-write) is `stuck`: counted and listed, no longer waited for or re-read |
| `fr3` | `health_file` | `~/work/franka_control/run/fr3_health.json` | health monitor file |
| `gpu` | `mode` | `auto` | `auto` (= `timeshare`), `timeshare`, `coexist`, `manual` |
| | `policy_ports` | `[8000]` | looked up in `/proc/net/tcp`, never connected to |
| | `busy_states` | `["running"]` | session states in which the policy is inferring |
| | `lead_s`, `lead_grace_s` | 3, 5 | the gate closes this long before the next episode (a session waiting for the reset starts running `levi.reset_wait_s` after it began waiting) and stays closed this long after the predicted start |
| | `lock_file`, `lock_agent` | `levi-hub/.gpu.lock`, `live` | the workspace's GPU `flock` |
| | `settle_s` | 20 | wait after a policy server appears or goes before starting vLLM |
| | `standby_min_s` | 20 | a cold start waits until a session has been in `standby` this long (its first episode follows within seconds). A mitigation, not a guarantee: use `--prewarm` before the evaluation |
| | `wake_margin_mib` | 300 | free VRAM kept beyond the budget (less what the sleeping vLLM still holds) when waking it; a start keeps `vllm.margin_mib`. **Not measured on a real GPU**: with the policy server at `.22` a sleeping vLLM leaves about 22804 MiB free and a wake at 0.7395 needs 21843 with 300 (22643 with the start margin, 161 MiB to spare) |
| | `blocked_pause_s` | 300 | the GPU lock held by another agent, somebody else's vLLM on the port, or a sleeping vLLM that cannot wake for lack of room is reported in `labelling_paused` after this long |
| | `policy_loaded_min_mib`, `policy_load_wait_s` | 6000, 120 | a listening policy port counts as a loaded server (for the budget) only when the process holds this much VRAM; one that holds less is still loading: vLLM waits (`settling`) up to `policy_load_wait_s` after it appeared, then plans as if alone. Memory that cannot be read is planned as alone too |
| | `resume_stable_s`, `resume_max_bounces` | 3, 3 | a person's run the live gate stopped (`blocked`) continues by itself once the gate has stayed open this long (so the `episode_imminent` window is not used), once per opening; after this many stops in a row without a finished episode in between it is left for a person to resume |
| | `unknown_client_pause_s` | 600 | a policy server no session vouches for keeps the gate shut; after this long the status says `labelling_paused` (`unknown_client`) |
| | `min_free_mib`, `policy_budget_mib` | 600, 8500 | vLLM sleeps when free VRAM falls below the first, or the policy server holds more than the second |
| `vllm` | `script`, `stop_script`, `pid_dir`, `port` | `tools/vllm/serve-qwen38.sh`, `serve.sh`, `logs`, 8100 | the launch scripts |
| | `gpu_memory_utilization`, `max_model_len`, `max_images`, `max_num_seqs`, `max_num_batched_tokens` | 0.74, 49152, 128, 2, 4096 | the measured shared profile; the budget and context actually used are chosen at each start from the free VRAM (see GPU management) |
| | `gpu_memory_utilization_max`, `gpu_memory_utilization_min`, `min_utilization_with_policy`, `min_utilization_alone`, `kv_bytes_per_token`, `min_model_len` | 0.747, 0.70, 0.725, 0.725, 39800, 32768 | the limits of that choice, calibrated with a **warm** compile cache (`live-validation.md` section 5): vLLM keeps 20.9 GiB of its 31.36 GiB for non-KV memory, so budget u leaves u × 31.36 − 20.9 GiB of KV cache (0.72: 1.73 GiB, 0.74: 2.36 GiB), and 49152 tokens need 1.82 GiB: at least about 0.7245. Alone and beside a policy server the floor is the same |
| | `sleep_mode`, `idle_action`, `margin_mib` | true, `auto`, 1100 | `--enable-sleep-mode` (+ `VLLM_SERVER_DEV_MODE=1`); `auto` = sleep while an evaluation is live, else stop; VRAM kept free beyond the budget (vLLM refuses a budget above its own free memory, about 930 MiB less than nvidia-smi's) |
| | `max_start_failures`, `start_backoff_s`, `start_backoff_max_s` | 3, 60, 600 | failed starts in a row before the service stops and asks for a person; back-off between tries |
| | `idle_timeout_s`, `start_timeout_s` | 120, 1800 | idle before sleeping/stopping; give up starting |
| | `adopt_external` | false | use a vLLM this service did not start |
| | `prewarm` | false | after the service starts, with no evaluation running, bring vLLM up and keep it resident: idle only puts it to sleep, it stops with the service (`levi live start --prewarm`). The way to have **no cold start during an evaluation** |
| `provider` | `name`, `prompt_style`, `requests_in_flight` | `live-qwen38`, `lean`, 2 | the model profile created and bound in the live workspace |
| `pipeline` | `auto_approve` | **false** | the automatic approver |
| | `coarse_step_seconds`, `refine` | 0.5, `always` | the evaluated temporal settings; the step widens for long episodes so frames fit `max_images` |
| | `guideline`, `vocabulary`, `anchored`, `anchored_spec`, `anchored_min_valid` | generic v1 files, true, 1 | see "The generic configuration" |
| | `max_attempts` | 2 | attempts per episode before it is left alone |
| | `cleanup` | true | drop finished runs' inputs and evidence |
| | `keep_review_runs` | 10 | open release-review runs per dataset that keep their frozen input (committing one needs it); older ones are cancelled and cleaned |
| `resources` | `nice`, `ionice_class`, `threads`, `view_workers` | 19, 3 (idle), 2, 1 | see "Keeping it light" |
| | `log_max_mb`, `log_backups`, `cache_max_gib`, `status_max_datasets` | 5, 3, 20, 64 | bounds |

The service sets `LEVI_DROID_SAMPLE=off` (no sample download), `LEVI_GPU_SHARING=allow` (its own policy replaces LEVI's off-peak guard, see below), `LEVI_SYNC_DISCOVER=off` and `LEVI_SYNC_INTERVAL=30` for the live workspace.

## GPU management

One 32 GB GPU is shared with the robot's policy server. The measurement (`levi-hub/reports/live-gpu.md`) gives the rules:

- With the policy server at `XLA_PYTHON_CLIENT_MEM_FRACTION=.22` (7.6 GB, inference p50 about 59 ms, the same as `.35`) and vLLM at `--gpu-memory-utilization 0.72` **both stay resident** (peak about 31.3 of 32.6 GB, 1.3 GB spare). That measurement was a first start with a cold compile cache; later starts need 0.74 for the 49152-token context (see "Why not 0.72" below), which leaves about 0.6 GB spare beside the policy server. The old `.35` (11.8 GB) does not fit with vLLM.
- They **cannot infer at the same time**: while vLLM works, each policy inference takes about 120 ms instead of 59 ms, over the 100 ms control cycle, and the recorded time steps jitter. vLLM idle (loaded, no request) costs the policy nothing.

So the default, **`timeshare`** (what `auto` means), keeps both resident and staggers the work with a **gate**:

| State of the evaluation | Gate | The model |
| --- | --- | --- |
| a session is `running` (the policy infers) | **closed** | sends no request; one in flight is cancelled at once and the run backs off |
| a session is `waiting_reset` and its next episode is due within `gpu.lead_s` (3 s) of `levi.reset_wait_s` after it began waiting | **closed** (`episode_imminent`) | the first inference of the new episode never meets a model request |
| `homing`, `waiting_reset` (earlier), `standby`, `fault` | open | works (vLLM that is already awake; it is not *started* now, see below) |
| a policy server listens and no live session vouches for it (a session that ended before the server appeared does not: session files are never deleted) | **closed** (someone we do not know may be using it) | waits |
| no policy server | open | works |

The supervisor re-decides the gate every 0.25 s while a worker runs (the worker reads it every 0.2 s; a test asserts the in-flight request is cut within 1 s of `running` appearing), writes it to `live/gate.json` (the worker reads a file nobody has refreshed for 20 s as closed, whatever it says, so a dead supervisor cannot leave it open; people are treated slightly differently, see "When things go wrong"; the supervisor deletes the file when it stops), and the worker obeys: when it closes, the worker pauses its runs (pausing aborts the in-flight HTTP request, so the server stops generating), waits for the run's thread to let go of its lease, and resumes when the gate opens. Nothing is repeated: a run continues from its last finished episode. A worker that does not stand down within 8 s is stopped by the supervisor. Between episodes (`homing`, `waiting_reset`, about 20 s) the model labels; when the session ends it labels the rest in one go.

| Mode | Behaviour |
| --- | --- |
| `timeshare` (`auto`) | as above |
| `coexist` | the same two resident, but the gate never closes: the model works whenever it has work, each policy inference then takes about 60 ms longer. A manual choice |
| `manual` | never starts vLLM: it uses the one already on `vllm.port` |

**Starting, sleeping and waking.** vLLM starts when a batch is waiting and (a) **the gate is open and no evaluation is under way**: a cold start is 45-70 s of heavy GPU load and **its effect on the policy's inference latency has not been measured**, so it is never started while a session is `running`, `homing` or `waiting_reset` (decision `evaluation_active`), only at `standby` (and then only once the session has been there `gpu.standby_min_s`, decision `standby_settling`), after the session ended, or with no session. To have the model ready before an evaluation, start the service with `--prewarm` (or `vllm.prewarm = true`) **before the policy server** and wait until `levi live status` shows vLLM ready: it comes up at once and stays resident (it only sleeps when idle, never stops until `levi live stop`). Started after the policy server, `--prewarm` waits behind the closed gate (decision `prewarm_waiting_for_policy`, in plain words in the status). Prewarm is for a policy server at `.22` only: with a larger one (`.25` holds 8575 MiB) vLLM could not stay awake beside it. While prewarmed it also holds the workspace GPU lock and about 2.2 GB even asleep (see Limits). A **wake** from sleep (0.75 s) also needs an open gate and no episode about to start (`episode_imminent`), and free VRAM for the budget less what the sleeping vLLM still holds plus `gpu.wake_margin_mib` (300; a start's `margin_mib` is 1100), (b) the free VRAM allows a budget, chosen at each start, not fixed. Beside a policy server that is loaded (its process holds at least `gpu.policy_loaded_min_mib`; `.22`: about 24.9 GB free) the budget is the free memory less `margin_mib`, at most 0.747; a policy server that holds more than `gpu.policy_budget_mib` (8500 MiB; `.25` holds 8575) is refused outright before any cold start (decision and `labelling_paused` code `policy_large`, saying to use `.22`), because vLLM would start and go straight back to sleep for good; a policy port that is listening but holds almost nothing is still loading: vLLM waits (`settling`), and after `gpu.policy_load_wait_s` (or if the memory cannot be read) it plans as if alone. Alone on the card the budget is the configured 0.74. **Why not 0.72**: the first start with these parameters worked at 0.72 only because the compile cache was cold; every later start loads the compiled graphs, keeps about 0.3 GiB more outside the KV cache, and 0.72 leaves a 1.73 GiB KV cache, below the 1.82 GiB that 49152 tokens need (measured on an idle GPU). 0.74 leaves 2.36 GiB, and about 0.6 GB beside it for a policy server that starts later (not verified on a real GPU; the first start after the compile cache is emptied may behave differently). If the budget does not serve `max_model_len` the context steps down by 4096 to `min_model_len` (32768); if even that does not fit nothing starts and the status says `insufficient_vram` with the numbers (the old `.35` policy server is such a case). The pre-check and the launch use the same number, and the event log says which values were used. If vLLM still fails for lack of KV cache, the error says so ("the memory budget is too small: … use at least X, or a context of at most N tokens"). (c) no policy server appeared or went in the last `gpu.settle_s` (one that is still loading preallocates its memory), (d) the workspace GPU lock is free. It checks `:8000` in `/proc/net/tcp` and VRAM with `nvidia-smi --query-gpu`; nothing connects to a policy port. The status shows `gpu_wait` meanwhile. vLLM runs with `--enable-sleep-mode` and `VLLM_SERVER_DEV_MODE=1` (development endpoints on 127.0.0.1 only):

- **Sleep** (level 1: 5.5 s down, 1.8 GB left, weights in host memory) when its memory is wanted: free VRAM falls below `gpu.min_free_mib`, or the policy server holds more than `gpu.policy_budget_mib` (8500 MiB: a larger fraction than `.22`, which no awake vLLM fits beside). The worker is stopped first. After `vllm.idle_timeout_s` without work it also sleeps while an evaluation is live (`idle_action = auto`), and is **stopped** once nothing evaluates any more (or at once with no evaluation live).
- **Wake** (0.75 s) when work is waiting and free VRAM again covers it. With a larger policy server up it stays asleep and labels only after that server exits (the pre-measurement behaviour). Level 2 sleep is not used.

**Failed starts.** A start that fails (vLLM's own last error line is read from its log, for example `ValueError ... KV cache ...`) is retried after 60 s, then 120 s, up to 600 s; after `vllm.max_start_failures` (3) in a row the service stops trying: labelling is paused (`status.attention`, `gpu.decision.code = needs_attention`), `levi live doctor` warns with the reason, and `levi live resume` clears it. `status.labelling_paused` says the same in a field the evaluation client can read (below). The service keeps `accepts_sessions` true meanwhile: the evaluation client only needs somewhere that is receiving its rollouts, and the rollouts keep being mirrored and are labelled after the resume.

**Stopping vLLM.** The lock is let go only after the server's whole process group is gone and `nvidia-smi` no longer lists any of its processes (up to 60 s); if they are still releasing the card the lock is kept, the decision is `gpu_not_free`, and each tick looks again.

Always true: the workspace GPU lock (`flock` on `levi-hub/.gpu.lock`, `LEVI_AGENT=live`) follows the vLLM process: vLLM is started with the lock's descriptor open, so the lock survives a `kill -9` of the supervisor for as long as vLLM lives, and a restarted supervisor takes the running vLLM back only if the lock is still held by it (else it refuses and says so); the lock is let go only once vLLM is really gone, and `levi live doctor` reports an orphan vLLM and how to stop it. It stops only a server it started (verified by process identity), never another agent's; a vLLM it did not start is used only with `adopt_external` (or in `manual` mode) and never touched; `:5000` and the policy ports are never connected to; an idle tick does not touch `:8100`. Residual risks (measured, see the report): only 1.3-1.5 GB spare; the first episode may see one 280 ms outlier from the policy; a policy server restarted with a larger `MEM_FRACTION` while vLLM is awake fails to load, so start it before vLLM wakes or stop the service first (`levi live stop`).

## The automatic approver

In every other LEVI workspace only a person approves a plan and commits annotations. The live service runs unattended, so its worker acts as the principal `live-auto` **only when all of this holds**:

- the service was started with `--auto-approve` (`pipeline.auto_approve`); the worker then gets `LEVI_LIVE_AUTO_APPROVE=1`, the UI and core never do;
- the workspace carries `live/workspace.json`, which only `levi live` writes;
- a principal built from an HTTP request is never `auto`, so neither the page nor a connected agent can become it.

What it may do: `runs.plan`, `plans.approve`, `runs.execute`/`resume`/`pause`/`cancel`, `runs.get`/`events`, `changes.diff`/`validate`/`approve`/`commit`, `anchored.get`; nothing else (no reset, clean, pilot review, knowledge or improvement publishing). It may approve, run and commit **only runs it planned itself**; a person's plan or draft in the same workspace is refused. Its plans waive the pilot episode inside the plan, which approving covers. **It may approve and commit only the subtask segments of a plain temporal run**: `changes.approve` and `changes.commit` are refused for a review (anchored) run and for any proposal that is not a language segment, such as an outcome, whatever the caller, and the outcome writer itself refuses anything the approver approved. That a live dataset never gets a human outcome label is enforced by authorization, not by the worker's call order.

What it leaves behind: `reviewer_type: auto` (and reviewer `live-auto`) on every change set it approves, `levi.review: "auto"` and `levi.origin.review: "auto"` on every segment it commits, and `<workspace>/live/audit.jsonl` gets a line for every state-changing call when it is allowed and another with its outcome (`completed` or `failed` with the error), and for every refusal; pure reads are not logged. If a person saves an auto segment with different text or times in the viewer, its mark becomes `edited`; saving it unchanged keeps `auto`. What a person wrote or changed is never replaced: a re-run replaces only segments still exactly as written by an earlier run, and the service skips an episode that already carries annotations it did not write (`skipped_human`).

**No outcome labels.** Committing never writes a human outcome label (`annotations/outcomes/`), so the training pool does not take an automatic verdict for a human label. The episode's automatic verdict is the **anchored review record** (`anchored.get`), copied into the dataset state with `review: auto` and `evaluated: false`. The release-review run is left in `waiting_for_review` with an `outcome` proposal a person may accept in the LEVI page (that commit is then a human action and a human label); the service never commits it. The newest `pipeline.keep_review_runs` of these runs per dataset keep their frozen input (committing one needs it); older ones are cancelled and cleaned, their verdicts staying in the dataset state (`review_runs_open` counts the open ones). A rollout from an unattended evaluation has `eval.outcome = "unlabeled"`; LEVI now reads that (and `aborted`) as "no robot label" instead of turning its placeholder `success_flag_final = 0` into a failure. The training-pool scan signature carries a version, so an index written before that fix is read again after the product LEVI restarts (restart first, then scan unattended-evaluation data).

**What a training manifest does with it.** Only a source of *outcomes* is at stake, and an automatic verdict is not a validated one: `levi export manifest` passes over an anchored review whose spec is a `candidate` (the generic release review is one) unless you name the run (`--anchored-run`) or pass `--allow-candidate-anchored`; the manifest then records `anchored.spec_status`. Automatic subtask segments carry `levi.review: auto` into `frames.parquet` as `subtask_review` and into `manifest.json` as `annotation.subtask_auto_share`, so a trainer can drop or down-weight them. Neither the verdicts nor the segments have been evaluated.

**Without `--auto-approve`.** The service plans and waits. Two gates need a person per batch: the temporal plan, then its draft (approve and commit), then the release review's own plan. The supervisor learns which gate the worker waits at, starts no worker and keeps no vLLM for the dataset until it is passed (a plan approved; a draft committed or rejected), and what the person commits is recorded as done by a person (`review: human`), not as a failed attempt.

## The generic configuration

The evaluated tasks are not the ones LEVI was tuned on, so nothing is task-specific. Four files in `levi/live/specs/`, versioned by name and never edited once used (a change is a new file and a new setting):

- `generic-guideline.v1.md`: the annotation guideline. It quotes the rollout's task instruction (`task_description.txt`, else the metadata's `task_description`, else the folder name) and judges every step against it. Six subtask ids as everywhere: `approach grasp transport place retreat other`.
- `generic-definitions.v1.json` / `generic-vocabulary.v1.json`: their definitions (written once to each dataset's own vocabulary).
- `generic-release.v1.json`: the release-review spec for the anchored review, **status `candidate`, not evaluated on any task**. One question at every gripper opening with the side camera at −2.5…+1.2 s and the wrist camera at −1.5…+0.4 s (the frames of the plates review), quoting the task instruction: was an object held, did it land at the destination the instruction names, does it stay. The episode succeeds when at least `pipeline.anchored_min_valid` (default 1) openings are valid. It cannot tell a task that needs two placements from one; set `anchored_min_valid = 2` for such a task. Its accuracy is unknown: evaluate it on development data before trusting a verdict, and read every verdict as "automatic, unreviewed".

The temporal run is the evaluated configuration (coarse 0.5 s, refinement always, lean prompt, profile `qwen38-27b-vllm-48k-lean` with two requests in flight) on the side camera, with the step widened for long episodes so the frames fit the model's image limit (LEVI refuses to thin silently).

## Keeping it light

- **Priority**: every process the service starts (supervisor, worker, page, core, vLLM launch) is set to nice 19 and ionice class idle (failure is reported by `doctor`, not fatal).
- **Threads**: `OMP/MKL/OPENBLAS/NUMEXPR/ARROW/OPENCV` thread variables are set to `resources.threads` (default 2) before those libraries load; the view build runs with `view_workers = 1`.
- **Idle**: the supervisor lists rollout folders with `os.scandir`, reads the tiny session files when they change and rewrites `status.json`. It opens no video and imports no numerical library (tested). Idle scans happen every `poll_idle_s`; an unchanged task folder is not listed again.
- **GPU**: vLLM starts for a batch, sleeps or stops after `idle_timeout_s`, and sleeps at once when its memory is wanted; the gate keeps it out of the policy's way.
- **Disk**: the mirror is hard links (zero extra). A run's frozen input and evidence are deleted when its batch ends (the open release-review run keeps its evidence for review); leftover run folders are trimmed oldest first above `cache_max_gib`; logs rotate at `log_max_mb` × `log_backups`; `status.json` and every API body are bounded.
- **Measured** (fake model, no vLLM, this machine, 10 minutes idle): see "Measured" below: 0.055 % CPU, 26 MiB.

## The live page

`/live` ("Live evaluation" in the top navigation) shows, read-only, what is happening while an evaluation runs. It needs the core of the live workspace (`levi live start`); another LEVI answers "not the live annotation workspace".

- **Red banner** "FR3 fault detected: the evaluation was interrupted" whenever the health monitor reports the red light or a session is in `fault`; the affected dataset cards carry a red mark. A missing or stale monitor is *not* a red light (amber "Monitor offline").
- **Evaluation sessions**: state, run id, episode `n / target` (valid episodes counted), step bar, policy config and checkpoint folder name, last episode, whether LEVI labelling is on, reset wait; "Lost contact" when the client's heartbeat is over 10 s old.
- **FR3 robot arm**: mode, red light, errors, hardware and controller state, reasons.
- **Annotation pipeline**: one card per dataset with mirrored, waiting, labelling, done and failed episodes, committed time segments, and the **automatic** outcome (dashed, tagged "auto", "not reviewed, accuracy not evaluated"; never drawn like a gold label). The review runs left for a person are listed (newest 3 by default; a per-browser view filter hides older ones without changing anything), with a link to the viewer.
- **Service and resources**: state, GPU mode, vLLM state, the labelling gate explained in plain words (for example why labelling waits while the policy infers), queue, worker, last error, supervisor memory, threads and CPU. When the service is not running the page says so and offers `levi live start` to copy.
- **Needs a person**: an amber banner when the service gave up starting the model server (`attention`, with the reason and a copyable `levi live resume`), when a dataset waits for you to approve a plan or commit a draft in the LEVI page (`awaiting`), or when the page or core the service starts did not come up (`frontend`). Dataset cards also show stuck episodes and replaced sources. The reset wait counts down in the browser between polls (`reset_wait_s`, `waiting_reset_since`) and says so when the next episode should have started. Every GPU gate and decision code has a plain-words explanation, in both languages. When the service paused labelling for a reason that does not pass by itself (`labelling_paused`: `vllm_failed`, `vllm_error`, `insufficient_vram`, `policy_large`, `unknown_client`, `vram`, `lock`, `external_busy`) the banner says why, that the evaluation is not affected and no episode is lost, and what to do (with a copyable `levi live resume` where that is the fix); a main loop that has not ticked for 5 minutes (`loop_at`) is called out. In the Agent Workbench, a Run or Resume refused while the policy infers shows a plain sentence (try again in a few seconds; a run that was already going continues by itself) instead of the core's log line. The decision codes `prewarm_waiting_for_policy`, `standby_settling` and `gpu_not_free` are explained too.

It polls `/api/levi/live/status` and `/sessions` every 2 s while an evaluation runs and every 10 s otherwise, stops while the tab is hidden, backs off (up to 30 s) after failures, and loads a dataset's details only for the few cards that matter or are open, and again only when its row changed. It has no write action and shows no token.

## Files the service writes

| Where | What |
| --- | --- |
| `~/.levi-live/status.json` | the status file (interface C4) |
| `~/.levi-live/live.pid`, `live.lock` | single-instance lock and pid record |
| `<workspace>/live.toml` | the configuration (created once) |
| `<workspace>/live/effective.toml` | the effective configuration the worker reads |
| `<workspace>/live/datasets/<name>.json` | per-dataset state: demos and their state, the batch in progress, last batch, verdicts |
| `<workspace>/live/audit.jsonl` | the approver's audit log (rotates) |
| `<workspace>/live/worker.json`, `gate.json`, `vllm.json`, `service.json` | worker progress, the gate, the vLLM this service started, first-start time |
| `<workspace>/live/logs/` | `live.log`, `worker.log`, `ui.log`, `vllm-launch.log` (rotated) |
| `<workspace>/captures/<name>/` | the mirrored capture LEVI registers |
| `<workspace>/outputs/LEVI/…` | LEVI's own state: views, runs, committed annotations |

## Status file (interface C4)

`~/.levi-live/status.json`, rewritten atomically every `heartbeat_s` (4 s; ≤ 5 s). The evaluation client treats the service as usable only when **all** of this holds: `schema` starts with `levi.live.status.`; `updated_at` (epoch seconds) is less than 15 s old; `pid` is alive; `accepts_sessions` is true; `state` is one of `idle active annotating gpu_wait`; and one of `watch_roots` (absolute paths) equals or contains the client's `--rollout-root` (or the root contains it). Otherwise the client falls back to manual labelling. `accepts_sessions` is false while `starting` and after `stopped`/`error`. `attention` is set (and labelling paused) when the service gave up starting vLLM and needs a person (`levi live resume`); it does not change `accepts_sessions` or `state`. **`labelling_paused`** is `null`, or `{code, reason, since}` when nothing is being labelled for a reason that does not pass by itself: `vllm_failed` (gave up starting vLLM; `levi live resume`), `vllm_error` (a start failed and it is backing off), `insufficient_vram` (the free memory does not serve even the shortest context), `policy_large` (the policy server holds more than `gpu.policy_budget_mib`: use `.22`; at once, from the server's memory whatever the gate or the evaluation is doing, as one steady pause), `unknown_client` (a policy server no session vouches for has kept the gate shut for `gpu.unknown_client_pause_s`), and, after `gpu.blocked_pause_s`, `vram` (a sleeping vLLM cannot wake, or a start has no room), `lock` (another agent holds the GPU lock) `external_busy` (somebody else's vLLM is on `vllm.port`) and `gpu_not_free` (vLLM has stopped but its processes still hold GPU memory, so the lock is kept). The gate closing while the policy infers and a settling policy server are ordinary waits and do not set it. It does not change `accepts_sessions`. `loop_at` is when the main loop last ticked (`updated_at` comes from a separate heartbeat thread and stays fresh if the loop is stuck; `levi live doctor` warns when `loop_at` is over 5 minutes old). `frontend` says whether the page / core API the service starts really came up (`levi live start --daemon` prints a failure and exits 2 while labelling keeps running). `gpu_wait` (work waiting for the model: the gate is closed, vLLM is starting or asleep) counts as usable.

```json
{
  "schema": "levi.live.status.v1", "pid": 1234, "started_at": 1790000000.0, "updated_at": 1790000400.2,
  "state": "idle|active|annotating|gpu_wait|starting|error|stopped",
  "accepts_sessions": true, "ui_url": "http://127.0.0.1:7880", "core_port": 7881,
  "workspace": "/home/marvel/work/wenkai/levi-live-ws", "config": null, "auto_approve": false,
  "watch_roots": ["/home/marvel/work/wenkai/online_rollout_data/models"],
  "gpu": {"mode": "timeshare", "configured_mode": "auto", "vllm_state": "ready|asleep|starting|stopped|error",
          "vllm": {"state": "stopped", "port": 8100, "profile": null, "max_model_len": null, "started_at": null, "error": "", "owned": false},
          "policy_server_seen": true,
          "gate": {"open": false, "code": "policy_inferring|episode_imminent|unknown_client|open", "reason": "…"},
          "decision": {"allowed": false, "code": "ok|settling|insufficient_vram|vram|lock|gate_closed|evaluation_active|standby_settling|prewarm_waiting_for_policy|gpu_not_free|backoff|needs_attention|external_busy|policy_large|manual|error|asleep", "reason": "…"},
          "free_mib": 21574, "lock_held": false},
  "datasets": {"pi05__stack_plates": {"episodes": 12, "pending": 2, "annotating": 0, "done": 9, "failed": 1,
        "skipped": 0, "rejected": 0, "waiting": 1, "backlog": 0, "incomplete": 1, "fr3_fault": 1, "discarded": 0,
        "available": true, "last_processed_at": 1790000300.0, "last_error": "",
        "state": "idle|pending|annotating|awaiting_approval|error", "awaiting": "plan|changes (when awaiting_approval)",
        "stuck": 0, "source_changed": 0, "review_runs_open": 3,
        "fault": true, "fault_reasons": ["…"]}},
  "queue_depth": 1, "worker": {"pid": 99, "dataset": "…", "phase": "temporal", "note": null, "started_at": 1.0},
  "sessions": [{"group": "pi05", "task_folder": "stack_plates", "state": "running", "reported_state": "running", "crashed": false,
        "age_s": 0.4, "reason": "", "levi_enabled": true, "episode": {"no": 3, "target": 10, "counted": 2}, "last_episode": {},
        "fr3": {}, "prompt": "…", "session_id": "…", "run_id": "…", "root": "…", "reset_wait_s": 10.0, "waiting_reset_since": 1790000390.0}],
  "fr3": {"state": "ok|red|offline|missing", "detail": "…", "age_s": 0.3, "robot_mode_name": "Idle", "current_errors": [], "reasons": []},
  "attention": null,
  "labelling_paused": null,
  "loop_at": 1790000400.0,
  "frontend": {"state": "starting|ok|failed", "error": "", "attempts": 0, "ui": true, "core_port": 7881},
  "events": [{"time": 1790000000.0, "level": "info|error", "text": "…"}],
  "last_error": "",
  "resources": {"rss_mb": 27.1, "threads": 1, "cpu_percent": 0.0}
}
```

## Robot-side interfaces the service reads

- **C2 sessions**: `<root>/.eval_sessions/<group>__<task_folder>.json`, schema `levi.eval.session.v1` (`state`: `standby homing running waiting_reset fault stopped finished`). A session is `crashed` when not updated for 10 s and its process is gone, or when it is older than an hour (a recycled pid must not keep it alive). `waiting_reset_since` (epoch seconds of the first entry into `waiting_reset` of the current wait; the gate's lead is counted from it, and from the time this service first saw the session when an older client does not write it; the supervisor looks at least once a second while a session is on the robot or between episodes), `run_id` (the evaluation run being continued), `episode.no` (this session's episode number) and `episode.counted` (valid episodes of the run so far) are passed through to the page. Abort reasons of an `incomplete_*`: `fr3_fault`, `user_quit`, `interrupted`, `process_killed` (only `fr3_fault` marks the dataset faulted; all are counted by reason).
- **C3 FR3 health**: `fr3_health.json`, schema `levi.fr3.health.v1` (`updated_at_epoch` preferred, else `updated_at`). Missing or older than `fr3.stale_s` is **offline**, never a red light. `red_light` also covers no live robot state for 5 s, an unreachable controller manager for 5 s, or no Franka hardware component; the service shows it, it does not act on it.

## HTTP API (read-only, `/api/levi/live/*`)

Served by the core of any workspace that has `live/workspace.json`; elsewhere every route answers `{"enabled": false}`. No route changes anything or returns a token.

| Route | Answer |
| --- | --- |
| `GET /status` | `{"enabled", "alive", "age_s", "service": <status.json or null>, "faults": [{"dataset", "reasons": []}], "fr3_red", "blocked_runs": {"count", "waiting", "needs_person"}}`. `alive` = the pid exists and `updated_at` is under 15 s old and the state is not `stopped`. |
| `GET /sessions` | `{"enabled", "sessions": [ {…session fields above…, "dataset", "fault"} ], "fr3": {…}, "active"}`, read fresh from the robot side's files (≤ 64 sessions). |
| `GET /datasets` | `{"enabled", "datasets": {name: row}}` (the rows of `status.json`). |
| `GET /datasets/{name}` | `{"enabled", "name", "repo_id" (LEVI dataset id once registered, else null), "group", "task_folder", "task_text", "counts": {mirrored, annotating, done, failed, skipped, rejected}, "total_demos", "demos": [ {"demo", "state", "episode_index", "run_id", "completed_at", "attempts", "reason", "segments", "committed_at", "verdict": {"outcome", "events", "valid_events", "undecided", "spec", "review": "auto", "evaluated": false, "at"} | null} ] (newest first, ≤ 200), "incomplete": {"count", "fr3_fault", "reasons"}, "discarded", "current": batch in progress | null, "last_batch", "last_processed_at", "last_error", "review": "auto", "evaluated": false}`. 404 for an unknown name. |
| `GET /audit?limit=50` | `{"enabled", "audit": [ {"time", "principal": "live-auto", "tool", "run_id"?, "changeset_id"?, "revision"?, "repo_id"?, "episodes"?, "decision": "allowed|refused", "reason"?} ]}` newest first, ≤ 100. |

Link a dataset to the viewer with its `repo_id` (`local/<name>`); the verdict's run can be opened in the LEVI page by its `run_id`.

## When things go wrong

- **FR3 fault**: the client aborts the episode (`incomplete_NNNN`, `abort_reason: fr3_fault`) and the session goes to `fault`. The page marks the dataset faulted. The aborted rollout is never labelled. After the operator clears the fault the client continues the run.
- **Waiting for a person** (approver off): the wait (`awaiting`: plan or draft, the run) is saved in the dataset state; a restarted supervisor reads it back, starts neither vLLM nor a worker for it, and a worker that is started anyway finds the unapproved plan or uncommitted draft from the store alone, without a model. The wait ends when the person approves, commits or rejects, cancels the run, or the draft is gone. The model is required only right before a run executes.
- **Service or worker crash / `levi live stop`**: the batch in progress is remembered in the dataset state. The next start resumes it; LEVI's own run records hold the finished episodes, so nothing is annotated or committed twice (commits use an idempotency key). A worker stopped with `SIGTERM` pauses its runs and waits for their leases to be released; one killed with `SIGKILL` leaves leases that expire after 3 minutes.
- **A person presses Run, Resume or the task console's advance while the policy infers**: the core of a live workspace reads `live/gate.json` and refuses (`409`, "the evaluation is inferring, try again shortly") while the gate is closed. A run started while the gate was open also stops: before **every** model request the core reads the gate (a stat of the workspace marker, and the small file at most every 0.2 s; every entry that sends requests goes through the same check, `gpu.require_free`), and a closed gate makes the request fail as `GpuBusy`, which leaves the run `blocked` (with `blocked_by: gpu` and `blocked_gate: live`, the gate's own mark). The worker's own runs are not held by this (it stands down itself). A gate file nobody has refreshed for 20 s (the supervisor is gone) is read as closed by the worker always (nobody supervises it any more, and a policy server may have appeared since). A person is let through only if its last word was `idle` (no policy server, no evaluation: nothing to protect) **and** none of the policy ports it names listens right now (a read of `/proc/net/tcp`, no connection); otherwise the page says the gate file is stale and asks whether the supervisor runs. While the supervisor lives the file is rewritten at every change and at least every 4 s (also while a long stop of vLLM waits), so idle-time labelling is never stalled by this; a service that stops cleanly deletes the file.

  **Such a run goes on by itself.** A small daemon thread of the core (`levi/live/resumer.py`; it exists only in a live workspace, a stat and a small file read a second when idle) reads `live/gate.json` once a second and calls `Workbench.launch`, what a press on Resume ends in, so every check a launch makes still applies (an approved plan, a free lease, no uncommitted approved draft). It runs in the core because that is where the run, its lease and its executor live and where the gate is already read for every request; it is not the automatic approver and not a call through the dispatcher, so the approver's rule (only runs it planned itself) is untouched. It resumes only: runs that are `blocked` with the gate's mark (a model error, a failed validation, a teacher's pending decision, the GPU guardian's block, a person's pause or cancel carry none, and a pause or cancel is never undone); runs a **person** planned (principal `local-human`, the LEVI page; the worker's own runs are the worker's and a connected agent's runs are its own); runs with no pause or cancel pending and no pilot episode waiting for a person's review. And only when the gate file is **fresh and open** (a stale file is never "open" for it, whatever its last word was; a missing file neither) and has **stayed open for `gpu.resume_stable_s`** (3 s: the `episode_imminent` window is closed by the lead before that). Each run is resumed at most once per opening of the gate, one run per second. A run the gate stopped again `gpu.resume_max_bounces` (3) times in a row with no finished episode in between is left for a person (Resume in the page), and the status says so. Every resume, a launch that failed and the give-up is a line in `live/audit.jsonl` (principal `live-resume`, tool `runs.resume`, decision `auto_resumed`/`failed`/`given_up`, with the run id, the run's reason and the gate's `open`/`code`/`updated_at`/`open_for_s`). `live/blocked_runs.json` (written by the same thread) lists the runs the gate holds; `/api/levi/live/status` shows it as `blocked_runs: {count, waiting, needs_person}`: `waiting` will continue by themselves, `needs_person` will not (not a person's run, a pause pending, a pilot to review, given up). A core that restarts finds the runs the gate left blocked by one pass over the run records at start. The GPU guardian (`inference.gpu.Watch`) skips these runs.
- **A person commits to the dataset meanwhile**: the plan's baseline is stale; the worker cancels the run and plans again.
- **Model server fails**: the run blocks, the worker exits and the supervisor retries with back-off (30 s doubling to 10 min); a failed episode costs an attempt (`max_attempts`). vLLM itself failing to start is covered above (back-off, then `levi live resume`).
- **A demo that never finishes** (a leftover raw capture after a failed mux, a client that died mid-write) becomes `stuck` after `watch.stuck_s`; it is counted, listed by `levi live doctor`, and looked at again only if it changes.
- **A source replaced after it was mirrored** (a demo deleted and written again under the same number, `metadata.json` replaced after the marker): flagged `source_changed`; mirrored again if not yet annotated; an annotated one keeps the flag (what was annotated is the old content).
- **`levi live doctor`** prints RSS/threads/nice of every service process, GPU use, vLLM state, disk and run-cache sizes, queue, FR3 state, and warnings (a supervisor over its budget, a main loop that has not ticked for 5 minutes, a process not at nice 19, vLLM up with nothing to do, vLLM failing to start, an orphan vLLM, low disk, copied instead of linked files, stuck demos, replaced sources, a stale status file) and notes (a cold start takes 45-70 s).

## Measured

Fake model server, no vLLM, this machine (32 threads, RTX 5090 not used), service started with `--daemon --no-ui` (supervisor + core API; the page itself needs a production build and was not running), nothing to do, 10 minutes after a 20 s settle:

| Process | CPU (10 min) | Resident | Threads | Priority |
| --- | --- | --- | --- | --- |
| supervisor | 0.33 s = **0.055 %** of one core | **26 MiB** | 1 | nice 19 |
| core (the API process `levi live` starts) | 0.14 s = 0.023 % | 177 MiB | 8 | nice 19 |

While a batch runs (four demos, fake model): supervisor 29 MiB / 2 threads; the worker process peaks at about 215 MiB and 44 threads, the view build at 148 MiB (8 threads) plus a 134 MiB pool process, ffmpeg remuxes one thread each; all at nice 19. The worker exists only while there is a batch. A batch of 2 demos takes about 8 s end to end with the fake model; real times are dominated by the model (see the GPU measurement). `levi live doctor` reports the same numbers for a running service and warns when the supervisor exceeds 150 MiB or 12 threads.


## Limits

- The release-review spec and the generic guideline are **not evaluated** on any task; their accuracy is unknown.
- The view of a dataset is rebuilt when demos are added (LEVI's raw-capture path): stream copies, seconds for tens of episodes, growing with the dataset.
- Timeshare labelling depends on the client reporting its state: between episodes the model has about 20 s; a long session's batch is finished after it ends.
- `inotify` is not used; polling with a slow idle interval is enough and works on any file system.
- **A session that ended witnesses the policy server for later clients.** A finished or stopped session that ended after the policy server appeared leaves the gate open (the server is idle then). A later client that writes no session file (an old one, or `--no-record`) and uses the same server is not noticed: the gate stays open while it infers. Clients that write session files are covered.
- **Candidate anchored specs are skipped by training manifests everywhere, not only for live datasets.** The default of `levi/training_manifest.py` (`_anchored`) ignores an anchored review whose spec is a candidate; the shipped `plates-release-3` (and the screws anchored spec used in the evaluations) are candidates too, so a non-live dataset's training manifest no longer uses their verdicts by default; name the run (`--anchored-run`) or pass `--allow-candidate-anchored` (see TRAINING_MANIFEST.md).
- **Prewarm holds the GPU lock and about 2.2 GB (asleep) from the day the service starts, evaluation or not** (`levi live stop` releases them); the workspace rule `flock -w 14400` for other jobs then waits for as long as the service runs. Say so in `COORDINATION.md` if you use it.
- **A sleeping vLLM during an evaluation still holds the GPU lock and about 2.2 GB.** Another agent that waits on the lock (`flock -w 14400`, as the workspace rules say) can wait up to 4 hours. `levi live stop` frees it (it stops the vLLM this service started and lets go of the lock); there is no automatic release while an evaluation is live, on purpose.
- **The workspace guard refuses what it can tell.** `levi live` always refuses the `.state` of the checkout it runs from and, from a git worktree, of the main checkout (where the product LEVI runs), and the workspace `LEVI_WORKSPACE` names (unless that already is a live workspace), whatever `--adopt-workspace` says. Any other workspace that holds LEVI state but no live marker needs `--adopt-workspace`, and then it is accepted: a product LEVI started with another `LEVI_WORKSPACE` elsewhere cannot be recognised. Do not point the live service at a workspace somebody uses.
- **The first pool scan after the product LEVI restarts is slower** once this branch is merged: the pool scanner's cache version (`FACTS_VERSION`) changed, so the cache is invalidated and each video's `video_sha256` is computed again once.
- **Anchored records are stored by episode index in path-sorted order.** A demo that finishes late with a small number, or a `demo_NNNN` number that is reused, shifts the indices of the demos after it in the next view. The evaluation client writes demos in order, so this happens only after a resume or an orphan recovery; a source whose files changed is flagged `source_changed`.

## Status and known issues (2026-10-01)

Branches not merged into `main`: `feat/live-eval` (service), `fix/live-review` (review fixes and the items below), `feat/live-ui` (the `/live` page). Merge in that order; merging changes product runtime code, so the product LEVI needs a restart afterwards (only when no job runs).

Verified with fakes (no GPU): mirror, controller, automatic approver, gate (including the lead and the one-second cancel), vLLM budget planning in both start orders, back-off and `resume`, GPU lock after a supervisor crash, the wait for a person, stuck and replaced demos, resource use. Verified once with the real Qwen3.8 model on 3 development episodes **without a policy server**: the whole pipeline takes 209 s including a 63 s vLLM cold start; the gate closes 0.6-1.6 s after a session turns `running`; vLLM is released afterwards.

Open:

- **The vLLM budget has not been run on the GPU by this service.** The default 0.74 and the floor 0.725 come from two measurements on an idle GPU with a warm compile cache (0.72: 1.73 GiB KV cache, 0.74: 2.36 GiB; `live-validation.md` section 5) and the KV formula (49152 tokens × 39.8 KB = 1.82 GiB); `plan_budget` is tested against fake `nvidia-smi` numbers only. That the non-KV memory is smaller with a cold compile cache (the 0.72 that once worked) is an inference from two runs, not a separate experiment, and the two vLLM launch scripts' extra options (`--structured-outputs-config`, `--override-generation-config`) were not isolated as a cause. Not verified: vLLM first at 0.74 and a policy server at .22 afterwards (about 0.6 GB spare), and the context step-down on a real card. Run both start orders once; `vllm.gpu_memory_utilization` and `min_utilization_*` are the numbers to adjust.
- **The wake margin (300 MiB) and the standby wait (20 s) are choices, not measurements.** The wake leaves 960 MiB to spare in the measured policy-`.22` case; the standby wait lowers the chance that a cold start overlaps the first episode but cannot remove it.
- **With the approver off (`auto_approve = false`) each human gate can still cost a wake or a cold start once**: the supervisor starts a worker only when vLLM is ready, and after the person commits a draft the worker plans the release review, which needs the model. With `--prewarm` that is a wake (cheap); without it, and during an evaluation, `evaluation_active` pushes it to after the evaluation.
- **The evaluation client does not read `loop_at`**: if the service's main loop hangs while its heartbeat thread keeps `updated_at` fresh, only `levi live doctor` notices.
- **The task console's advance is refused as a whole while the gate is closed**, though it also does steps that send no request (refreshing the waiting state); the per-request check would be enough. Minor.
- **Principal names are not a credential.** The worker's principals `live-planner`/`live-auto` are exempt from the check at Run/Resume by name; the per-request check uses the worker's environment instead. A person with a principal of that name could start a run (its requests would still stop at the next request).
- **Cold-start effect on the policy is unmeasured.** The service never cold-starts vLLM while a session is `running`, `homing` or `waiting_reset`; use `--prewarm` to have it ready before an evaluation.
- **Co-residence in daemon mode, sleep and wake with a real policy server, idle release** were not verified with the real model.
- **The generic release-review spec and the generic guideline are not evaluated** (3 development episodes only: one false success). Treat every verdict as automatic and unreviewed. The comparison against the task-specific specs on development data has not been run.
- Training manifests skip candidate anchored reviews by default, but nothing validates automatic subtask segments; use `subtask_review` / `annotation.subtask_auto_share` to decide.
- A release-review run is not committable by anyone once it is archived (older than `keep_review_runs`); its verdict remains in the dataset state and the anchored records.
- Without `--auto-approve` each batch needs a person at three gates (temporal plan, draft, review plan).
- Other limits are listed above under "Limits".
