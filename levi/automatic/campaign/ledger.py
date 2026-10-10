"""The trial ledger of a multi-arm campaign (T-CP-05, design X3 §1.6, §1.7, §3).

One row per forward episode that a campaign segment ran, derived again
every time from what the child runs wrote: the campaign keeps no truth of
its own, so ``derive`` over the same inputs gives the same rows, byte for
byte (``write_ledger`` is idempotent).

**Inputs.**

- ``CampaignLayout``: the campaign id and its segments in campaign order,
  each with its arm, round, layout cards in slot order and the child run
  ids that ran it (the first run, then the reruns that finished its missing
  slots, design §1.6 "段内缺额"). The schedule module produces this; the
  ledger only reads it.
- ``EpisodeFact`` lists, one per run id, from either source:
  ``read_aeri_run`` (an AERI child run: ``manifest.json``, the run journal
  and ``labels/``) or ``levi.automatic.campaign.guided.legacy_fact`` (the
  legacy evaluation client: the rollout's ``metadata.json``).

**Rows.** ``trial_id = <campaign>:<round>:<card>:<arm>`` for an episode that
holds a card; ``row_id`` names every row (``<campaign>:s<NN>:<episode>``).
A row carries ``slot`` (the card's 1-based position in its segment),
``segment``, ``run_id``, ``episode_id``, ``order_index`` (position in the
campaign, 0-based), wall-clock ``started_at``/``ended_at``,
``layout_fidelity`` (``attested``, ``verified`` or ``deviated``, with a
reason for ``deviated``; None when the run recorded none),
``preceded_by_arm`` (the arm of the segment before this one, None for the
first) and ``rerun_of`` (the segment's first run id, for rows of a rerun).

**Which card an episode had.** A card named by the run manifest
(``episodes[*].campaign.card``) or confirmed by a person
(``card_source: operator_confirmed``) is taken as given. An AERI episode
without one takes the segment's next unfinished card: the run asked the
operator for exactly that card (``schedule_order``). A legacy client
episode without a confirmation holds **no** card: its ``candidate_card`` is
the next unfinished card, for a person to confirm, and it never enters a
paired analysis (``pairable`` false). A card outside the segment, a card a
valid episode already holds, or a valid episode beyond the last card is
flagged in ``card_problem`` and not paired.

**Counts.** Discarded episodes (the operator's ``d`` or ``discarded``
label) and incomplete ones (aborted, not sealed) stay in the ledger and are
counted, but hold no card and no outcome. Deviated layouts stay in every
analysis and are counted (the report adds an analysis without them).

**Labels** (design §3) never mix: each episode keeps the autonomous
verdict, the post-hoc verdict, the operator label and the adjudicated
label apart; ``label_value`` reads one of the five campaign bases. The
first two are automatic and never called ground truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from levi.automatic import metrics
from levi.automatic.figure_files import write_durable

SCHEMA = "levi.aeri.campaign_trial.v1"
LEDGER_FILE = "ledger.jsonl"
LAYOUT_FIDELITY = ("attested", "verified", "deviated")
LABEL_KINDS = metrics.LABEL_KINDS
LABEL_BASES = (
    "autonomous_verdict",
    "posthoc_verdict",
    "operator_label",
    "adjudicated_ground_truth",
    "adjudicated_then_operator",
)
# Bases whose rates are automatic verdicts nobody reviewed (design §3).
AUTOMATIC_BASES = frozenset({"autonomous_verdict", "posthoc_verdict"})
STATUSES = ("valid", "discarded", "incomplete")
SOURCES = ("aeri", "legacy_client")
CARD_SOURCES = ("run_manifest", "schedule_order", "operator_confirmed", "unconfirmed")
CONFIRMED = frozenset({"run_manifest", "schedule_order", "operator_confirmed"})
CARD_PROBLEMS = ("not_in_segment", "duplicate", "no_card_left")
# How the automatic verdict reads (``metrics.AUTOMATIC_KINDS``).
VERDICTS = metrics.AUTOMATIC_KINDS
DECIDED = ("success", "failure")
# How a forward episode ended, the same in both sources: the budget ran
# out, the detector stopped it, a person stopped it, anything else.
ENDED = ("budget", "early_stop", "operator_stop", "unknown")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ARM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")


class LedgerError(ValueError):
    """The inputs cannot give a ledger as they stand (a broken journal, a
    run id used by two segments, an unknown value). Nothing is guessed."""


def _slug(value, what: str, pattern=SLUG) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LedgerError(f"{what} {value!r} is not a valid id")
    return value


# --------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class SegmentPlan:
    """One segment: an arm runs ``cards`` in order in ``round``."""

    segment: int
    arm: str
    round: int
    cards: tuple[str, ...]
    run_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "cards", tuple(self.cards))
        object.__setattr__(self, "run_ids", tuple(self.run_ids))
        for name in ("segment", "round"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LedgerError(f"{name} must be a positive whole number")
        _slug(self.arm, "arm", ARM)
        for card in self.cards:
            _slug(card, "card")
        if len(set(self.cards)) != len(self.cards):
            raise LedgerError(f"segment {self.segment} names a card twice")
        for run in self.run_ids:
            _slug(run, "run id")
        if len(set(self.run_ids)) != len(self.run_ids):
            raise LedgerError(f"segment {self.segment} names a run twice")


@dataclass(frozen=True)
class CampaignLayout:
    campaign_id: str
    segments: tuple[SegmentPlan, ...]
    # ``card_set``: layout cards pair the arms; ``none``: a reset policy
    # sets the scene, nothing is paired (design §1.3).
    layout_source: str = "card_set"

    def __post_init__(self):
        object.__setattr__(self, "segments", tuple(self.segments))
        _slug(self.campaign_id, "campaign id")
        if self.layout_source not in ("card_set", "none"):
            raise LedgerError("layout_source is card_set or none")
        numbers = [s.segment for s in self.segments]
        if len(set(numbers)) != len(numbers):
            raise LedgerError("a segment number is given twice")
        seen: dict = {}
        for seg in self.segments:
            for run in seg.run_ids:
                if run in seen:
                    raise LedgerError(
                        f"run {run} is in segments {seen[run]} and {seg.segment}"
                    )
                seen[run] = seg.segment

    @property
    def arms(self) -> list:
        return sorted({s.arm for s in self.segments})

    @staticmethod
    def from_dict(data: dict) -> CampaignLayout:
        return CampaignLayout(
            campaign_id=data["campaign_id"],
            layout_source=data.get("layout_source", "card_set"),
            segments=tuple(
                SegmentPlan(
                    segment=s["segment"],
                    arm=s["arm"],
                    round=s["round"],
                    cards=tuple(s.get("cards", ())),
                    run_ids=tuple(s.get("run_ids", ())),
                )
                for s in data["segments"]
            ),
        )


@dataclass(frozen=True)
class EpisodeFact:
    """What one source says about one forward episode of one run."""

    run_id: str
    episode_id: str
    number: int
    source: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    steps: int | None = None
    max_steps: int | None = None
    stop_reason: str | None = None
    ended_by: str = "unknown"
    control: bool = False
    would_stop_step: int | None = None
    control_recorded: bool = False
    card: str | None = None
    card_source: str = "unconfirmed"
    layout_fidelity: str | None = None
    layout_reason: str = ""
    # {kind: value} for the four label kinds (absent: no label of that kind)
    labels: dict = field(default_factory=dict)
    failure_mode: str | None = None
    # the operator changed a decided label after the verdict was revealed
    revised_after_reveal: bool = False

    def __post_init__(self):
        if self.source not in SOURCES:
            raise LedgerError(f"source {self.source!r}")
        if self.status not in STATUSES:
            raise LedgerError(f"status {self.status!r}")
        if self.card_source not in CARD_SOURCES:
            raise LedgerError(f"card_source {self.card_source!r}")
        if (self.card is None) != (self.card_source == "unconfirmed"):
            raise LedgerError("a card comes with its source, and only then")
        if self.layout_fidelity not in (None, *LAYOUT_FIDELITY):
            raise LedgerError(f"layout_fidelity {self.layout_fidelity!r}")
        if self.layout_fidelity == "deviated" and not self.layout_reason.strip():
            raise LedgerError(f"{self.episode_id}: a deviated layout needs a reason")
        if self.ended_by not in ENDED:
            raise LedgerError(f"ended_by {self.ended_by!r}")
        for kind, value in self.labels.items():
            if kind not in LABEL_KINDS:
                raise LedgerError(f"label kind {kind!r}")
            allowed = (
                VERDICTS if kind == "autonomous_verdict" else metrics.OPERATOR_VALUES
            )
            if value not in allowed:
                raise LedgerError(f"{kind}={value!r}")


# ----------------------------------------------------------------------- rows


@dataclass(frozen=True)
class TrialRow:
    campaign_id: str
    row_id: str
    trial_id: str | None
    source: str
    segment: int
    arm: str
    round: int
    slot: int | None
    card: str | None
    card_source: str
    card_problem: str | None
    candidate_card: str | None
    run_id: str
    episode_id: str
    number: int
    order_index: int
    started_at: str | None
    ended_at: str | None
    status: str
    layout_fidelity: str | None
    layout_reason: str
    preceded_by_arm: str | None
    rerun_of: str | None
    steps: int | None
    max_steps: int | None
    stop_reason: str | None
    ended_by: str
    control: bool
    would_stop_step: int | None
    control_recorded: bool
    schema: str = SCHEMA

    @property
    def card_confirmed(self) -> bool:
        return self.card_source in CONFIRMED and self.card is not None

    @property
    def pairable(self) -> bool:
        """A valid episode on a card it surely had: it may be paired."""
        return (
            self.status == "valid"
            and self.card_confirmed
            and self.card_problem is None
            and self.trial_id is not None
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ledger:
    campaign_id: str
    layout_source: str
    rows: list
    # {episode key: {"labels": {...}, "failure_mode", "revised_after_reveal"}}
    labels: dict
    # run ids the facts came from that no segment names (left out)
    unplanned_runs: list

    def counts(self) -> dict:
        return counts(self)


def _key(fact: EpisodeFact) -> str:
    return fact.episode_id


def derive(layout: CampaignLayout, facts_by_run: dict) -> Ledger:
    """Rows of every planned run's episodes, in campaign order. A run the
    layout names but that has no facts yet contributes nothing (its slots
    stay missing); facts of a run no segment names are left out and listed
    in ``unplanned_runs``."""
    planned = {run for seg in layout.segments for run in seg.run_ids}
    unplanned = sorted(set(facts_by_run) - planned)
    rows: list = []
    labels: dict = {}
    seen_episodes: set = set()
    previous_arm = None
    order = 0
    for seg in sorted(layout.segments, key=lambda s: s.segment):
        held: set = set()
        for index, run in enumerate(seg.run_ids):
            facts = sorted(facts_by_run.get(run, ()), key=lambda f: f.number)
            for fact in facts:
                if fact.run_id != run:
                    raise LedgerError(f"{fact.episode_id} is listed under run {run}")
                key = _key(fact)
                if key in seen_episodes:
                    raise LedgerError(f"episode {key} appears twice")
                seen_episodes.add(key)
                card, problem, candidate = None, None, None
                source = fact.card_source
                if fact.status == "valid":
                    remaining = [c for c in seg.cards if c not in held]
                    if fact.card is not None:
                        if fact.card not in seg.cards:
                            problem = "not_in_segment"
                        elif fact.card in held:
                            problem = "duplicate"
                        card = fact.card
                    elif fact.source == "aeri":
                        if remaining:
                            card, source = remaining[0], "schedule_order"
                        else:
                            problem = "no_card_left"
                    else:
                        candidate = remaining[0] if remaining else None
                        if not remaining:
                            problem = "no_card_left"
                    if card is not None and problem is None:
                        held.add(card)
                else:
                    # A discarded or incomplete episode holds no card: the
                    # operator places the same card again.
                    source = "unconfirmed"
                slot = (
                    seg.cards.index(card) + 1
                    if card is not None and card in seg.cards
                    else None
                )
                trial_id = (
                    f"{layout.campaign_id}:{seg.round}:{card}:{seg.arm}"
                    if card is not None and problem is None
                    else None
                )
                rows.append(
                    TrialRow(
                        campaign_id=layout.campaign_id,
                        row_id=f"{layout.campaign_id}:s{seg.segment:02d}:{key}",
                        trial_id=trial_id,
                        source=fact.source,
                        segment=seg.segment,
                        arm=seg.arm,
                        round=seg.round,
                        slot=slot,
                        card=card,
                        card_source=source if card is not None else "unconfirmed",
                        card_problem=problem,
                        candidate_card=candidate,
                        run_id=run,
                        episode_id=fact.episode_id,
                        number=fact.number,
                        order_index=order,
                        started_at=fact.started_at,
                        ended_at=fact.ended_at,
                        status=fact.status,
                        layout_fidelity=fact.layout_fidelity,
                        layout_reason=fact.layout_reason,
                        preceded_by_arm=previous_arm,
                        rerun_of=seg.run_ids[0] if index > 0 else None,
                        steps=fact.steps,
                        max_steps=fact.max_steps,
                        stop_reason=fact.stop_reason,
                        ended_by=fact.ended_by,
                        control=fact.control,
                        would_stop_step=fact.would_stop_step,
                        control_recorded=fact.control_recorded,
                    )
                )
                labels[key] = {
                    "labels": dict(sorted(fact.labels.items())),
                    "failure_mode": fact.failure_mode,
                    "revised_after_reveal": fact.revised_after_reveal,
                }
                order += 1
        previous_arm = seg.arm
    return Ledger(layout.campaign_id, layout.layout_source, rows, labels, unplanned)


def counts(ledger: Ledger, layout: CampaignLayout | None = None) -> dict:
    """Per arm: valid, discarded, incomplete, deviated, rerun rows, rows
    without a confirmed card, card problems and (with the layout) planned
    and missing slots."""
    out: dict = {}
    for row in ledger.rows:
        entry = out.setdefault(
            row.arm,
            {
                "rows": 0,
                "valid": 0,
                "discarded": 0,
                "incomplete": 0,
                "deviated": 0,
                "from_reruns": 0,
                "unconfirmed": 0,
                "card_problems": 0,
                "pairable": 0,
            },
        )
        entry["rows"] += 1
        entry[row.status] += 1
        if row.status == "valid":
            entry["deviated"] += row.layout_fidelity == "deviated"
            entry["unconfirmed"] += not row.card_confirmed and row.card_problem is None
            entry["card_problems"] += row.card_problem is not None
            entry["pairable"] += row.pairable
        entry["from_reruns"] += row.rerun_of is not None
    if layout is not None:
        for seg in layout.segments:
            entry = out.setdefault(
                seg.arm,
                {
                    "rows": 0,
                    "valid": 0,
                    "discarded": 0,
                    "incomplete": 0,
                    "deviated": 0,
                    "from_reruns": 0,
                    "unconfirmed": 0,
                    "card_problems": 0,
                    "pairable": 0,
                },
            )
            entry["planned"] = entry.get("planned", 0) + len(seg.cards)
        held = {(r.arm, r.round, r.card) for r in ledger.rows if r.pairable}
        for seg in layout.segments:
            missing = sum((seg.arm, seg.round, c) not in held for c in seg.cards)
            out[seg.arm]["missing"] = out[seg.arm].get("missing", 0) + missing
    return dict(sorted(out.items()))


# --------------------------------------------------------------------- labels


def label_value(entry: dict, basis: str) -> tuple:
    """``(value, kind)`` of an episode under ``basis``: ``value`` is
    ``success``, ``failure`` or None (no label on this basis), ``kind`` the
    label kind it came from. An undecided or missing automatic verdict, a
    ``discarded`` or ``unclear`` operator label, are no value.

    ``metrics.LabelStore.truth`` reads the last two bases; the first three
    are read here so its default behaviour stays as it is."""
    if basis not in LABEL_BASES:
        raise LedgerError(f"label basis is one of {', '.join(LABEL_BASES)}")
    labels = (entry or {}).get("labels", {})
    kinds = {
        "autonomous_verdict": ("autonomous_verdict",),
        "posthoc_verdict": ("posthoc_verdict",),
        "operator_label": ("operator_label",),
        "adjudicated_ground_truth": ("adjudicated_ground_truth",),
        "adjudicated_then_operator": ("adjudicated_ground_truth", "operator_label"),
    }[basis]
    for kind in kinds:
        value = labels.get(kind)
        if value in DECIDED:
            return value, kind
    return None, None


# ---------------------------------------------------------------- AERI runs


def _iso_ns(ns) -> str | None:
    if not isinstance(ns, int):
        return None
    return (
        datetime.fromtimestamp(ns / 1e9, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def run_header(events) -> dict:
    """The run header's ``plan_sha256``, ``levi_commit``, ``reset_mode`` and
    ``scene_check`` (None where absent)."""
    for event in events:
        if event.record == "run_header":
            header = getattr(event, "header", None)
            if header is None:
                break
            return {
                name: getattr(header, name, None)
                for name in ("plan_sha256", "levi_commit", "reset_mode", "scene_check")
            }
    return dict.fromkeys(("plan_sha256", "levi_commit", "reset_mode", "scene_check"))


def read_aeri_run(run_dir, *, max_steps: int | None = None) -> tuple[list, dict]:
    """``(facts, run)`` of one AERI child run, read without any lock and
    without writing: the run manifest, the journal (the source of truth for
    each episode's result) and the label files. ``run`` holds the run id,
    its state, the header fields and the manifest's ``campaign`` block.

    A forward episode is ``valid`` when its result is committed and its
    rollout sealed complete and no operator discarded it, ``discarded``
    when the operator's current label says so, else ``incomplete``. A
    broken journal raises ``LedgerError``: a person must look first."""
    from levi.automatic.journal import Journal

    run_dir = Path(run_dir)
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{run_dir.name}: unreadable run manifest ({exc})") from None
    scan = Journal.read(run_dir)
    if scan.corrupt:
        raise LedgerError(
            f"{run_dir.name}: the run journal is corrupt ({scan.corrupt})"
        )
    events = list(scan.events)
    run_id = manifest.get("run_id")
    _slug(run_id, "run id")
    records = {r.episode_id: r for r in metrics.episodes(events, manifest=manifest)}
    ended = {
        e.episode_id: getattr(e, "emitted_wall_ns", None)
        for e in events
        if e.record == "committed" and e.episode_result is not None
    }
    store = metrics.LabelStore(run_dir)
    try:
        posthoc = store.latest("posthoc_verdict")
        adjudicated = store.latest("adjudicated_ground_truth")
        operator = metrics.operator_view(store.lines("operator_label"))
        failure_modes = {
            line["episode_id"]: line.get("failure_mode")
            for line in store.lines("posthoc_verdict")
            if line.get("subject") == "task_outcome" and line.get("failure_mode")
        }
    except metrics.LabelRefused as exc:
        raise LedgerError(f"{run_dir.name}: {exc}") from None
    facts = []
    for entry in manifest.get("episodes", []):
        if entry.get("role") != "forward":
            continue
        episode_id = entry["episode_id"]
        number = int(episode_id.rsplit(".", 1)[1])
        record = records.get(episode_id)
        op = operator.get(episode_id, {})
        current = op.get("current")
        if current == "discarded":
            status = "discarded"
        elif (
            record is not None
            and record.sealed == "complete"
            and entry.get("state") == "complete"
        ):
            status = "valid"
        else:
            status = "incomplete"
        campaign = entry.get("campaign") or {}
        card = campaign.get("card")
        labels = {}
        if record is not None:
            labels["autonomous_verdict"] = metrics.automatic_of(
                record.goal_verification
            )
        for kind, found in (
            ("posthoc_verdict", posthoc),
            ("adjudicated_ground_truth", adjudicated),
        ):
            if episode_id in found:
                labels[kind] = found[episode_id]
        if current is not None:
            labels["operator_label"] = current
        facts.append(
            EpisodeFact(
                run_id=run_id,
                episode_id=episode_id,
                number=number,
                source="aeri",
                status=status,
                started_at=entry.get("opened_at"),
                ended_at=_iso_ns(ended.get(episode_id)),
                steps=record.steps if record is not None else entry.get("steps"),
                max_steps=max_steps,
                stop_reason=record.stop_reason if record is not None else None,
                ended_by=metrics.ended_by(record.stop_reason)
                if record is not None
                else "unknown",
                control=bool(record.control) if record is not None else False,
                would_stop_step=record.would_stop_step if record is not None else None,
                control_recorded=bool(record.control_recorded)
                if record is not None
                else False,
                card=card,
                card_source="run_manifest" if card is not None else "unconfirmed",
                layout_fidelity=campaign.get("layout_fidelity"),
                layout_reason=str(campaign.get("layout_reason") or ""),
                labels=labels,
                failure_mode=failure_modes.get(episode_id),
                revised_after_reveal=bool(op.get("revised_after_reveal")),
            )
        )
    run = {
        "run_id": run_id,
        "state": scan.effective_state,
        **run_header(events),
        "campaign": manifest.get("campaign"),
    }
    return facts, run


# ---------------------------------------------------------------- the file


def ledger_bytes(ledger: Ledger) -> bytes:
    """The ledger file's bytes: one JSON line per row, keys sorted. The
    same rows always give the same bytes."""
    return b"".join(
        (json.dumps(row.to_dict(), sort_keys=True, ensure_ascii=False) + "\n").encode()
        for row in ledger.rows
    )


def write_ledger(path, ledger: Ledger) -> bool:
    """Write ``ledger.jsonl`` durably (temporary file, fsync, rename).
    Returns False, writing nothing, when the file already holds exactly
    these rows (a re-derivation is idempotent)."""
    path = Path(path)
    data = ledger_bytes(ledger)
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    write_durable(path, data)
    return True


def read_ledger(path) -> list:
    """The rows of a ledger file. A line that is not a row of this schema
    raises ``LedgerError`` (the file is derived: derive it again)."""
    rows = []
    names = set(TrialRow.__dataclass_fields__)
    for number, line in enumerate(Path(path).read_bytes().split(b"\n")):
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            raw = None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            raise LedgerError(f"line {number + 1} is not a {SCHEMA} row")
        rows.append(TrialRow(**{k: v for k, v in raw.items() if k in names}))
    return rows
