"""Chunk acceleration v3 entry point (experimental): the phase clock for plain, RTC and VLASH inference.

The existing FR3 collector runs unchanged: every gate, the clamp and its 0.10 m / 0.35 rad jump check, the keyboard, the
recording. This file only installs hooks (design 2026-11-01, section 4) and checks, at every start, that the client
files they wrap still look like the ones they were written for.

    plain  hook A  rc._query_policy   chunk -> placeholder plan (PlainPhase.on_query)
           hook B  rc._clamp_action   the engine's target for this tick goes through the real clamp
    rtc    hook C  rc._rtc_chunker    PhaseRtcChunker over the user's own request closure (args.rtc stays True)
           hook B  rc._clamp_action   action untouched; executed / rejected and the trace only
    vlash  hook D  collector.main(scheduler_factory=..., server_validator=vrt.check_server)
           hook B  as for rtc
    all    hook E  rc._run_episode, rc.EvalLog.__init__, RolloutRecorder.{stage, _session_metadata, close, abort}

Recorded: policy.chunk_accel (settings, parameters, code shas, code_drift) in every demo and in the session's eval log
header; eval.chunk_accel (that episode's counts, the trace file with its sha256; aborted episodes included, in
eval.abort_detail.chunk_accel); one trace file per episode next to the eval log: <eval log>.phase/ep_NNNN.jsonl.

Startup refuses: --control-hz other than 10, step-by-step, an unbinarized gripper, lambda outside [1, 3], alpha outside
{0.005, 0.01}, a mode that disagrees with --rtc, client files that fail the AST check (client_check.py), and a VLASH run
on a runtime other than the reviewed bytes. run_collect_accel.py (v1/v2) is a different file and is untouched.
"""

import dataclasses
import json
import math
import os
import sys
import time
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).absolute().parent


def openpi_root():
    """The openpi checkout this file runs in (lab/chunk_accel/ is two levels below it). CHUNK_ACCEL_OPENPI_ROOT names
    one explicitly (the end-to-end tests run this file from the LEVI source tree); the root in use is logged."""
    explicit = os.environ.get("CHUNK_ACCEL_OPENPI_ROOT")
    return Path(explicit).absolute() if explicit else HERE.parents[1]


ROOT = openpi_root()
sys.path.insert(0, str(ROOT / "examples/fr3_local"))
sys.path.insert(0, str(HERE))
import client_check
import phase
import run_robotiq_client_collect_data as collector
import schedulers
import tyro

MODES = ("plain", "rtc", "vlash")
HZ = 10  # the policy's own rate: the caps, rows and delays are in 0.1 s steps
LAMBDA_RANGE = (
    1.0,
    3.0,
)  # PhaseParams validates the same range; the page offers 2.0 / 2.5 / 3.0
OWN_FILES = (
    "phase.py",
    "schedulers.py",
    "run_collect_phase.py",
    "client_check.py",
)  # the sha256 of these go into the header next to the client's
RECEIPT_NAME = "installation-receipt.json"
FLUSH_EVERY_TICKS = (
    10  # a trace line also flushes at once when it is not a plain tick line
)
STEP_NOTES = (
    "STEP",
    "ROT",
)  # the clamp's own jump notes: the client refuses the action and re-queries


@dataclasses.dataclass
class Args(collector.Args):
    # plain | rtc | vlash. rtc needs --rtc (+ --rtc-execute-horizon), vlash needs --vlash-overlap-steps; plain uses
    # --open-loop-horizon. The page writes all of them next to --phase-mode.
    phase_mode: str = "plain"
    # rows per tick at most; [1, 3]. The page offers 2.0 / 2.5 / 3.0 and the page default is 3.0 (2026-11-02); the page
    # always writes this flag, so 2.0 here is only the default of a hand-typed command line.
    phase_lambda_max: float = 2.0
    # the controller's setpoint filter: must match `set_clip.py --show` (0.005 or 0.01)
    phase_alpha: float = 0.005
    vlash_overlap_steps: int = 2
    inference_mode: str = dataclasses.field(default="plain", init=False)


# ---- validation and the start checks -----------------------------------------------------------------------------


def validate(args) -> "phase.PhaseParams":
    """The run's parameters, or ValueError. Nothing has connected to a server or a robot yet."""
    mode = args.phase_mode
    if mode not in MODES:
        raise ValueError(f"--phase-mode must be one of {MODES}, got {mode!r}")
    # The rule's rows, delays and caps are for the policy's own 10 Hz; the gripper test is for binarized commands; and
    # every planned action must be executed (step-by-step skips break the phase bookkeeping).
    if args.control_hz != HZ or args.step_by_step or not args.binarize_gripper:
        raise ValueError(
            "chunk acceleration v3 needs --control-hz 10, --no-step-by-step and --binarize-gripper"
        )
    lam, alpha = args.phase_lambda_max, args.phase_alpha
    if (
        isinstance(lam, bool)
        or not isinstance(lam, (int, float))
        or not math.isfinite(lam)
        or not LAMBDA_RANGE[0] <= lam <= LAMBDA_RANGE[1]
    ):
        raise ValueError(
            f"--phase-lambda-max must be in {list(LAMBDA_RANGE)}, got {lam!r}"
        )
    if (
        isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not any(
            math.isclose(alpha, a, rel_tol=0, abs_tol=1e-9) for a in phase.ALPHAS
        )
    ):
        raise ValueError(
            f"--phase-alpha must be one of {list(phase.ALPHAS)} (it must match `set_clip.py --show`), got {alpha!r}"
        )
    # RELAXED[2026-11-01] E11 was: run_collect_accel.py:126-129 refused RTC and VLASH (plain inference only); this entry
    # accepts both, each through its own hook. v1/v2 keep the refusal in their own file.
    if bool(args.rtc) != (mode == "rtc"):
        raise ValueError(
            "--phase-mode rtc needs --rtc, and plain and vlash must not have it (the RTC and VLASH servers differ)"
        )
    if mode == "plain" and int(args.open_loop_horizon) < 1:
        raise ValueError("--open-loop-horizon must be at least 1")
    if mode == "rtc" and int(args.rtc_execute_horizon) < 1:
        raise ValueError("--rtc-execute-horizon must be at least 1")
    if mode == "vlash" and (
        type(args.vlash_overlap_steps) is not int or args.vlash_overlap_steps < 1
    ):
        raise ValueError("--vlash-overlap-steps must be an integer of at least 1")
    return phase.PhaseParams.for_page(float(lam), float(alpha))


def load_receipt(path=None):
    """The installer's receipt next to this file, or None when there is none (a run from the source tree)."""
    path = Path(path) if path else HERE / RECEIPT_NAME
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def drift_names(shas, own, receipt):
    """Names (client files, then our own files) whose sha256 differs from the receipt; None without a usable receipt."""
    if not receipt or "client" not in receipt:
        return None
    names = [
        name
        for name in client_check.CLIENT_FILES
        if receipt["client"].get(name, {}).get("sha256") != shas.get(name)
    ]
    recorded = {row.get("name"): row.get("sha256") for row in receipt.get("files", [])}
    names += [name for name in OWN_FILES if recorded.get(name) != own.get(name)]
    return names


def load_vrt(root=None, reviewed=client_check.VRT_REVIEWED_SHA256):
    """The VLASH runtime from the exact bytes whose sha256 is the reviewed one (hashed, then compiled: the file cannot
    change in between). Loaded under its own module name, as lab/vlash/test_runtime.py does."""
    path = Path(root or ROOT) / client_check.CLIENT_FILES["vrt"]
    data = path.read_bytes()
    sha = client_check.sha256_bytes(data)
    if sha != reviewed:
        raise ValueError(
            f"{path}: sha256 {sha[:12]}.. is not the reviewed VLASH runtime {reviewed[:12]}..; the VLASH scheduler of "
            "the phase clock is written against the reviewed bytes (re-review it, then update "
            "client_check.VRT_REVIEWED_SHA256)"
        )
    module = types.ModuleType("vlash_runtime_phase")
    module.__file__ = str(path)
    sys.modules["vlash_runtime_phase"] = module
    exec(compile(data, str(path), "exec"), module.__dict__)  # noqa: S102 - the reviewed bytes, hashed just above
    return module


def _loaded_from_root(root):
    """The modules in use must be the files the AST check read (a same-named module earlier on sys.path would not be)."""
    expected = {
        collector: "coll",
        getattr(collector, "rc", None): "rc",
        getattr(getattr(collector, "rc", None), "_rtc", None): "rtcc",
    }
    for module, name in expected.items():
        loaded = getattr(module, "__file__", None)
        if loaded is None:
            continue
        want = (Path(root) / client_check.CLIENT_FILES[name]).resolve()
        if Path(loaded).resolve() != want:
            raise ValueError(
                f"the {name} module in use is {loaded}, not {want}: another copy shadows the checked client"
            )


def startup(mode, root=None):
    """Everything checked before a hook is installed: the client's AST, which files are loaded, the shas, the drift
    against the installer's receipt, and (VLASH) the reviewed runtime. Returns the static part of the header and, for
    VLASH, the loaded runtime. ValueError refuses the start."""
    root = Path(root or ROOT)
    pinned = getattr(
        schedulers, "REVIEWED_VRT_SHA256", client_check.VRT_REVIEWED_SHA256
    )
    if pinned != client_check.VRT_REVIEWED_SHA256:
        raise ValueError(
            "schedulers.REVIEWED_VRT_SHA256 and client_check.VRT_REVIEWED_SHA256 disagree: update both together"
        )
    shas = client_check.check_client(root)
    _loaded_from_root(root)
    own = {
        name: client_check.sha256_file(HERE / name)
        for name in OWN_FILES
        if (HERE / name).is_file()
    }
    receipt = load_receipt()
    drift = drift_names(shas, own, receipt)
    vrt = None
    reviewed = shas["vrt"] == client_check.VRT_REVIEWED_SHA256
    if mode == "vlash":
        vrt = load_vrt(root)  # refuses any other bytes
    # Plain and RTC never touch the VLASH runtime, so a different one is recorded (code_drift) but does not stop them.
    return {
        "shas": {**own, **shas},
        "code_drift": drift is None or bool(drift) or not reviewed,
        "receipt": {
            "found": drift is not None,
            "drift": drift,
            "vrt_reviewed": reviewed,
        },
        "root": str(root),
        "vrt_module": vrt,
    }


# ---- trace writer (design section 8) -----------------------------------------------------------------------------


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def jsonable(value):
    """A JSON round trip: what the metadata writers can dump, whatever numpy types an engine summary holds."""
    return json.loads(json.dumps(value, default=_json_default))


class PhaseTrace:
    """One episode's JSON-lines file. A write failure is a warning, never a stop (the arm does not depend on it)."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.lines, self.error = path, 0, None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for n in range(
            100
        ):  # a name is never reused: a second process or a restart gets the next one
            candidate = (
                path if n == 0 else path.with_name(f"{path.stem}_{n}{path.suffix}")
            )
            try:
                fd = os.open(candidate, flags, 0o644)
            except FileExistsError:
                continue
            self.path = candidate
            self._file = os.fdopen(fd, "wb")
            return
        raise OSError(f"no free trace name next to {path}")

    def write(self, record):
        if self.error is not None or self._file is None:
            return
        try:
            line = json.dumps(record, default=_json_default, separators=(",", ":"))
            self._file.write(line.encode() + b"\n")
            self.lines += 1
        except (OSError, TypeError, ValueError) as exc:
            self._fail(exc)

    def flush(self):
        if self.error is None and self._file is not None:
            try:
                self._file.flush()
            except OSError as exc:
                self._fail(exc)

    def _fail(self, exc):
        self.error = f"{type(exc).__name__}: {exc}"[:200]
        print(f"      WARNING: chunk accel trace {self.path} stopped: {self.error}")

    def close(self):
        """Closes the file; returns {file, sha256, lines} read back from disk (the sha of what is really there)."""
        file, self._file = self._file, None
        info = {"file": str(self.path)}
        try:
            if file is not None:
                file.flush()
                os.fsync(file.fileno())
                file.close()
            data = self.path.read_bytes()
            info.update(sha256=client_check.sha256_bytes(data), lines=data.count(b"\n"))
        except OSError as exc:
            info.update(sha256=None, lines=self.lines, read_error=str(exc)[:200])
        if self.error is not None:
            info["write_error"] = self.error
        return info


# ---- the run: state shared by the hooks --------------------------------------------------------------------------


class Run:
    """One launcher run (the hooks live as long as the process): static settings, the registry of the episode's
    scheduler, the trace, and the summary waiting for the recorder's close or abort."""

    def __init__(self, args, params, mode, info):
        self.args, self.params, self.mode, self.info = args, params, mode, info
        self.eval_path = self.trace_dir = None
        self.seq = 0
        self.sched = None
        self.aperture = None
        self.obs_t = None  # time.monotonic() where the client read this tick's observation (hook E), plain's time stamp
        self.live_args = None  # the client's own Args as the last clamp call saw them (apply_task_min_z mutates them)
        self.clamp = None  # the client's real rc._clamp_action, captured by install()
        self.ticks = 0
        self.trace = None
        self.summary = None
        self.static = jsonable(
            {
                "rule": phase.PHASE_RULE,
                "mode": mode,
                "lambda_max": params.lambda_max,
                "alpha": params.alpha,
                "params": params.as_dict(),
                "derived": params.derived(HZ),
                "shas": info["shas"],
                "code_drift": info["code_drift"],
                "receipt": info["receipt"],
                "relaxed_tag": phase.RELAXED_TAG,
                "experimental": True,
                "root": info["root"],
            }
        )

    @property
    def engine(self):
        return getattr(self.sched, "engine", None)

    def settings(self):
        return {
            **self.static,
            "trace_dir": str(self.trace_dir) if self.trace_dir else None,
        }

    # -- registry (hooks C and D, and the plain scheduler)
    def register(self, sched):
        if not hasattr(sched, "engine"):
            raise RuntimeError(
                f"{type(sched).__name__} has no .engine: the launcher reads the trace and summary from it"
            )
        self.sched = sched
        # invariant I7: the engine shrinks a blend offset until the client's own workspace clamp (min_z / max_xy / rpy
        # ranges of THIS task) does not touch it, so that acceleration never causes a clamp, a VLASH reset or a
        # second query
        setter = getattr(sched.engine, "set_workspace", None)
        if callable(setter) and self.clamp is not None:
            setter(self.workspace_hit)
        return sched

    def workspace_hit(self, target):
        """True when the client's clamp would change the pose of ``target`` (a clamp note; with current == target there
        is no STEP / ROT note, so any note is a workspace one). Reads the live Args: apply_task_min_z changes min_z
        per task, after the engine was built."""
        args_ = self.live_args if self.live_args is not None else self.args
        t = np.asarray(target, np.float32)
        return bool(self.clamp(t, t, args_)[1])

    def wrap_factory(self, factory):
        """A scheduler factory (rc._rtc_chunker's or collector.main's) that registers what it builds."""

        def registered(client, args_, prompt):
            return self.register(factory(client, args_, prompt))

        return registered

    # -- episode lifecycle (hook E)
    def begin_episode(self):
        if self.eval_path is None:
            raise RuntimeError(
                "no EvalLog was created, so there is no directory for the trace; chunk acceleration v3 refuses to "
                "run an episode without it (keep --eval-dir or --eval-log set)"
            )
        self.seq += 1
        self.sched, self.aperture, self.obs_t, self.ticks, self.summary = (
            None,
            None,
            None,
            0,
            None,
        )
        try:
            self.trace = PhaseTrace(self.trace_dir / f"ep_{self.seq:04d}.jsonl")
        except OSError as exc:
            self.trace = None
            print(
                f"      WARNING: chunk accel trace not written for this episode: {exc}"
            )
        if self.mode == "plain":
            self.register(
                schedulers.PlainPhase(self.params, int(self.args.open_loop_horizon))
            )

    def end_episode(self, outcome, recorder):
        """Always runs (finally): the last records, the summary, the trace closed with its sha256. Never raises."""
        try:
            self._emit_records(final=True)
            demo = getattr(getattr(recorder, "demo_dir", None), "name", None)
            if self.trace is not None:
                self.trace.write({"kind": "end", "demo": demo, "outcome": outcome})
            info = {"file": None, "sha256": None, "lines": 0}
            if self.trace is not None:
                info = self.trace.close()
                self.trace = None
                print(
                    f"      chunk accel v3 trace: {info['file']} ({info['lines']} lines, sha256 "
                    f"{str(info['sha256'])[:12]})"
                )
            body = {}
            if self.engine is not None:
                try:
                    body = dict(self.engine.summary())
                    # the scheduler-side counters (starved_ticks, early_triggers, delays_rows, plain_end_holds, ...)
                    # know their own episode best, so they win a key both report
                    pipeline = getattr(self.sched, "pipeline_stats", None)
                    if callable(pipeline):
                        body.update(pipeline())
                except Exception as exc:  # noqa: BLE001 - the summary must not hide the episode's own outcome
                    body = {"summary_error": f"{type(exc).__name__}: {exc}"[:200]}
            self.summary = jsonable(
                {
                    **body,
                    "rule": phase.PHASE_RULE,
                    "mode": self.mode,
                    "episode_in_session": self.seq,
                    "outcome": outcome,
                    "trace": info,
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"      WARNING: chunk accel summary failed: {type(exc).__name__}: {exc}"
            )
            self.summary = {
                "rule": phase.PHASE_RULE,
                "mode": self.mode,
                "summary_error": str(exc)[:200],
            }

    def take_summary(self):
        summary, self.summary = self.summary, None
        return summary

    # -- per tick (hook B)
    def after_action(self, out, notes):
        """The client's clamp has answered: tell the engine, then write what the engine and this hook recorded."""
        rejected = any(str(n).startswith(STEP_NOTES) for n in notes)
        engine = self.engine
        if engine is not None:
            if rejected:
                engine.rejected()
            else:
                engine.executed(out)
        self.ticks += 1
        self._emit_records()
        if self.trace is not None:
            self.trace.write(
                {
                    "kind": "sent",
                    "k": self.ticks,
                    "t": time.monotonic(),
                    "out": np.asarray(out).tolist(),
                    "notes": [str(n) for n in notes],
                    "rejected": rejected,
                }
            )
            if self.ticks % FLUSH_EVERY_TICKS == 0:
                self.trace.flush()

    def _emit_records(self, final=False):
        """Drain the engine's trace lines into the file. Flush at once for anything but a plain tick line (a chunk, a
        request, a gripper event), so a crash loses at most FLUSH_EVERY_TICKS ticks."""
        engine = self.engine
        if engine is None:
            return
        try:
            records = list(engine.drain_records())
            more = getattr(
                self.sched, "drain_records", None
            )  # request / reply lines only the scheduler knows
            if callable(more):
                records += list(more())
        except Exception as exc:  # an engine that cannot drain is still an engine whose error will surface itself
            if final:
                print(
                    f"      WARNING: chunk accel records not drained: {type(exc).__name__}: {exc}"
                )
                return
            raise
        if self.trace is None:
            return
        urgent = final
        for record in records:
            self.trace.write(record)
            urgent = urgent or record.get("kind", record.get("type")) != "tick"
        if urgent:
            self.trace.flush()


# ---- hooks ---------------------------------------------------------------------------------------------------------


def install(args, params, info) -> Run:
    """Wrap the collector's client functions for the chosen mode; returns the run. ``info`` is startup()'s result."""
    mode = args.phase_mode
    run = Run(args, params, mode, info)
    rc, recorder = collector.rc, collector.RolloutRecorder
    run.clamp = (
        rc._clamp_action
    )  # the client's real clamp, before hook B replaces the module global
    need = {
        "plain": ("PlainPhase",),
        "rtc": ("wrap_rtc_chunker",),
        "vlash": ("phase_vlash_factory",),
    }[mode]
    missing = [name for name in need if not hasattr(schedulers, name)]
    if missing:
        raise ValueError(
            f"schedulers.py has no {', '.join(missing)}: mode {mode} is not available"
        )
    query, clamp, run_episode, rtc_chunker = (
        rc._query_policy,
        rc._clamp_action,
        rc._run_episode,
        rc._rtc_chunker,
    )
    eval_init, metadata = rc.EvalLog.__init__, recorder._session_metadata
    close, abort, stage = recorder.close, recorder.abort, recorder.stage

    def phased_query(client, obs, state, prompt):  # hook A (plain)
        if run.sched is None:
            raise RuntimeError("plain phase clock: a policy query outside an episode")
        chunk = query(client, obs, state, prompt)  # blocks for the inference
        return run.sched.on_query(chunk, state, run.aperture, run.obs_t)

    def checked_action(action, current, args_):  # hook B
        run.live_args = args_
        if mode == "plain":
            # the loop's row is the placeholder plan; this tick's target comes from the engine, then the real clamp
            if run.sched is None:
                raise RuntimeError(
                    "plain phase clock: an action check outside an episode"
                )
            action = run.sched.on_action(current, run.aperture, run.obs_t)
        out, notes = clamp(action, current, args_)
        run.after_action(out, notes)
        return out, notes

    def staged(
        self, obs
    ):  # hook E: the aperture and the time of this tick (plain), before the recorder's own stage
        run.obs_t = (
            time.monotonic()
        )  # rc:1575 calls this right after env.get_observation, before any inference
        try:
            run.aperture = float(obs["robot_state"]["gripper_position"])
        except (KeyError, TypeError, ValueError):
            run.aperture = None
        return stage(self, obs)

    def eval_log_init(self, *a, **k):
        eval_init(self, *a, **k)
        run.eval_path = Path(self.path)
        run.trace_dir = run.eval_path.with_name(run.eval_path.stem + ".phase")
        self.header["chunk_accel"] = run.settings()

    def session_metadata(self):
        md = metadata(self)
        md["policy"]["chunk_accel"] = run.settings()
        return md

    def close_(
        self, outcome, verdict_by, counted, stats, discard_if_invalid=False, extra=None
    ):
        summary = run.take_summary()
        if summary is not None:
            extra = {**(extra or {}), "chunk_accel": summary}
        return close(
            self,
            outcome,
            verdict_by,
            counted,
            stats,
            discard_if_invalid=discard_if_invalid,
            extra=extra,
        )

    def abort_(self, reason, stats=None, detail=None):
        summary = run.take_summary()
        if summary is not None:
            detail = {**(detail or {}), "chunk_accel": summary}
        return abort(self, reason, stats, detail)

    def run_episode_(*a, **kw):
        run.begin_episode()
        outcome = None
        try:
            result = run_episode(*a, **kw)
            outcome = result[0]
            return result
        except BaseException as exc:
            outcome = f"exception:{type(exc).__name__}"
            raise
        finally:
            run.end_episode(outcome, kw.get("recorder"))

    rc._clamp_action, rc._run_episode = checked_action, run_episode_
    rc.EvalLog.__init__ = eval_log_init
    recorder._session_metadata, recorder.close, recorder.abort, recorder.stage = (
        session_metadata,
        close_,
        abort_,
        staged,
    )
    if mode == "plain":
        rc._query_policy = phased_query
    elif (
        mode == "rtc"
    ):  # hook C: args.rtc stays True, so the client's own server check and log headers stand
        rc._rtc_chunker = run.wrap_factory(
            schedulers.wrap_rtc_chunker(rtc_chunker, params)
        )
    return run


def main(args):
    params = validate(args)
    info = startup(args.phase_mode)
    args.inference_mode = "vlash" if args.phase_mode == "vlash" else "plain"
    run = install(args, params, info)
    s = run.static
    print(
        f"chunk acceleration v3 (phase clock): mode={s['mode']} lambda_max={s['lambda_max']} alpha={s['alpha']} "
        f"openpi={info['root']} code_drift={s['code_drift']} shas "
        + " ".join(f"{k}={v[:8]}" for k, v in sorted(s["shas"].items()))
    )
    if info["receipt"]["drift"]:
        print(
            f"WARNING: {', '.join(info['receipt']['drift'])} changed since the install receipt; "
            "re-run the golden tests (design section 14) before trusting this run."
        )
    elif info["receipt"]["drift"] is None:
        print(
            "WARNING: no install receipt next to this file; code_drift is recorded as true."
        )
    if not info["receipt"]["vrt_reviewed"]:
        print(
            "WARNING: the VLASH runtime is not the reviewed one (VLASH runs are refused, plain and RTC are not)."
        )
    if args.phase_mode == "vlash":
        vrt = info["vrt_module"]
        factory = run.wrap_factory(schedulers.phase_vlash_factory(params, vrt))
        collector.main(
            args, scheduler_factory=factory, server_validator=vrt.check_server
        )
    else:
        collector.main(args)


if __name__ == "__main__":
    main(tyro.cli(Args))
