"""Guided legacy-client campaign, data side (T-CP-11, design X3 §1.7).

Until a real AERI robot adapter exists, a campaign can run on the legacy
evaluation client: for each segment a person copies a command and runs it,
and the campaign collects what the client wrote.

**Commands.** ``base_command`` takes the dual-label evaluation command from
the operator guide (``setup.md`` §6.3, its first code block), read with the
same parser as the setup recipes (``levi.setup.recipes``). ``render``
replaces only the allowed parameters (``--eval-num``, ``--rollout-group``,
``--eval-note`` and, optionally, ``--prompt``) and checks that everything
else is character for character the guide's text; ``--levi-mode dual`` must
already be there. A campaign keeps the excerpt it was planned with
(``BaseCommand.to_dict``); ``check_drift`` compares it with the guide as it
is now and ``render_checked`` refuses, with a unified diff, when the guide
changed (all segments must run the same command). The guide holds
machine-specific values (camera serials, home pose), so the repository
keeps no copy of it.

**Collecting.** ``scan_rollouts`` lists a task folder's rollouts (read
only: each ``metadata.json`` and whether ``.complete`` exists).
``collect_segment`` keeps the dual-label rollouts whose ``eval.eval_note``
names this campaign and segment, groups them by the client's ``run_id``
(the first run, then reruns, by start time) and turns each into an
``EpisodeFact`` (``legacy_fact``). Matching an episode to a layout card by
its position is error-prone, so a card counts only once a person has
confirmed it (``CardConfirmations``); ``pending`` lists the valid
episodes still waiting, each with its candidate card (the next unfinished
one). An unconfirmed episode never enters a paired analysis.

**Agreement.** ``segment_agreement`` compares the automatic label with the
operator's, apart for episodes that ran the full step budget and for those
the operator's key ended early: only the full-budget agreement carries over
to unattended runs (``ended_by``; setup §6.3).
"""

from __future__ import annotations

import difflib
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from levi.automatic import analysis as an
from levi.automatic import metrics
from levi.live import criteria
from levi.setup import recipes

from . import ledger as L

SECTION = "6.3"
BLOCK = 1
NOTE = re.compile(
    r"^(?P<campaign>[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}) s(?P<segment>\d{2,3})"
    r"(?: (?P<code>[A-Za-z0-9][A-Za-z0-9_-]{0,15}))?$"
)
GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Text that goes between double quotes in bash: nothing that ends the
# quotes, expands or escapes.
UNSAFE = re.compile(r'["\\$`\x00-\x1f\x7f!]')
FLAGS = {
    "eval_num": re.compile(r"(--eval-num[ =])(\d+)"),
    "rollout_group": re.compile(r"(--rollout-group[ =])([^\s\\]+)"),
    "eval_note": re.compile(r'(--eval-note[ =])"([^"\n]*)"'),
    "prompt": re.compile(r'(--prompt[ =])"([^"\n]*)"'),
}
MODE = re.compile(r"--levi-mode[ =](\S+)")
FOLDER = re.compile(r"^(demo|discarded|incomplete)_(\d+)")
CARD_SCHEMA = "levi.aeri.campaign_card.v1"
MAX_METADATA_BYTES = 1 << 20
ENDED_OF_CLIENT = {"operator_key": "operator_stop", "budget": "budget"}
STOP_OF_ENDED = {"operator_stop": "operator_stop", "budget": "horizon_exhausted"}


class GuidedError(ValueError):
    pass


class GuideDrift(GuidedError):
    """The guide's command changed since the campaign was planned."""

    def __init__(self, diff: str):
        super().__init__("the operator guide's command changed:\n" + diff)
        self.diff = diff


# ------------------------------------------------------------------ commands


@dataclass(frozen=True)
class BaseCommand:
    section: str
    block: int
    text: str
    first_line: int

    @property
    def sha256(self) -> str:
        return recipes.digest(self.text)

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "block": self.block,
            "text": self.text,
            "first_line": self.first_line,
            "sha256": self.sha256,
        }

    @staticmethod
    def from_dict(data: dict) -> BaseCommand:
        base = BaseCommand(
            data["section"], int(data["block"]), data["text"], int(data["first_line"])
        )
        if data.get("sha256") not in (None, base.sha256):
            raise GuidedError("the saved command does not match its digest")
        return base


def base_command(
    guide_text: str, section: str = SECTION, block: int = BLOCK
) -> BaseCommand:
    """The guide's code block ``block`` of section ``section``, which must
    be a dual-label evaluation command with each allowed parameter once."""
    guide = recipes.parse_guide(guide_text)
    if guide.problems:
        raise GuidedError("; ".join(guide.problems))
    found = guide.by_number(section)
    if len(found) != 1:
        raise GuidedError(
            f"section {section} appears {len(found)} times in the guide (need once)"
        )
    excerpt = recipes.excerpt(found[0], block)
    if excerpt is None:
        raise GuidedError(f"section {section} has no code block {block}")
    modes = MODE.findall(excerpt.text)
    if modes != ["dual"]:
        raise GuidedError(
            f"section {section} block {block} is not a dual-label command "
            f"(--levi-mode {', '.join(modes) or 'missing'})"
        )
    for name in ("eval_num", "rollout_group", "eval_note"):
        if len(FLAGS[name].findall(excerpt.text)) != 1:
            raise GuidedError(
                f"section {section} block {block} must give --{name.replace('_', '-')} once"
            )
    return BaseCommand(section, block, excerpt.text, excerpt.first)


def _masked(text: str) -> str:
    for name, pattern in FLAGS.items():
        text = pattern.sub(lambda m, n=name: f"{m.group(1)}<{n}>", text)
    return text


def _check_value(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuidedError(f"{name} must be non-empty text")
    if name == "rollout_group":
        if not GROUP.fullmatch(value):
            raise GuidedError(
                f"rollout group {value!r} must be a checkpoint's full name"
            )
    elif UNSAFE.search(value) or len(value) > 300:
        raise GuidedError(
            f"{name} may not hold quotes, backslashes, $, `, !, control characters "
            "or more than 300 characters"
        )
    return value


def eval_note(campaign_id: str, segment: int, code: str | None = None) -> str:
    """``<campaign id> s<NN> <arm code>``: what ``--eval-note`` says."""
    L._slug(campaign_id, "campaign id")
    if (
        isinstance(segment, bool)
        or not isinstance(segment, int)
        or not 1 <= segment <= 999
    ):
        raise GuidedError("segment must be 1..999")
    note = f"{campaign_id} s{segment:02d}"
    if code is not None:
        L._slug(code, "arm code", L.ARM)
        note += f" {code}"
    return note


def parse_note(note) -> tuple | None:
    """``(campaign id, segment, code or None)`` of an evaluation note made by
    ``eval_note``; None for any other note."""
    if not isinstance(note, str):
        return None
    m = NOTE.fullmatch(note.strip())
    if not m:
        return None
    return m["campaign"], int(m["segment"]), m["code"]


def render(
    base: BaseCommand,
    *,
    eval_num: int,
    rollout_group: str,
    eval_note: str,
    prompt: str | None = None,
) -> str:
    """The guide's command with only the allowed parameters replaced."""
    if isinstance(eval_num, bool) or not isinstance(eval_num, int) or eval_num < 1:
        raise GuidedError("eval_num must be a positive whole number")
    values = {
        "eval_num": str(eval_num),
        "rollout_group": _check_value("rollout_group", rollout_group),
        "eval_note": _check_value("eval_note", eval_note),
    }
    if prompt is not None:
        if len(FLAGS["prompt"].findall(base.text)) != 1:
            raise GuidedError("the guide's command gives no single --prompt to replace")
        values["prompt"] = _check_value("prompt", prompt)
    text = base.text
    for name, value in values.items():
        quoted = value if name in ("eval_num", "rollout_group") else f'"{value}"'
        text, count = FLAGS[name].subn(lambda m, q=quoted: m.group(1) + q, text)
        if count != 1:
            raise GuidedError(f"--{name.replace('_', '-')} appears {count} times")
    # Everything but the allowed values is the guide's text, unchanged.
    if _masked(text) != _masked(base.text):
        raise GuidedError("rendering changed more than the allowed parameters")
    return text


def check_drift(snapshot: BaseCommand, guide_text: str) -> str | None:
    """None when the guide still holds the snapshot's command; otherwise a
    unified diff (snapshot first). A section or block that is gone is
    drift too."""
    try:
        now = base_command(guide_text, snapshot.section, snapshot.block).text
    except GuidedError as exc:
        return f"--- planned\n+++ guide now\n{exc}\n"
    if now == snapshot.text:
        return None
    return "".join(
        difflib.unified_diff(
            (snapshot.text + "\n").splitlines(keepends=True),
            (now + "\n").splitlines(keepends=True),
            fromfile=f"planned (setup §{snapshot.section}, block {snapshot.block})",
            tofile="guide now",
        )
    )


def render_checked(snapshot: BaseCommand, guide_text: str, **values) -> str:
    """``render`` after making sure the guide has not changed since the
    campaign was planned (``GuideDrift`` with the diff otherwise)."""
    diff = check_drift(snapshot, guide_text)
    if diff is not None:
        raise GuideDrift(diff)
    return render(snapshot, **values)


def segment_commands(
    base: BaseCommand,
    layout: L.CampaignLayout,
    groups: dict,
    *,
    codes: dict | None = None,
    prompt: str | None = None,
) -> list:
    """One command per segment, in campaign order: ``--eval-num`` its card
    count, ``--rollout-group`` its arm's checkpoint full name (``groups``),
    ``--eval-note`` ``<campaign> s<NN> <code>``. ``codes`` maps arms to the
    codes the operator sees (default ``X1``, ``X2``, ... in arm order)."""
    arms = layout.arms
    codes = codes or {arm: f"X{i + 1}" for i, arm in enumerate(arms)}
    if set(codes) != set(arms) or len(set(codes.values())) != len(arms):
        raise GuidedError("every arm needs its own code")
    missing = [arm for arm in arms if arm not in groups]
    if missing:
        raise GuidedError(f"no rollout group for arm(s) {', '.join(missing)}")
    out = []
    for seg in sorted(layout.segments, key=lambda s: s.segment):
        note = eval_note(layout.campaign_id, seg.segment, codes[seg.arm])
        out.append(
            {
                "segment": seg.segment,
                "round": seg.round,
                "code": codes[seg.arm],
                "cards": list(seg.cards),
                "eval_note": note,
                "command": render(
                    base,
                    eval_num=len(seg.cards),
                    rollout_group=groups[seg.arm],
                    eval_note=note,
                    prompt=prompt,
                ),
            }
        )
    return out


# ----------------------------------------------------------------- rollouts


@dataclass(frozen=True)
class RolloutRecord:
    key: str  # <group>/<task folder>/<folder name>: no local path
    folder: str  # demo, discarded or incomplete
    number: int
    complete: bool
    metadata: dict


def scan_rollouts(root, group: str, task_folder: str) -> list:
    """Every rollout folder of one task, read only: its ``metadata.json``
    (folders without a readable one are skipped) and whether ``.complete``
    exists."""
    folder = Path(root) / group / task_folder
    out = []
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        m = FOLDER.match(path.name)
        if not m or not path.is_dir():
            continue
        meta_path = path / "metadata.json"
        try:
            if meta_path.stat().st_size > MAX_METADATA_BYTES:
                continue
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        out.append(
            RolloutRecord(
                key=f"{group}/{task_folder}/{path.name}",
                folder=m[1],
                number=int(m[2]),
                complete=(path / ".complete").exists(),
                metadata=meta,
            )
        )
    return out


def _automatic(meta) -> str | None:
    """The online judgement relayed into the rollout, as a verdict kind
    (``success``, ``failure``, ``undecided``, ``none``); None without one."""
    label = criteria.agent_label(meta)
    if label is None:
        return None
    verdict = label.get("verdict")
    if not verdict:
        return "none"
    if verdict.get("outcome") == "success":
        return "success"
    return "undecided" if verdict.get("undecided") else "failure"


def legacy_fact(
    record: RolloutRecord,
    *,
    card: str | None = None,
    posthoc: str | None = None,
) -> L.EpisodeFact:
    """What a legacy client rollout says about its episode. ``card``: a
    person's confirmation; ``posthoc``: the background review's verdict
    (``success``/``failure``), when the caller has it."""
    meta = record.metadata
    ev = meta.get("eval") if isinstance(meta.get("eval"), dict) else {}
    run_id = ev.get("run_id")
    if not isinstance(run_id, str) or not L.SLUG.fullmatch(run_id):
        raise GuidedError(f"{record.key}: no evaluation run id")
    operator = criteria.operator_label(meta)
    op = operator.get("outcome") if operator else None
    outcome = ev.get("outcome")
    if record.folder == "incomplete" or outcome == "aborted" or not record.complete:
        status = "incomplete"
    elif (
        record.folder == "discarded"
        or op == "discarded"
        or outcome == "discarded"
        or ev.get("counted") is False
    ):
        status = "discarded"
    else:
        status = "valid"
    labels = {}
    if op in metrics.OPERATOR_VALUES:
        labels["operator_label"] = op
    automatic = _automatic(meta)
    if automatic is not None:
        labels["autonomous_verdict"] = automatic
    if posthoc in L.DECIDED:
        labels["posthoc_verdict"] = posthoc
    ended = ENDED_OF_CLIENT.get(ev.get("ended_by"), "unknown")
    number = ev.get("episode_in_session")
    if isinstance(number, bool) or not isinstance(number, int):
        number = record.number

    def whole(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return L.EpisodeFact(
        run_id=run_id,
        episode_id=record.key,
        number=number,
        source="legacy_client",
        status=status,
        started_at=meta.get("created_at")
        if isinstance(meta.get("created_at"), str)
        else None,
        ended_at=meta.get("stopped_at")
        if isinstance(meta.get("stopped_at"), str)
        else None,
        steps=whole(ev.get("steps")),
        max_steps=whole(ev.get("max_steps")),
        stop_reason=STOP_OF_ENDED.get(ended),
        ended_by=ended,
        card=card,
        card_source="operator_confirmed" if card is not None else "unconfirmed",
        labels=labels,
    )


def collect_segment(
    records,
    layout: L.CampaignLayout,
    segment: int,
    *,
    confirmations: dict | None = None,
    posthoc: dict | None = None,
) -> dict:
    """The legacy client's episodes of one segment and what is still to be
    confirmed: ``runs`` (run ids, first run first), ``facts`` (per run),
    ``pending`` (valid episodes without a confirmed card, with their
    candidate card), ``ignored`` (rollouts of this segment that are not
    dual-label runs or name another arm code) and the segment's ledger
    rows and counts."""
    seg = next((s for s in layout.segments if s.segment == segment), None)
    if seg is None:
        raise GuidedError(f"segment {segment} is not in the campaign")
    confirmations = confirmations or {}
    posthoc = posthoc or {}
    mine, ignored = [], []
    codes = set()
    for record in records:
        ev = record.metadata.get("eval")
        parsed = parse_note(ev.get("eval_note") if isinstance(ev, dict) else None)
        if parsed is None or parsed[0] != layout.campaign_id or parsed[1] != segment:
            continue
        if ev.get("label_mode") != "dual":
            ignored.append({"key": record.key, "reason": "not a dual-label run"})
            continue
        codes.add(parsed[2])
        mine.append(record)
    if len(codes) > 1:
        raise GuidedError(
            f"segment {segment} has rollouts of several arm codes: {sorted(map(str, codes))}"
        )
    facts: dict = {}
    first_seen: dict = {}
    for record in mine:
        fact = legacy_fact(
            record,
            card=confirmations.get(record.key),
            posthoc=posthoc.get(record.key),
        )
        facts.setdefault(fact.run_id, []).append(fact)
        first_seen[fact.run_id] = min(
            first_seen.get(fact.run_id, "~"), fact.started_at or "~"
        )
    for found in facts.values():
        found.sort(key=lambda f: (f.number, f.episode_id))
    runs = sorted(facts, key=lambda r: (first_seen[r], r))
    bound = L.CampaignLayout(
        layout.campaign_id,
        (L.SegmentPlan(seg.segment, seg.arm, seg.round, seg.cards, tuple(runs)),),
        layout_source=layout.layout_source,
    )
    led = L.derive(bound, facts)
    pending = [
        {
            "key": r.episode_id,
            "run_id": r.run_id,
            "number": r.number,
            "candidate_card": r.candidate_card,
        }
        for r in led.rows
        if r.status == "valid" and r.card is None
    ]
    return {
        "segment": segment,
        "runs": runs,
        "facts": facts,
        "pending": pending,
        "ignored": ignored,
        "rows": led.rows,
        "counts": L.counts(led, bound),
    }


def bind_runs(layout: L.CampaignLayout, collected: list) -> L.CampaignLayout:
    """The layout with each segment's run ids taken from ``collect_segment``
    results (segments not collected keep theirs)."""
    runs = {c["segment"]: tuple(c["runs"]) for c in collected}
    return L.CampaignLayout(
        layout.campaign_id,
        tuple(
            L.SegmentPlan(
                s.segment, s.arm, s.round, s.cards, runs.get(s.segment, s.run_ids)
            )
            for s in layout.segments
        ),
        layout_source=layout.layout_source,
    )


def segment_agreement(facts) -> dict:
    """The automatic label against the operator's, over all valid episodes
    and apart by how each ended: ``budget`` (the full step budget: the only
    one that carries over to unattended runs) and ``operator_stop`` (the
    operator's key ended it early)."""
    by: dict = {"all": [], "budget": [], "operator_stop": [], "unknown": []}
    for fact in facts:
        if fact.status != "valid":
            continue
        op = fact.labels.get("operator_label")
        verdict = fact.labels.get("autonomous_verdict")
        pair = (
            op if op in L.DECIDED else None,
            None if verdict in (None, "none") else verdict,
        )
        by["all"].append(pair)
        by[fact.ended_by if fact.ended_by in by else "unknown"].append(pair)
    out = {k: an.agreement(v) for k, v in by.items() if k == "all" or v}
    out["carries_over"] = "budget"
    return out


# ------------------------------------------------------------- confirmations


class CardConfirmations:
    """A person's card confirmations, one append-only JSON line each
    (``levi.aeri.campaign_card.v1``); the last line for an episode wins
    and earlier ones stay on file. ``by`` is an opaque principal id."""

    def __init__(self, path):
        self.path = Path(path)

    def lines(self) -> list:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        out = []
        # What follows the last newline is a write cut short: no line.
        for number, raw in enumerate(data.split(b"\n")[:-1]):
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            if not isinstance(value, dict) or value.get("schema") != CARD_SCHEMA:
                raise GuidedError(
                    f"{self.path.name} line {number + 1} is not a card confirmation"
                )
            out.append(value)
        return out

    def latest(self) -> dict:
        out = {}
        for line in self.lines():
            if line.get("card") is None:
                out.pop(line["episode_key"], None)
            else:
                out[line["episode_key"]] = line["card"]
        return out

    def add(self, episode_key: str, card: str | None, *, by: str) -> dict:
        """Confirm ``card`` for an episode (``None`` withdraws a
        confirmation)."""
        if not isinstance(episode_key, str) or not episode_key or "\n" in episode_key:
            raise GuidedError("episode key")
        if card is not None:
            L._slug(card, "card")
        if not isinstance(by, str) or not metrics.PRINCIPAL.fullmatch(by):
            raise GuidedError("by is an opaque principal id (no names, no addresses)")
        record = {
            "schema": CARD_SCHEMA,
            "episode_key": episode_key,
            "card": card,
            "by": by,
            "at_wall_ns": time.time_ns(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / f".{self.path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._cut_torn()
            line = (json.dumps(record, sort_keys=True) + "\n").encode()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def _cut_torn(self) -> None:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return
        if data and not data.endswith(b"\n"):
            with self.path.open("r+b") as handle:
                handle.truncate(data.rfind(b"\n") + 1)
                handle.flush()
                os.fsync(handle.fileno())
