"""The causal trigger's offline evaluation: replay, scores, role split, guards."""

import csv
import json

import numpy as np
import pandas as pd
import pytest

from levi.events import causal_eval


def episode(name="e", n=90):
    """Descend, close at 1.2 s, lift, stop, open at 3.7 s, stay still."""
    z = np.empty(n)
    for k in range(n):
        if k < 10:
            z[k] = round(0.28 - 0.02 * k, 6)
        elif k < 15:
            z[k] = 0.10
        elif k < 35:
            z[k] = round(0.10 + 0.01 * (k - 14), 6)
        else:
            z[k] = 0.30
    width = np.where((np.arange(n) >= 12) & (np.arange(n) < 37), 0.01, 0.08)
    times = np.arange(n) / 10
    xyz = np.stack([np.zeros(n), np.zeros(n), z], axis=1)
    state = np.concatenate([xyz, np.zeros((n, 3)), width[:, None]], axis=1)
    return {
        "id": name,
        "source": f"captures/{name}",
        "sha256": {"end_effector_pose.csv": name, "gripper_state.csv": name},
        "times": times,
        "xyz": xyz,
        "width": width,
        "command": (width > 0.05).astype(float),
        "table": pd.DataFrame({"timestamp": times, "observation.state": list(state)}),
        "boundaries": [1.2, 3.7],
        "duration": float(times[-1]),
    }


def cut(ep, k):
    out = dict(ep)
    for key in ("times", "xyz", "width", "command"):
        out[key] = ep[key][:k]
    return out


def test_derive_reads_dataset_level_values_from_dev_episodes_only():
    params = causal_eval.derive([episode("a"), episode("b")])
    assert params["gripper_range"] == [0.01, 0.08]
    assert params["derived_from"] == ["a", "b"]
    # 0.15 (signals.TURN) of the 0.20 m height span.
    assert params["height_turn_m"] == pytest.approx(0.03)
    # 0.1 (motion.STILL) of the 95th percentile speed, 0.2 m/s.
    assert params["motion_speed_m_s"] == pytest.approx(0.02)
    assert params["still_ns"] == 500_000_000


def test_replay_of_a_prefix_is_the_prefix_of_the_replay():
    ep = episode()
    params = causal_eval.derive([ep])
    full = causal_eval.replay(ep, params)["looks"]
    assert full
    for k in (5, 13, 20, 38, 60, 89):
        assert causal_eval.replay(cut(ep, k), params)["looks"] == [
            x for x in full if x["row"] < k
        ], k


def test_replay_counts_rejected_and_clipped_samples():
    ep = episode()
    ep["times"] = ep["times"].copy()
    ep["times"][20] = ep["times"][19]  # a repeated timestamp
    ep["width"] = ep["width"].copy()
    ep["width"][50] = 0.2
    params = causal_eval.derive([episode()])
    out = causal_eval.replay(ep, params)
    assert out["rejected"] == 1 and out["clipped"] == 1
    assert out["steps"] == len(ep["times"]) - 1


def _record_feeds(monkeypatch):
    """Make ``replay`` build a ``Trigger`` that records every ``feed`` call
    (the sample and ``now_ns``) before passing it on, and return that list."""
    fed = []
    real = causal_eval.Trigger

    class Recording(real):
        def feed(self, sample, **kwargs):
            fed.append((sample, kwargs["now_ns"]))
            return super().feed(sample, **kwargs)

    monkeypatch.setattr(causal_eval, "Trigger", Recording)
    return fed


@pytest.mark.parametrize("damaged", [False, True])
def test_replay_hands_the_trigger_exactly_the_recorded_samples(monkeypatch, damaged):
    """At row k the trigger is fed row k's recorded values, stamped with row
    k's own time, and nothing from a later row: the evaluator never reads
    ahead of the step it scores. ``damaged`` adds a repeated timestamp (the
    stream rejects that sample) and a width outside the declared range
    (clipped into it): the rows still map one to one."""
    ep = episode()
    params = causal_eval.derive([ep])
    lo, hi = params["gripper_range"]
    if damaged:
        ep["times"] = ep["times"].copy()
        ep["times"][20] = ep["times"][19]
        ep["width"] = ep["width"].copy()
        ep["width"][50] = 0.2
    fed = _record_feeds(monkeypatch)
    causal_eval.replay(ep, params)
    assert len(fed) == len(ep["times"])
    steps = 0
    for row, (sample, now_ns) in enumerate(fed):
        stamp = causal_eval.ORIGIN_NS + round(float(ep["times"][row]) * 1e9)
        # Not earlier and not later than the row's own time, and the clock
        # the stream checks against is that same instant.
        assert sample.observed_ns == stamp and now_ns == stamp, row
        width = float(ep["width"][row])
        assert sample.values == {
            (causal_eval.STATE, 0): float(ep["xyz"][row, 0]),
            (causal_eval.STATE, 1): float(ep["xyz"][row, 1]),
            (causal_eval.STATE, 2): float(ep["xyz"][row, 2]),
            (causal_eval.STATE, 6): min(max(width, lo), hi),
            (causal_eval.COMMAND, 0): float(ep["command"][row]),
        }, row
        assert sample.step == steps, row
        if not (damaged and row == 20):
            steps += 1


def test_evaluate_scores_decisions_against_reference_boundaries():
    eps = [episode("a"), episode("b")]
    params = causal_eval.derive(eps)
    section, runs, live = causal_eval.evaluate(eps, params)
    # The close and the open are decided at the very rows they happen.
    assert section["live"]["at"]["0.1"]["recall"] == 1.0
    assert section["live"]["at"]["0.1"]["matched"] == 4
    assert section["per_reason"]["grasp"]["count"] >= 2
    assert section["per_reason"]["release"]["rule_delay_seconds"]["max"] == 0
    stall = section["per_reason"]["stall"]["rule_delay_seconds"]
    assert stall["max"] >= 0.5 - 1e-9
    assert set(section["per_reason"]) == {
        "grasp",
        "release",
        "stall",
        "anomaly",
        "maybe_done",
    }
    assert live[0] == [x["time_s"] for x in runs[0]["looks"]]


def test_first_look_latency():
    assert causal_eval.first_after([1.0, 5.0], [0.7, 1.2, 9.0], 0.5, 3.0) == [
        pytest.approx(-0.3),
        None,
    ]
    out = causal_eval._latency({0: [1.0, 5.0]}, {0: [1.4]}, 3.0)
    assert out["references"] == 2 and out["covered"] == 1 and out["share"] == 0.5
    assert out["mean"] == pytest.approx(0.4)


def test_choose_takes_best_recall_within_the_budget():
    sweep = [
        {"min_score": 0.0, "looks_per_minute": 20.0, "recall@0.5": 1.0},
        {"min_score": 0.5, "looks_per_minute": 5.0, "recall@0.5": 0.8},
        {"min_score": 0.9, "looks_per_minute": 2.0, "recall@0.5": 0.8},
    ]
    assert causal_eval.choose(sweep, 6) == (0.9, True)
    assert causal_eval.choose(sweep, 1) == (0.9, False)


def test_cpu_report_against_the_one_percent_budget():
    eps = [episode()]
    cheap = causal_eval.cpu_report({0: {"cpu": [1e-4] * 10}}, eps)
    assert cheap["core_percent_mean"] == pytest.approx(0.1)
    assert cheap["within_budget"] is True
    costly = causal_eval.cpu_report({0: {"cpu": [2e-3] * 10}}, eps)
    assert costly["within_budget"] is False
    assert costly["core_percent_peak"] == pytest.approx(2.0)


def test_split_refuses_mixed_or_missing_roles(tmp_path):
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"dev": ["a", "b"], "confirm": ["b"]}))
    with pytest.raises(SystemExit, match="both dev and confirm"):
        causal_eval.load_split(split, "dev")
    split.write_text(json.dumps({"dev": ["a"]}))
    with pytest.raises(SystemExit, match="no 'confirm' list"):
        causal_eval.load_split(split, "confirm")
    assert causal_eval.load_split(split, "dev") == {"a"}


# --- the command line, on raw captures -------------------------------------------


def _capture(folder, n=120):
    """10 Hz; the gripper closes at 4.0 s and opens at 9.0 s."""
    folder.mkdir(parents=True)
    with (folder / "end_effector_pose.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec",
                    "px", "py", "pz", "qx", "qy", "qz", "qw"])  # fmt: skip
        for i in range(n):
            t = 1000 + i / 10
            z = 0.2 - 0.05 * min(i / 10, 3)
            w.writerow([t, i + 1, 1, t, 0.4, 0.0, z, 1, 0, 0, 0])
    with (folder / "gripper_state.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec",
                    "finger_left", "finger_right", "gripper_width",
                    "last_gripper_command"])  # fmt: skip
        for i in range(n):
            t = 1000 + i / 10
            width = 0.08 if i < 40 or i >= 90 else 0.0
            w.writerow([t, i + 1, 1, t, width / 2, width / 2, width,
                        "open" if width else "close"])  # fmt: skip
    return n


def _gold(folder, name, source, frames):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(
        json.dumps(
            {
                "source": source,
                "frames": frames,
                "segments": [
                    {"start_n": 0, "end_n": 40},
                    {"start_n": 40, "end_n": 90},
                    {"start_n": 90, "end_n": frames - 1},
                ],
            }
        )
    )


def _setup(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    gold = tmp_path / "gold"
    _gold(gold, "A", "captures/A", _capture(tmp_path / "captures" / "A"))
    _gold(gold, "B", "captures/B", _capture(tmp_path / "captures" / "B", n=130))
    # The same capture as A under another name.
    _gold(gold, "A2", "captures/A", 120)
    lists = tmp_path / "lists"
    lists.mkdir()
    exclusion = lists / "test-list.json"
    exclusion.write_text(json.dumps({"episodes": [{"path": "captures/Z"}]}))
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"dev": ["A"], "confirm": ["B"]}))
    base = ["--root", str(tmp_path), "--gold", str(gold)]
    return base + ["--exclusion-list", str(exclusion)], split


def test_dev_run_chooses_and_confirm_run_reuses(tmp_path, monkeypatch, capsys):
    base, split = _setup(tmp_path, monkeypatch)
    dev_out = tmp_path / "dev.json"
    assert (
        causal_eval.main(
            [*base, "--split", str(split), "--role", "dev"]
            + ["--budget-per-minute", "60", "--out", str(dev_out)]
        )
        == 0
    )
    dev = json.loads(dev_out.read_text())
    assert dev["role"] == "dev" and [e["id"] for e in dev["episodes"]] == ["A"]
    assert {s["reason"] for s in dev["skipped"]} == {"not in this role"}
    assert dev["sweep"] and dev["chosen_by"]
    assert dev["params"]["trigger"]["min_score"] == 0.9
    assert dev["causal"]["live"]["at"]["0.1"]["recall"] == 1.0
    assert dev["offline"]["scores"]["reference_boundaries"] == 2
    assert dev["cpu"] is None

    confirm_out = tmp_path / "confirm.json"
    causal_eval.main(
        [*base, "--split", str(split), "--role", "confirm"]
        + ["--params", str(dev_out), "--timing", "--out", str(confirm_out)]
    )
    confirm = json.loads(confirm_out.read_text())
    assert [e["id"] for e in confirm["episodes"]] == ["B"]
    assert confirm["params"] == dev["params"]
    assert confirm["sweep"] is None and confirm["chosen_by"] is None
    assert confirm["params_from"]["path"] == str(dev_out)
    assert confirm["cpu"]["steps"] == 130
    assert confirm["cpu"]["budget_percent"] == 1.0
    capsys.readouterr()


def test_confirm_refuses_without_dev_params_or_with_dev_episodes(
    tmp_path, monkeypatch, capsys
):
    base, split = _setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="needs --params"):
        causal_eval.main([*base, "--split", str(split), "--role", "confirm"])
    dev_out = tmp_path / "dev.json"
    causal_eval.main(
        [*base, "--split", str(split), "--role", "dev", "--out", str(dev_out)]
    )
    again = tmp_path / "again.json"
    again.write_text(json.dumps({"dev": ["A"], "confirm": ["A2"]}))
    with pytest.raises(SystemExit, match="was in the dev run"):
        causal_eval.main(
            [*base, "--split", str(again), "--role", "confirm"]
            + ["--params", str(dev_out)]
        )
    # A confirm report is not a dev report.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": causal_eval.SCHEMA, "role": "x"}))
    with pytest.raises(SystemExit, match="report of a dev run"):
        causal_eval.main(
            [*base, "--split", str(split), "--role", "confirm", "--params", str(bad)]
        )
    capsys.readouterr()


def test_confirm_refuses_a_dev_episode_even_when_the_split_hash_matches(
    tmp_path, monkeypatch, capsys
):
    """The overlap refusal does not rest on the split-hash check: dev and
    confirm here use one split file (so one sha256), and its confirm episode
    A2 is the capture of its dev episode A under another name."""
    base, _ = _setup(tmp_path, monkeypatch)
    split = tmp_path / "overlap-split.json"
    split.write_text(json.dumps({"dev": ["A"], "confirm": ["A2"]}))
    dev_out = tmp_path / "dev.json"
    causal_eval.main(
        [*base, "--split", str(split), "--role", "dev", "--out", str(dev_out)]
    )
    confirm_out = tmp_path / "confirm.json"
    with pytest.raises(SystemExit, match="A2 was in the dev run"):
        causal_eval.main(
            [*base, "--split", str(split), "--role", "confirm"]
            + ["--params", str(dev_out), "--out", str(confirm_out)]
        )
    assert not confirm_out.exists()
    capsys.readouterr()


def test_test_sets_are_refused_before_anything_is_read(tmp_path, monkeypatch):
    import builtins
    from pathlib import Path

    from levi.conversion import raw

    _, split = _setup(tmp_path, monkeypatch)
    frozen = tmp_path / "screws-frozen"
    _gold(frozen, "A", "captures/A", 120)
    # A locked set: gold_files would read this first, were the folder not
    # refused before it.
    (frozen / "MANIFEST.json").write_text(json.dumps({"episodes": {"A": {}}}))
    args = ["--root", str(tmp_path), "--split", str(split), "--role", "dev"]
    lists = ["--exclusion-list", str(tmp_path / "lists" / "test-list.json")]

    opened, loaded = [], []
    read_text, read_bytes, real_open = Path.read_text, Path.read_bytes, builtins.open

    def spy_text(self, *a, **k):
        opened.append(Path(self).resolve())
        return read_text(self, *a, **k)

    def spy_bytes(self, *a, **k):
        opened.append(Path(self).resolve())
        return read_bytes(self, *a, **k)

    def spy_open(file, *a, **k):
        if isinstance(file, (str, Path)):
            opened.append(Path(file).resolve())
        return real_open(file, *a, **k)

    def spy_load(demo):
        loaded.append(Path(demo))
        return real_load(demo)

    real_load = raw.load
    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(raw, "load", spy_load)

    # (a) A test-set folder: refused before any of its gold files is opened.
    with pytest.raises(SystemExit, match="refused"):
        causal_eval.main([*args, "--gold", str(frozen), *lists])
    inside = frozen.resolve()
    assert not [p for p in opened if p == inside or inside in p.parents]
    assert loaded == []

    # (b) A source on an exclusion list (relative to --root, as heldout.load
    # reads it): its gold is read (that is how the source is known), its
    # capture never is.
    listed = tmp_path / "lists" / "listed.json"
    listed.write_text(json.dumps({"episodes": [{"path": "captures/A"}]}))
    opened.clear()
    with pytest.raises(SystemExit, match="is on a list"):
        causal_eval.main(
            [*args, "--gold", str(tmp_path / "gold"), "--exclusion-list", str(listed)]
        )
    assert loaded == []
    capture = (tmp_path / "captures" / "A").resolve()
    assert not [p for p in opened if capture in p.parents]

    # An exclusion list is required.
    with pytest.raises(SystemExit):
        causal_eval.main([*args, "--gold", str(tmp_path / "gold")])


def test_dev_refuses_params_and_confirm_refuses_another_split(
    tmp_path, monkeypatch, capsys
):
    base, split = _setup(tmp_path, monkeypatch)
    dev_out = tmp_path / "dev.json"
    causal_eval.main(
        [*base, "--split", str(split), "--role", "dev", "--out", str(dev_out)]
    )
    # A dev run never borrows parameters.
    with pytest.raises(SystemExit, match="derives its parameters"):
        causal_eval.main(
            [*base, "--split", str(split), "--role", "dev", "--params", str(dev_out)]
        )
    # A confirm run only accepts a dev report of the very same split.
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"dev": ["A"], "confirm": ["B"], "note": 1}))
    with pytest.raises(SystemExit, match="another split"):
        causal_eval.main(
            [*base, "--split", str(other), "--role", "confirm"]
            + ["--params", str(dev_out)]
        )
    capsys.readouterr()


CANARY = "CANARY-LABEL-7731"


@pytest.mark.parametrize("case", ["listed", "pattern"])
def test_nothing_of_a_refused_gold_reaches_any_output(
    tmp_path, monkeypatch, capsys, caplog, case
):
    base, split = _setup(tmp_path, monkeypatch)
    source = "captures/A" if case == "listed" else f"captures/frozen-{CANARY}"
    gold = tmp_path / "gold"
    (gold / "A.json").write_text(
        json.dumps(
            {
                "episode": f"ep-{CANARY}",
                "source": source,
                "frames": 120,
                "task": CANARY,
                "segments": [
                    {"start_n": 0, "end_n": 40, "subtask": CANARY},
                    {"start_n": 40, "end_n": 119, "subtask": f"{CANARY}-2"},
                ],
            }
        )
    )
    listed = tmp_path / "lists" / "listed.json"
    listed.write_text(json.dumps({"episodes": [{"path": "captures/A"}]}))
    out = tmp_path / "report.json"
    with pytest.raises(SystemExit) as refused:
        causal_eval.main(
            [*base, "--exclusion-list", str(listed)]
            + ["--split", str(split), "--role", "dev", "--out", str(out)]
        )
    message = str(refused.value)
    assert message == "refused: source A is on a list"
    assert refused.value.__suppress_context__
    printed = capsys.readouterr()
    for text in (message, printed.out, printed.err, caplog.text):
        assert CANARY not in text and "captures/" not in text
    assert not out.exists()
