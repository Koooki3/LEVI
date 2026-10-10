"""Subtask analysis for the campaign report: the time segments the live
service labelled for a campaign's episodes, per arm.

A multi-model evaluation says how often each arm succeeded. This module adds
*where the trials went*: per arm, the share of valid trials that reached each
subtask, how long each subtask took, and the subtask a failed trial last
completed. The numbers come from the live workspace's analysis records
(``live/analysis.jsonl``, ``levi.live.analysis``); the campaign ledger says
which episodes belong to which arm and which of them failed on the report's
label basis.

An episode is matched by ``<group>/<task folder>/<folder name>``, the key a
legacy-client episode has in the ledger (``guided.scan_rollouts``); an episode
of another source has no such key and is simply not matched. The section
exists only when at least one valid trial has time segments; without data
nothing is added to the report. The segments are the background labelling's
output and have not been reviewed by a person: the section and its table say
so, and none of its numbers enters the success rate, the comparisons or the
conclusion level. No language model writes any text here; every number is
computed and formatted by code.

Standard library plus ``levi.live.analysis`` (also standard-library only).
"""

from __future__ import annotations

import re
from string import Template

from levi.live import analysis as live_analysis

from . import ledger as L

SCHEMA = "levi.aeri.campaign_subtasks.v1"
COVERAGE_MIN = live_analysis.MIN_COVERAGE
FAILURES_MIN = live_analysis.MIN_FAILURES
ID_OK = re.compile(r"[\w\- ]{1,64}")
HEADER = (
    "arm",
    "subtask",
    "valid_trials",
    "reached",
    "reached_share",
    "duration_n",
    "median_s",
    "q1_s",
    "q3_s",
    "failed_last_completed_here",
)


def episode_key(record) -> str | None:
    """The ledger's key for a legacy-client episode, from an analysis
    record; None when the record does not name all three parts."""
    parts = [record.get("group"), record.get("task"), record.get("demo")]
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return "/".join(parts)


def load(live_dir) -> dict:
    """``{episode key: newest analysis record}`` of a live workspace's
    ``live/`` folder. Empty when there is no file. Reads only."""
    found = {}
    for record in live_analysis.latest(live_analysis.read(live_dir)):
        key = episode_key(record)
        if key is not None:
            found[key] = record
    return found


def _clean_id(value) -> str:
    """A subtask id fit for a report: no paths, addresses or serial-like
    numbers (the report's own scrubbing rules), else ``other``."""
    from . import report as R

    text = str(value)
    if R.scrub(text) != text or not ID_OK.fullmatch(text):
        return "other"
    return text


def _clean(record):
    segments = record.get("segments")
    if not isinstance(segments, dict):
        return record
    items = [
        {**i, "id": _clean_id(i.get("id"))}
        for i in segments.get("items") or []
        if isinstance(i, dict)
    ]
    sequence = [_clean_id(x) for x in segments.get("sequence") or []]
    return {
        **record,
        "segments": {**segments, "items": items, "sequence": sequence},
    }


def analyse(ledger: L.Ledger, basis: str, records, stages=live_analysis.STAGES):
    """The per-arm subtask analysis of the campaign's valid trials, or None
    when no valid trial has time segments (nothing is then added to the
    report). ``records``: ``load``'s answer. Failure is the trial's outcome
    on the report's ``basis``; a trial with no outcome on it is left out of
    the landing counts."""
    stages = tuple(_clean_id(s) for s in stages)
    arms = []
    seen_ids: list = []
    any_segments = False
    for arm in dict.fromkeys(r.arm for r in ledger.rows):
        rows = [r for r in ledger.rows if r.arm == arm and r.status == "valid"]
        matched = []
        failed = []
        for row in rows:
            record = records.get(row.episode_id)
            if record is None:
                continue
            record = _clean(record)
            matched.append(record)
            outcome = L.label_value(ledger.labels.get(row.episode_id), basis)[0]
            if outcome == "failure":
                failed.append(record)
        with_seg = [r for r in matched if live_analysis._has_segments(r)]
        any_segments = any_segments or bool(with_seg)
        order = live_analysis.order_stages(with_seg, stages)
        seen_ids += [s for s in order if s not in seen_ids]
        tally = live_analysis.landing_tally(failed, with_seg, stages)
        arms.append(
            {
                "arm": arm,
                "valid_trials": len(rows),
                "with_record": len(matched),
                "with_segments": len(with_seg),
                "funnel": live_analysis.funnel(with_seg, stages),
                "durations": live_analysis.durations(with_seg, stages),
                "landing": tally,
            }
        )
    if not any_segments:
        return None
    for arm in arms:
        arm["warnings"] = _warnings(arm)
    return {
        "schema": SCHEMA,
        "review": live_analysis.AUTO,
        "basis": basis,
        "stages": seen_ids,
        "arms": arms,
    }


def _warnings(arm) -> list:
    out = []
    trials = arm["valid_trials"]
    if trials and arm["with_segments"] / trials < COVERAGE_MIN:
        out.append(
            {
                "code": "low_coverage",
                "n": arm["with_segments"],
                "of": trials,
                "min": COVERAGE_MIN,
            }
        )
    landing = arm["landing"]
    if landing["of"] and landing["with_segments"] < FAILURES_MIN:
        out.append(
            {"code": "few_failures", "n": landing["with_segments"], "min": FAILURES_MIN}
        )
    return out


def table(data: dict) -> tuple[list, list]:
    """``(header, rows)`` of the subtask table: one row per arm and subtask,
    plus a ``none`` row for failed trials that completed nothing. Counts are
    whole numbers, shares and times are numbers or ``None`` (the report's
    ``fmt`` prints ``NA``)."""
    rows = []
    ids = [*data["stages"], "none"]
    for arm in data["arms"]:
        funnel = {s["id"]: s for s in arm["funnel"]["stages"]}
        for sid in ids:
            step = funnel.get(sid)
            landed = arm["landing"]["counts"].get(sid, 0)
            if sid == "none":
                if not landed:
                    continue
                rows.append(
                    [
                        arm["arm"],
                        sid,
                        arm["valid_trials"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        landed,
                    ]
                )
                continue
            length = arm["durations"].get(sid) or {}
            rows.append(
                [
                    arm["arm"],
                    sid,
                    arm["valid_trials"],
                    step["reached"]["n"] if step else 0,
                    step["reached"]["rate"] if step else None,
                    length.get("n", 0),
                    length.get("median_s"),
                    length.get("q1_s"),
                    length.get("q3_s"),
                    landed,
                ]
            )
    return list(HEADER), rows


def text_table(data: dict, fmt) -> tuple[list, list]:
    """``table`` with every cell printed through the report's ``fmt`` (what
    the report's CSV and TeX tables hold)."""
    header, rows = table(data)
    kinds = (None, None, "int", "int", "pct", "int", "stat", "stat", "stat", "int")
    return header, [
        [c if k is None else fmt(c, k) for c, k in zip(r, kinds, strict=True)]
        for r in rows
    ]


SECTION = {
    "en": Template(
        """## Subtask analysis (automatic annotation, unreviewed)

The time segments below were labelled by the background model and no person has reviewed them. They show where trials went and where failed trials stopped; none of these numbers is part of the success rates, the comparisons or the conclusion level above. A trial counts as failed on the label basis named at the top of this summary. "Reached" means the trial has that subtask or a later one; the last column counts failed trials whose last completed subtask (not marked as failed) is the row's, and `none` counts failed trials that completed no subtask.

$coverage

$table
$warnings"""
    ),
    "zh-CN": Template(
        """## 子任务分析（自动标注，未复核）

下面的时间片段由后台模型标注，没有人复核过。它们说明各组的试验走到了哪一步、失败的试验停在哪里；这些数字不进入上文的成功率、比较和结论级别。试验是否失败按本摘要开头写明的标签口径判定。“到达”指试验有该子任务或更靠后的子任务；最后一列统计失败试验中，最后完成（未被标为失败）的子任务正是该行子任务的个数，`none` 是没有完成任何子任务的失败试验。

$coverage

$table
$warnings"""
    ),
}
WORDS = {
    "en": {
        "coverage": "Arm %s: %s of %s valid trials have time segments (automatic, unreviewed).",
        "head": [
            "arm",
            "subtask",
            "valid trials",
            "reached",
            "share",
            "n",
            "median s",
            "Q1 s",
            "Q3 s",
            "failed here",
        ],
        "warn": "Warnings",
        "low_coverage": "Arm {arm}: time segments cover {n} of {of} valid trials (below {min:.0%}); read the table as a partial view.",
        "few_failures": "Arm {arm}: only {n} failed trials have time segments (fewer than {min}); the stopping points are anecdotal.",
    },
    "zh-CN": {
        "coverage": "组 %s：%s/%s 个有效试验有时间片段（自动、未复核）。",
        "head": [
            "组",
            "子任务",
            "有效试验",
            "到达",
            "占比",
            "个数",
            "中位数（秒）",
            "下四分位（秒）",
            "上四分位（秒）",
            "在此失败",
        ],
        "warn": "警告",
        "low_coverage": "组 {arm}：时间片段只覆盖 {n}/{of} 个有效试验（低于 {min:.0%}），表只是局部视图。",
        "few_failures": "组 {arm}：带时间片段的失败试验只有 {n} 个（少于 {min}），停止位置只能当个例看。",
    },
}


def section(data: dict, lang: str, fmt) -> str:
    """The report section as Markdown in ``lang`` (``en`` or ``zh-CN``).
    ``fmt`` is the report's number formatter, so every number is printed the
    way the rest of the report prints it."""
    words = WORDS[lang]
    coverage = "\n".join(
        words["coverage"]
        % (a["arm"], fmt(a["with_segments"], "int"), fmt(a["valid_trials"], "int"))
        for a in data["arms"]
    )
    header, rows = text_table(data, fmt)
    lines = ["| " + " | ".join(words["head"]) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    warns = [
        "- "
        + words[w["code"]].format(
            arm=a["arm"], **{k: v for k, v in w.items() if k != "code"}
        )
        for a in data["arms"]
        for w in a["warnings"]
    ]
    text = SECTION[lang].substitute(
        coverage=coverage,
        table="\n".join(lines),
        warnings=("\n**" + words["warn"] + "**\n\n" + "\n".join(warns) + "\n")
        if warns
        else "",
    )
    return text.rstrip() + "\n"


def splice(text: str, extra: str) -> str:
    """``text`` with ``extra`` inserted before its last paragraph (the
    summary's closing line about where the numbers come from); appended when
    the text has a single paragraph."""
    head, sep, tail = text.rstrip("\n").rpartition("\n\n")
    if not sep:
        return text.rstrip("\n") + "\n\n" + extra
    return head + "\n\n" + extra + "\n" + tail + "\n"
