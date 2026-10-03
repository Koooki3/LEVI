"""Text forms of the live statistics: CSV (the per-episode table), JSON and
Markdown (English or Chinese). Pure functions over what ``statsview.build``
returns; no file is read here. A figure that was not measured prints as ``-``
(never as 0).
"""

import csv
import io
import json
import time

EPISODE_COLUMNS = (
    "dataset",
    "demo",
    "episode_index",
    "session",
    "at",
    "state",
    "attempts",
    "episode_seconds",
    "to_mirror_s",
    "to_first_request_s",
    "to_commit_s",
    "to_verdict_s",
    "requests",
    "model_seconds",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "images",
    "closed_wait_s",
    "segments",
    "verdict",
    "review",
    "in_session",
    "excluded",
)

KEY_FIGURES = (
    ("episodes", "episodes.count"),
    ("median_commit", "latency.to_commit_s.median"),
    ("median_verdict", "latency.to_verdict_s.median"),
    ("p90_verdict", "latency.to_verdict_s.p90"),
    ("tokens_per_episode", "model.tokens_per_episode.mean"),
    ("realtime", "throughput.realtime_factor"),
    ("in_session", "in_session.ratio"),
    ("gate_wait", "gpu.closed_wait_s"),
)

TEXT = {
    "en": {
        "title": "Live annotation statistics",
        "scope": "Scope",
        "all": "all datasets and sessions",
        "dataset": "dataset",
        "session": "session",
        "since": "since",
        "generated": "Generated",
        "setup": "Setup",
        "facts": "Facts",
        "episodes": "Episodes",
        "labelled": "labelled",
        "failed": "failed",
        "retrying": "waiting for a retry",
        "retried": "needed a retry",
        "excluded": "removed by a person (left out of these figures)",
        "excluded_included": "removed by a person (included in these figures)",
        "segments": "Time segments",
        "segments_per": "time segments per episode",
        "labels": "Time-segment labels",
        "verdicts": "Automatic success/failure",
        "review": "Who committed the segments",
        "latency": "Latency (seconds after the episode ended)",
        "stage": "stage",
        "to_mirror_s": "mirrored",
        "to_plan_s": "plan ready",
        "to_first_request_s": "first model request",
        "to_commit_s": "time segments committed",
        "to_verdict_s": "success/failure decided",
        "n": "n",
        "median": "median",
        "p90": "p90",
        "max": "max",
        "overhead": "Throughput and model cost",
        "realtime": "real-time factor (episode seconds per model second)",
        "wall": "episode seconds per wall-clock second",
        "span": "wall-clock span, first episode end to last verdict (s)",
        "requests_per": "model requests per episode (median)",
        "tokens_total": "tokens in total",
        "tokens_per": "tokens per episode (mean / median)",
        "prompt_share": "prompt share of tokens",
        "images_per": "images per episode (mean)",
        "external": "external tokens",
        "probe_tokens": "request-cost calibration tokens (not in the total)",
        "reserved": "reserved tokens for steps without server usage (steps; not in the total)",
        "by_kind": "By kind of request",
        "kind": "kind",
        "requests": "requests",
        "seconds": "model seconds",
        "seconds_share": "time share",
        "tokens": "tokens",
        "tokens_share": "token share",
        "gpu": "GPU and gate",
        "batches": "batches labelled",
        "gate_wait": "waited for a closed gate (s)",
        "interruptions": "interruptions by the gate",
        "wakes": "vLLM wakes (count, total s, max s)",
        "cold": "vLLM cold starts (count, total s)",
        "sleeps": "vLLM sleeps",
        "unknown": "not recorded",
        "gate_closed": "gate closed in the window (s, share, times)",
        "in_session": "Labelled during the session",
        "in_session_note": "episodes whose first model request began before the session's last episode ended",
        "sessions": "Sessions",
        "episode_table": "Per episode",
        "compare": "Compared with the previous report",
        "metric": "metric",
        "value": "value",
        "outcome": "success/failure",
        "now": "this report",
        "before": "previous",
        "change": "change",
        "notes": "How to read this",
        "yes": "yes",
        "no": "no",
        "episode_seconds": "episode seconds",
        "tokens_short": "tokens",
        "model_s": "model s",
        "commit": "commit",
        "verdict": "verdict",
        "metrics": {
            "episodes": "episodes",
            "median_commit": "median time to committed segments (s)",
            "median_verdict": "median time to verdict (s)",
            "p90_verdict": "p90 time to verdict (s)",
            "tokens_per_episode": "tokens per episode (mean)",
            "realtime": "real-time factor",
            "in_session": "labelled during the session",
            "gate_wait": "waited for a closed gate (s)",
        },
        "notes_text": [
            "Tokens are what the server reported for the steps it reported (total = prompt + completion); calibration and reserved tokens are listed apart and not added in.",
            "Episodes a person removed are left out unless this report says it includes them.",
            "Per-episode figures use the newest record of each episode; token, request and gate totals count every attempt.",
            "Latency is measured from the episode's completion marker. p90 is the 90th percentile by linear interpolation.",
            "Real-time factor = total episode seconds / total model seconds (model time is the sum of request durations, not wall-clock). Above 1 means the model labels faster than the episode ran.",
            "Labelled during the session = share of episodes whose first model request started before the last episode of their session ended; the last episode can never count, so the figure is at most (n-1)/n.",
            "A dash means the figure was not measured (an older record, an episode that made no request). It is not zero.",
            "The automatic success/failure and the time segments are unreviewed; their accuracy has not been evaluated.",
        ],
    },
    "zh": {
        "title": "后台实时标注统计",
        "scope": "范围",
        "all": "全部数据集和会话",
        "dataset": "数据集",
        "session": "会话",
        "since": "起始时间",
        "generated": "生成时间",
        "setup": "设置",
        "facts": "事实",
        "episodes": "片段",
        "labelled": "已标注",
        "failed": "失败",
        "retrying": "等待重试",
        "retried": "重试过",
        "excluded": "被人工排除（不计入这些数字）",
        "excluded_included": "被人工排除（计入这些数字）",
        "segments": "时间片段",
        "segments_per": "每个片段的时间片段数",
        "labels": "时间片段标签",
        "verdicts": "自动成败判定",
        "review": "时间片段由谁提交",
        "latency": "延迟（片段结束后的秒数）",
        "stage": "阶段",
        "to_mirror_s": "镜像完成",
        "to_plan_s": "计划就绪",
        "to_first_request_s": "首个模型请求",
        "to_commit_s": "时间片段提交",
        "to_verdict_s": "成败判定",
        "n": "个数",
        "median": "中位",
        "p90": "p90",
        "max": "最大",
        "overhead": "吞吐与模型开销",
        "realtime": "实时倍率（片段秒数 ÷ 模型秒数）",
        "wall": "每墙钟秒标注的片段秒数",
        "span": "墙钟跨度，首个片段结束到最后一次判定（秒）",
        "requests_per": "每个片段的模型请求数（中位）",
        "tokens_total": "token 总数",
        "tokens_per": "每个片段的 token（均值 / 中位）",
        "prompt_share": "提示词占 token 比例",
        "images_per": "每个片段的图片数（均值）",
        "external": "外部 token",
        "probe_tokens": "请求开销校准的 token（不计入总数）",
        "reserved": "服务器没给用量的步骤的预留 token（步数；不计入总数）",
        "by_kind": "按请求种类",
        "kind": "种类",
        "requests": "请求数",
        "seconds": "模型秒数",
        "seconds_share": "耗时占比",
        "tokens": "token",
        "tokens_share": "token 占比",
        "gpu": "GPU 与门控",
        "batches": "标注批次数",
        "gate_wait": "等门控关闭的累计秒数",
        "interruptions": "被门控打断次数",
        "wakes": "vLLM 唤醒（次数、合计秒、最长秒）",
        "cold": "vLLM 冷启动（次数、合计秒）",
        "sleeps": "vLLM 睡眠",
        "unknown": "未记录",
        "gate_closed": "窗口内门控关闭（秒、占比、次数）",
        "in_session": "会话内标注比例",
        "in_session_note": "首个模型请求发生在所属会话最后一个片段结束之前的片段占比",
        "sessions": "会话",
        "episode_table": "逐片段",
        "compare": "与上一份报告对比",
        "metric": "指标",
        "value": "数值",
        "outcome": "成败",
        "now": "本报告",
        "before": "上一份",
        "change": "变化",
        "notes": "口径说明",
        "yes": "是",
        "no": "否",
        "episode_seconds": "片段秒数",
        "tokens_short": "token",
        "model_s": "模型秒",
        "commit": "提交",
        "verdict": "判定",
        "metrics": {
            "episodes": "片段数",
            "median_commit": "到时间片段提交的中位秒数",
            "median_verdict": "到成败判定的中位秒数",
            "p90_verdict": "到成败判定的 p90 秒数",
            "tokens_per_episode": "每个片段的 token（均值）",
            "realtime": "实时倍率",
            "in_session": "会话内标注比例",
            "gate_wait": "等门控关闭的秒数",
        },
        "notes_text": [
            "token 取服务器报告了用量的步骤的数字（总数 = prompt + completion）；校准和预留 token 单独列出，不加进总数。",
            "被人排除的片段不计入，除非本报告注明包含它们。",
            "逐片段的数字取每个片段最新的一条记录；token、请求数和门控的合计包含每一次尝试。",
            "延迟从片段的完成标记算起。p90 是第 90 百分位（线性插值）。",
            "实时倍率 = 片段总秒数 ÷ 模型总秒数（模型时间是各请求耗时之和，不是墙钟）。大于 1 表示模型标注得比片段本身的时长快。",
            "会话内标注比例 = 首个模型请求早于所属会话最后一个片段结束的片段占比；最后一个片段本身永远不可能计入，所以最大是 (n−1)/n。",
            "“-”表示没有测到（旧记录，或该片段没有发出请求），不是 0。",
            "自动成败判定和时间片段未经人工审核，准确率没有评估过。",
        ],
    },
}


def lookup(payload, dotted):
    """``payload["a"]["b"]`` for ``"a.b"``; None when a step is missing."""
    for key in dotted.split("."):
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def num(value, digits=1, suffix=""):
    """A number for a table cell; ``-`` when it is missing."""
    if isinstance(value, bool) or value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.{digits}f}"
        return text.rstrip("0").rstrip(".") + suffix if "." in text else text + suffix
    return f"{value}{suffix}"


def pct(value):
    return "-" if value is None else f"{value * 100:.0f}%"


def when(epoch) -> str:
    if epoch is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


# --- CSV and JSON ------------------------------------------------------------


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value):
    """A cell a spreadsheet will not run as a formula: a text that starts with
    ``= + - @`` or a tab or return gets a leading apostrophe. Numbers and
    booleans are written as they are."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def to_csv(payload) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EPISODE_COLUMNS)
    for row in (payload.get("episodes") or {}).get("rows") or []:
        writer.writerow([csv_cell(row.get(c)) for c in EPISODE_COLUMNS])
    return out.getvalue()


def to_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False)


# --- Markdown ---------------------------------------------------------------------


def table(header, rows) -> list:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return lines + [""]


def pairs(rows, t) -> list:
    return table([t["metric"], t["value"]], rows)


def to_markdown(
    payload, lang="en", *, title=None, settings=None, previous=None, max_rows=None
) -> str:
    """The statistics as a Markdown report. ``settings`` is a dict of
    ``{section: {key: value}}`` printed under "Setup"; ``previous`` is an
    earlier payload to compare with."""
    t = TEXT.get(lang) or TEXT["en"]
    s = payload.get("summary") or {}
    scope = payload.get("scope") or {}
    ep = s.get("episodes") or {}
    out = [f"# {title or t['title']}", ""]
    parts = [f"{t[k]}: {scope[k]}" for k in ("dataset", "session") if scope.get(k)]
    if scope.get("since") is not None:
        parts.append(f"{t['since']}: {when(scope['since'])}")
    out.append(f"{t['scope']}: {', '.join(parts) or t['all']}  ")
    out.append(f"{t['generated']}: {when(payload.get('generated_at'))}")
    out.append("")
    if settings:
        out += [f"## {t['setup']}", ""]
        out += pairs(
            [
                (f"{sec}.{key}", val)
                for sec, items in settings.items()
                for key, val in items.items()
            ],
            t,
        )

    out += [f"## {t['facts']}", ""]
    verdicts = (s.get("outcome") or {}).get("verdicts") or {}
    labels = (s.get("outcome") or {}).get("labels") or {}
    review = (s.get("outcome") or {}).get("review") or {}
    segments = s.get("outcome") or {}
    per = segments.get("segments_per_episode") or {}
    ins = s.get("in_session") or {}
    rows = [
        (t["episodes"], num(ep.get("count"))),
        (t["labelled"], num(ep.get("done"))),
        (t["failed"], num(ep.get("failed"))),
        (t["retrying"], num(ep.get("retrying"))),
        (t["retried"], num(ep.get("retried"))),
        (
            t["excluded_included"]
            if payload.get("include_excluded")
            else t["excluded"],
            num(payload.get("excluded_demos")),
        ),
        (t["segments"], num(segments.get("segments_total"))),
        (
            t["segments_per"],
            "-"
            if per.get("mean") is None
            else f"{num(per.get('mean'))} ({num(per.get('min'))}-{num(per.get('max'))})",
        ),
        (t["labels"], ", ".join(f"{k} {v}" for k, v in labels.items()) or "-"),
        (
            t["verdicts"],
            ", ".join(f"{k} {v}" for k, v in sorted(verdicts.items())) or "-",
        ),
        (t["review"], ", ".join(f"{k} {v}" for k, v in sorted(review.items())) or "-"),
        (
            t["in_session"],
            f"{pct(ins.get('ratio'))} ({num(ins.get('count'))}/{num(ins.get('evaluable'))})",
        ),
    ]
    out += pairs(rows, t)

    out += [f"## {t['latency']}", ""]
    out += table(
        [t["stage"], t["n"], t["median"], t["p90"], t["max"]],
        [
            [
                t[key],
                num(d.get("n")),
                num(d.get("median")),
                num(d.get("p90")),
                num(d.get("max")),
            ]
            for key, d in (
                (k, (s.get("latency") or {}).get(k) or {})
                for k in (
                    "to_mirror_s",
                    "to_plan_s",
                    "to_first_request_s",
                    "to_commit_s",
                    "to_verdict_s",
                )
            )
        ],
    )

    th = s.get("throughput") or {}
    m = s.get("model") or {}
    tpe = m.get("tokens_per_episode") or {}
    out += [f"## {t['overhead']}", ""]
    out += pairs(
        [
            (t["realtime"], num(th.get("realtime_factor"), 2)),
            (t["wall"], num(th.get("wall_factor"), 3)),
            (t["span"], num(th.get("span_s"))),
            (
                t["requests_per"],
                num((m.get("requests_per_episode") or {}).get("median")),
            ),
            (t["tokens_total"], num(m.get("tokens_total"))),
            (
                t["tokens_per"],
                f"{num(tpe.get('mean'), 0)} / {num(tpe.get('median'), 0)}",
            ),
            (t["prompt_share"], pct(m.get("prompt_share"))),
            (t["images_per"], num((m.get("images_per_episode") or {}).get("mean"))),
            (t["probe_tokens"], num(m.get("probe_tokens"))),
            (
                t["reserved"],
                f"{num(m.get('reserved_tokens'))} ({num(m.get('unreported_steps'))})",
            ),
            (t["external"], num(m.get("external_tokens"))),
        ],
        t,
    )
    out += [f"### {t['by_kind']}", ""]
    out += table(
        [
            t["kind"],
            t["requests"],
            t["seconds"],
            t["seconds_share"],
            t["tokens"],
            t["tokens_share"],
        ],
        [
            [
                kind,
                num(v.get("requests")),
                num(v.get("seconds")),
                pct(v.get("seconds_share")),
                num(v.get("tokens")),
                pct(v.get("tokens_share")),
            ]
            for kind, v in (m.get("by_kind") or {}).items()
        ],
    )

    g = s.get("gpu") or {}
    wake = g.get("vllm_wakes") or {}
    cold = g.get("vllm_cold_starts") or {}
    window = g.get("gate_window")
    out += [f"## {t['gpu']}", ""]
    out += pairs(
        [
            (t["batches"], num(g.get("batches"))),
            (t["gate_wait"], num(g.get("closed_wait_s"))),
            (t["interruptions"], num(g.get("interruptions"))),
            (
                t["wakes"],
                f"{num(wake.get('count'))}, {num(wake.get('total_s'), 2)}, {num(wake.get('max_s'), 2)}",
            ),
            (t["cold"], f"{num(cold.get('count'))}, {num(cold.get('total_s'))}"),
            (
                t["sleeps"],
                t["unknown"] if g.get("vllm_sleeps") is None else num(g["vllm_sleeps"]),
            ),
            (
                t["gate_closed"],
                "-"
                if not window
                else f"{num(window.get('closed_s'))}, {pct(window.get('closed_share'))}, {num(window.get('closures'))}",
            ),
        ],
        t,
    )

    sessions = payload.get("sessions") or []
    if sessions:
        out += [f"## {t['sessions']}", ""]
        out += table(
            [
                t["dataset"],
                t["session"],
                t["episodes"],
                t["labelled"],
                t["failed"],
                t["commit"],
                t["verdict"],
                "p90",
                t["tokens_short"],
                t["model_s"],
                t["realtime"].split(" (")[0],
                t["in_session"],
            ],
            [
                [
                    r.get("dataset"),
                    r.get("session") or "-",
                    num(r.get("episodes")),
                    num(r.get("done")),
                    num(r.get("failed")),
                    num(r.get("to_commit_median_s")),
                    num(r.get("to_verdict_median_s")),
                    num(r.get("to_verdict_p90_s")),
                    num(r.get("tokens")),
                    num(r.get("model_seconds")),
                    num(r.get("realtime_factor"), 2),
                    pct(r.get("in_session_ratio")),
                ]
                for r in sessions
            ],
        )

    if previous is not None:
        out += [f"## {t['compare']}", ""]
        body = []
        for key, dotted in KEY_FIGURES:
            now = lookup(s, dotted)
            before = lookup(previous.get("summary") or {}, dotted)
            digits = {"realtime": 2, "episodes": 0, "tokens_per_episode": 0}.get(key, 1)
            if key == "in_session":
                a, b = pct(now), pct(before)
            else:
                a, b = num(now, digits), num(before, digits)
            if isinstance(now, (int, float)) and isinstance(before, (int, float)):
                delta = now - before
                change = (
                    f"{delta * 100:+.0f} pp"
                    if key == "in_session"
                    else f"{delta:+.{digits}f}"
                )
            else:
                change = "-"
            body.append([t["metrics"][key], a, b, change])
        out += table([t["metric"], t["now"], t["before"], t["change"]], body)

    episodes = (payload.get("episodes") or {}).get("rows") or []
    if episodes:
        shown = episodes if max_rows is None else episodes[:max_rows]
        out += [
            f"## {t['episode_table']} ({len(shown)}/{(payload['episodes'] or {}).get('total', len(shown))})",
            "",
        ]
        out += table(
            [
                "demo",
                t["session"],
                t["episode_seconds"],
                "mirror",
                "1st req",
                t["commit"],
                t["verdict"],
                t["requests"],
                t["model_s"],
                t["tokens_short"],
                t["segments"],
                t["outcome"],
                t["in_session"],
            ],
            [
                [
                    r.get("demo"),
                    r.get("session") or "-",
                    num(r.get("episode_seconds")),
                    num(r.get("to_mirror_s")),
                    num(r.get("to_first_request_s")),
                    num(r.get("to_commit_s")),
                    num(r.get("to_verdict_s")),
                    num(r.get("requests")),
                    num(r.get("model_seconds")),
                    num(r.get("total_tokens")),
                    num(r.get("segments")),
                    r.get("verdict") or "-",
                    "-"
                    if r.get("in_session") is None
                    else (t["yes"] if r["in_session"] else t["no"]),
                ]
                for r in shown
            ],
        )

    out += [f"## {t['notes']}", ""]
    out += [f"- {line}" for line in t["notes_text"]]
    out.append("")
    return "\n".join(out)
