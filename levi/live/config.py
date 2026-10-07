"""The live service's configuration: one TOML file, every default here.

``levi live start`` reads ``<workspace>/live.toml`` (or ``--config``), creating
it from these defaults when it is missing, so the file a person edits always
lists every setting. Unknown keys are an error, not silently ignored: a typo
in ``[gpu]`` must not leave the GPU policy at its default.

Standard library only (the idle supervisor imports this).
"""

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Every default is either relative to the service's home (``~/.levi-live``) or
# to this checkout, or empty ("not configured"); no default names a folder of
# one machine. What a machine must say for itself (the rollout roots) has no
# default: ``preflight`` names what is missing and how to set it.
DEFAULT_HOME = "~/.levi-live"
DEFAULT_WORKSPACE = "~/.levi-live/workspace"
DEFAULT_ROOTS: list = []
# The vLLM launcher shipped with LEVI (docs/VLLM.md); a relative script path
# is resolved against the LEVI checkout.
DEFAULT_VLLM_SCRIPT = "scripts/vllm/serve.sh"
DEFAULT_VLLM_PID_DIR = "~/.levi-live/vllm"
GPU_MODES = ("auto", "timeshare", "coexist", "manual")
LOCK_UNAVAILABLE = ("continue", "wait")
CHECKOUT = Path(__file__).resolve().parents[2]


@dataclass
class Service:
    workspace: str = DEFAULT_WORKSPACE
    # Where status.json, the pid file and the single-instance lock live.
    home: str = DEFAULT_HOME
    host: str = "127.0.0.1"
    ui_port: int = 7880
    core_port: int = 7881
    # Polling: stat-only directory listings. Idle is slow, activity is fast.
    poll_idle_s: float = 15.0
    poll_active_s: float = 3.0
    # While a worker runs beside an evaluation session: how often the gate
    # (may the model work now?) is re-decided. A new episode closes it.
    gate_poll_s: float = 0.25
    heartbeat_s: float = 4.0


@dataclass
class Watch:
    # Rollout roots: <root>/<group>/<task_folder>/demo_NNNN/
    roots: list = field(default_factory=lambda: list(DEFAULT_ROOTS))
    # fnmatch patterns over "<group>/<task_folder>"; empty means all.
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    # What to do with rollouts finished before the service (or the task's
    # evaluation session) began: "skip" records them as backlog and never
    # touches them, "process" annotates them too.
    backlog: str = "skip"
    # ISO time: overrides the service's first-start time as the backlog cutoff.
    since: str = ""
    # Only tasks that have an evaluation session file (levi.enabled) count.
    require_session: bool = False
    # A completion marker must be this old (seconds) before the demo is taken.
    settle_s: float = 2.0
    # A demo that has not finished and has not changed for this long (a
    # leftover raw capture after a failed mux, a client that died mid-write)
    # is "stuck": counted and listed, no longer waited for or re-read.
    stuck_s: float = 600.0
    # Completed demos to take into one batch at most.
    batch_max_episodes: int = 40


@dataclass
class Fr3:
    # The robot side's health monitor file (docs/LIVE.md, interface C4); empty:
    # there is none, and the client's own signals are all there is.
    health_file: str = ""
    # A health file older than this is "monitor offline", not a red light.
    stale_s: float = 3.0


@dataclass
class Gpu:
    # timeshare (what auto means): the policy server (XLA_PYTHON_CLIENT_MEM_FRACTION=.22)
    # and vLLM both stay resident, but vLLM only works while the policy is
    # not inferring; a new episode cancels its requests at once. coexist:
    # the same two resident, vLLM works whenever it has work (each policy
    # inference then takes about twice as long). manual: never start vLLM,
    # use the one already listening on vllm.port.
    mode: str = "auto"
    # Ports of a robot-side policy server. Only ever looked up in the kernel's
    # socket table, never connected to.
    policy_ports: list = field(default_factory=lambda: [8000])
    # flock file shared with the machine's other GPU users (the same file they
    # take with ``flock``); empty: no shared lock.
    lock_file: str = ""
    lock_agent: str = "live"
    # When ``lock_file`` is set but cannot be opened (a missing folder that
    # cannot be made, no permission): "continue" starts vLLM without the lock
    # and says so in the status and the doctor; "wait" does not start it.
    lock_unavailable: str = "continue"
    # Session states in which the policy is inferring (timeshare sends the
    # model no request then). Homing, waiting for the reset, standby, fault,
    # stopped and finished leave the GPU to the model.
    busy_states: list = field(default_factory=lambda: ["running"])
    # The next episode follows a ``waiting_reset`` by the client's
    # ``reset_wait_s``: the gate closes this many seconds before it (the
    # policy's first inference must not meet a model request), and stays
    # closed this long after the predicted start in case the client is late.
    lead_s: float = 3.0
    lead_grace_s: float = 5.0
    # After a policy server appears or goes, wait this long before starting
    # vLLM: a policy server that is still loading preallocates its memory.
    settle_s: float = 20.0
    # Free VRAM below which a resident vLLM is put to sleep (its memory is
    # needed by someone else: a policy server that took more than expected).
    min_free_mib: int = 600
    # A policy server that holds more than this (MiB) cannot share the card
    # with an awake vLLM: vLLM sleeps while it runs. 0 checks only free VRAM.
    policy_budget_mib: int = 8500
    # A listening port is not a loaded policy server: only one that holds at
    # least this much VRAM (the measured server holds 7.7 GB) counts as already
    # on the card when vLLM's budget is planned. One that listens but holds
    # less is still loading: vLLM waits (up to ``policy_load_wait_s`` after the
    # port appeared, then plans as if alone). Memory that cannot be read is
    # planned as alone too: the conservative budget leaves room to grow.
    policy_loaded_min_mib: int = 6000
    # A policy server listens but no session vouches for it (``unknown_client``
    # keeps the gate closed): after this long the status says labelling is
    # paused for it (``labelling_paused``).
    unknown_client_pause_s: float = 600.0
    # A wake of a sleeping vLLM keeps this much free beyond the budget less what
    # it still holds (``gpumgr.ASLEEP_RESIDENT_MIB``). A start needs
    # ``vllm.margin_mib`` (vLLM sees about 930 MiB less free than nvidia-smi
    # when it starts); a wake makes no such check. Measured on the real GPU
    # (policy server at .22): a sleeping vLLM leaves 22768 MiB free, and waking
    # and serving the first requests used about 21758 MiB, so 22.7 GB free is
    # enough. With 300 the check passed at 21843 free, which would leave about
    # 85 MiB, below ``min_free_mib`` (600): vLLM would be put back to sleep at
    # once. With 850 the check needs 22393, which leaves 635 MiB after a wake
    # even right at the line (at 800: 585, just under), and the measured 22768
    # passes with about 375 MiB to spare (computed from those numbers; flapping
    # itself was not observed).
    wake_margin_mib: int = 850
    # A cold start (45-70 s of GPU load) waits until a session has been in
    # ``standby`` this long: a client leaves standby for its first episode within
    # seconds, and the cold start would overlap it. Not a guarantee (the operator
    # may press start at any time): ``--prewarm`` before the evaluation is the
    # way to have no cold start at all.
    standby_min_s: float = 20.0
    # A block that does not pass by itself (the GPU lock held by another agent,
    # somebody else's vLLM on the port, a sleeping vLLM that cannot wake for
    # lack of room) is reported in ``labelling_paused`` after this long.
    blocked_pause_s: float = 300.0
    policy_load_wait_s: float = 120.0
    # A person's run the gate stopped (``blocked``) continues by itself once the
    # gate has stayed open this long (seconds, 0.5-60: a window about to close,
    # the ``episode_imminent`` lead, must not be used), at most once per opening.
    # After ``resume_max_bounces`` stops in a row without progress (a finished
    # episode or an answer from the model in between) it is left for a person to
    # resume; 0 switches the automatic resume off (``levi/live/resumer.py``).
    resume_stable_s: float = 3.0
    resume_max_bounces: int = 3


@dataclass
class Vllm:
    # The launch and stop scripts and where they write ``vllm_<port>.pid``: the
    # contract is in docs/VLLM.md. A relative path is relative to the checkout.
    script: str = DEFAULT_VLLM_SCRIPT
    stop_script: str = DEFAULT_VLLM_SCRIPT
    pid_dir: str = DEFAULT_VLLM_PID_DIR
    port: int = 8100
    # The model the shipped script serves (LEVI_VLLM_MODEL) under the name the
    # worker asks for (LEVI_VLLM_SERVED_NAME). Scripts of your own may ignore
    # both and serve a fixed model, as long as its name is ``served_model``.
    model: str = "RedHatAI/Qwen3.8-27B-INT4"
    served_model: str = "qwen3.8-27b"
    # The memory budget (a share of vLLM's own total) is chosen at each start
    # from the VRAM that is free then (``gpumgr.plan_budget``). CALIBRATION
    # (live-validation.md section 5, results/live-validation-20261001/b/): with
    # a warm compile cache vLLM keeps 20.9 GiB of its 31.36 GiB for weights and
    # other non-KV memory, so budget u leaves u*31.36 - 20.9 GiB of KV cache
    # (0.72 -> 1.73 GiB, 0.74 -> 2.36 GiB, measured on an idle GPU), and 49152
    # tokens need 1.82 GiB: the budget must be at least about 0.7245. 0.72,
    # which worked once, was the first start with a cold cache (about 0.3 GiB
    # less non-KV memory); every later start loads the compiled graphs and
    # does not fit. Alone on the card it asks for this much (0.74 leaves
    # about 0.6 GB beside it for a policy server that starts later: not
    # verified on a real GPU)...
    gpu_memory_utilization: float = 0.74
    # ...beside a policy server that is already loaded it takes what is free
    # up to this (measured: 0.747 is vLLM's own limit there) and never below
    # the floors.
    gpu_memory_utilization_max: float = 0.747
    gpu_memory_utilization_min: float = 0.70
    # Smallest budget that serves max_model_len 49152 (the formula above with a
    # little to spare), with another process on the card and alone: the same,
    # the non-KV memory does not depend on it. A shorter context needs less
    # (``kv_bytes_per_token``).
    min_utilization_with_policy: float = 0.725
    min_utilization_alone: float = 0.725
    # KV cache bytes per token (measured 39.8 KB): a shorter context needs
    # less budget, so a start that does not fit at max_model_len tries
    # shorter contexts down to min_model_len before it gives up.
    kv_bytes_per_token: int = 39800
    min_model_len: int = 32768
    max_model_len: int = 49152
    max_images: int = 128
    max_num_seqs: int = 2
    max_num_batched_tokens: int = 4096
    # Seconds without work before vLLM is put to sleep (while an evaluation
    # is live) or stopped, releasing the GPU.
    idle_timeout_s: float = 120.0
    start_timeout_s: float = 1800.0
    # What an idle vLLM does: "auto" sleeps (level 1: 5 s down, under 1 s
    # up, 1.8 GB left) while a policy server or session is live and stops
    # otherwise; or always "sleep" / "stop".
    idle_action: str = "auto"
    # --enable-sleep-mode + VLLM_SERVER_DEV_MODE=1 (dev endpoints, loopback only).
    sleep_mode: bool = True
    # vLLM refuses a budget above its own free memory, which is about 930 MiB
    # less than nvidia-smi's free (measured: 0.747 is the limit beside a policy
    # server at .22); this much is kept free beyond the budget at start. The
    # budget already covers the growth after the first requests (22.05 -> 23.4
    # GB at 0.72).
    margin_mib: int = 1100
    # Failed starts in a row before the service stops trying and asks for a
    # person (`levi live resume`); waits 60 s doubling to 600 s between tries.
    max_start_failures: int = 3
    start_backoff_s: float = 60.0
    start_backoff_max_s: float = 600.0
    # Greedy decoding, as the evaluated configuration.
    temperature: float = 0.0
    # Use a vLLM this service did not start (another agent's) instead of
    # waiting for the port. Never stopped by this service either way; in
    # manual mode it is always used.
    adopt_external: bool = False
    # Bring vLLM up once the service starts (with no evaluation running) and
    # keep it resident: idle only puts it to sleep, it stops with the service.
    # The way to avoid any cold start while the robot evaluates.
    prewarm: bool = False


@dataclass
class Provider:
    # The LEVI provider profile the worker creates and binds in the live
    # workspace; context size and image limit follow the vLLM profile.
    name: str = "live-qwen38"
    prompt_style: str = "lean"
    requests_in_flight: int = 2


@dataclass
class Pipeline:
    # The automatic approver (docs/LIVE.md "The automatic approver"). Off:
    # the service mirrors, builds views and plans, then waits for a person.
    auto_approve: bool = False
    coarse_step_seconds: float = 0.5
    refine: str = "always"
    # Time segments (the temporal run: coarse + refine). false: the release review alone
    # labels each episode (needs anchored = true and a spec without episode.require_place).
    temporal: bool = True
    # The generic subtask vocabulary and guideline shipped with LEVI.
    guideline: str = "generic-guideline.v1.md"
    vocabulary: str = "generic-vocabulary.v1.json"
    anchored: bool = True
    anchored_spec: str = "generic-release.v1.json"
    # Valid releases an episode needs to be judged a success. The task
    # instruction is not parsed: a task that needs two placements sets 2.
    anchored_min_valid: int = 1
    # An episode whose annotation fails this many attempts is left alone.
    max_attempts: int = 2
    # Seconds a waiting approval is re-checked when auto_approve is off.
    human_recheck_s: float = 30.0
    budget_seconds: int = 86400
    # Drop a finished run's frozen input and evidence after its commit.
    cleanup: bool = True
    # Release-review runs left open (waiting_for_review) so a person can still
    # accept their outcome proposals per dataset: the newest this many keep
    # their frozen input and evidence; older ones are cancelled (their
    # verdicts stay in the dataset state and the anchored records) and cleaned.
    keep_review_runs: int = 10
    # The background labelling (the worker: time segments, the release or
    # final-state review). false: no worker is started and no model request
    # is made for it; finished rollouts are still mirrored into the dataset
    # state with their operator label and any online judgement the client
    # relayed (``eval.agent_label``), and the status file, the page and the
    # statistics work as before. ``temporal``, ``anchored`` and the spec
    # settings are then not used (and not checked); they stay as they are, so
    # switching back needs no other edit.
    background: bool = True


@dataclass
class Online:
    # The online judgement (docs/LIVE.md "Online judgement", interface C5): a
    # small HTTP endpoint of the supervisor that judges one episode from the
    # images the evaluation client sends, with the final-state spec. Off by
    # default; on, the supervisor loads the answer-checking code at start.
    enabled: bool = False
    # Loopback only: anything else is refused.
    host: str = "127.0.0.1"
    port: int = 7882
    # A spec of levi/live/specs whose rule is ``final_state`` (one question
    # per episode on its last seconds, no gripper channel).
    spec: str = "generic-final.v1.json"
    # Upper bound of the model request (seconds); past it the answer is
    # ``error`` (timeout) and the request is cut.
    timeout_s: float = 15.0
    # Largest request body accepted (MiB); a larger one is refused with 413.
    max_body_mb: float = 8.0


@dataclass
class Resources:
    nice: int = 19
    # ionice class 3 = idle
    ionice_class: int = 3
    threads: int = 2
    view_workers: int = 1
    log_max_mb: int = 5
    log_backups: int = 3
    # Cap on the regenerable run files (input, evidence) kept on disk.
    cache_max_gib: float = 20.0
    # Session reports kept in live/reports/ (the newest this many; the oldest
    # are deleted when a new one is written).
    report_keep: int = 20
    # Bound on status.json / API bodies.
    status_max_datasets: int = 64
    # Worker exits after this many idle seconds with nothing left to do.
    worker_idle_exit_s: float = 5.0


@dataclass
class Config:
    service: Service = field(default_factory=Service)
    watch: Watch = field(default_factory=Watch)
    fr3: Fr3 = field(default_factory=Fr3)
    gpu: Gpu = field(default_factory=Gpu)
    vllm: Vllm = field(default_factory=Vllm)
    provider: Provider = field(default_factory=Provider)
    pipeline: Pipeline = field(default_factory=Pipeline)
    resources: Resources = field(default_factory=Resources)
    online: Online = field(default_factory=Online)
    # Where this configuration was read from (None: built-in defaults).
    path: str | None = None

    # --- derived locations -------------------------------------------------

    @property
    def workspace(self) -> Path:
        return Path(self.service.workspace).expanduser()

    @property
    def home(self) -> Path:
        return Path(self.service.home).expanduser()

    @property
    def live_dir(self) -> Path:
        """Service state inside the workspace (not LEVI's own ``outputs/``)."""
        return self.workspace / "live"

    @property
    def captures_dir(self) -> Path:
        return self.workspace / "captures"

    @property
    def status_file(self) -> Path:
        return self.home / "status.json"

    @property
    def logs_dir(self) -> Path:
        return self.live_dir / "logs"

    @property
    def vllm_script(self) -> Path:
        return resolve_path(self.vllm.script)

    @property
    def vllm_stop_script(self) -> Path:
        return resolve_path(self.vllm.stop_script)

    @property
    def vllm_pid_dir(self) -> Path:
        return resolve_path(self.vllm.pid_dir)

    def effective_gpu_mode(self) -> str:
        """``auto`` resolved: timeshare."""
        return "timeshare" if self.gpu.mode == "auto" else self.gpu.mode

    def vllm_profile(self) -> dict:
        """The vLLM launch parameters (one profile for every mode)."""
        v = self.vllm
        return {
            "gpu_memory_utilization": v.gpu_memory_utilization,
            "max_model_len": v.max_model_len,
            "max_images": v.max_images,
            "max_num_seqs": v.max_num_seqs,
            "max_num_batched_tokens": v.max_num_batched_tokens,
            "sleep_mode": v.sleep_mode,
            "profile": "shared",
        }

    def validate(self) -> "Config":
        s, w, g, v, p, r = (
            self.service,
            self.watch,
            self.gpu,
            self.vllm,
            self.pipeline,
            self.resources,
        )
        problems = []
        if g.mode not in GPU_MODES:
            problems.append(f"gpu.mode must be one of {', '.join(GPU_MODES)}")
        if g.lock_unavailable not in LOCK_UNAVAILABLE:
            problems.append(
                f"gpu.lock_unavailable must be one of {', '.join(LOCK_UNAVAILABLE)}"
            )
        if w.backlog not in ("skip", "process"):
            problems.append("watch.backlog must be skip or process")
        if p.refine not in ("always", "auto"):
            problems.append("pipeline.refine must be always or auto")
        # With the background labelling off nothing reads the time-segment
        # and review settings: they are kept as they are and not checked.
        if p.background and not p.temporal:
            problems.extend(_review_only_problems(p))
        if p.background:
            problems.extend(_final_state_problems(p))
        problems.extend(_online_problems(self))
        if s.ui_port == s.core_port:
            problems.append("service.ui_port and service.core_port must differ")
        for name, port in (("ui_port", s.ui_port), ("core_port", s.core_port)):
            if port in (5000, 8000, 7860, 7861):
                problems.append(
                    f"service.{name} {port} is reserved (robot servers, product LEVI)"
                )
        if any(int(x) in (5000,) for x in g.policy_ports):
            problems.append("gpu.policy_ports must not list the robot server port 5000")
        if not 0.05 <= v.gpu_memory_utilization <= 0.98:
            problems.append("vllm.gpu_memory_utilization must be 0.05-0.98")
        if not (
            0.05 <= v.gpu_memory_utilization_min <= v.gpu_memory_utilization_max <= 0.98
        ):
            problems.append(
                "vllm.gpu_memory_utilization_min/max must satisfy 0.05 <= min <= max <= 0.98"
            )
        if v.min_model_len < 4096 or v.min_model_len > v.max_model_len:
            problems.append("vllm.min_model_len must be 4096..max_model_len")
        if v.idle_action not in ("auto", "sleep", "stop"):
            problems.append("vllm.idle_action must be auto, sleep or stop")
        if s.poll_idle_s < 1 or s.poll_active_s < 0.5:
            problems.append("service.poll_idle_s >= 1 and poll_active_s >= 0.5")
        if not 0 <= r.nice <= 19:
            problems.append("resources.nice must be 0-19")
        if r.threads < 1 or r.view_workers < 1:
            problems.append("resources.threads and view_workers must be >= 1")
        if not 0.5 <= g.resume_stable_s <= 60:
            problems.append("gpu.resume_stable_s must be 0.5-60 seconds")
        if g.resume_max_bounces < 0:
            problems.append(
                "gpu.resume_max_bounces must be >= 0 (0 switches the automatic resume off)"
            )
        if w.batch_max_episodes < 1:
            problems.append("watch.batch_max_episodes must be >= 1")
        if p.max_attempts < 1:
            problems.append("pipeline.max_attempts must be >= 1")
        if r.report_keep < 1:
            problems.append("resources.report_keep must be >= 1")
        # No roots is valid here (``status``, ``doctor``, ``init`` need none);
        # a command that watches refuses it in ``preflight``.
        if not all(isinstance(x, str) and x for x in w.roots):
            problems.append("watch.roots must list directory paths")
        if problems:
            raise ValueError("Invalid live configuration: " + "; ".join(problems))
        return self


def _review_only_problems(p) -> list:
    """Why ``pipeline.temporal = false`` cannot run with these settings: the
    release review must be on and its spec must not read place time segments."""
    if not p.anchored:
        return [
            (
                "pipeline.temporal = false needs pipeline.anchored = true "
                "(nothing would label the episodes)"
            )
        ]
    import json

    from levi.live import generic

    name = p.anchored_spec
    try:
        spec = json.loads(generic.text(name))
    except (OSError, ValueError) as exc:
        return [f"pipeline.anchored_spec {name!r} cannot be read: {exc}"]
    if not isinstance(spec, dict):
        return [f"pipeline.anchored_spec {name!r} cannot be read: not a JSON object"]
    if (spec.get("episode") or {}).get("require_place"):
        return [
            (
                f"pipeline.temporal = false cannot use {name}: its "
                "episode.require_place reads the place time segments; use "
                "generic-release.v3.json or generic-final.v1.json"
            )
        ]
    return []


def _final_state_problems(p) -> list:
    """The final-state judgement (``episode.rule`` ``final_state``) asks one
    question per episode, so ``pipeline.anchored_min_valid`` above 1 can never
    be met. A spec that cannot be read is reported elsewhere (or by the run)."""
    if not p.anchored or p.anchored_min_valid == 1:
        return []
    import json

    from levi.live import generic

    try:
        spec = json.loads(generic.text(p.anchored_spec))
    except (OSError, ValueError):
        return []
    if isinstance(spec, dict) and (spec.get("episode") or {}).get("rule") == (
        "final_state"
    ):
        return [
            (
                f"pipeline.anchored_min_valid = {p.anchored_min_valid} cannot be "
                f"used with {p.anchored_spec}: it asks one question per episode, "
                "so it cannot have more than one valid event; leave it at 1"
            )
        ]
    return []


RESERVED_PORTS = (5000, 8000, 7860, 7861)


def _online_problems(config) -> list:
    """What is wrong with ``[online]``. The address must be a loopback one
    whether or not the endpoint is enabled; the port, the limits and the spec
    are checked when it is (a port nothing listens on cannot clash)."""
    import ipaddress

    o, out = config.online, []
    try:
        loopback = ipaddress.ip_address(o.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        out.append(
            f"online.host {o.host!r} must be a loopback address such as "
            "127.0.0.1 (the online judgement is never served to the network)"
        )
    if not 1 <= o.timeout_s <= 120:
        out.append("online.timeout_s must be 1-120 seconds")
    if not 0.5 <= o.max_body_mb <= 64:
        out.append("online.max_body_mb must be 0.5-64")
    if not o.enabled:
        return out
    taken = {
        config.service.ui_port: "service.ui_port",
        config.service.core_port: "service.core_port",
        config.vllm.port: "vllm.port",
    }
    if not 1024 <= o.port <= 65535:
        out.append("online.port must be 1024-65535")
    elif o.port in RESERVED_PORTS or o.port in {
        int(p) for p in config.gpu.policy_ports
    }:
        out.append(f"online.port {o.port} is reserved (robot servers, product LEVI)")
    elif o.port in taken:
        out.append(f"online.port {o.port} is already {taken[o.port]}")
    out.extend(online_spec_problems(o.spec))
    return out


def online_spec_problems(name) -> list:
    """Why ``name`` cannot be the online judgement's spec: it must be a file
    of levi/live/specs whose rule is ``final_state``, ask its one question
    only (no start check, no vetoes), give its offsets in seconds and quote
    the task instruction. Standard library only (the full check of the spec
    runs when the endpoint starts)."""
    import json

    from levi.live import generic

    try:
        spec = json.loads(generic.text(name))
    except (OSError, ValueError) as exc:
        return [f"online.spec {name!r} cannot be read: {exc}"]
    if not isinstance(spec, dict):
        return [f"online.spec {name!r} cannot be read: not a JSON object"]
    if (spec.get("episode") or {}).get("rule") != "final_state":
        return [
            (
                f"online.spec {name!r} is not a final-state spec (episode.rule "
                "final_state); use generic-final.v1.json"
            )
        ]
    if spec.get("start") or spec.get("vetoes"):
        return [
            (
                f"online.spec {name!r} has a start check or vetoes: the online "
                "judgement asks the spec's one question only"
            )
        ]
    views = spec.get("views")
    if (
        not isinstance(views, list)
        or not views
        or any(not isinstance(v, dict) or not v.get("offsets_seconds") for v in views)
    ):
        return [f"online.spec {name!r} must give every view's offsets in seconds"]
    if "{task}" not in str(spec.get("question") or ""):
        return [f"online.spec {name!r}: the question has no {{task}} placeholder"]
    return []


def resolve_path(value) -> Path:
    """A configured path: ``~`` expanded; a relative one is relative to the
    LEVI checkout (where the shipped ``scripts/vllm/serve.sh`` lives)."""
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else CHECKOUT / path


def foreign_home(path) -> str | None:
    """The user whose home ``path`` points into when that is not this user's
    (``/home/<someone>/...`` or ``/Users/<someone>/...``): a configuration
    copied from another machine or account. None otherwise."""
    try:
        parts = Path(str(path)).expanduser().parts
    except (RuntimeError, ValueError):
        return None
    if len(parts) < 3 or parts[0] != "/" or parts[1] not in ("home", "Users"):
        return None
    mine = Path.home().parts
    if len(mine) >= 3 and mine[1] == parts[1] and mine[2] == parts[2]:
        return None
    return parts[2]


def _writable_dir(path: Path) -> bool:
    """``path`` is a writable folder, or the nearest folder above it that
    exists is writable (so it can be made). Nothing is created."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)


def _check(level, key, message, fix="") -> dict:
    return {"level": level, "key": key, "message": message, "fix": fix}


def checks(config: "Config", *, watching: bool = True) -> list:
    """What this configuration needs from the machine, read only: each check
    is ``{"level": ok|warn|fail, "key", "message", "fix"}``. ``fail`` means a
    service started with it cannot work; ``preflight`` refuses those.
    ``watching``: the rollout roots are needed (start, once)."""
    c, out = config, []
    where = f"{c.path or (str(c.workspace / 'live.toml'))}"
    # Rollout roots: a machine's own folders, never defaulted.
    if not c.watch.roots:
        out.append(
            _check(
                "fail" if watching else "warn",
                "watch.roots",
                "no rollout root is configured",
                f'set [watch] roots = ["/path/to/rollouts"] in {where}, or pass --root PATH',
            )
        )
    for root in c.watch.roots:
        if not Path(root).expanduser().is_dir():
            out.append(
                _check(
                    "warn",
                    "watch.roots",
                    f"watch root {root} does not exist",
                    "create it, or correct [watch] roots (the service waits for it)",
                )
            )
    # The vLLM scripts and their pid folder (not used in manual mode).
    if c.gpu.mode != "manual":
        for key, path in (
            ("vllm.script", c.vllm_script),
            ("vllm.stop_script", c.vllm_stop_script),
        ):
            if not path.is_file():
                out.append(
                    _check(
                        "fail",
                        key,
                        f"{key} {path} does not exist",
                        f"point [vllm] {key.split('.')[1]} at a launcher that follows "
                        "docs/VLLM.md (the shipped one is scripts/vllm/serve.sh), "
                        'or use gpu.mode = "manual" with a vLLM you run yourself',
                    )
                )
            elif not os.access(path, os.X_OK):
                out.append(
                    _check(
                        "fail",
                        key,
                        f"{key} {path} is not executable",
                        f"chmod +x {path}",
                    )
                )
            else:
                out.append(_check("ok", key, f"{key} {path}"))
        if not _writable_dir(c.vllm_pid_dir):
            out.append(
                _check(
                    "fail",
                    "vllm.pid_dir",
                    f"vllm.pid_dir {c.vllm_pid_dir} cannot be written",
                    "choose a folder you can write ([vllm] pid_dir); the launcher "
                    "writes vllm_<port>.pid there",
                )
            )
    # The shared GPU lock.
    if not c.gpu.lock_file:
        out.append(
            _check(
                "ok",
                "gpu.lock_file",
                "no shared GPU lock configured (gpu.lock_file is empty): other "
                "GPU users on this machine are not coordinated with",
            )
        )
    else:
        lock = Path(c.gpu.lock_file).expanduser()
        usable = (
            os.access(lock, os.R_OK | os.W_OK)
            if lock.exists()
            else _writable_dir(lock.parent)
        )
        if usable:
            out.append(_check("ok", "gpu.lock_file", f"GPU lock {lock}"))
        else:
            out.append(
                _check(
                    "fail" if c.gpu.lock_unavailable == "wait" else "warn",
                    "gpu.lock_file",
                    f"GPU lock {lock} cannot be opened or created: "
                    + (
                        "vLLM will not start (gpu.lock_unavailable = wait)"
                        if c.gpu.lock_unavailable == "wait"
                        else "vLLM starts without it (gpu.lock_unavailable = continue)"
                    ),
                    'fix the path or its permissions, or set gpu.lock_file = ""',
                )
            )
    # Paths that point into another user's home: a configuration copied from
    # another machine or account.
    named = [
        ("service.workspace", c.service.workspace),
        ("service.home", c.service.home),
        ("fr3.health_file", c.fr3.health_file),
        ("gpu.lock_file", c.gpu.lock_file),
        ("vllm.script", c.vllm.script),
        ("vllm.stop_script", c.vllm.stop_script),
        ("vllm.pid_dir", c.vllm.pid_dir),
        *(("watch.roots", r) for r in c.watch.roots),
    ]
    for key, value in named:
        who = foreign_home(value) if value else None
        if who:
            out.append(
                _check(
                    "warn",
                    key,
                    f"{key} {value} points into the home of another user ({who})",
                    f"a configuration from another machine or account? correct {key} in {where}",
                )
            )
    return out


def preflight(config: "Config", *, watching: bool = True) -> list:
    """Refuse (ValueError) a configuration a service cannot run with, naming
    each problem and its fix; returns the warnings otherwise."""
    found = checks(config, watching=watching)
    failed = [c for c in found if c["level"] == "fail"]
    if failed:
        raise ValueError(
            "the live configuration cannot run: "
            + "; ".join(
                c["message"] + (f" (fix: {c['fix']})" if c["fix"] else "")
                for c in failed
            )
        )
    return [c for c in found if c["level"] == "warn"]


def _fill(cls, data: dict, where: str):
    """A dataclass from a TOML table; unknown keys and wrong types are errors."""
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ValueError(f"Unknown key(s) in [{where}]: {', '.join(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if dataclasses.is_dataclass(default):
            if not isinstance(value, dict):
                raise ValueError(f"[{where}.{name}] must be a table")
            kwargs[name] = _fill(type(default), value, f"{where}.{name}")
            continue
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif isinstance(default, list):
            ok = isinstance(value, list)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ValueError(
                f"{where}.{name} must be {type(default).__name__}, got {value!r}"
            )
        kwargs[name] = value
    return cls(**kwargs)


def from_dict(data: dict, path: str | None = None) -> Config:
    known = {f.name for f in dataclasses.fields(Config)} - {"path"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"Unknown table(s): {', '.join(unknown)}")
    sections = {
        name: _fill(type(getattr(Config(), name)), value, name)
        for name, value in data.items()
    }
    return Config(**sections, path=path).validate()


def load(path=None, workspace=None) -> Config:
    """The configuration at ``path``, else ``<workspace>/live.toml`` when it
    exists, else the defaults. ``workspace`` overrides ``service.workspace``."""
    data: dict = {}
    source = None
    if path is None and workspace is not None:
        candidate = Path(workspace).expanduser() / "live.toml"
        path = candidate if candidate.is_file() else None
    if path is not None:
        source = str(Path(path).expanduser())
        try:
            data = tomllib.loads(Path(source).read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Cannot read {source}: {exc}") from exc
    config = from_dict(data, source)
    if workspace is not None:
        config.service.workspace = str(Path(workspace).expanduser().resolve())
    return config


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(x) for x in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render(config: Config | None = None) -> str:
    """The configuration as a TOML file with a comment per table."""
    config = config or Config()
    notes = {
        "service": "ports, polling and where the service keeps its state",
        "watch": "which rollout directories to follow",
        "fr3": "the FR3 health file the robot side writes",
        "gpu": "when the local model may use the GPU",
        "vllm": "how the local vLLM server is launched",
        "provider": "the LEVI model profile created for the live workspace",
        "pipeline": "what runs on each batch; auto_approve is off by default",
        "resources": "keeping the service light",
        "online": "the online judgement of one episode (interface C5); off by default",
    }
    lines = ["# LEVI live annotation service. docs/LIVE.md describes every setting.\n"]

    def table(name, obj):
        lines.append(f"[{name}]  # {notes.get(name, '')}".rstrip(" #"))
        nested = []
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if dataclasses.is_dataclass(value):
                nested.append((f"{name}.{f.name}", value))
            else:
                lines.append(f"{f.name} = {_toml_value(value)}")
        lines.append("")
        for child, value in nested:
            table(child, value)

    for f in dataclasses.fields(config):
        if f.name == "path":
            continue
        table(f.name, getattr(config, f.name))
    return "\n".join(lines)
