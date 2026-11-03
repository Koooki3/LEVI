"""Hover rule statistics from v3 engine logs (PhaseParams.hover_dp_m / hover_dr_rad / hover_rows).

The figures in the PhaseParams hover comment come from this script. It replays the rule on the engine's own trace
lines (``*.phase/ep_*.jsonl``, the records ``PhaseEngine.drain_records`` returns) with ``PhaseEngine``'s own code
(``_rebuild``, ``_hover_at``), so the numbers follow the engine:

* per chunk record the stored plan is rebuilt as the engine stored it: sync / swap / replan keep the rows with
  chunk-local row 0 first; append adds the rows to the stored plan and drops the rows more than two behind the phase
  (``PhaseEngine.append``). Plain records are refused: plain stores the last target as a virtual row -1 the trace
  does not carry.
* per tick the rule is evaluated at the pre-tick phase ``u - lam`` (a tick record's ``u`` is the phase after the
  tick, a hold's ``lam`` is 0). The first advancing tick after a synchronous load lands on row 0 and is never a
  hover (as in the engine); a tick with no plan at all (after ``reset()``, before the next load) is not marked in the
  trace and is evaluated on the last plan.
* "window extent" is the plan-only quantity the thresholds are set on: with ``a`` the floor row of the pre-tick
  phase (clipped to the stored rows) and ``a0 = min(a, n - hover_rows)``, the largest distance (and largest
  per-axis rotation) of any stored row from ``a0`` on from row ``a0``.
* "start-up stall" (``--stall`` episodes only): the ticks before the issued target first leaves 30 mm of its tick-0
  position.

Usage (reads only; prints a summary)::

    python hover_stats.py LOG_DIR [--stall PATTERN] [--lambda-max 3.0] [--set name=value ...] [--per-episode]

``LOG_DIR`` holds ``*.phase/ep_*.jsonl``; episodes whose path contains PATTERN are the hover episodes, the rest are
"other".
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase import PhaseEngine, PhaseParams, _floor, wrap_to_pi

STALL_LEAVE_M = 0.03  # a start-up stall ends when the issued target leaves this far from its tick-0 position
FIRST_TICKS = 10  # "other" episodes are counted from this tick on (the first plans still land and ramp)
FUTURE_TICKS = 10  # a flagged tick "followed by motion": the target moves > FUTURE_M within this many ticks
FUTURE_M = 0.02


def _plan_after(kind: str, rows: np.ndarray, R, lo: int, last_s, origin: float):
    if kind in ("sync", "swap", "replan"):
        return rows, 0
    if kind == "append":
        if R is None or last_s is None:
            raise ValueError("append record before any plan or tick")
        R2, lo2 = np.vstack([R, rows]), lo
        drop = _floor(last_s - origin) - 2 - lo2
        if drop > 0:
            R2, lo2 = R2[drop:], lo2 + drop
        return R2, lo2
    raise ValueError(
        f"chunk kind {kind!r} is not supported (plain keeps a virtual row the trace does not carry)"
    )


def evaluate(records, params: PhaseParams) -> list[dict]:
    """Per tick record of one episode: the hover rule's parts at the pre-tick phase. Keys: k, u_pre, lam, plan_static
    (the window rule), hover (the window rule, off on a landing tick: what the engine counts in hover_ticks), fresh (a landing tick), extent_m, extent_rad (window extent, None without a plan), tgt (issued
    target, (7,)). Raises ValueError on a plain record or an append before any plan."""
    eng = PhaseEngine(params)
    plans: dict = {}
    R, lo, last_s = None, 0, None
    fresh = False  # PhaseEngine._fresh: a synchronous load lands on row 0 on its first advancing tick
    out = []
    for r in records:
        kind = r.get("kind")
        if kind == "chunk":
            rows = np.asarray(r["rows"], np.float64)
            ck = r["chunk_kind"]
            # set_chunk's label ("sync" in the schedulers)
            sync = ck not in ("swap", "append", "replan", "plain")
            R, lo = _plan_after(
                "sync" if sync else ck, rows, R, lo, last_s, float(r["origin"])
            )
            if sync:
                fresh = True
            elif ck != "append":
                fresh = False
            eng._R, eng._lo, eng._row0_change = R, lo, False
            eng._rebuild(1)
            plans[r["chunk_id"]] = (R, lo, list(eng._static))
        elif kind == "tick":
            u_pre = float(r["u"]) - float(r["lam"])
            x = {
                "k": r["k"],
                "u_pre": u_pre,
                "lam": float(r["lam"]),
                "plan_static": False,
                "hover": False,
                "fresh": fresh,
                "extent_m": None,
                "extent_rad": None,
                "tgt": np.asarray(r["tgt"], np.float64),
            }
            plan = plans.get(r["chunk_id"])
            if plan is not None:
                Rp, lop, st = plan
                eng._static, eng._lo = st, lop
                x["plan_static"] = bool(eng._hover_at(u_pre))
                x["hover"] = x["plan_static"] and not fresh
                n = len(Rp)
                a = min(max(_floor(u_pre) - lop, 0), n - 1)
                a0 = min(a, max(n - params.hover_rows, 0))
                W = Rp[a0:]
                x["extent_m"] = float(np.linalg.norm(W[:, :3] - W[0, :3], axis=1).max())
                x["extent_rad"] = float(np.abs(wrap_to_pi(W[:, 3:6] - W[0, 3:6])).max())
            out.append(x)
            if not r.get("hold"):
                fresh = False
            last_s = float(r["s"])
    return out


def _pct(a, qs=(50, 90, 98)) -> str:
    a = np.asarray(a, np.float64)
    return " / ".join(f"{np.percentile(a, q):.2f}" for q in qs) if a.size else "-"


def summarize(
    episodes: dict, params: PhaseParams, stall: str | None, per_episode: bool = False
) -> list[str]:
    """Summary lines for {path: [tick dicts]}; with ``per_episode`` one more line per episode."""
    lines = []
    total = sum(len(t) for t in episodes.values())
    flagged = sum(x["hover"] for t in episodes.values() for x in t)
    lines.append(
        f"episodes {len(episodes)}, ticks {total}, hover {flagged}; params hover_dp_m {params.hover_dp_m}"
        f" hover_dr_rad {params.hover_dr_rad} hover_rows {params.hover_rows}"
    )
    hov = {p: t for p, t in episodes.items() if stall and stall in p}
    oth = {p: t for p, t in episodes.items() if p not in hov}
    if hov:
        st = []
        for t in hov.values():
            T = np.array([x["tgt"][:3] for x in t])
            d = np.linalg.norm(T - T[0], axis=1)
            k1 = (
                int(np.argmax(d > STALL_LEAVE_M))
                if (d > STALL_LEAVE_M).any()
                else len(t)
            )
            st.append(t[:k1])
        pooled = [x for t in st for x in t if x["extent_m"] is not None]
        em = np.array([x["extent_m"] for x in pooled]) * 1000
        er = np.array([x["extent_rad"] for x in pooled]) * 1000
        under = (
            float(
                np.mean(
                    (em < params.hover_dp_m * 1000) & (er < params.hover_dr_rad * 1000)
                )
            )
            if pooled
            else 0.0
        )
        lines.append(
            f"stall episodes ({stall}): {len(hov)}; start-up stall ticks {sum(len(t) for t in st)}"
            f" ({', '.join(str(len(t)) for t in st)}); hover {sum(x['hover'] for t in st for x in t)},"
            f" window rule alone {sum(x['plan_static'] for t in st for x in t)}"
        )
        lines.append(
            f"  window extent p50 / p90 / p98: {_pct(em)} mm, {_pct(er)} mrad; under hover_dp_m and hover_dr_rad"
            f" {under:.3f}"
        )
        allh = [x for t in hov.values() for x in t]
        lines.append(
            f"  all ticks of the stall episodes: {len(allh)}, hover {sum(x['hover'] for x in allh)}"
        )
    if oth:
        rest = [x for t in oth.values() for x in t if x["k"] >= FIRST_TICKS]
        ext = [x["extent_m"] * 1000 for x in rest if x["extent_m"] is not None]
        nh = sum(x["hover"] for x in rest)
        lines.append(
            f"other episodes: {len(oth)}; ticks from tick {FIRST_TICKS} on {len(rest)}: hover {nh}"
            f" ({nh / max(len(rest), 1):.4f}), window rule alone {sum(x['plan_static'] for x in rest)};"
            f" window extent median {np.median(ext) if ext else float('nan'):.1f} mm"
        )
    fut = 0
    nflag = 0
    for t in episodes.values():
        T = np.array([x["tgt"][:3] for x in t])
        for i, x in enumerate(t):
            if x["hover"]:
                nflag += 1
                j = min(len(t), i + FUTURE_TICKS + 1)
                fut += bool(np.linalg.norm(T[i:j] - T[i], axis=1).max() > FUTURE_M)
    lines.append(
        f"hover ticks followed by > {FUTURE_M * 1000:.0f} mm of target motion within {FUTURE_TICKS} ticks:"
        f" {fut} of {nflag}"
    )
    if per_episode:
        for p, t in episodes.items():
            lines.append(
                f"  {p}: ticks {len(t)}, hover {sum(x['hover'] for x in t)},"
                f" window rule alone {sum(x['plan_static'] for x in t)}"
            )
    return lines


def _coerce(text: str):
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    if text in ("true", "false"):
        return text == "true"
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log_dir", type=Path)
    ap.add_argument(
        "--stall",
        default=None,
        help="path substring of the hover (start-up stall) episodes",
    )
    ap.add_argument("--lambda-max", type=float, default=3.0)
    ap.add_argument("--alpha", type=float, default=0.005)
    ap.add_argument(
        "--set",
        nargs="*",
        default=[],
        metavar="NAME=VALUE",
        help="PhaseParams overrides",
    )
    ap.add_argument(
        "--per-episode", action="store_true", help="one line per episode as well"
    )
    a = ap.parse_args(argv)
    params = PhaseParams.for_page(a.lambda_max, a.alpha)
    if a.set:
        params = dataclasses.replace(
            params, **{k: _coerce(v) for k, v in (s.split("=", 1) for s in a.set)}
        )
    files = sorted(a.log_dir.glob("*.phase/ep_*.jsonl"))
    if not files:
        print(f"no *.phase/ep_*.jsonl under {a.log_dir}", file=sys.stderr)
        return 2
    episodes = {}
    for f in files:
        with f.open() as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        episodes[str(f.relative_to(a.log_dir))] = evaluate(records, params)
    print("\n".join(summarize(episodes, params, a.stall, a.per_episode)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
