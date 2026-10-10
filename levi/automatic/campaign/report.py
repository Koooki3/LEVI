"""The campaign report generator (T-CP-06, design X3 §2, §3, §4).

``analyse`` feeds the trial ledger and one label basis to the analysis
library (``levi.automatic.analysis``) and returns every number in one JSON
document (schema ``levi.aeri.campaign_report.v1``). ``write_report`` turns
it into ``<report root>/<basis>/``::

    summary.en.md  summary.zh-CN.md
    tables/   success  pairwise  continuous  failure_modes  agreement  power (.csv, .tex); drift.csv
    figures/  f1-success ... f7-agreement (.svg, .zh-CN.svg, .pdf, .json)
    data/     trials.parquet  trials.csv  labels.csv  analysis.json
    manifest.json

One folder per basis; writing one never touches another. The folder is
built aside under a per-basis lock and swapped in whole, so a reader sees
the old report or the new one.

**Words.** The summaries are filled from fixed templates
(``templates/``) by rules; no language model is called and every number is
formatted from ``analysis.json`` by ``fmt``. A conclusion is confirmatory
only when every condition of design §4.4 holds (pre-registered, planned n
reached, no peeks, a basis a person reviewed, no drift warning, a schedule
other than blocked or interleaved, and the library's planned-power rule);
otherwise each conclusion sentence is marked exploratory. Phrases that
claim more than the data ("significantly outperforms", "proves", ...)
are refused outside the confirmatory branch (``check_wording``). The basis
names are enforced (``check_naming``): an automatic basis is always an
"automatic-verdict success rate (unreviewed)", and only the adjudicated
basis may say "ground truth". Post-hoc power is never reported; the power
table gives the design's detectable differences.

**Privacy.** The manifest never holds names or e-mail addresses; camera
serial numbers, IP addresses, host names and local paths are dropped
unless ``include_site_details`` is set. Principals are opaque ids.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from levi.automatic import analysis as an
from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis.references import REFERENCES
from levi.automatic.figure_files import write_durable, write_figure

from . import ledger as L

SCHEMA = "levi.aeri.campaign_report.v1"
MANIFEST_SCHEMA = "levi.aeri.campaign_report_manifest.v1"
TEMPLATES = Path(__file__).with_name("templates")
LANGS = ("en", "zh-CN")
COVERAGE_GAP = 0.10
DESIGN_NS = (20, 25, 30, 50)
DESIGN_BASELINES = (0.5, 0.3)
SCHEDULES_EXPLORATORY = frozenset({"blocked", "interleaved"})
SCHEDULES = (
    "randomized_blocks",
    "counterbalanced_segments",
    "latin_square",
    "interleaved",
    "blocked",
)
REVIEWED_BASES = frozenset(
    {"operator_label", "adjudicated_ground_truth", "adjudicated_then_operator"}
)
BLIND = ("full", "partial", "none")
# Phrases that claim more than an interval shows (design §4.4): allowed only
# in a confirmatory sentence and only right before its interval.
FORBIDDEN = (
    "显著优于",
    "明显优于",
    "证明",
    "significantly outperforms",
    "proves",
    "state-of-the-art",
)
BASIS_NAMES = {
    "autonomous_verdict": {
        "en": "automatic-verdict success rate (unreviewed)",
        "zh-CN": "自动判定成功率（未经人工核实）",
    },
    "posthoc_verdict": {
        "en": "automatic-verdict success rate (unreviewed)",
        "zh-CN": "自动判定成功率（未经人工核实）",
    },
    "operator_label": {
        "en": "success rate (operator label)",
        "zh-CN": "成功率（操作员标签）",
    },
    "adjudicated_ground_truth": {
        "en": "ground-truth success rate",
        "zh-CN": "真值成功率",
    },
    "adjudicated_then_operator": {
        "en": "success rate (adjudicated where available, else operator)",
        "zh-CN": "成功率（裁定优先，否则操作员）",
    },
}
TRUTH_WORDS = ("ground-truth", "ground truth", "真值")
STRATA = ("all", "budget", "operator_stop", "early_stop", "unknown")
CONDITIONS = (
    "preregistered",
    "sample_size_reached",
    "no_peeks",
    "reviewed_basis",
    "no_drift_warning",
    "schedule_allows",
    "powered_design",
)


# The end of a sentence: a full stop that is not a decimal point.
SENTENCE_END = re.compile(r"(?<!\d)\.|\.(?!\d)|。|\n")


class ReportError(ValueError):
    """The report cannot be produced as asked."""


class NamingError(ReportError):
    """A text names a rate in a way its label basis does not allow."""


class WordingError(ReportError):
    """A text claims more than the analysis allows."""


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class ArmInfo:
    id: str
    role: str = "candidate"
    checkpoint: str | None = None
    config: str | None = None
    sha256_status: str = "none"
    manifest_sha256: str | None = None
    versions: str = ""
    not_verified: tuple[str, ...] = ()

    def __post_init__(self):
        L._slug(self.id, "arm", L.ARM)
        if self.role not in ("candidate", "reference"):
            raise ReportError(f"arm role {self.role!r}")
        if self.sha256_status not in ("verified", "recorded", "none"):
            raise ReportError(f"sha256_status {self.sha256_status!r}")
        object.__setattr__(self, "not_verified", tuple(self.not_verified))
        if self.checkpoint is not None:
            # Only the checkpoint's full name: never a local path.
            object.__setattr__(
                self, "checkpoint", Path(str(self.checkpoint)).name or None
            )


@dataclass(frozen=True)
class CampaignInfo:
    campaign_id: str
    arms: tuple[ArmInfo, ...]
    task: str = ""
    schedule: str = "counterbalanced_segments"
    seed: int = 0
    trials_per_arm: int | None = None
    comparison: tuple[str, str] | None = None  # (B, A): B minus A
    alpha: float = 0.05
    label_basis: str = "operator_label"
    preregistered: bool = False
    design_difference: float | None = None
    design_baseline: float | None = None
    peeks: int = 0
    operator_blind: str = "none"
    reset_mode: str | None = None
    scene_check: str | None = None
    treatment_includes_reset: bool = False
    campaign_sha256: str | None = None
    settings_sha256: str | None = None
    switches: int | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        L._slug(self.campaign_id, "campaign id")
        object.__setattr__(self, "arms", tuple(self.arms))
        ids = [a.id for a in self.arms]
        if len(ids) < 2 or len(set(ids)) != len(ids):
            raise ReportError("a campaign compares two or more distinct arms")
        if sum(a.role == "reference" for a in self.arms) > 1:
            raise ReportError("at most one reference arm")
        if self.schedule not in SCHEDULES:
            raise ReportError(f"schedule {self.schedule!r}")
        if self.operator_blind not in BLIND:
            raise ReportError(f"operator_blind {self.operator_blind!r}")
        if self.label_basis not in L.LABEL_BASES:
            raise ReportError(f"label basis {self.label_basis!r}")
        if self.comparison is not None:
            pair = tuple(self.comparison)
            if len(pair) != 2 or pair[0] == pair[1] or not set(pair) <= set(ids):
                raise ReportError("comparison names two arms of the campaign")
            object.__setattr__(self, "comparison", pair)
        if isinstance(self.peeks, bool) or not isinstance(self.peeks, int):
            raise ReportError("peeks is a count")

    @property
    def reference(self) -> str | None:
        return next((a.id for a in self.arms if a.role == "reference"), None)

    @staticmethod
    def from_dict(data: dict) -> CampaignInfo:
        data = dict(data)
        primary = data.pop("primary", {}) or {}
        arms = tuple(
            ArmInfo(**{k: v for k, v in a.items() if k in ArmInfo.__dataclass_fields__})
            for a in data.pop("arms")
        )
        known = set(CampaignInfo.__dataclass_fields__)
        fields_ = {k: v for k, v in data.items() if k in known}
        for key, target in (
            ("comparison", "comparison"),
            ("alpha", "alpha"),
            ("label_basis", "label_basis"),
            ("preregistered", "preregistered"),
            ("design_difference", "design_difference"),
            ("design_baseline", "design_baseline"),
        ):
            if key in primary:
                fields_[target] = primary[key]
        if "comparison" in fields_ and fields_["comparison"] is not None:
            fields_["comparison"] = tuple(fields_["comparison"])
        return CampaignInfo(arms=arms, **fields_)


# ------------------------------------------------------------- formatting


def fmt(value, kind: str = "stat") -> str:
    """Every number a report prints goes through here. ``rate`` and
    ``stat``: three decimals; ``diff``: three decimals with a sign;
    ``pct``: a percentage with one decimal; ``p``: three decimals, or
    ``<0.001``; ``int``: a whole number. None (or a non-finite value)
    prints ``NA``."""
    if value is None or isinstance(value, bool):
        return "NA" if value is None else str(value).lower()
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "NA"
    if kind == "int":
        return str(int(value))
    if kind == "pct":
        return f"{100 * value:.1f}%"
    if kind == "p":
        return "<0.001" if value < 0.001 else f"{value:.3f}"
    if kind == "diff":
        return f"{value:+.3f}"
    return f"{value:.3f}"


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# What ``fmt`` prints for a number: never treated as a formula.
NUMBER_TEXT = re.compile(r"[+-]?\d+(\.\d+)?%?|<0\.001")


def csv_cell(value) -> str:
    """Text for a CSV cell. A text cell that a spreadsheet would run as a
    formula gets a leading apostrophe; numbers stay as they are."""
    if (
        isinstance(value, str)
        and value.startswith(FORMULA_START)
        and not NUMBER_TEXT.fullmatch(value)
    ):
        return "'" + value
    return value


TEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
}


def tex_escape(text: str) -> str:
    return "".join(TEX_SPECIAL.get(c, c) for c in str(text).replace("\n", " "))


def table_csv(header, rows) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([csv_cell(h) for h in header])
    for row in rows:
        writer.writerow([csv_cell(c) for c in row])
    return out.getvalue().encode("utf-8")


def table_tex(header, rows) -> bytes:
    """``tabular`` and ``\\hline`` only: no extra package."""
    spec = "l" + "r" * (len(header) - 1)
    lines = [f"\\begin{{tabular}}{{{spec}}}", "\\hline"]
    lines.append(" & ".join(tex_escape(h) for h in header) + r" \\")
    lines.append("\\hline")
    for row in rows:
        lines.append(" & ".join(tex_escape(c) for c in row) + r" \\")
    lines += ["\\hline", "\\end{tabular}", ""]
    return "\n".join(lines).encode("utf-8")


# ------------------------------------------------------------------ texts


def sentences() -> dict:
    return json.loads((TEMPLATES / "sentences.json").read_text(encoding="utf-8"))


def summary_template(lang: str) -> str:
    return (TEMPLATES / f"summary.{lang}.md").read_text(encoding="utf-8")


def basis_name(basis: str, lang: str = "en") -> str:
    return BASIS_NAMES[basis][lang]


def check_naming(text: str, basis: str, lang: str | None = None) -> None:
    """Refuse a text that names its rates wrongly for ``basis``: only the
    adjudicated basis may say ground truth; a text about an automatic basis
    must call it the automatic-verdict rate (unreviewed). ``lang`` set: the
    text must also carry the basis name in that language."""
    lowered = text.lower()
    if basis != "adjudicated_ground_truth":
        for word in TRUTH_WORDS:
            if word in lowered:
                raise NamingError(f"{word!r} is reserved for adjudicated labels")
    if lang is not None and basis_name(basis, lang) not in text:
        raise NamingError(
            f"the {basis} text must name its rate {basis_name(basis, lang)!r}"
        )


def check_wording(text: str, *, confirmatory: bool, mask=()) -> None:
    """Refuse phrases that claim more than an interval shows. They may
    appear only in a confirmatory text, and each must be followed in the
    same sentence by a 95 % interval. ``mask``: strings the caller supplied
    (ids, task text) that are not the report's wording."""
    for item in sorted({m for m in mask if m}, key=len, reverse=True):
        text = text.replace(item, "⁣")
    lowered = text.lower()
    for word in FORBIDDEN:
        start = lowered.find(word.lower())
        while start != -1:
            if not confirmatory:
                raise WordingError(f"{word!r} outside a confirmatory conclusion")
            rest = SENTENCE_END.split(lowered[start:], maxsplit=1)[0]
            if "95% ci [" not in rest:
                raise WordingError(f"{word!r} is not followed by its interval")
            start = lowered.find(word.lower(), start + 1)


# --------------------------------------------------------------- analysis


def _outcomes(ledger: L.Ledger, basis: str) -> dict:
    return {
        r.episode_id: L.label_value(ledger.labels.get(r.episode_id), basis)
        for r in ledger.rows
        if r.status == "valid"
    }


def _value(outcome) -> int | None:
    return None if outcome is None else int(outcome == "success")


def _arm_order(info: CampaignInfo, ledger: L.Ledger) -> list:
    order = [a.id for a in info.arms]
    for row in ledger.rows:
        if row.arm not in order:
            order.append(row.arm)
    return order


def _comparisons(info: CampaignInfo, arms: list) -> list:
    """``[(b, a), ...]``: the primary comparison first (B minus A), then
    every other pair of arms."""
    primary = info.comparison or (arms[1], arms[0])
    out = [primary]
    for i, a in enumerate(arms):
        for b in arms[i + 1 :]:
            if {a, b} != set(primary):
                out.append((b, a))
    return out


def _paired(rows, outcomes, a, b, *, drop_deviated=False) -> tuple[list, list]:
    """``(pairs, keys)``: 0/1 outcomes of A and B on the same (round, card),
    from pairable rows with a label on the basis."""
    sides: dict = {a: {}, b: {}}
    for r in rows:
        if r.arm not in sides or not r.pairable:
            continue
        if drop_deviated and r.layout_fidelity == "deviated":
            continue
        value = _value(outcomes.get(r.episode_id, (None, None))[0])
        if value is None:
            continue
        sides[r.arm].setdefault((r.round, r.card), (value, r))
    keys = sorted(set(sides[a]) & set(sides[b]))
    return [(sides[a][k][0], sides[b][k][0]) for k in keys], [
        (sides[a][k][1], sides[b][k][1]) for k in keys
    ]


def _excludes_zero(low, high) -> bool:
    return low is not None and high is not None and (low > 0 or high < 0)


def _methods(node, found: dict) -> None:
    if isinstance(node, dict):
        if "method" in node and "implementation" in node:
            key = (node["method"], node["implementation"])
            refs = found.setdefault(key, set())
            refs.update(node.get("references") or [])
        for value in node.values():
            _methods(value, found)
    elif isinstance(node, list):
        for value in node:
            _methods(value, found)


def _safe(fn, *args, **kwargs) -> dict:
    """A library call whose input may be too small: its refusal becomes an
    unavailable result instead of an error."""
    try:
        return fn(*args, **kwargs)
    except an.AnalysisInputError as exc:
        return {"available": False, "error": str(exc)}


def analyse(
    ledger: L.Ledger,
    info: CampaignInfo,
    basis: str,
    *,
    layout: L.CampaignLayout | None = None,
    seed: int | None = None,
    resamples: int = 10_000,
) -> dict:
    """Every number of the report for ``basis``. Pure: the same ledger,
    labels, information and seed give the same document."""
    if basis not in L.LABEL_BASES:
        raise ReportError(f"label basis is one of {', '.join(L.LABEL_BASES)}")
    if ledger.campaign_id != info.campaign_id:
        raise ReportError("the ledger and the campaign information disagree")
    seed = info.seed if seed is None else int(seed)
    arms = _arm_order(info, ledger)
    rows = list(ledger.rows)
    valid = [r for r in rows if r.status == "valid"]
    outcomes = _outcomes(ledger, basis)
    paired_design = ledger.layout_source == "card_set"

    # --- header block: basis, coverage, blinding, layout, modes
    coverage = {}
    used_kinds = {"adjudicated_ground_truth": 0, "operator_label": 0}
    for arm in arms:
        mine = [r for r in valid if r.arm == arm]
        labelled = [r for r in mine if outcomes[r.episode_id][0] is not None]
        for r in labelled:
            kind = outcomes[r.episode_id][1]
            if kind in used_kinds:
                used_kinds[kind] += 1
        coverage[arm] = {
            "valid": len(mine),
            "labelled": len(labelled),
            "unlabelled": len(mine) - len(labelled),
            "coverage": len(labelled) / len(mine) if mine else None,
        }
    rates = [c["coverage"] for c in coverage.values() if c["coverage"] is not None]
    gap = (max(rates) - min(rates)) if rates else None
    counts = L.counts(ledger, layout)
    deviated = sum(c.get("deviated", 0) for c in counts.values())
    header = {
        "basis": basis,
        "basis_name": dict(BASIS_NAMES[basis]),
        "automatic": basis in L.AUTOMATIC_BASES,
        "coverage": coverage,
        "coverage_gap": gap,
        "coverage_warning": gap is not None and gap > COVERAGE_GAP,
        "labels_used": used_kinds if basis == "adjudicated_then_operator" else None,
        "operator_blind": info.operator_blind,
        "layout_source": ledger.layout_source,
        "deviated": deviated,
        "reset_mode": info.reset_mode,
        "scene_check": info.scene_check,
        "treatment_includes_reset": info.treatment_includes_reset,
    }

    # --- success per arm
    success = {}
    for arm in arms:
        mine = [
            _value(outcomes[r.episode_id][0])
            for r in valid
            if r.arm == arm and outcomes[r.episode_id][0] is not None
        ]
        success[arm] = an.proportion(sum(mine), len(mine))

    # --- pairwise comparisons, Holm over the family
    design = {
        "design_difference": info.design_difference,
        "design_baseline": info.design_baseline,
    }
    comparisons = []
    for index, (b, a) in enumerate(_comparisons(info, arms)):
        entry = {"b": b, "a": a, "label": f"{b} - {a}", "primary": index == 0}
        if paired_design:
            pairs, _ = _paired(valid, outcomes, a, b)
            entry["design"] = "paired"
            entry["n_pairs"] = len(pairs)
            entry["mcnemar"] = an.mcnemar(pairs, alpha=info.alpha, **design)
            entry["newcombe"] = an.newcombe_paired(pairs, **design)
            entry["bootstrap"] = an.paired_bootstrap(
                pairs, seed=seed + index, resamples=resamples, **design
            )
            entry["p"] = entry["mcnemar"].get("p_exact")
            entry["exploratory"] = entry["mcnemar"]["exploratory"]
            if any(r.layout_fidelity == "deviated" for r in valid if r.arm in (a, b)):
                kept, _ = _paired(valid, outcomes, a, b, drop_deviated=True)
                entry["without_deviated"] = {
                    "n_pairs": len(kept),
                    "newcombe": an.newcombe_paired(kept, **design),
                    "mcnemar": an.mcnemar(kept, alpha=info.alpha, **design),
                }
        else:
            sa, sb = success[a], success[b]
            entry["design"] = "unpaired"
            entry["fisher"] = an.fisher_exact(
                sb["k"], sb["n"], sa["k"], sa["n"], **design
            )
            entry["newcombe"] = an.newcombe_independent(
                sb["k"], sb["n"], sa["k"], sa["n"], **design
            )
            entry["p"] = entry["fisher"].get("p_value")
            entry["exploratory"] = entry["fisher"]["exploratory"]
        comparisons.append(entry)
    holm = an.holm(
        [c["p"] for c in comparisons],
        alpha=info.alpha,
        exploratory=[c["exploratory"] for c in comparisons],
    )
    for c, adj, rej in zip(comparisons, holm["adjusted"], holm["reject"], strict=True):
        c["p_holm"], c["holm_reject"] = adj, rej

    # --- more than two arms
    omnibus = None
    if len(arms) > 2 and paired_design:
        blocks: dict = {}
        for r in valid:
            v = _value(outcomes[r.episode_id][0])
            if r.pairable and v is not None:
                blocks.setdefault((r.round, r.card), {}).setdefault(r.arm, v)
        matrix = [
            [blk[arm] for arm in arms]
            for _, blk in sorted(blocks.items())
            if all(arm in blk for arm in arms)
        ]
        omnibus = an.cochran_q(matrix, seed=seed) if matrix else None

    # --- continuous: steps on pairs where both succeeded (primary)
    b0, a0 = comparisons[0]["b"], comparisons[0]["a"]
    continuous = {"metric": "steps", "comparison": comparisons[0]["label"]}
    if paired_design:
        _, row_pairs = _paired(valid, outcomes, a0, b0)
        steps = [
            (ra.steps, rb.steps)
            for ra, rb in row_pairs
            if outcomes[ra.episode_id][0] == "success"
            and outcomes[rb.episode_id][0] == "success"
            and ra.steps is not None
            and rb.steps is not None
        ]
        continuous["design"] = "paired_both_succeeded"
        continuous["n_pairs"] = len(steps)
        continuous["wilcoxon"] = an.wilcoxon_signed_rank(steps)
        continuous["hodges_lehmann"] = an.hodges_lehmann(
            steps, paired=True, seed=seed, resamples=2000
        )
        continuous["p"] = continuous["wilcoxon"].get("p_value")
    else:

        def succeeded(arm):
            return [
                r.steps
                for r in valid
                if r.arm == arm
                and outcomes[r.episode_id][0] == "success"
                and r.steps is not None
            ]

        sa_, sb_ = succeeded(a0), succeeded(b0)
        continuous["design"] = "unpaired_succeeded"
        continuous["mann_whitney"] = _safe(an.mann_whitney, sa_, sb_)
        continuous["cliffs_delta"] = _safe(an.cliffs_delta, sa_, sb_)
        continuous["p"] = continuous["mann_whitney"].get("p_value")

    # --- time to success
    groups = {}
    for arm in arms:
        times, events = [], []
        for r in valid:
            if r.arm != arm or outcomes[r.episode_id][0] is None or r.steps is None:
                continue
            times.append(r.steps)
            events.append(int(outcomes[r.episode_id][0] == "success"))
        groups[arm] = (times, events)
    caps = [r.max_steps for r in valid if r.max_steps]
    observed = [r.steps for r in valid if r.steps is not None]
    tau = max(caps) if caps else (max(observed) if observed else None)
    survival = {
        "tau": tau,
        "time_unit": "policy steps",
        "time_source": "the episode's last step (success: the step it ended on)",
        "kaplan_meier": {arm: an.kaplan_meier(*groups[arm]) for arm in arms},
        "logrank": _safe(an.logrank, groups),
        "rmst": (
            _safe(an.rmst, groups, tau=float(tau), seed=seed, reference=a0)
            if tau
            else {"available": False, "error": "no step counts"}
        ),
    }

    # --- failure modes and early termination
    labels = ledger.labels
    failure_rows = []
    for r in valid:
        o = outcomes[r.episode_id][0]
        failure_rows.append(
            {
                "arm": r.arm,
                "success": None if o is None else o == "success",
                "stop_reason": r.stop_reason,
                "failure_mode": (labels.get(r.episode_id) or {}).get("failure_mode"),
            }
        )
    failures = an.failure_modes(failure_rows)
    # The detector is judged against people, whatever the report's basis.
    person = {
        r.episode_id: L.label_value(
            labels.get(r.episode_id), "adjudicated_then_operator"
        )[0]
        for r in valid
    }
    early = an.early_stop(
        [
            {
                "arm": r.arm,
                "control": r.control,
                "truth": person[r.episode_id],
                "would_stop": r.would_stop_step is not None,
                "complete": r.control_recorded,
                "early_stop": r.stop_reason == "goal_verified",
                "slot": r.card if r.pairable else None,
                "round": r.round,
                "saved_steps": (r.max_steps - r.steps)
                if r.stop_reason == "goal_verified" and r.max_steps and r.steps
                else None,
            }
            for r in valid
        ]
    )
    early["truth_basis"] = "adjudicated_then_operator"

    # --- drift and order
    rounds = sorted({r.round for r in valid})

    def per_round(arm):
        out = []
        for rnd in rounds:
            vs = [
                _value(outcomes[r.episode_id][0])
                for r in valid
                if r.arm == arm
                and r.round == rnd
                and outcomes[r.episode_id][0] is not None
            ]
            out.append((sum(vs), len(vs)))
        return out

    by_round = {arm: per_round(arm) for arm in arms}
    diagnostics = []
    ref = info.reference
    reference = an.reference_drift(by_round[ref]) if ref in by_round else None
    if reference is not None:
        diagnostics.append(reference)
    interaction = an.arm_time_interaction(
        [
            (ka, na, kb, nb)
            for (ka, na), (kb, nb) in zip(by_round[a0], by_round[b0], strict=True)
        ],
        seed=seed,
    )
    diagnostics.append(interaction)
    carry = an.carryover(
        [
            {
                "arm": r.arm,
                "previous_arm": r.preceded_by_arm,
                "success": None
                if outcomes[r.episode_id][0] is None
                else outcomes[r.episode_id][0] == "success",
            }
            for r in valid
        ]
    )
    diagnostics.append(carry)
    warning = an.drift_warning(*diagnostics)
    drift = {
        "rounds": rounds,
        "by_round": {
            arm: [
                {
                    "round": rnd,
                    "k": k,
                    "n": n,
                    "rate": k / n if n else None,
                    "wilson": an.proportion(k, n).get("wilson"),
                }
                for rnd, (k, n) in zip(rounds, by_round[arm], strict=True)
            ]
            for arm in arms
        },
        "reference_arm": ref,
        "reference_drift": reference,
        "arm_time_interaction": interaction,
        "carryover": carry,
        "warning": warning,
    }

    # --- judge agreement (automatic verdict against the operator)
    def judge_pairs(arm, stratum):
        out = []
        for r in valid:
            if r.arm != arm or (stratum != "all" and r.ended_by != stratum):
                continue
            got = (labels.get(r.episode_id) or {}).get("labels", {})
            op = got.get("operator_label")
            verdict = got.get("autonomous_verdict")
            out.append(
                (
                    op if op in L.DECIDED else None,
                    None if verdict in (None, "none") else verdict,
                )
            )
        return out

    agreement = {
        arm: {
            stratum: an.agreement(judge_pairs(arm, stratum))
            for stratum in STRATA
            if stratum == "all" or judge_pairs(arm, stratum)
        }
        for arm in arms
    }
    judge = an.misjudgement_by_arm(
        {arm: judge_pairs(arm, "all") for arm in arms}, seed=seed
    )
    secondary = an.benjamini_hochberg(
        [
            continuous.get("p"),
            survival["logrank"].get("p_value"),
        ],
        exploratory=[True, True],
    )

    # --- power: the design's detectable differences, never post-hoc
    planned_n = info.trials_per_arm
    ns = sorted(set(DESIGN_NS) | ({planned_n} if planned_n else set()))
    baselines = list(DESIGN_BASELINES)
    if info.design_baseline is not None and info.design_baseline not in baselines:
        baselines.append(float(info.design_baseline))
    table = an.power_table(ns, baselines, alpha=info.alpha)
    n_for_mdd = planned_n or max((c["labelled"] for c in coverage.values()), default=0)
    baseline = info.design_baseline if info.design_baseline is not None else 0.5
    power = {
        "table": table,
        "n": n_for_mdd,
        "baseline": baseline,
        "mdd_paired": an.min_detectable_difference(
            n_for_mdd, baseline, design="paired", alpha=info.alpha
        )
        if n_for_mdd
        else None,
        "mdd_unpaired": an.min_detectable_difference(
            n_for_mdd, baseline, design="unpaired", alpha=info.alpha
        )
        if n_for_mdd
        else None,
        "post_hoc_power": "not reported",
    }

    # --- the conclusion level (design §4.4)
    reached = bool(planned_n) and all(
        coverage[arm]["labelled"] >= planned_n for arm in arms
    )
    conditions = {
        "preregistered": bool(info.preregistered),
        "sample_size_reached": reached,
        "no_peeks": info.peeks == 0,
        "reviewed_basis": basis in REVIEWED_BASES,
        "no_drift_warning": not warning["warning"],
        "schedule_allows": info.schedule not in SCHEDULES_EXPLORATORY,
        "powered_design": not holm["exploratory"],
    }
    level = "confirmatory" if all(conditions.values()) else "exploratory"

    out = {
        "schema": SCHEMA,
        "campaign_id": info.campaign_id,
        "task": info.task,
        "arms": arms,
        "seed": seed,
        "header": header,
        "counts": counts,
        "unplanned_runs": list(ledger.unplanned_runs),
        "success": success,
        "comparisons": comparisons,
        "holm": holm,
        "omnibus": omnibus,
        "continuous": continuous,
        "survival": survival,
        "failure_modes": failures,
        "early_stop": early,
        "drift": drift,
        "agreement": agreement,
        "misjudgement": judge,
        "secondary_screen": secondary,
        "power": power,
        "conclusion_level": {
            "level": level,
            "conditions": conditions,
            "schedule": info.schedule,
            "peeks": info.peeks,
            "preregistered": bool(info.preregistered),
            "trials_per_arm": planned_n,
        },
    }
    found: dict = {}
    _methods(out, found)
    out["methods"] = [
        {
            "method": method,
            "implementation": impl,
            "references": [
                {
                    "key": key,
                    "citation": f"{REFERENCES[key]['authors']} {REFERENCES[key]['year']}, "
                    f"{REFERENCES[key]['id']}",
                }
                for key in sorted(refs)
                if key in REFERENCES
            ],
        }
        for (method, impl), refs in sorted(found.items())
    ]
    return out


# ----------------------------------------------------------------- summary


def _t(table: dict, key: str, **values) -> str:
    return Template(table[key]).substitute(values)


def _conclusion(c: dict, analysis: dict, table: dict, lang: str) -> list:
    level = analysis["conclusion_level"]["level"]
    prefix = table[f"prefix.{level}"]
    name = analysis["header"]["basis_name"][lang]
    a, b = c["a"], c["b"]
    est = c["newcombe"]
    lines = []
    if not est.get("available"):
        reason = "reason.no_pairs" if c["design"] == "paired" else "reason.no_trials"
        return [
            _t(
                table,
                "conclusion.unavailable",
                prefix=prefix,
                a=a,
                b=b,
                reason=table[reason],
            )
        ]
    low, high, delta = est["low"], est["high"], est["difference"]
    values = {
        "prefix": prefix,
        "a": a,
        "b": b,
        "basis_name": name,
        "delta": fmt(delta, "diff"),
        "delta_abs": fmt(abs(delta), "rate"),
        "low": fmt(low, "diff"),
        "high": fmt(high, "diff"),
        "p_holm": fmt(c["p_holm"], "p"),
        "direction": table["dir.higher" if delta > 0 else "dir.lower"],
    }
    if _excludes_zero(low, high):
        key = (
            "conclusion.confirmatory_excludes_zero"
            if level == "confirmatory" and c["holm_reject"]
            else "conclusion.exploratory_excludes_zero"
        )
        if key.startswith("conclusion.exploratory"):
            values["prefix"] = table["prefix.exploratory"]
        lines.append(_t(table, key, **values))
    else:
        power = analysis["power"]
        mdd = power["mdd_paired" if c["design"] == "paired" else "mdd_unpaired"]
        values["n"] = fmt(power["n"], "int")
        if mdd is None:
            lines.append(_t(table, "conclusion.includes_zero_no_mdd", **values))
        else:
            values["mdd"] = fmt(mdd, "rate")
            lines.append(_t(table, "conclusion.includes_zero", **values))
    if c["design"] == "paired":
        m, boot = c["mcnemar"], c["bootstrap"]
        lines.append(
            _t(
                table,
                "comparison.paired_detail",
                p_exact=fmt(m["p_exact"], "p"),
                p_mid=fmt(m["p_mid"], "p"),
                n=fmt(c["n_pairs"], "int"),
                b_low=fmt(boot["low"], "diff"),
                b_high=fmt(boot["high"], "diff"),
            )
        )
        dev = c.get("without_deviated")
        if dev and dev["newcombe"].get("available"):
            lines.append(
                _t(
                    table,
                    "comparison.deviated",
                    delta=fmt(dev["newcombe"]["difference"], "diff"),
                    low=fmt(dev["newcombe"]["low"], "diff"),
                    high=fmt(dev["newcombe"]["high"], "diff"),
                    n=fmt(dev["n_pairs"], "int"),
                )
            )
    else:
        lines.append(
            _t(
                table,
                "comparison.unpaired_detail",
                p=fmt(c["fisher"].get("p_value"), "p"),
                n_b=fmt(analysis["success"][b]["n"], "int"),
                n_a=fmt(analysis["success"][a]["n"], "int"),
            )
        )
    return lines


def summary(analysis: dict, lang: str, *, blinded: bool = False) -> str:
    """The summary in ``lang`` from the templates; every number formatted
    from ``analysis``. ``blinded``: no per-arm value at all (the campaign
    is still running)."""
    if lang not in LANGS:
        raise ReportError(f"language is one of {LANGS}")
    table = sentences()[lang]
    header = analysis["header"]
    basis = header["basis"]
    name = header["basis_name"][lang]
    level = analysis["conclusion_level"]
    if level["level"] == "confirmatory":
        level_text = table["level.confirmatory"]
    else:
        failed = [table[f"cond.{c}"] for c in CONDITIONS if not level["conditions"][c]]
        sep = "；" if lang == "zh-CN" else "; "
        level_text = _t(table, "level.exploratory", failed=sep.join(failed))

    basis_lines = [_t(table, "basis.line", basis_name=name, basis=basis)]
    if header["automatic"]:
        basis_lines.append(table["basis.automatic_note"])
    if header["labels_used"] is not None:
        basis_lines.append(
            _t(
                table,
                "basis.mixed_counts",
                adjudicated=fmt(
                    header["labels_used"]["adjudicated_ground_truth"], "int"
                ),
                operator=fmt(header["labels_used"]["operator_label"], "int"),
            )
        )
    for arm in [] if blinded else analysis["arms"]:
        cov = header["coverage"][arm]
        basis_lines.append(
            _t(
                table,
                "coverage.arm",
                arm=arm,
                labelled=fmt(cov["labelled"], "int"),
                valid=fmt(cov["valid"], "int"),
                coverage=fmt(cov["coverage"], "pct"),
                unlabelled=fmt(cov["unlabelled"], "int"),
            )
        )
    if header["coverage_warning"] and not blinded:
        basis_lines.append(
            _t(table, "coverage.warning", gap=fmt(header["coverage_gap"], "pct"))
        )
    basis_lines.append(_t(table, "blind.line", blind=header["operator_blind"]))
    if header["layout_source"] == "card_set":
        basis_lines.append(
            _t(table, "layout.cards", deviated=fmt(header["deviated"], "int"))
        )
    else:
        basis_lines.append(table["layout.none"])
    basis_lines.append(
        _t(
            table,
            "mode.line",
            reset_mode=header["reset_mode"] or "unknown",
            scene_check=header["scene_check"] or "unknown",
        )
    )
    if header["treatment_includes_reset"]:
        basis_lines.append(table["reset.treatment"])

    count_lines = []
    for arm in analysis["arms"]:
        c = analysis["counts"].get(arm, {})
        count_lines.append(
            _t(
                table,
                "counts.arm",
                arm=arm,
                valid=fmt(c.get("valid", 0), "int"),
                discarded=fmt(c.get("discarded", 0), "int"),
                incomplete=fmt(c.get("incomplete", 0), "int"),
                reruns=fmt(c.get("from_reruns", 0), "int"),
                unconfirmed=fmt(c.get("unconfirmed", 0), "int"),
                missing=fmt(c.get("missing"), "int"),
            )
        )

    if blinded:
        results = "\n".join(
            [table["results.blinded"]]
            + [
                _t(
                    table,
                    "results.blinded_arm",
                    arm=arm,
                    valid=fmt(header["coverage"][arm]["valid"], "int"),
                )
                for arm in analysis["arms"]
            ]
        )
        comparisons = table["results.blinded"]
        drift = ""
        agreement = table["results.blinded"]
    else:
        result_lines = []
        for arm in analysis["arms"]:
            s = analysis["success"][arm]
            if not s.get("available"):
                result_lines.append(_t(table, "results.arm_empty", arm=arm))
                continue
            result_lines.append(
                _t(
                    table,
                    "results.arm",
                    arm=arm,
                    k=fmt(s["k"], "int"),
                    n=fmt(s["n"], "int"),
                    rate=fmt(s["rate"], "pct"),
                    low=fmt(s["wilson"]["low"], "pct"),
                    high=fmt(s["wilson"]["high"], "pct"),
                )
            )
        results = f"{name}\n\n" + "\n".join(result_lines)
        comp_lines = []
        for c in analysis["comparisons"]:
            comp_lines += _conclusion(c, analysis, table, lang)
        omni = analysis.get("omnibus")
        if omni and omni.get("available"):
            comp_lines.append(
                _t(
                    table,
                    "omnibus.line",
                    prefix=table["prefix.exploratory"],
                    k=fmt(len(analysis["arms"]), "int"),
                    p=fmt(omni["p_permutation"], "p"),
                    blocks=fmt(omni["blocks"], "int"),
                )
            )
        comparisons = "\n".join(comp_lines)
        warn = analysis["drift"]["warning"]
        drift = (
            _t(table, "drift.warning", raised=", ".join(warn["raised_by"]))
            if warn["warning"]
            else table["drift.none"]
        )
        agree_lines = []
        for arm in analysis["arms"]:
            for stratum, res in analysis["agreement"][arm].items():
                label = table[f"stratum.{stratum}"]
                share = res["agreement"]
                if not res.get("available") or not share["n"]:
                    agree_lines.append(
                        _t(table, "agreement.arm_empty", arm=arm, stratum=label)
                    )
                    continue
                agree_lines.append(
                    _t(
                        table,
                        "agreement.arm",
                        arm=arm,
                        stratum=label,
                        agree=fmt(share["k"], "int"),
                        judged=fmt(share["n"], "int"),
                        rate=fmt(share["rate"], "pct"),
                        low=fmt(share["wilson"][0], "pct"),
                        high=fmt(share["wilson"][1], "pct"),
                        kappa=fmt(res["kappa"], "stat"),
                    )
                )
        agree_lines.append(table["agreement.carry"])
        judge = analysis["misjudgement"]
        if judge.get("warning"):
            agree_lines.append(
                _t(table, "agreement.judge_warning", p=fmt(judge["p_value"], "p"))
            )
        agreement = "\n".join(agree_lines)

    power = analysis["power"]
    if power["mdd_paired"] is None or power["mdd_unpaired"] is None:
        power_lines = [
            _t(
                table,
                "power.none",
                n=fmt(power["n"], "int"),
                baseline=fmt(power["baseline"], "rate"),
            )
        ]
    else:
        power_lines = [
            _t(
                table,
                "power.line",
                n=fmt(power["n"], "int"),
                baseline=fmt(power["baseline"], "rate"),
                paired=fmt(power["mdd_paired"], "rate"),
                unpaired=fmt(power["mdd_unpaired"], "rate"),
            )
        ]
    power_lines.append(table["power.note"])

    limits = _limitations(analysis, table, lang, blinded=blinded)
    text = Template(summary_template(lang)).substitute(
        title=_t(table, "title", campaign_id=analysis["campaign_id"], basis_name=name),
        level=level_text,
        basis_block="\n".join(basis_lines),
        counts="\n".join(count_lines),
        results=results,
        comparisons=comparisons,
        drift=drift,
        agreement=agreement,
        power="\n".join(power_lines),
        limitations="\n".join(limits),
    )
    return re.sub(r"\n{3,}", "\n\n", text)


def _limitations(analysis: dict, table: dict, lang: str, *, blinded=False) -> list:
    header = analysis["header"]
    power = analysis["power"]
    mdd = (
        power["mdd_paired"]
        if header["layout_source"] == "card_set"
        else power["mdd_unpaired"]
    )
    out = []
    if mdd is None:
        out.append(_t(table, "limit.sample_none", n=fmt(power["n"], "int")))
    else:
        out.append(
            _t(table, "limit.sample", n=fmt(power["n"], "int"), mdd=fmt(mdd, "rate"))
        )
    if header["automatic"]:
        out.append(table["limit.basis_automatic"])
    rates = [
        c["coverage"] for c in header["coverage"].values() if c["coverage"] is not None
    ]
    if rates and not blinded:
        out.append(
            _t(
                table,
                "limit.coverage",
                low=fmt(min(rates), "pct"),
                high=fmt(max(rates), "pct"),
            )
        )
    out.append(table["limit.judge"])
    out.append(_t(table, "limit.blind", blind=header["operator_blind"]))
    if header["layout_source"] == "card_set":
        out.append(_t(table, "limit.layout", deviated=fmt(header["deviated"], "int")))
    else:
        out.append(table["limit.layout_none"])
    warn = analysis["drift"]["warning"]
    drift = (
        _t(table, "drift.warning", raised=", ".join(warn["raised_by"]))
        if warn["warning"]
        else table["drift.none"]
    )
    if blinded:
        drift = table["results.blinded"]
    out.append(
        _t(
            table,
            "limit.drift",
            schedule=analysis["conclusion_level"]["schedule"],
            drift=drift,
        )
    )
    out.append(table["limit.scope"])
    out.append(table["limit.early_stop"])
    counts = analysis["counts"]
    reruns = sum(c.get("from_reruns", 0) for c in counts.values())
    discarded = sum(c.get("discarded", 0) for c in counts.values())
    out.append(
        _t(
            table,
            "limit.reruns",
            reruns=fmt(reruns, "int"),
            discarded=fmt(discarded, "int"),
        )
    )
    unconfirmed = sum(c.get("unconfirmed", 0) for c in counts.values())
    if unconfirmed:
        out.append(_t(table, "limit.unconfirmed", unconfirmed=fmt(unconfirmed, "int")))
    out.append(table["limit.survival"])
    for arm in analysis.get("not_verified", []):
        out.append(
            _t(
                table,
                "limit.not_verified",
                arm=arm["arm"],
                items=", ".join(arm["items"]),
            )
        )
    return out


# ----------------------------------------------------------------- figures


def _name(analysis: dict) -> dict:
    return dict(analysis["header"]["basis_name"])


def figure_specs(analysis: dict) -> list:
    """F1-F7 (design §4.2) as FigureSpecs; a figure without data is left
    out and listed in ``skipped``. Returns ``[(spec, skipped_reason)]``."""
    arms = analysis["arms"]
    name = _name(analysis)
    ref = analysis["drift"]["reference_arm"]
    task = analysis["task"] or "All trials"
    out = []

    # F1 success per arm
    series = []
    for arm in arms:
        s = analysis["success"][arm]
        if not s.get("available"):
            series.append(fs.Series(arm, (), unavailable=True, emphasis=arm == ref))
            continue
        w = s["wilson"]
        series.append(
            fs.Series(
                arm,
                (fs.Point(0, s["rate"], w["low"], w["high"], f"{s['k']}/{s['n']}"),),
                emphasis=arm == ref,
            )
        )
    out.append(
        (
            fs.FigureSpec(
                id="f1-success",
                kind="grouped_bar",
                title={
                    "en": f"{name['en'].capitalize()} by arm",
                    "zh-CN": f"各组{name['zh-CN']}",
                },
                summary={
                    "en": "Each bar is one arm's rate with its 95% Wilson interval; k/n above it.",
                    "zh-CN": "每根柱是一组的比率和 95% Wilson 区间；上方标 k/n。",
                },
                panels=(
                    fs.Panel(
                        fs.Axis(kind="category", categories=(task,)),
                        fs.Axis(label=name, min=0, max=1, fmt="percent"),
                        tuple(series),
                    ),
                ),
            ),
            None,
        )
    )

    # F2 paired differences
    comps = analysis["comparisons"]
    newc, boot, notes = [], [], []
    for i, c in enumerate(comps):
        est = c["newcombe"]
        if not est.get("available"):
            notes.append(f"{c['label']}: not available")
            continue
        newc.append(
            fs.Point(
                est["difference"],
                i,
                est["low"],
                est["high"],
                f"p(Holm)={fmt(c['p_holm'], 'p')}",
            )
        )
        b = c.get("bootstrap")
        if b and b.get("available"):
            boot.append(fs.Point(b["estimate"], i, b["low"], b["high"]))
    f2_series = [fs.Series("Newcombe", tuple(newc), unavailable=not newc)]
    if boot:
        f2_series.append(fs.Series("Bootstrap", tuple(boot)))
    out.append(
        (
            fs.FigureSpec(
                id="f2-differences",
                kind="forest",
                title={"en": "Differences between arms", "zh-CN": "组间差值"},
                summary={
                    "en": f"First arm minus second; {name['en']}; 0 means no difference observed.",
                    "zh-CN": f"前一组减后一组；{name['zh-CN']}；0 表示观测到的差值为零。",
                },
                panels=(
                    fs.Panel(
                        fs.Axis(label={"en": "Difference", "zh-CN": "差值"}),
                        fs.Axis(
                            kind="category", categories=tuple(c["label"] for c in comps)
                        ),
                        tuple(f2_series),
                        reflines=(fs.RefLine("x", 0, {"en": "0", "zh-CN": "0"}),),
                    ),
                ),
                notes=tuple(notes),
            ),
            None,
        )
    )

    # F3 time to success
    surv = analysis["survival"]
    series = []
    for arm in arms:
        km = surv["kaplan_meier"][arm]
        if not km.get("available") or not km["steps"]:
            series.append(fs.Series(arm, (), unavailable=True, emphasis=arm == ref))
            continue
        pts = [fs.Point(0, 0.0)]
        for s in km["steps"]:
            if s["low"] is None:
                pts.append(fs.Point(s["time"], s["incidence"]))
            else:
                pts.append(
                    fs.Point(s["time"], s["incidence"], 1 - s["high"], 1 - s["low"])
                )
        series.append(fs.Series(arm, tuple(pts), emphasis=arm == ref))
    if any(not s.unavailable for s in series):
        reflines = (
            (fs.RefLine("x", surv["tau"], {"en": "step cap", "zh-CN": "步数上限"}),)
            if surv["tau"]
            else ()
        )
        out.append(
            (
                fs.FigureSpec(
                    id="f3-time-to-success",
                    kind="step_curve",
                    title={"en": "Time to success", "zh-CN": "时间到成功"},
                    summary={
                        "en": "Kaplan-Meier share of trials that succeeded by each step.",
                        "zh-CN": "Kaplan-Meier：到每一步为止成功的试验比例。",
                    },
                    panels=(
                        fs.Panel(
                            fs.Axis(label={"en": "Policy step", "zh-CN": "策略步"}),
                            fs.Axis(
                                label={"en": "Cumulative success", "zh-CN": "累计成功"},
                                min=0,
                                max=1,
                                fmt="percent",
                            ),
                            tuple(series),
                            reflines=reflines,
                        ),
                    ),
                ),
                None,
            )
        )
    else:
        out.append(
            (None, ("f3-time-to-success", "no labelled trial with a step count"))
        )

    # F4 failure modes (by stop reason)
    fm = analysis["failure_modes"]["arms"]
    totals: dict = {}
    for arm in arms:
        for reason, share in (fm.get(arm) or {}).get("by_stop_reason", {}).items():
            totals[reason] = totals.get(reason, 0) + share["k"]
    if totals:
        ordered = sorted(totals, key=lambda r: (-totals[r], r))
        keep, rest = ordered[:7], ordered[7:]
        series = []
        for reason in keep:
            series.append(
                fs.Series(
                    reason,
                    tuple(
                        fs.Point(
                            i,
                            (fm.get(arm) or {})
                            .get("by_stop_reason", {})
                            .get(reason, {})
                            .get("k", 0),
                        )
                        for i, arm in enumerate(arms)
                    ),
                )
            )
        if rest:
            series.append(
                fs.Series(
                    "other",
                    tuple(
                        fs.Point(
                            i,
                            sum(
                                (fm.get(arm) or {})
                                .get("by_stop_reason", {})
                                .get(r, {})
                                .get("k", 0)
                                for r in rest
                            ),
                        )
                        for i, arm in enumerate(arms)
                    ),
                )
            )
        out.append(
            (
                fs.FigureSpec(
                    id="f4-failure-modes",
                    kind="stacked_bar",
                    title={
                        "en": "Failed trials by stop reason",
                        "zh-CN": "失败试验按停止原因",
                    },
                    summary={
                        "en": f"Counts of failed trials per arm ({name['en']}).",
                        "zh-CN": f"各组失败试验的计数（{name['zh-CN']}）。",
                    },
                    panels=(
                        fs.Panel(
                            fs.Axis(
                                label={"en": "Arm", "zh-CN": "组"},
                                kind="category",
                                categories=tuple(arms),
                            ),
                            fs.Axis(label={"en": "Failed trials", "zh-CN": "失败试验"}),
                            tuple(series),
                        ),
                    ),
                ),
                None,
            )
        )
    else:
        out.append((None, ("f4-failure-modes", "no failed trial on this basis")))

    # F5 early termination
    es = analysis["early_stop"]["arms"]
    saved = [
        fs.Point(i, es[arm]["saved_steps"]["mean"])
        for i, arm in enumerate(arms)
        if arm in es and es[arm]["saved_steps"]["mean"] is not None
    ]
    rate = [
        fs.Point(
            i,
            es[arm]["false_early_stop_rate"]["rate"],
            es[arm]["false_early_stop_rate"]["wilson"][0],
            es[arm]["false_early_stop_rate"]["wilson"][1],
            f"{es[arm]['false_early_stop_rate']['k']}/{es[arm]['false_early_stop_rate']['n']}",
        )
        for i, arm in enumerate(arms)
        if arm in es and es[arm]["false_early_stop_rate"]["status"] == "available"
    ]
    panels = []
    cat = fs.Axis(
        label={"en": "Arm", "zh-CN": "组"}, kind="category", categories=tuple(arms)
    )
    if saved:
        panels.append(
            fs.Panel(
                cat,
                fs.Axis(
                    label={
                        "en": "Steps saved per early stop",
                        "zh-CN": "每次早停节省的步数",
                    }
                ),
                (fs.Series({"en": "Saved", "zh-CN": "节省"}, tuple(saved)),),
                title={"en": "Steps saved", "zh-CN": "节省的步数"},
            )
        )
    if rate:
        panels.append(
            fs.Panel(
                cat,
                fs.Axis(
                    label={"en": "False early stop rate", "zh-CN": "误终止率"},
                    min=0,
                    max=1,
                    fmt="percent",
                ),
                (fs.Series({"en": "False stop", "zh-CN": "误终止"}, tuple(rate)),),
                title={
                    "en": "Control episodes that truly failed",
                    "zh-CN": "真正失败的对照片段",
                },
            )
        )
    if panels:
        out.append(
            (
                fs.FigureSpec(
                    id="f5-early-stop",
                    kind="early_stop",
                    title={"en": "Early termination", "zh-CN": "提前终止"},
                    summary={
                        "en": "Steps saved by early stops and the false early stop rate on control episodes (95% Wilson).",
                        "zh-CN": "早停节省的步数，以及对照片段上的误终止率（95% Wilson）。",
                    },
                    panels=tuple(panels),
                ),
                None,
            )
        )
    else:
        out.append(
            (None, ("f5-early-stop", "no early stop and no failed control episode"))
        )

    # F6 drift
    drift = analysis["drift"]
    series = []
    for arm in arms:
        pts = [
            fs.Point(p["round"], p["rate"], p["wilson"]["low"], p["wilson"]["high"])
            for p in drift["by_round"][arm]
            if p["n"]
        ]
        series.append(
            fs.Series(arm, tuple(pts), emphasis=arm == ref, unavailable=not pts)
        )
    if any(not s.unavailable for s in series):
        out.append(
            (
                fs.FigureSpec(
                    id="f6-drift",
                    kind="drift_lines",
                    title={"en": "Rate by round", "zh-CN": "按轮次的比率"},
                    summary={
                        "en": f"{name['en'].capitalize()} per round with 95% Wilson intervals"
                        + (f"; {ref} is the reference arm." if ref else "."),
                        "zh-CN": f"每轮的{name['zh-CN']}及 95% Wilson 区间"
                        + (f"；{ref} 是参照组。" if ref else "。"),
                    },
                    panels=(
                        fs.Panel(
                            fs.Axis(label={"en": "Round", "zh-CN": "轮次"}),
                            fs.Axis(label=name, min=0, max=1, fmt="percent"),
                            tuple(series),
                        ),
                    ),
                ),
                None,
            )
        )
    else:
        out.append((None, ("f6-drift", "no labelled trial")))

    # F7 judge agreement, at most four arms per figure
    with_data = [arm for arm in arms if analysis["agreement"][arm]["all"]["labelled"]]
    if not with_data:
        out.append((None, ("f7-agreement", "no trial with an operator label")))
    for part, start in enumerate(range(0, len(with_data), fs.MAX_PANELS)):
        chunk = with_data[start : start + fs.MAX_PANELS]
        panels = []
        for arm in chunk:
            matrix = analysis["agreement"][arm]["all"]["matrix"]
            pts = [
                fs.Point(c, r, value=matrix[person][verdict])
                for r, person in enumerate(("success", "failure"))
                for c, verdict in enumerate(("success", "failure", "undecided"))
            ]
            panels.append(
                fs.Panel(
                    fs.Axis(
                        label={"en": "Automatic verdict", "zh-CN": "自动判定"},
                        kind="category",
                        categories=(
                            {"en": "success", "zh-CN": "成功"},
                            {"en": "failure", "zh-CN": "失败"},
                            {"en": "undecided", "zh-CN": "未定"},
                        ),
                    ),
                    fs.Axis(
                        label={"en": "Operator label", "zh-CN": "操作员标签"},
                        kind="category",
                        categories=(
                            {"en": "success", "zh-CN": "成功"},
                            {"en": "failure", "zh-CN": "失败"},
                        ),
                    ),
                    (fs.Series({"en": "agreement", "zh-CN": "一致性"}, tuple(pts)),),
                    title=arm,
                )
            )
        suffix = "" if part == 0 else f"-{part + 1}"
        out.append(
            (
                fs.FigureSpec(
                    id=f"f7-agreement{suffix}",
                    kind="confusion_matrix",
                    title={
                        "en": "Automatic verdict against the operator",
                        "zh-CN": "自动判定与操作员标签",
                    },
                    summary={
                        "en": "Counts per arm; trials without a verdict are left out.",
                        "zh-CN": "各组计数；没有判定的试验不计入。",
                    },
                    panels=tuple(panels),
                ),
                None,
            )
        )
    return out


# ------------------------------------------------------------------ tables


def tables(analysis: dict) -> dict:
    """``{name: (header, rows)}``; every number through ``fmt``."""
    arms = analysis["arms"]
    name = analysis["header"]["basis_name"]["en"]
    out = {}
    rows = []
    for arm in arms:
        s = analysis["success"][arm]
        cov = analysis["header"]["coverage"][arm]
        w = s.get("wilson") or {}
        cp = s.get("clopper_pearson") or {}
        rows.append(
            [
                arm,
                fmt(s["k"], "int"),
                fmt(s["n"], "int"),
                fmt(s.get("rate"), "rate"),
                fmt(w.get("low"), "rate"),
                fmt(w.get("high"), "rate"),
                fmt(cp.get("low"), "rate"),
                fmt(cp.get("high"), "rate"),
                fmt(cov["unlabelled"], "int"),
                fmt(cov["coverage"], "rate"),
            ]
        )
    out["success"] = (
        [
            "arm",
            "successes",
            f"n ({name})",
            name,
            "wilson_low",
            "wilson_high",
            "clopper_pearson_low",
            "clopper_pearson_high",
            "unlabelled",
            "label_coverage",
        ],
        rows,
    )

    rows = []
    for c in analysis["comparisons"]:
        variants = [(c["label"], c)]
        if c.get("without_deviated"):
            variants.append(
                (
                    f"{c['label']} (without deviated)",
                    {
                        **c,
                        **c["without_deviated"],
                        "bootstrap": None,
                        "p_holm": None,
                        "holm_reject": None,
                    },
                )
            )
        for label, v in variants:
            est = v["newcombe"]
            if c["design"] == "paired":
                cells = v["mcnemar"].get("cells") or {}
                boot = v.get("bootstrap") or {}
                rows.append(
                    [
                        label,
                        "paired",
                        fmt(v["n_pairs"], "int"),
                        fmt(cells.get("both"), "int"),
                        fmt(cells.get("only_a"), "int"),
                        fmt(cells.get("only_b"), "int"),
                        fmt(cells.get("neither"), "int"),
                        fmt(est.get("difference"), "diff"),
                        fmt(est.get("low"), "diff"),
                        fmt(est.get("high"), "diff"),
                        fmt(boot.get("low"), "diff"),
                        fmt(boot.get("high"), "diff"),
                        fmt(v["mcnemar"].get("p_exact"), "p"),
                        fmt(v["mcnemar"].get("p_mid"), "p"),
                        fmt(v.get("p_holm"), "p"),
                    ]
                )
            else:
                rows.append(
                    [
                        label,
                        "unpaired",
                        fmt(
                            analysis["success"][c["b"]]["n"]
                            + analysis["success"][c["a"]]["n"],
                            "int",
                        ),
                        "NA",
                        "NA",
                        "NA",
                        "NA",
                        fmt(est.get("difference"), "diff"),
                        fmt(est.get("low"), "diff"),
                        fmt(est.get("high"), "diff"),
                        "NA",
                        "NA",
                        fmt(v["fisher"].get("p_value"), "p"),
                        "NA",
                        fmt(v.get("p_holm"), "p"),
                    ]
                )
    out["pairwise"] = (
        [
            "comparison",
            "design",
            "n",
            "both",
            "only_a",
            "only_b",
            "neither",
            "difference",
            "newcombe_low",
            "newcombe_high",
            "bootstrap_low",
            "bootstrap_high",
            "p_exact",
            "p_mid",
            "p_holm",
        ],
        rows,
    )

    cont = analysis["continuous"]
    rows = []
    if cont["design"] == "paired_both_succeeded":
        w, hl = cont["wilcoxon"], cont["hodges_lehmann"]
        rows.append(
            [
                cont["comparison"],
                "steps (both succeeded), Wilcoxon",
                fmt(cont["n_pairs"], "int"),
                fmt(w.get("statistic"), "stat"),
                fmt(w.get("p_value"), "p"),
                fmt(hl.get("estimate"), "stat"),
                fmt(hl.get("low"), "stat"),
                fmt(hl.get("high"), "stat"),
            ]
        )
    else:
        mw, cd = cont["mann_whitney"], cont["cliffs_delta"]
        rows.append(
            [
                cont["comparison"],
                "steps (succeeded), Mann-Whitney",
                "NA",
                fmt(mw.get("statistic"), "stat"),
                fmt(mw.get("p_value"), "p"),
                fmt(cd.get("delta"), "stat"),
                "NA",
                "NA",
            ]
        )
    lr = analysis["survival"]["logrank"]
    rows.append(
        [
            "all arms",
            "time to success, log-rank",
            "NA",
            fmt(lr.get("statistic"), "stat"),
            fmt(lr.get("p_value"), "p"),
            "NA",
            "NA",
            "NA",
        ]
    )
    rm = analysis["survival"]["rmst"]
    for arm, d in (rm.get("differences") or {}).items():
        rows.append(
            [
                f"{arm} - {rm['reference']}",
                f"RMST up to {fmt(rm['tau'], 'int')} steps",
                "NA",
                "NA",
                "NA",
                fmt(d["difference"], "stat"),
                fmt(d["low"], "stat"),
                fmt(d["high"], "stat"),
            ]
        )
    out["continuous"] = (
        [
            "comparison",
            "metric",
            "n",
            "statistic",
            "p_value",
            "effect",
            "effect_low",
            "effect_high",
        ],
        rows,
    )

    rows = []
    for arm, entry in analysis["failure_modes"]["arms"].items():
        for level, key in (
            ("stop_reason", "by_stop_reason"),
            ("failure_mode", "by_failure_mode"),
        ):
            for cat, share in entry[key].items():
                w = share.get("wilson") or [None, None]
                rows.append(
                    [
                        arm,
                        level,
                        cat,
                        fmt(share["k"], "int"),
                        fmt(share["n"], "int"),
                        fmt(share["rate"], "rate"),
                        fmt(w[0], "rate"),
                        fmt(w[1], "rate"),
                    ]
                )
    out["failure_modes"] = (
        ["arm", "level", "category", "k", "n", "rate", "wilson_low", "wilson_high"],
        rows,
    )

    rows = []
    for arm in arms:
        for stratum, res in analysis["agreement"][arm].items():
            share = res["agreement"]
            w = share.get("wilson") or [None, None]
            rows.append(
                [
                    arm,
                    stratum,
                    fmt(share["n"], "int"),
                    fmt(share["k"], "int"),
                    fmt(share["rate"], "rate"),
                    fmt(w[0], "rate"),
                    fmt(w[1], "rate"),
                    fmt(res["false_success"]["rate"], "rate"),
                    fmt(res["missed_success"]["rate"], "rate"),
                    fmt(res.get("kappa"), "stat"),
                    fmt(res.get("undecided"), "int"),
                    fmt(res.get("no_verdict"), "int"),
                ]
            )
    out["agreement"] = (
        [
            "arm",
            "ended_by",
            "judged",
            "agree",
            "agreement",
            "wilson_low",
            "wilson_high",
            "false_success",
            "missed_success",
            "kappa",
            "undecided",
            "no_verdict",
        ],
        rows,
    )

    table = analysis["power"]["table"]
    rows = []
    rhos = sorted(
        {
            rho
            for row in table["rows"]
            for b in row["baselines"]
            for rho in b["paired_mcnemar"]
        }
    )
    for row in table["rows"]:
        for b in row["baselines"]:
            rows.append(
                [
                    fmt(row["n"], "int"),
                    fmt(row["wilson_width_at_half"], "rate"),
                    fmt(b["baseline"], "rate"),
                    fmt(b["unpaired_fisher"], "rate"),
                    *[fmt(b["paired_mcnemar"][rho], "rate") for rho in rhos],
                ]
            )
    out["power"] = (
        [
            "n_per_arm",
            "wilson_width_at_0.5",
            "baseline",
            "mdd_unpaired_fisher",
            *[f"mdd_paired_rho_{r}" for r in rhos],
        ],
        rows,
    )

    drift = analysis["drift"]
    rows = []
    for key in ("reference_drift", "arm_time_interaction", "carryover"):
        d = drift.get(key)
        if d is None:
            rows.append([key, "NA", "NA", "not run"])
            continue
        p = d.get("p_value")
        if key == "reference_drift":
            p = (d.get("trend") or {}).get("p_value")
        if key == "carryover":
            found = [
                a["fisher_p"] for a in d.get("arms", {}).values() if "fisher_p" in a
            ]
            p = min(found) if found else None
        rows.append(
            [
                key,
                fmt(d.get("statistic"), "stat"),
                fmt(p, "p"),
                str(bool(d.get("warning"))).lower(),
            ]
        )
    out["drift"] = (["diagnostic", "statistic", "p_value", "warning"], rows)
    return out


# ----------------------------------------------------------------- privacy

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
ABS_PATH = re.compile(r"(?<![\w.~/-])(?:~/|/)[^\s\"'()\[\]{}<>,;]+")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
# Keys never written: names and addresses of people.
PERSON_KEYS = frozenset(
    {
        "name",
        "names",
        "full_name",
        "email",
        "e_mail",
        "user",
        "username",
        "author",
        "operator",
        "operator_name",
        "person",
    }
)
# Keys that are site details (dropped unless asked for).
SITE_KEYS = re.compile(
    r"(serial|camera|_ip$|^ip$|host|url|path|dir$|root$|remote|address|mac$)"
)


def scrub(value, *, site_details: bool = False):
    """A copy of ``value`` without names or e-mail addresses and, unless
    ``site_details``, without serial numbers, IP addresses, host names,
    URLs and local paths (by key, and inside strings)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            k = str(key).lower()
            if k in PERSON_KEYS:
                continue
            if not site_details and SITE_KEYS.search(k):
                continue
            out[str(key)] = scrub(item, site_details=site_details)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v, site_details=site_details) for v in value]
    if isinstance(value, str):
        value = EMAIL.sub("[redacted]", value)
        if not site_details:
            value = URL.sub("[redacted]", value)
            value = IPV4.sub("[redacted]", value)
            value = ABS_PATH.sub("[redacted]", value)
        return value
    return value


# ------------------------------------------------------------------ output


def _data_rows(ledger: L.Ledger, basis: str) -> tuple[list, list]:
    header = [
        *[f for f in L.TrialRow.__dataclass_fields__ if f != "schema"],
        "pairable",
        "outcome",
        "outcome_label_kind",
        *L.LABEL_KINDS,
        "failure_mode",
    ]
    rows = []
    for r in ledger.rows:
        entry = ledger.labels.get(r.episode_id) or {}
        labels = entry.get("labels", {})
        value, kind = (
            L.label_value(entry, basis) if r.status == "valid" else (None, None)
        )
        d = r.to_dict()
        d["layout_reason"] = scrub(d["layout_reason"])
        rows.append(
            [
                *[d[f] for f in L.TrialRow.__dataclass_fields__ if f != "schema"],
                r.pairable,
                value,
                kind,
                *[labels.get(k) for k in L.LABEL_KINDS],
                entry.get("failure_mode"),
            ]
        )
    return header, rows


def _csv_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


def _parquet(header, rows) -> bytes:
    import pandas as pd

    frame = pd.DataFrame(rows, columns=header)
    for column in frame.columns:
        values = [v for v in frame[column] if v is not None]
        if values and all(isinstance(v, bool) for v in values):
            frame[column] = frame[column].astype("boolean")
        elif values and all(
            isinstance(v, int) and not isinstance(v, bool) for v in values
        ):
            frame[column] = frame[column].astype("Int64")
        else:
            frame[column] = frame[column].astype("string")
    buffer = io.BytesIO()
    frame.to_parquet(buffer, engine="pyarrow", index=False)
    return buffer.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=1, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _recover(report_root: Path, basis: str) -> None:
    """Under the basis lock: what an earlier writer left. A crash between
    moving the old report aside and moving the new one in leaves no
    ``<basis>`` folder and an ``.old-*`` one: put the old one back. Every
    other leftover (half-built ``.tmp-*`` folders, a second ``.old-*``) is
    removed."""
    target = report_root / basis
    olds = sorted(report_root.glob(f".{basis}.old-*"))
    if not target.exists() and olds:
        os.replace(max(olds, key=lambda p: p.stat().st_mtime_ns), target)
        _fsync_dir(report_root)
    for stale in report_root.glob(f".{basis}.*-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)


def _fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_report(
    report_root,
    ledger: L.Ledger,
    info: CampaignInfo,
    basis: str,
    *,
    layout: L.CampaignLayout | None = None,
    runs=(),
    site: dict | None = None,
    include_site_details: bool = False,
    blinded: bool = False,
    seed: int | None = None,
    resamples: int = 10_000,
    now: float | None = None,
    pdf: bool = True,
) -> dict:
    """Write ``<report_root>/<basis>/`` and return its manifest. ``runs``:
    one ``read_aeri_run`` record per child run (plan digest, state, LEVI
    commit, modes); ``site``: the device snapshot and other site facts,
    scrubbed (see ``scrub``). Nothing outside ``<report_root>/<basis>`` and
    its lock file is written."""
    report_root = Path(report_root)
    analysis = analyse(
        ledger, info, basis, layout=layout, seed=seed, resamples=resamples
    )
    analysis["not_verified"] = [
        {"arm": a.id, "items": list(a.not_verified)}
        for a in info.arms
        if a.not_verified
    ]
    level = analysis["conclusion_level"]["level"]
    texts = {lang: summary(analysis, lang, blinded=blinded) for lang in LANGS}
    mask = [info.campaign_id, info.task, *analysis["arms"]]
    for lang, text in texts.items():
        check_naming(text, basis, lang)
        check_wording(text, confirmatory=level == "confirmatory", mask=mask)
    table_data = tables(analysis)
    for header, _ in table_data.values():
        check_naming(" ".join(header), basis)
    specs = figure_specs(analysis)
    for spec, _ in specs:
        if spec is not None:
            check_naming(spec.to_json(), basis)

    target = report_root / basis
    with _locked(report_root / f".{basis}.lock"):
        _recover(report_root, basis)
        work = report_root / f".{basis}.tmp-{os.getpid()}-{time.time_ns()}"
        work.mkdir(parents=True)
        try:
            files = {}

            def put(rel: str, data: bytes) -> None:
                path = work / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                write_durable(path, data)
                files[rel] = {"bytes": len(data), "sha256": _sha(data)}

            for lang, text in texts.items():
                put(f"summary.{lang}.md", text.encode("utf-8"))
            for name, (header, rows) in table_data.items():
                put(f"tables/{name}.csv", table_csv(header, rows))
                if name != "drift":
                    put(f"tables/{name}.tex", table_tex(header, rows))
            figures, skipped = {}, {}
            (work / "figures").mkdir()
            for spec, why in specs:
                if spec is None:
                    skipped[why[0]] = why[1]
                    continue
                stem = work / "figures" / spec.id
                records = write_figure(spec, stem, ("svg", "pdf") if pdf else ("svg",))
                records["svg_zh"] = write_figure(
                    spec, work / "figures" / f"{spec.id}.zh-CN", ("svg",), lang="zh-CN"
                )["svg"]
                put(f"figures/{spec.id}.json", spec.to_json().encode("utf-8"))
                for fmt_name, rec in records.items():
                    rel = str(Path(rec["path"]).relative_to(work))
                    rec = {**rec, "path": rel}
                    files[rel] = {"bytes": rec["bytes"], "sha256": rec["sha256"]}
                    records[fmt_name] = rec
                figures[spec.id] = records
            header, rows = _data_rows(ledger, basis)
            put(
                "data/trials.csv",
                table_csv(header, [[_csv_value(v) for v in row] for row in rows]),
            )
            put("data/trials.parquet", _parquet(header, rows))
            label_rows = []
            for r in ledger.rows:
                entry = ledger.labels.get(r.episode_id) or {}
                for kind, value in sorted(entry.get("labels", {}).items()):
                    label_rows.append([r.episode_id, r.arm, kind, value])
            put(
                "data/labels.csv",
                table_csv(["episode_id", "arm", "kind", "value"], label_rows),
            )
            put("data/analysis.json", _json_bytes(analysis))
            manifest = _manifest(
                analysis,
                info,
                basis,
                runs=runs,
                site=site,
                include_site_details=include_site_details,
                blinded=blinded,
                files=files,
                figures=figures,
                skipped=skipped,
                now=now,
            )
            write_durable(work / "manifest.json", _json_bytes(manifest))
            _fsync_dir(work)
            old = None
            if target.exists():
                old = report_root / f".{basis}.old-{os.getpid()}-{time.time_ns()}"
                os.replace(target, old)
            os.replace(work, target)
            _fsync_dir(report_root)
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
    return manifest


def _manifest(
    analysis,
    info,
    basis,
    *,
    runs,
    site,
    include_site_details,
    blinded,
    files,
    figures,
    skipped,
    now,
) -> dict:
    commits = sorted({r.get("levi_commit") for r in runs if r.get("levi_commit")})
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "report_schema": SCHEMA,
        "campaign_id": info.campaign_id,
        "basis": basis,
        "basis_name": analysis["header"]["basis_name"],
        "conclusion_level": analysis["conclusion_level"]["level"],
        "campaign_sha256": info.campaign_sha256,
        "settings_sha256": info.settings_sha256,
        "runs": [
            {
                k: r.get(k)
                for k in (
                    "run_id",
                    "plan_sha256",
                    "state",
                    "levi_commit",
                    "reset_mode",
                    "scene_check",
                )
            }
            for r in runs
        ],
        "levi_commits": commits,
        "arms": [
            {
                "id": a.id,
                "role": a.role,
                "checkpoint": a.checkpoint,
                "config": a.config,
                "sha256_status": a.sha256_status,
                "manifest_sha256": a.manifest_sha256,
                "versions": a.versions,
                "not_verified": list(a.not_verified),
            }
            for a in info.arms
        ],
        "seed": analysis["seed"],
        "schedule": info.schedule,
        "trials_per_arm": info.trials_per_arm,
        "switches": info.switches,
        "peeks": info.peeks,
        "operator_blind": info.operator_blind,
        "deviated": analysis["header"]["deviated"],
        "unplanned_runs": analysis["unplanned_runs"],
        "methods": analysis["methods"],
        "files": dict(sorted(files.items())),
        "figures": figures,
        "figures_skipped": skipped,
        "png": "skipped(no converter)",
        "blinded_summary": bool(blinded),
        "include_site_details": bool(include_site_details),
        "site": scrub(site or {}, site_details=include_site_details),
        "extra": scrub(info.extra, site_details=include_site_details),
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now)
        ),
        "llm": "none: the text is filled from templates",
    }
    return (
        scrub(manifest, site_details=True)
        if include_site_details
        else _scrub_manifest(manifest)
    )


def _scrub_manifest(manifest: dict) -> dict:
    """The manifest without site details: strings are scrubbed, keys of
    the report's own structure are kept."""
    keep = {"files", "figures", "methods", "runs", "arms"}
    out = {}
    for key, value in manifest.items():
        out[key] = value if key in keep else scrub(value)
    for arm in out["arms"]:
        arm["versions"] = scrub(arm["versions"])
        arm["not_verified"] = scrub(arm["not_verified"])
    return out
