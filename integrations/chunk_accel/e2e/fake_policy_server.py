"""A FAKE policy server for the chunk-acceleration end-to-end tests. No JAX, no GPU, no checkpoint.

It speaks openpi's websocket + msgpack protocol (metadata frame first, then one reply per observation) and plays a
pick-and-place SCRIPT like a policy that reads the state: from the observation's pose (and commanded gripper) it finds
where on the script the arm is, and answers with the next ten rows of the script (row spacing = 0.1 s of nominal time),
a few rows ahead of the measured pose because the real arm lags its targets.

Three metadata flavours, one per client mode:
  plain   the base contract (config, euler rebase, 10-row chunks);
  rtc     plus the RTC block (protocol version 1); a request carrying ``rtc_prev_actions`` gets that prefix back
          (the first ``rtc_inference_delay`` rows are the previous plan's), as a guided chunk would;
  vlash   the reviewed state-conditioned contract (config pi05_fr3_all_state, ...) and the VLASH block with the
          overlap the client asked for; a request carrying ``vlash_future_state`` is answered FROM that state.

What the script puts in front of the phase clock (design section 7, WP3): a descent to the grasp and to the place, the
close and the open at exact rows (the policy is asked again before and after them, so the flips sit in the unexecuted
tails of chunks too), a transport whose rx crosses +pi, chunk-to-chunk offsets of a few mm with injected 46 / 75 mm
switch jumps, 25 % of the chunks with a phantom gripper flip in their last rows, and reply latency drawn like the
real one (median 83 ms, 10 % between 177 and 261 ms).

Ports: only 8041-8049, never 5000, 5001, 5100, 7470, 8000, 8010. Binds 127.0.0.1.

    python3 fake_policy_server.py --port 8041 --mode rtc

Needs websockets and openpi_client (the openpi virtualenv); the script and the chunk maker need only numpy.
"""

import argparse
import socket
import threading
import time

import numpy as np

FORBIDDEN_PORTS = (5000, 5001, 5100, 7470, 8000, 8010)
POLICY_PORTS = tuple(range(8041, 8050))
HORIZON = 10
HOME = (
    0.45,
    0.0,
    0.18,
    3.119139,
    0.008288,
    -0.883962,
)  # same start as lag_robot_server.DEFAULT_START_EULER
GRIPPER_CONVENTION = (
    "1.0=open, 0.0=closed, commanded not measured"  # lab/vlash/runtime.py:capabilities
)

BASE_META = {
    "config": "pi05_fr3_fake_phase",
    "rebase_euler_dim": 3,
    "rebase_euler_offset": float(np.pi),
    "action_dim": 7,
    "action_horizon": HORIZON,
    "ee_frame": "franka_hand_tcp",
    "checkpoint_dir": "checkpoints/fake_phase",
}


def assert_port(port):
    if int(port) in FORBIDDEN_PORTS or int(port) not in POLICY_PORTS:
        raise SystemExit(
            f"refusing port {port}: the fake policy server uses {POLICY_PORTS[0]}-{POLICY_PORTS[-1]} only, never "
            f"{FORBIDDEN_PORTS}"
        )
    return int(port)


def bindable(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def pick_port():
    for port in POLICY_PORTS:
        assert_port(port)
        if bindable(port):
            return port
    raise RuntimeError(f"no free port among {POLICY_PORTS}")


def metadata(mode, overlap_steps=2):
    meta = dict(BASE_META)
    if mode == "rtc":
        meta["rtc"] = {
            "version": 1,
            "max_guidance_weight": 5.0,
            "schedule": "exp",
            "action_horizon": HORIZON,
        }
    elif mode == "vlash":
        meta.update(
            config="pi05_fr3_all_state",
            discrete_state_input=True,
            gripper_convention=GRIPPER_CONVENTION,
            vlash={
                "protocol_version": 1,
                "state_action_space": "fr3_absolute_pose_gripper_v1",
                "action_horizon": HORIZON,
                "overlap_steps": int(overlap_steps),
                "experimental": True,
                "delay_training_verified": False,
                "implementation": "openpi_future_state_adapter",
            },
        )
    elif mode != "plain":
        raise ValueError(f"mode must be plain, rtc or vlash, got {mode!r}")
    return meta


def wrap_pi(x):
    return (np.asarray(x) + np.pi) % (2 * np.pi) - np.pi


# ---- the script ---------------------------------------------------------------------------------------------------

# (x, y, z, rx, ry, rz) the arm eases to, and the rows it takes. The grasp and the place dwell (rows with the same
# pose), the gripper flips inside the dwell; rx passes +pi on the transport (the euler wrap the client clamps through).
_P_ABOVE_OBJECT = (0.55, 0.08, 0.20, 3.119, 0.008, -0.78)
_P_GRASP = (0.55, 0.08, 0.115, 3.119, 0.008, -0.78)
_P_ABOVE_PLACE = (0.42, -0.12, 0.20, 3.17, 0.0, -0.45)
_P_PLACE = (0.42, -0.12, 0.12, 3.17, 0.0, -0.45)
SEGMENTS = (
    ("approach", _P_ABOVE_OBJECT, 12),
    ("descend", _P_GRASP, 12),
    ("grasp_dwell", _P_GRASP, 4),
    ("lift", _P_ABOVE_OBJECT, 10),
    ("transport", _P_ABOVE_PLACE, 22),
    ("lower", _P_PLACE, 12),
    ("place_dwell", _P_PLACE, 4),
    ("raise", _P_ABOVE_PLACE, 10),
)


# A straight out-and-back at 7 mm per row (0.07 m/s nominal), no gripper events: the one path where acceleration has
# room to show (the pick-and-place above is all corners, descents and dwells, so lambda hovers near 1 on it).
_STRAIGHT = (0.45, 0.0, 0.18, 3.119139, 0.008288, -0.883962)
_R = _STRAIGHT[3:]
STRAIGHT_SEGMENTS = (
    *(
        (f"leg{i}", (0.45 + 0.07 * (i + 1), 0.0, 0.18, *_R), 10) for i in range(5)
    ),  # out along x ...
    (
        "shift",
        (0.80, 0.08, 0.18, *_R),
        12,
    ),  # ... over to y = 0.08 (so the way back never overlaps the way out) ...
    *(
        (f"back{i}", (0.80 - 0.07 * (i + 1), 0.08, 0.18, *_R), 10) for i in range(4)
    ),  # ... and back
)
SCRIPTS = {
    "pickplace": (SEGMENTS, True),
    "straight": (STRAIGHT_SEGMENTS, False),
}  # name -> (segments, eased)


class Script:
    """The path as a function of the row index s (floats), sampled densely; gripper 1 -> 0 -> 1 at two exact rows
    (when the segments have the dwells that mark them)."""

    STEP = 0.05  # rows between samples

    def __init__(self, segments=SEGMENTS, start=HOME, eased=True):
        marks, row, prev = {}, 0, np.asarray(start, dtype=float)
        s_values, poses = [0.0], [prev.copy()]
        for name, target, rows in segments:
            target = np.asarray(target, dtype=float)
            marks[name] = row
            for i in range(1, rows + 1):
                f = i / rows
                ease = f * f * (3 - 2 * f) if eased else f
                s_values.append(row + i)
                poses.append(prev + (target - prev) * ease)
            row += rows
            prev = target
        self.end_row = row
        # the close command is the row in the middle of the grasp dwell; a script without dwells has no gripper events
        self.s_close = marks["grasp_dwell"] + 2 if "grasp_dwell" in marks else 1e9
        self.s_open = marks["place_dwell"] + 2 if "place_dwell" in marks else 1e9
        self._knot_s, self._knot_pose = np.asarray(s_values), np.asarray(poses)
        self.grid = np.arange(0.0, self.end_row + 1e-9, self.STEP)
        self.pose_grid = np.stack([self.pose(s) for s in self.grid])
        self.grip_grid = np.array([self.grip(s) for s in self.grid])

    def pose(self, s):
        s = float(np.clip(s, 0.0, self.end_row))
        return np.array(
            [np.interp(s, self._knot_s, self._knot_pose[:, i]) for i in range(6)]
        )

    def grip(self, s):
        """The commanded gripper at row s (a floor row: a binary value, never interpolated)."""
        return 0.0 if self.s_close <= np.floor(s + 1e-9) < self.s_open else 1.0

    def locate(self, state7):
        """The row whose pose and gripper are nearest to a state (xyz, euler, commanded gripper)."""
        state = np.asarray(state7, dtype=float)
        d_pos = np.linalg.norm(self.pose_grid[:, :3] - state[:3], axis=1)
        d_rot = np.max(np.abs(wrap_pi(self.pose_grid[:, 3:6] - state[3:6])), axis=1)
        d_grip = (self.grip_grid != (1.0 if state[6] >= 0.5 else 0.0)) * 0.08
        return float(self.grid[int(np.argmin(d_pos + 0.05 * d_rot + d_grip))])


# ---- the chunk maker (no network) ---------------------------------------------------------------------------------


class Latency:
    """Seconds a reply takes. 'real': 90 % about 83 ms, 10 % between 177 and 261 ms (the recorded distribution)."""

    def __init__(self, spec="real", rng=None):
        self.spec = spec
        self.rng = rng or np.random.default_rng(0)

    def draw(self):
        if self.spec != "real":
            return float(self.spec)
        if self.rng.random() < 0.10:
            return float(self.rng.uniform(0.177, 0.261))
        return float(np.clip(self.rng.normal(0.083, 0.008), 0.06, 0.12))


class ChunkMaker:
    """Answers an observation with a (10, 7) float32 chunk from the script.

    lead_rows: how far ahead of the measured pose row 0 is (the arm trails its targets by about this many rows);
    from a VLASH future state (a target, not a measurement) row 0 is the next row (future_lead_rows).
    jumps_mm: the chunk offsets (along y) cycled per request; consecutive differences are the switch jumps.
    noise_mm: per-chunk random offset (the policy's own chunk-to-chunk noise).
    tail_flip_prob: chance that the last ``tail_flip_rows`` rows carry a gripper flip the script does not have."""

    def __init__(
        self,
        script=None,
        script_name="pickplace",
        lead_rows=3.0,
        future_lead_rows=1.0,
        jumps_mm=(0.0, 46.0, 0.0, 75.0),
        noise_mm=3.0,
        tail_flip_prob=0.25,
        tail_flip_rows=2,
        seed=0,
    ):
        self.script = script or Script(
            SCRIPTS[script_name][0], eased=SCRIPTS[script_name][1]
        )
        self.lead_rows, self.future_lead_rows = lead_rows, future_lead_rows
        self.jumps_mm, self.noise_mm = tuple(jumps_mm), noise_mm
        self.tail_flip_prob, self.tail_flip_rows = tail_flip_prob, tail_flip_rows
        self.rng = np.random.default_rng(seed)
        self.n = 0
        self.last_noise = np.zeros(3)

    def chunk(self, state7, prev=None, delay=0, from_future=False):
        script = self.script
        s0 = script.locate(state7) + (
            self.future_lead_rows if from_future else self.lead_rows
        )
        rows = np.zeros((HORIZON, 7))
        for j in range(HORIZON):
            rows[j, :6] = script.pose(s0 + j)
            rows[j, 6] = script.grip(s0 + j)
        noise = self.rng.normal(0.0, self.noise_mm / 1000.0 / np.sqrt(3), 3)
        jump = np.zeros(3)
        if self.jumps_mm:
            jump[1] = self.jumps_mm[self.n % len(self.jumps_mm)] / 1000.0
        rows[:, :3] += jump
        if prev is not None and int(delay) > 0:
            # a guided chunk: the first rows are the plan it continues; its own noise takes over smoothly (guidance
            # leaves no kink), while the injected jump stays a step at the join: the switch jumps of the real data
            k = min(int(delay), len(prev), HORIZON)
            f = np.clip((np.arange(HORIZON) - k + 1) / 5.0, 0.0, 1.0)
            w = (f * f * (3 - 2 * f))[:, None]
            rows[:, :3] += self.last_noise + (noise - self.last_noise) * w
            rows[:k] = np.asarray(prev, dtype=float)[:k]
        else:
            rows[:, :3] += noise
        self.last_noise = noise
        if self.rng.random() < self.tail_flip_prob:
            rows[-self.tail_flip_rows :, 6] = 1.0 - rows[-self.tail_flip_rows :, 6]
        self.n += 1
        return rows.astype(np.float32), s0


# ---- the server ---------------------------------------------------------------------------------------------------


class FakePhasePolicyServer:
    def __init__(
        self,
        port,
        mode="plain",
        overlap_steps=2,
        latency="real",
        host="127.0.0.1",
        seed=0,
        nan_at_request=None,
        **maker,
    ):
        self.port, self.host, self.mode = assert_port(port), host, mode
        self.nan_at_request = nan_at_request  # the n-th reply (1-based) is all NaN: a broken model, for the I10 test
        self.meta = metadata(mode, overlap_steps)
        self.latency = Latency(latency, np.random.default_rng(seed + 1))
        # A phantom gripper flip in the last rows is for the unexecuted tails of plain and RTC chunks; a VLASH chunk is
        # executed whole, so there the flip would be a real (and unwanted) gripper command.
        maker.setdefault("tail_flip_prob", 0.0 if mode == "vlash" else 0.25)
        self.maker = ChunkMaker(seed=seed, **maker)
        self.requests = []  # one dict per request: kind, n_prev, delay, future, latency_s, s_est
        self._server = self._thread = None

    def answer(self, obs):
        """(reply dict, request log entry) for one decoded observation."""
        state = np.asarray(obs["observation/state"], dtype=np.float32).reshape(-1)
        prev, delay = obs.get("rtc_prev_actions"), obs.get("rtc_inference_delay")
        future = obs.get("vlash_future_state")
        if future is not None:
            future = np.asarray(future, dtype=np.float32).reshape(-1)
        if self.mode == "vlash" and obs.get("vlash_protocol_version") != 1:
            raise ValueError("a VLASH request must carry vlash_protocol_version 1")
        latency = self.latency.draw()
        time.sleep(latency)
        used = future if future is not None else state
        chunk, s0 = self.maker.chunk(
            used,
            prev=None if prev is None or self.mode != "rtc" else np.asarray(prev),
            delay=0 if delay is None else int(delay),
            from_future=future is not None,
        )
        if (
            self.nan_at_request is not None
            and len(self.requests) + 1 == self.nan_at_request
        ):
            chunk = np.full_like(chunk, np.nan)
        kind = {
            "plain": "sync",
            "rtc": "rtc" if prev is not None else "sync",
            "vlash": "prefetch" if future is not None else "sync",
        }[self.mode]
        entry = {
            "kind": kind,
            "n_prev": None if prev is None else int(np.asarray(prev).shape[0]),
            "delay": None if delay is None else int(delay),
            "future": future is not None,
            "latency_s": latency,
            "s_est": float(s0),
            "t": time.monotonic(),
        }
        reply = {"actions": chunk, "server_timing": {"infer_ms": latency * 1000.0}}
        if self.mode == "rtc" and prev is not None:
            reply["rtc"] = {
                "inference_delay": int(delay),
                "prev_len": int(np.asarray(prev).shape[0]),
                "frozen_pos_err_m": 0.0005,
            }
        if self.mode == "vlash":
            reply["vlash"] = {"future_state_used": future is not None}
        return reply, entry

    def start(self):
        import websockets.sync.server as wss
        from openpi_client import msgpack_numpy

        owner = self

        def handler(conn):
            packer = msgpack_numpy.Packer()
            conn.send(packer.pack(dict(owner.meta)))
            try:
                while True:
                    obs = msgpack_numpy.unpackb(conn.recv())
                    reply, entry = owner.answer(obs)
                    owner.requests.append(entry)
                    conn.send(packer.pack(reply))
            except Exception:  # noqa: BLE001 - the client went away / the server is shutting down
                return

        self._server = wss.serve(
            handler, self.host, self.port, compression=None, max_size=None
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-phase-policy"
        )
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--port", type=int, default=None, help="8041-8049; default: the first free one"
    )
    ap.add_argument("--mode", choices=["plain", "rtc", "vlash"], default="plain")
    ap.add_argument(
        "--overlap-steps",
        type=int,
        default=2,
        help="vlash: the overlap advertised in the metadata",
    )
    ap.add_argument("--latency", default="real", help="'real' or seconds per reply")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--jump-mm",
        default="0,46,0,75",
        help="comma list cycled per request ('' or 0 = none)",
    )
    ap.add_argument("--script", choices=sorted(SCRIPTS), default="pickplace")
    ap.add_argument("--noise-mm", type=float, default=3.0)
    ap.add_argument(
        "--tail-flip-prob",
        type=float,
        default=None,
        help="default 0.25 (plain, rtc), 0 (vlash)",
    )
    ap.add_argument(
        "--nan-at-request",
        type=int,
        default=None,
        help="the n-th reply (1-based) is all NaN",
    )
    a = ap.parse_args()
    jumps = (
        tuple(float(v) for v in a.jump_mm.split(",") if v != "") if a.jump_mm else ()
    )
    srv = FakePhasePolicyServer(
        a.port if a.port is not None else pick_port(),
        mode=a.mode,
        overlap_steps=a.overlap_steps,
        latency=a.latency,
        seed=a.seed,
        jumps_mm=jumps,
        script_name=a.script,
        noise_mm=a.noise_mm,
        **({} if a.tail_flip_prob is None else {"tail_flip_prob": a.tail_flip_prob}),
        nan_at_request=a.nan_at_request,
    ).start()
    print(
        f"FAKE phase policy server ({a.mode}) on ws://127.0.0.1:{srv.port} (no model, no GPU)",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
