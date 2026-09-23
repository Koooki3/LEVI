"""Evaluation records of subtask annotation: a person's session or an agent's
full-dataset task, one markdown file each in ``<workspace>/eval``.

- person: ``<dataset>_human_<YYYYmmddTHH>.md``, between Start and Stop in the
  annotation view;
- agent: ``<dataset>_agent_<YYYYmmddTHH>_<driver>.md``, written when a run
  that annotated every episode of the dataset with subtasks is committed.

The hour is the completion time in UTC (as in run ids). A second record for the same name in the
same hour is appended to the file as another section, never overwritten.
Both kinds share sections 1-6, computed by ``metrics`` from the dataset's
annotation files, so they compare line by line; an agent record adds its
own efficiency and token accounting (section 7). A JSON block at the end
carries every number for later aggregation.
"""

import json
import os
import time
from pathlib import Path

from . import metrics

DRIVERS = {
    "external-mcp": "外部 MCP agent",
    "external-pilot": "外部 agent（Pilot 运行时）",
    "api": "API 模型",
    "local-vlm": "本地 VLM 独立",
    "local-vlm-teacher": "本地 VLM + 外部老师",
    # Work done without LEVI and saved into the dataset afterwards, so it can
    # be measured side by side with LEVI's own modes.
    "native-external": "外部 agent（不经 LEVI）",
    "native-local-vlm": "本地 VLM 独立（不经 LEVI）",
    "native-local-vlm-teacher": "本地 VLM + 外部老师（不经 LEVI）",
}


def eval_dir() -> Path:
    from levi.paths import ROOT

    return ROOT / "eval"


def stamp(at: float) -> str:
    """The file name's hour: UTC, the same clock and spelling as run ids."""
    from levi.harness.layout import hour_stamp

    return hour_stamp(at)


def _clock(at: float | None) -> str:
    """Times inside a record are local, with their offset."""
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(at)) if at else "—"


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _num(x, digits=2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{digits}f}" if isinstance(x, float) else f"{x:,}"


# ---------------------------------------------------------------- sources


def dataset_root(name: str) -> Path:
    from levi import catalog

    root = catalog.local_root(f"local/{name}")
    if root is None:
        raise ValueError(f"{name} is not a local LEVI dataset")
    return Path(root)


def annotations_dir(name: str) -> Path:
    from levi import catalog
    from levi.agent.store import resolve

    return resolve(catalog.STATE, name, "annotations")


def vocabulary(name: str) -> set[str]:
    from levi.annotations import vocabulary as vocab

    return set(vocab.ids(annotations_dir(name)))


def episode_segments(name: str, episodes, run_id: str | None = None) -> dict:
    """index -> {"frames", "fps", "segments"} read from the annotation files.
    With ``run_id``, only the atoms that run wrote are counted."""
    lengths, fps = metrics.episode_meta(dataset_root(name))
    folder = annotations_dir(name)
    out = {}
    for ep in episodes:
        atoms = metrics.read_atoms(folder, ep)
        if atoms is None or ep not in lengths:
            continue
        if run_id:
            atoms = [
                a
                for a in atoms
                if ((a.get("levi") or {}).get("origin") or {}).get("run_id") == run_id
            ]
        last = (lengths[ep] - 1) / fps
        out[ep] = {
            "frames": lengths[ep],
            "fps": fps,
            "segments": metrics.segments(atoms, last),
        }
    return out


def siblings(name: str) -> list[str]:
    """Other catalog datasets with the same content (same episodes and frame
    counts) whose names share this one's stem -- the copies made for side by
    side runs, e.g. ``<base>_human`` and ``<base>_agentVLM``."""
    from levi import catalog

    try:
        mine = metrics.episode_meta(dataset_root(name))[0]
    except (OSError, ValueError, KeyError):
        return []
    stem = name.rsplit("_", 1)[0]
    found = []
    for other in sorted(catalog.datasets()):
        if other == name or not (other.startswith(stem + "_") or other == stem):
            continue
        try:
            if metrics.episode_meta(dataset_root(other))[0] == mine:
                found.append(other)
        except (OSError, ValueError, KeyError):
            continue
    return found


def comparisons(name: str, episodes: dict) -> list[dict]:
    rows = []
    mine = {ep: row["segments"] for ep, row in episodes.items()}
    for other in siblings(name):
        theirs = {
            ep: row["segments"]
            for ep, row in episode_segments(other, list(episodes)).items()
            if row["segments"]
        }
        result = metrics.agreement(theirs, mine)
        if result:
            rows.append({"reference": other, **result})
    return rows


# ---------------------------------------------------------------- render


def _sections(block: dict, compared: list[dict]) -> list[str]:
    m = block["metrics"]
    c, s, st = m["coverage"], m["semantics"], m["structure"]
    minutes = block["seconds"] / 60 if block.get("seconds") else None
    lines = [
        "### 2. 完成量",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 确认完成子任务标注的 episode 数 | {m['episodes']} / {block['dataset_episodes']} |",
        f"| 子任务标注总数 | {m['segments']} |",
        f"| 每集子任务数 | {_num(m['segments_per_episode'])} |",
        f"| 覆盖的视频时长 | {_num(m['video_seconds'], 1)} s |",
        "",
        "### 3. 覆盖率",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 秒级覆盖率（全部集） | {_pct(c['seconds'])} |",
        f"| 帧级覆盖率（全部集） | {_pct(c['frames'])} |",
        f"| 每集秒级覆盖率 均值 / 中位 / 最低 | {_pct(c['episode_mean'])} / {_pct(c['episode_median'])} / {_pct(c['episode_min'])} |",
        f"| 覆盖 ≥95% 的集 | {c['fully_covered']} / {m['episodes']} |",
        f"| 空隙（>{metrics.GAP_SECONDS} s）个数 / 总时长 | {c['gaps']} / {c['gap_seconds']} s |",
        "",
        "### 4. 语义丰富性",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 不同子任务标签数 | {s['distinct_labels']} |",
        f"| 带词表子任务 id 的比例 | {_pct(s['with_subtask_id'])} |",
        f"| 使用的词表条目 | {s['vocabulary_used'] if s['vocabulary_used'] is not None else '—'} / {s['vocabulary_size'] or '无词表'} |",
        f"| 标了成败（outcome）的比例 | {_pct(s['with_outcome'])} |",
        f"| outcome 分布 | {', '.join(f'{k} {v}' for k, v in sorted(s['outcomes'].items())) or '—'} |",
        f"| 有文字描述的比例 | {_pct(s['with_description'])} |",
        f"| 每条描述的词数 | {_num(s['words_per_description'], 1)} |",
        f"| 不重复描述的比例 | {_pct(s['distinct_descriptions'])} |",
        f"| 词汇多样性（type/token） | {_num(s['type_token_ratio'], 3)} |",
        f"| 描述只重复标签名的比例 | {_pct(s['label_only'])} |",
        "",
        "### 5. 正确性",
        "",
        "结构正确性（无需参考答案）：",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 结构有效的子任务比例 | {_pct(st['valid_share'])} |",
        f"| 无效区间（空区间 / 超出集边界） | {st['invalid_intervals']} |",
        f"| 同层重叠 | {st['overlaps']} |",
        f"| 词表外的子任务 id | {st['unknown_subtask_ids']} |",
        "",
    ]
    if compared:
        lines += [
            "与同内容副本数据集的逐条一致性（参考 = 对方的标注；同一子任务且 IoU ≥ 0.3 视为匹配）：",
            "",
            "| 参考数据集 | 共同集数 | 片段 F1 | outcome 一致 | 时长一致（子任务） | 时长一致（子任务+outcome） | 边界误差 |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in compared:
            lines.append(
                f"| {row['reference']} | {row['episodes']} | {_num(row['segment_f1'], 3)} | "
                f"{_pct(row['outcome_accuracy'])} | {_pct(row['time_accuracy'])} | "
                f"{_pct(row['outcome_time_accuracy'])} | "
                f"{_num(row['boundary_mae'])} s |"
            )
        lines.append("")
    else:
        lines += ["参考一致性：暂无同内容副本数据集的标注可比。", ""]
    worst = sorted(m["per_episode"].items(), key=lambda kv: kv[1]["coverage_seconds"])[
        :3
    ]
    lines += [
        "定性观察（自动生成，测试人员可在下方补充）：",
        "",
        *[
            f"- {ep}：覆盖 {_pct(row['coverage_seconds'])}，{row['segments']} 段，"
            f"空隙 {row['gaps']}，重叠 {row['overlaps']}"
            for ep, row in worst
        ],
        "- 评语：",
        "",
        "### 6. 效率",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 记录时长 | {_num(block.get('seconds'), 1)} s |",
        f"| 每集用时 | {_num(block['seconds'] / m['episodes'], 1) if m['episodes'] and block.get('seconds') else '—'} s |",
        f"| 每分钟子任务数 | {_num(m['segments'] / minutes) if minutes else '—'} |",
        f"| 每分钟完成的视频秒数 | {_num(m['video_seconds'] / minutes, 1) if minutes else '—'} |",
        "",
    ]
    return lines


def render(block: dict, compared: list[dict], extra: list[str]) -> str:
    who = (
        "人工"
        if block["kind"] == "human"
        else DRIVERS.get(block["driver"], block["driver"])
    )
    lines = [
        f"## {block['dataset']} · {who} · {_clock(block['finished_at'])}",
        "",
        "### 1. 概况",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 数据集 | {block['dataset']} |",
        f"| 标注方 | {who} |",
        f"| 开始 | {_clock(block['started_at'])} |",
        f"| 完成 | {_clock(block['finished_at'])} |",
        *[f"| {k} | {v} |" for k, v in block.get("about", {}).items()],
        "",
        *_sections(block, compared),
        *extra,
        "<details><summary>全部数值（JSON）</summary>",
        "",
        "```json",
        json.dumps({**block, "comparisons": compared}, ensure_ascii=False, indent=1),
        "```",
        "",
        "</details>",
        "",
    ]
    return "\n".join(lines)


def write(name_parts: list[str], finished_at: float, body: str) -> Path:
    folder = eval_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (
        "_".join([name_parts[0], name_parts[1], stamp(finished_at), *name_parts[2:]])
        + ".md"
    )
    title = f"# {name_parts[0]} · 子任务标注能力记录\n\n"
    if path.exists():
        # Same dataset, same annotator, same hour: another section, never a
        # replaced record.
        with path.open("a") as f:
            f.write("\n---\n\n" + body)
    else:
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(title + body)
        os.replace(temp, path)
    return path


# ---------------------------------------------------------------- person


def _session_path(name: str) -> Path:
    return eval_dir() / ".sessions" / f"{name}.json"


def session(name: str) -> dict | None:
    try:
        return json.loads(_session_path(name).read_text())
    except (OSError, ValueError):
        return None


def start(name: str, who: str = "human") -> dict:
    current = session(name)
    if current:
        return current
    value = {"dataset": name, "started_at": time.time(), "by": who}
    path = _session_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def cancel(name: str) -> None:
    _session_path(name).unlink(missing_ok=True)


def stop(name: str) -> dict:
    """Close the running session: count the episodes confirmed complete
    while it ran and write the record."""
    from levi.annotations import status

    current = session(name)
    if not current:
        raise ValueError("No recording is running for this dataset")
    started, finished = current["started_at"], time.time()
    folder = annotations_dir(name)
    confirmed = sorted(
        ep
        for ep, row in status.read_all(folder).items()
        if started <= row.get("confirmed_at", 0) <= finished
    )
    lengths, _ = metrics.episode_meta(dataset_root(name))
    touched = sorted(
        ep
        for ep in lengths
        if (folder / f"episode_{ep:06d}.json").exists()
        and started <= (folder / f"episode_{ep:06d}.json").stat().st_mtime <= finished
    )
    episodes = episode_segments(name, confirmed)
    block = {
        "schema": "levi.eval.subtask-annotation.v1",
        "kind": "human",
        "driver": "human",
        "dataset": name,
        "dataset_episodes": len(lengths),
        "started_at": started,
        "finished_at": finished,
        "seconds": round(finished - started, 1),
        "about": {
            "统计口径": "记录期间点击「确认本集完成」的 episode；其标注以停止时的保存内容为准",
            "记录期间编辑过但未确认的 episode": ", ".join(
                str(e) for e in touched if e not in confirmed
            )
            or "无",
        },
        "confirmed": confirmed,
        "metrics": metrics.summarize(episodes, vocabulary(name)),
    }
    compared = comparisons(name, episodes)
    path = write([name, "human"], finished, render(block, compared, []))
    cancel(name)
    return {"path": str(path), "episodes": len(confirmed), "block": block}


# ---------------------------------------------------------------- agent


def driver(run: dict) -> str:
    context = run["context"]
    if context.get("imported_from"):
        return context["imported_from"]
    if context.get("pilot_runtime"):
        return "external-pilot"
    kind = (run.get("provider_config") or {}).get("kind") or context.get("provider")
    if kind in {"external", "local-tools"}:
        return "external-mcp"
    if kind == "ollama":
        return (
            "local-vlm-teacher"
            if context.get("supervision", "none") != "none"
            else "local-vlm"
        )
    return "api"


def _cost_lines(e: dict) -> list[str]:
    decisions = e.get("teacher_decisions") or {}
    gates = e.get("human_gates") or {}
    return [
        "### 7. Agent 效率与成本",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 外部（非本地算力）token | {_num(e['external_tokens'])}（{e['external_source']}） |",
        f"| 外部 token / 集 | {_num(e['external_per_episode'])} |",
        f"| 本地 VLM token | {_num(e['local_tokens'])} |",
        f"| 本地 VLM token / 集 | {_num(e['local_per_episode'])} |",
        f"| 本地模型耗时 | {_num(e['local_model_seconds'], 1)} s |",
        f"| LEVI 送达 agent 的 token（下限） | {_num(e.get('delivered_tokens'))} |",
        f"| 工具调用 / 耗时 | {_num(e.get('tool_calls'))} 次 / {_num(e.get('tool_seconds'), 1)} s |",
        f"| 模型请求 / 缓存命中 | {_num(e.get('model_requests'))} / {e.get('cache_hits', '—')} |",
        f"| 被拒调用（重试成本） | {', '.join(f'{k} {v}' for k, v in (e.get('refused_calls') or {}).items()) or '—'} |",
        f"| 老师决定 | {', '.join(f'{k} {v}' for k, v in sorted(decisions.items())) or '—'} |",
        f"| 人工闸门操作 | {', '.join(f'{k} {v}' for k, v in sorted(gates.items())) or '—'} |",
        "",
        (
            "token 口径：外部 token 是非本地算力的消耗（API 计量 / agent 自报 / "
            "LEVI 实测送达量下限，以来源为准）；本地 VLM token 由 LEVI 计量，只占本机 GPU。"
        ),
        "",
    ]


def eligible(run: dict) -> bool:
    """A committed subtask-annotation run over every episode of its dataset."""
    if run.get("status") != "succeeded":
        return False
    if run["context"]["workflow"].get("kind") != "temporal":
        return False
    try:
        lengths, _ = metrics.episode_meta(dataset_root(run["dataset_key"]))
    except (OSError, ValueError, KeyError):
        return False
    return set(run["context"]["episodes"]) >= set(lengths)


def _committed_at(store, run_id) -> float | None:
    from levi.harness.ledger import all_events

    times = [
        e["time"] for e in all_events(store, run_id) if e.get("type") == "committed"
    ]
    return times[-1] if times else None


def agent(store, run: dict, facts: dict) -> Path:
    from levi.harness.cost import of
    from levi.harness.ledger import all_events

    name = run["dataset_key"]
    how = driver(run)
    cost = of(facts, run, all_events(store, run["id"]))
    finished = _committed_at(store, run["id"]) or time.time()
    started = run.get("created_at") or facts.get("started_at")
    window = run["context"].get("imported_window")
    if window:
        started, finished = window
    done = sorted(run.get("completed", []))
    episodes = episode_segments(name, done, run["id"])
    lengths, _ = metrics.episode_meta(dataset_root(name))
    tokens = cost["tokens"]
    local = how.startswith("local-vlm")
    if how == "api":
        external = {"tokens": tokens["metered"], "source": "API 计量"}
    elif how == "local-vlm":
        external = {"tokens": 0, "source": "无外部模型"}
    elif tokens["reported"]:
        external = {"tokens": tokens["reported"], "source": "agent 自报"}
    else:
        external = {
            "tokens": tokens["delivered"],
            "source": "LEVI 实测送达量（下限，不含 agent 自身推理与输出）",
        }
    teaching = [t for t in store.list("teaching") if t.get("run_id") == run["id"]]
    decisions = {}
    for t in teaching:
        d = (t.get("feedback") or {}).get("decision") or t.get("status")
        decisions[d] = decisions.get(d, 0) + 1
    human = facts.get("human_calls", {})
    gates = {
        tool: row["calls"]
        for tool, row in human.items()
        if tool.split(".")[-1] in {"approve", "review", "commit", "resume", "revise"}
    }
    seconds = round(finished - started, 1) if started else None
    n = max(1, len(done))
    efficiency = {
        "driver": how,
        "external_tokens": external["tokens"],
        "external_source": external["source"],
        "external_per_episode": external["tokens"] // n if external["tokens"] else 0,
        "local_tokens": tokens["metered"] if local else 0,
        "local_per_episode": (tokens["metered"] or 0) // n if local else 0,
        "local_model_seconds": cost["seconds"]["model"] if local else None,
        "delivered_tokens": tokens["delivered"],
        "tool_seconds": cost["seconds"]["tool"],
        "tool_calls": sum(row["calls"] for row in facts.get("tools", {}).values()),
        "model_requests": run.get("requests"),
        "cache_hits": run.get("cache_hits", 0),
        "teacher_decisions": decisions,
        "human_gates": gates,
        "breakdown": cost["breakdown"],
        "refused_calls": cost["waste"].get("refused_calls", {}),
    }
    block = {
        "schema": "levi.eval.subtask-annotation.v1",
        "kind": "agent",
        "driver": how,
        "dataset": name,
        "dataset_episodes": len(lengths),
        "run_id": run["id"],
        "model": cost["model"],
        "started_at": started,
        "finished_at": finished,
        "seconds": seconds,
        "about": {
            "run": run["id"],
            "模型": cost["model"] or "外部 agent",
            "驱动形式": DRIVERS.get(how, how),
            "统计口径": "该 run 提交的 episode；只计该 run 写入的子任务",
        },
        "confirmed": done,
        "metrics": metrics.summarize(episodes, vocabulary(name)),
        "efficiency": efficiency,
    }
    lines = _cost_lines(efficiency)
    return write(
        [name, "agent", how],
        finished,
        render(block, comparisons(name, episodes), lines),
    )


def imported(
    name: str,
    driver: str,
    started: float,
    finished: float,
    efficiency: dict,
    about: dict | None = None,
) -> Path:
    """The record of a whole-dataset annotation made outside LEVI and saved
    into the dataset's annotation files afterwards: the same sections as any
    other record, measured from those files, with the cost its maker reports
    (``efficiency`` uses the keys of an agent record's section 7)."""
    if driver not in DRIVERS:
        raise ValueError(f"Unknown driver {driver!r}; one of {sorted(DRIVERS)}")
    lengths, _ = metrics.episode_meta(dataset_root(name))
    episodes = episode_segments(name, sorted(lengths))
    n = max(1, len(episodes))
    e = {
        "driver": driver,
        "external_tokens": 0,
        "external_source": "—",
        "local_tokens": 0,
        "local_model_seconds": None,
        **efficiency,
    }
    e.setdefault("external_per_episode", (e["external_tokens"] or 0) // n)
    e.setdefault("local_per_episode", (e["local_tokens"] or 0) // n)
    block = {
        "schema": "levi.eval.subtask-annotation.v1",
        "kind": "agent",
        "driver": driver,
        "dataset": name,
        "dataset_episodes": len(lengths),
        "run_id": None,
        "model": e.get("model"),
        "started_at": started,
        "finished_at": finished,
        "seconds": round(finished - started, 1),
        "about": {
            "驱动形式": DRIVERS[driver],
            "统计口径": "数据集标注文件中的全部子任务（在 LEVI 之外完成后导入）",
            **(about or {}),
        },
        "confirmed": sorted(ep for ep, row in episodes.items() if row["segments"]),
        "metrics": metrics.summarize(episodes, vocabulary(name)),
        "efficiency": e,
    }
    return write(
        [name, "agent", driver],
        finished,
        render(block, comparisons(name, episodes), _cost_lines(e)),
    )
