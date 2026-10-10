"""The ``campaign`` block of a job file, its layout cards, and its expansion
into one ordinary AERI job file per segment (design X3 §1.1, §1.3).

A campaign job file is a ``levi.aeri.job.v1`` file (the shared settings:
task, termination, reset, recording...) plus a ``campaign`` block. It uses
the same strict YAML subset as the job file
(``scene_assessment.parse_document``: block mappings, scalar lists, no flow
mappings, no lists of mappings), so the arms are a mapping keyed by arm id
(``A`` to ``H``) and the cards a mapping keyed by card id::

    campaign:
      id: c20261010-eggplant
      robot: fr3
      trials_per_arm: 30
      schedule:
        kind: counterbalanced_segments
        segment_trials: 5
        seed: 7
      layouts:
        source: card_set          # or none (reset policy mode)
        file: layouts.yaml
      pairing:                    # checkpoint folder name pattern -> config
        recap_cfg_*: pi05_fr3_all_state_cfg
        pi05_fr3_all_step*: pi05_fr3_all_state
      arms:
        A:
          role: reference
          policy_forward:
            checkpoint_dir: /abs/checkpoints/pi05_fr3_all_step49999
            config: pi05_fr3_all_state
            port: 8000
          checkpoint:
            sha256_status: recorded
            manifest_sha256: <64 hex>
        B:
          policy_forward:
            checkpoint_dir: /abs/checkpoints/recap_cfg_r2_best_step14300_jax
            config: pi05_fr3_all_state_cfg
            cfg_scale: 1.0
            port: 8000

``plan_campaign`` writes ``<job_root>/campaigns/<id>/<id>__<arm>__s<NN>.yaml``
(new files only: an existing file must hold the same bytes) and
``campaign.plan.json``, asks the child planner for each child's
``plan_sha256``, checks that every child has the same settings
(``settings_sha256``) and computes ``campaign_sha256``.
"""

import contextlib
import copy
import fnmatch
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from levi.domain import aeri

from .. import scene_assessment as sa
from ..journal import fsync_dir
from . import schedule as sched

PLAN_SCHEMA = "levi.aeri.campaign_plan.v1"
LAYOUTS_SCHEMA = "levi.aeri.layouts.v1"
PLAN_FILE = "campaign.plan.json"
MAX_ARMS = 8
LABEL_BASES = (
    "autonomous_verdict",
    "posthoc_verdict",
    "operator_label",
    "adjudicated_ground_truth",
    "adjudicated_then_operator",
)
# Keys of a child job document that may differ between arms and segments:
# the run id, the trial count (derived from the schedule), the policies and
# the recording group (design X3 §0-2). Everything else is a shared setting.
MASKED_KEYS = (
    ("experiment", "name"),
    ("experiment", "episodes"),
    ("policies",),
    ("recording", "group"),
)
ERROR_CODES = (
    "E_CAMPAIGN_SCHEMA",
    "E_CAMPAIGN_JOB",
    "E_CAMPAIGN_ARMS",
    "E_CAMPAIGN_PAIRING",
    "E_CAMPAIGN_CHECKPOINT",
    "E_CAMPAIGN_SCHEDULE",
    "E_CAMPAIGN_LAYOUTS",
    "E_CAMPAIGN_SETTINGS_DIFFER",
    "E_CAMPAIGN_EXISTS",
    "E_CAMPAIGN_PLAN",
)


class CampaignError(ValueError):
    """A campaign that cannot be planned; ``code`` is one of ``ERROR_CODES``."""

    def __init__(self, code: str, detail: str):
        assert code in ERROR_CODES, code
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- the campaign block -----------------------------------------------------------------

CampaignId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
]
ArmId = Annotated[str, StringConstraints(pattern=r"^[A-H]$")]
CardId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
RobotId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
ConfigName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
]
Pattern = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.*?\[\]!-]{1,128}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Path_ = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


def _sections(value, names):
    """``key:`` with nothing under it is an empty section (YAML null)."""
    if isinstance(value, dict):
        value = dict(value)
        for name in names:
            if name in value and value[name] is None:
                value[name] = {}
    return value


class PolicyForward(aeri.Part):
    checkpoint_dir: Path_
    config: ConfigName
    port: Annotated[int, Field(ge=1, le=65535)]
    cfg_scale: Annotated[float, Field(ge=0.0, le=100.0)] | None = None

    @property
    def checkpoint_name(self) -> str:
        return Path(self.checkpoint_dir.rstrip("/")).name


class CheckpointHash(aeri.Part):
    # How far the checkpoint's bytes were checked (design X2 §9.1): never
    # rehashed by default (3.35 B parameters).
    sha256_status: Literal["verified", "recorded", "none"] = "none"
    manifest_sha256: aeri.Sha256 | None = None


class Arm(aeri.Part):
    role: Literal["candidate", "reference"] = "candidate"
    policy_forward: PolicyForward
    # None: the shared reset block (needs treatment_includes_reset otherwise).
    policy_reset: PolicyForward | None = None
    checkpoint: CheckpointHash = Field(default_factory=CheckpointHash)
    versions: Text | None = None
    # The rollout group; default: the checkpoint's folder name.
    group: aeri.JobFolder | None = None


class Primary(aeri.Part):
    metric: aeri.Name = "success"
    comparison: Annotated[list[ArmId], Field(min_length=2, max_length=2)]
    alpha: Annotated[float, Field(gt=0.0, le=0.5)] = 0.05
    label_basis: Literal[LABEL_BASES] = "operator_label"
    preregistered: bool = False
    # Reserved: STEP sequential testing is not implemented (design X3 §1.4).
    sequential: Literal["none", "step"] = "none"


class ScheduleBlock(aeri.Part):
    kind: Literal[sched.KINDS] = "counterbalanced_segments"
    segment_trials: Annotated[int, Field(ge=1, le=1000)] | None = None
    seed: Annotated[int, Field(ge=-aeri.INT64_MAX, le=aeri.INT64_MAX)] = 0


class LayoutsBlock(aeri.Part):
    source: Literal["card_set", "none"]
    file: Path_ | None = None
    per_round: Annotated[int, Field(ge=1, le=1000)] | None = None


class Control(aeri.Part):
    fraction: aeri.Unit = 0.2
    key: Literal["layout_slot"] = "layout_slot"


class Blinding(aeri.Part):
    operator: Literal["arm_codes", "none"] = "arm_codes"


class StopRules(aeri.Part):
    consecutive_faults: Annotated[int, Field(ge=1, le=100)] = 2
    unplanned_interventions_per_arm: Annotated[int, Field(ge=0, le=100_000)] = 5


class CampaignBlock(aeri.Part):
    id: CampaignId
    robot: RobotId = "main"
    primary: Primary | None = None
    trials_per_arm: Annotated[int, Field(ge=1, le=100_000)]
    schedule: ScheduleBlock = Field(default_factory=ScheduleBlock)
    layouts: LayoutsBlock
    control: Control = Field(default_factory=Control)
    blinding: Blinding = Field(default_factory=Blinding)
    stop_rules: StopRules = Field(default_factory=StopRules)
    treatment_includes_reset: bool = False
    # Checkpoint folder name pattern (fnmatch) -> the config it must be
    # served with (setup.md §5.1: a CFG checkpoint with a plain config
    # silently samples without CFG). Required: no table is built in.
    pairing: Annotated[dict[Pattern, ConfigName], Field(max_length=64)] | None = None
    arms: Annotated[dict[ArmId, Arm], Field(min_length=2, max_length=MAX_ARMS)]

    @classmethod
    def model_validate_block(cls, value):
        value = _sections(
            value, ("schedule", "control", "blinding", "stop_rules", "pairing")
        )
        if isinstance(value, dict) and isinstance(value.get("arms"), dict):
            value["arms"] = {
                key: _sections(arm, ("checkpoint",))
                for key, arm in value["arms"].items()
            }
        return BLOCK_ADAPTER.validate_python(value, strict=True)


BLOCK_ADAPTER = TypeAdapter(CampaignBlock)


class LayoutCard(aeri.Part):
    description: Text | None = None
    # Relative to the layouts file; never opened by the backend.
    reference_image: Path_ | None = None
    predicates: Annotated[dict[aeri.Name, bool], Field(max_length=64)] = Field(
        default_factory=dict
    )
    params: Annotated[dict[aeri.Name, int | float | Text], Field(max_length=64)] = (
        Field(default_factory=dict)
    )


class LayoutSet(aeri.Part):
    schema_version: Literal["levi.aeri.layouts.v1"]
    cards: Annotated[dict[CardId, LayoutCard], Field(min_length=1, max_length=1000)]


LAYOUTS_ADAPTER = TypeAdapter(LayoutSet)


def _where(exc: ValidationError) -> str:
    first = exc.errors(include_url=False, include_input=False)[0]
    where = ".".join(str(part) for part in first["loc"]) or "$"
    more = len(exc.errors()) - 1
    return f"{where}: {first['msg']}" + (f" (+{more} more)" if more else "")


# --- canonical text and digests ---------------------------------------------------------


def canonical(value) -> bytes:
    """Sorted keys, no spaces, UTF-8; refuses what JSON cannot hold."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- the YAML subset, written ----------------------------------------------------------


def _scalar_text(value, where: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if text in ("inf", "-inf", "nan"):
            raise CampaignError("E_CAMPAIGN_SCHEMA", f"{where}: not a finite number")
        return text
    if isinstance(value, str):
        if any(ord(c) < 0x20 or c == "\x7f" for c in value):
            raise CampaignError(
                "E_CAMPAIGN_SCHEMA", f"{where}: control characters cannot be written"
            )
        # Single quotes: the reader's comment stripper tracks them (a
        # doubled quote toggles twice), and nothing else is an escape.
        return "'" + value.replace("'", "''") + "'"
    raise CampaignError("E_CAMPAIGN_SCHEMA", f"{where}: cannot write {type(value)}")


def _emit(value: dict, indent: int, where: str, out: list) -> None:
    pad = " " * indent
    for key, item in value.items():
        key = str(key)
        if not key or key[0] in "'\"&*!?{[-#" or ":" in key or " " in key:
            raise CampaignError("E_CAMPAIGN_SCHEMA", f"{where}.{key}: unsupported key")
        here = f"{where}.{key}"
        if isinstance(item, dict):
            if item:
                out.append(f"{pad}{key}:")
                _emit(item, indent + 2, here, out)
            else:
                out.append(f"{pad}{key}:")
        elif isinstance(item, list):
            if any(isinstance(x, (dict, list)) for x in item):
                raise CampaignError(
                    "E_CAMPAIGN_SCHEMA", f"{here}: lists hold scalars only"
                )
            inner = ", ".join(_scalar_text(x, here) for x in item)
            out.append(f"{pad}{key}: [{inner}]")
        else:
            out.append(f"{pad}{key}: {_scalar_text(item, here)}")


def to_yaml(document: dict) -> str:
    """``document`` in the strict YAML subset; it must read back as itself
    (``scene_assessment.parse_document``), else ``E_CAMPAIGN_SCHEMA``."""
    lines: list = []
    _emit(document, 0, "$", lines)
    text = "\n".join(lines) + "\n"
    try:
        back = sa.parse_document(text)
    except sa.ContractError as exc:
        raise CampaignError(
            "E_CAMPAIGN_SCHEMA", f"cannot write the job: {exc}"
        ) from None
    if canonical(back) != canonical(_empty_as_none(document)):
        raise CampaignError("E_CAMPAIGN_SCHEMA", "the job does not read back as itself")
    return text


def _empty_as_none(value):
    """What the reader gives back for an empty section (``key:`` = null)."""
    if isinstance(value, dict):
        return {k: (None if v == {} else _empty_as_none(v)) for k, v in value.items()}
    return value


# --- loading ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignSpec:
    path: Path
    block: CampaignBlock
    shared: dict  # the job document without ``campaign``
    job: aeri.JobSpec  # the shared settings, as a job
    cards: dict | None  # card id -> normalised card; None without cards
    layouts_sha256: str | None
    ignored: tuple = ()  # (key, why)

    @property
    def campaign_id(self) -> str:
        return self.block.id

    @property
    def arm_ids(self) -> tuple:
        return tuple(sorted(self.block.arms))

    def group_of(self, arm: str) -> str:
        found = self.block.arms[arm]
        return found.group or found.policy_forward.checkpoint_name


def _relative(base: Path, value: str) -> str:
    found = Path(value)
    return str(found if found.is_absolute() else (base.parent / found))


def load_layouts(path: Path) -> tuple[dict, str]:
    """``({card id: card}, sha256 of the file)``; ``E_CAMPAIGN_LAYOUTS``."""
    try:
        data = Path(path).read_bytes()
        raw = sa.parse_document(data.decode("utf-8"))
    except OSError as exc:
        raise CampaignError(
            "E_CAMPAIGN_LAYOUTS", f"{path}: {exc.strerror or exc}"
        ) from None
    except UnicodeDecodeError:
        raise CampaignError("E_CAMPAIGN_LAYOUTS", f"{path}: not UTF-8") from None
    except sa.ContractError as exc:
        raise CampaignError("E_CAMPAIGN_LAYOUTS", f"{path}: {exc}") from None
    if isinstance(raw, dict) and isinstance(raw.get("cards"), dict):
        raw["cards"] = {
            key: _sections(card, ("predicates", "params"))
            for key, card in raw["cards"].items()
        }
    try:
        found = LAYOUTS_ADAPTER.validate_python(raw, strict=True)
    except ValidationError as exc:
        raise CampaignError("E_CAMPAIGN_LAYOUTS", f"{path}: {_where(exc)}") from None
    cards = {
        key: card.model_dump(mode="json") for key, card in sorted(found.cards.items())
    }
    return cards, hashlib.sha256(data).hexdigest()


def check_pairing(block: CampaignBlock) -> None:
    """Every arm's checkpoint folder name matches the pairing table and is
    served with the config the table names (``E_CAMPAIGN_PAIRING``)."""
    if not block.pairing:
        raise CampaignError(
            "E_CAMPAIGN_PAIRING",
            "campaign.pairing is required: map each checkpoint folder name "
            "pattern to the config it must be served with (setup.md §5.1)",
        )
    for arm_id, arm in sorted(block.arms.items()):
        for role, policy in (
            ("policy_forward", arm.policy_forward),
            ("policy_reset", arm.policy_reset),
        ):
            if policy is None:
                continue
            name = policy.checkpoint_name
            wanted = {
                config
                for pattern, config in block.pairing.items()
                if fnmatch.fnmatchcase(name, pattern)
            }
            where = f"arms.{arm_id}.{role}"
            if not wanted:
                raise CampaignError(
                    "E_CAMPAIGN_PAIRING",
                    f"{where}: checkpoint {name} matches no campaign.pairing "
                    "pattern; add the pattern and its config",
                )
            if len(wanted) > 1:
                raise CampaignError(
                    "E_CAMPAIGN_PAIRING",
                    f"{where}: checkpoint {name} matches patterns of different "
                    f"configs ({', '.join(sorted(wanted))})",
                )
            (config,) = wanted
            if policy.config != config:
                raise CampaignError(
                    "E_CAMPAIGN_PAIRING",
                    f"{where}: checkpoint {name} is served with config {config}, "
                    f"not {policy.config}",
                )


def check_block(block: CampaignBlock, job: aeri.JobSpec) -> None:
    """The cross-field rules of the campaign block (codes per rule)."""
    arms = block.arms
    references = [a for a, arm in arms.items() if arm.role == "reference"]
    if len(references) > 1:
        raise CampaignError(
            "E_CAMPAIGN_ARMS", f"at most one reference arm ({', '.join(references)})"
        )
    if block.primary is not None:
        compared = block.primary.comparison
        unknown = [a for a in compared if a not in arms]
        if unknown or len(set(compared)) != 2:
            raise CampaignError(
                "E_CAMPAIGN_ARMS",
                "primary.comparison names two different arms of the campaign",
            )
        if block.primary.sequential == "step":
            raise CampaignError(
                "E_CAMPAIGN_SCHEDULE",
                "primary.sequential step is reserved and not implemented",
            )
    groups: dict = {}
    for arm_id, arm in sorted(arms.items()):
        group = arm.group or arm.policy_forward.checkpoint_name
        try:
            TypeAdapter(aeri.JobFolder).validate_python(group, strict=True)
        except ValidationError:
            raise CampaignError(
                "E_CAMPAIGN_ARMS",
                f"arms.{arm_id}: the checkpoint folder name {group!r} is not a "
                "recording group name; set arms.{arm_id}.group",
            ) from None
        if group in groups:
            raise CampaignError(
                "E_CAMPAIGN_ARMS",
                f"arms {groups[group]} and {arm_id} record into the same group "
                f"{group}; set a group per arm",
            )
        groups[group] = arm_id
        if arm.policy_reset is not None:
            if job.human_assisted:
                raise CampaignError(
                    "E_CAMPAIGN_ARMS",
                    f"arms.{arm_id}.policy_reset: human_assisted runs no reset policy",
                )
            if not block.treatment_includes_reset:
                raise CampaignError(
                    "E_CAMPAIGN_ARMS",
                    f"arms.{arm_id}.policy_reset differs from the shared reset: "
                    "set treatment_includes_reset: true (the report then says the "
                    "treatment includes the reset policy)",
                )
        status = arm.checkpoint.sha256_status
        manifest = arm.checkpoint.manifest_sha256
        if status == "none" and manifest is not None:
            raise CampaignError(
                "E_CAMPAIGN_CHECKPOINT",
                f"arms.{arm_id}.checkpoint: sha256_status none carries no "
                "manifest_sha256",
            )
        if status != "none" and manifest is None:
            raise CampaignError(
                "E_CAMPAIGN_CHECKPOINT",
                f"arms.{arm_id}.checkpoint: sha256_status {status} needs "
                "manifest_sha256",
            )
    check_pairing(block)
    kind = block.schedule.kind
    segment = block.schedule.segment_trials
    if kind in sched.SINGLE_TRIAL_KINDS and segment not in (None, 1):
        raise CampaignError(
            "E_CAMPAIGN_SCHEDULE", f"{kind}: a segment is one trial (segment_trials: 1)"
        )
    layouts = block.layouts
    reset_policy_mode = not job.human_assisted
    if reset_policy_mode and layouts.source != "none":
        raise CampaignError(
            "E_CAMPAIGN_LAYOUTS",
            "a reset policy sets the initial scene, cards cannot: with "
            f"reset.strategy {job.reset_strategy} write layouts.source: none",
        )
    if not reset_policy_mode and layouts.source != "card_set":
        raise CampaignError(
            "E_CAMPAIGN_LAYOUTS",
            "human_assisted campaigns lay out cards: write layouts.source: card_set "
            "and layouts.file (source none belongs to the reset policy mode)",
        )
    if layouts.source == "none" and (
        layouts.file is not None or layouts.per_round is not None
    ):
        raise CampaignError(
            "E_CAMPAIGN_LAYOUTS", "layouts.source none takes no file and no per_round"
        )
    if layouts.source == "card_set" and layouts.file is None:
        raise CampaignError("E_CAMPAIGN_LAYOUTS", "layouts.source card_set needs file")
    size = segment or sched.DEFAULT_SEGMENT_TRIALS[kind]
    if layouts.per_round is not None and layouts.per_round != size:
        raise CampaignError(
            "E_CAMPAIGN_LAYOUTS",
            f"layouts.per_round {layouts.per_round} must equal the segment size {size}",
        )


def load(path) -> CampaignSpec:
    """Read and check a campaign job file (``CampaignError`` otherwise)."""
    path = Path(path)
    try:
        raw = sa.parse_document(path.read_text())
    except OSError as exc:
        raise CampaignError(
            "E_CAMPAIGN_JOB", f"{path}: {exc.strerror or exc}"
        ) from None
    except sa.ContractError as exc:
        raise CampaignError("E_CAMPAIGN_JOB", f"{path}: {exc}") from None
    if "campaign" not in raw:
        raise CampaignError("E_CAMPAIGN_SCHEMA", "the job file has no campaign block")
    block_raw = raw.pop("campaign")
    if not isinstance(block_raw, dict):
        raise CampaignError("E_CAMPAIGN_SCHEMA", "campaign is a block mapping")
    try:
        block = CampaignBlock.model_validate_block(block_raw)
    except ValidationError as exc:
        raise CampaignError("E_CAMPAIGN_SCHEMA", f"campaign.{_where(exc)}") from None
    shared = copy.deepcopy(raw)
    try:
        job = aeri.parse_job(_with_paths(shared, path))
    except aeri.AeriError as exc:
        raise CampaignError("E_CAMPAIGN_JOB", exc.detail) from None
    check_block(block, job)
    ignored = []
    if (raw.get("experiment") or {}).get("episodes") is not None:
        ignored.append(("experiment.episodes", "derived from campaign.trials_per_arm"))
    cards, layouts_sha = None, None
    if block.layouts.source == "card_set":
        cards, layouts_sha = load_layouts(Path(_relative(path, block.layouts.file)))
        size = (
            block.schedule.segment_trials
            or sched.DEFAULT_SEGMENT_TRIALS[block.schedule.kind]
        )
        need = min(size, block.trials_per_arm)
        if len(cards) < need:
            raise CampaignError(
                "E_CAMPAIGN_LAYOUTS",
                f"a round lays out {need} distinct cards; the set has {len(cards)}",
            )
    return CampaignSpec(
        path=path,
        block=block,
        shared=shared,
        job=job,
        cards=cards,
        layouts_sha256=layouts_sha,
        ignored=tuple(ignored),
    )


def _with_paths(document: dict, path: Path) -> dict:
    """The job document with its relative paths made absolute (the child
    files live in another folder than the campaign job file)."""
    document = copy.deepcopy(document)
    for section, key in (
        ("recording", "rollout_root"),
        ("task", "initial_state_spec"),
    ):
        part = document.get(section)
        if isinstance(part, dict) and isinstance(part.get(key), str):
            part[key] = _relative(path, part[key])
    return document


# --- expansion --------------------------------------------------------------------------


def run_id(campaign_id: str, arm: str, segment: int, width: int = 2) -> str:
    return f"{campaign_id}__{arm}__s{segment:0{width}d}"


def child_document(spec: CampaignSpec, arm: str, segment) -> tuple[str, dict]:
    """``(run id, job document)`` of one segment: the shared settings with
    the run id, the trial count, the arm's recording group and the shared
    forward (and reset) folder."""
    width = max(2, len(str(segment.index)))
    name = run_id(spec.campaign_id, arm, segment.index, width)
    document = _with_paths(spec.shared, spec.path)
    experiment = dict(document.get("experiment") or {})
    experiment["name"] = name
    experiment["episodes"] = segment.trials
    document["experiment"] = experiment
    recording = dict(document.get("recording") or {})
    recording["group"] = spec.group_of(arm)
    # Every arm writes the same task folders (design X3 §1.1); the job's
    # own default would be one folder per run id.
    recording.setdefault("forward_folder", f"forward__{spec.campaign_id}")
    if not spec.job.human_assisted:
        recording.setdefault("reset_folder", f"reset__{spec.campaign_id}")
    document["recording"] = recording
    return name, document


def settings_view(document: dict) -> dict:
    """A child document without the keys that may differ between arms
    (``MASKED_KEYS``), with the Initial State Contract file named by the
    sha256 of its bytes (read now)."""
    view = copy.deepcopy(document)
    for path in MASKED_KEYS:
        part = view
        for key in path[:-1]:
            part = part.get(key) if isinstance(part, dict) else None
        if isinstance(part, dict):
            part.pop(path[-1], None)
    task = view.get("task")
    if isinstance(task, dict) and isinstance(task.get("initial_state_spec"), str):
        where = task["initial_state_spec"]
        try:
            found = file_sha256(Path(where))
        except OSError as exc:
            raise CampaignError(
                "E_CAMPAIGN_JOB", f"task.initial_state_spec: {exc.strerror or exc}"
            ) from None
        task["initial_state_spec"] = {"path": where, "sha256": found}
    return view


def _first_difference(a, b, where="$") -> str:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{where}.{key}"
            if canonical(a[key]) != canonical(b[key]):
                return _first_difference(a[key], b[key], f"{where}.{key}")
    return where


def settings_sha256(documents: Mapping[str, dict]) -> str:
    """The sha256 of the shared settings (``settings_view``) that every child
    must have; ``E_CAMPAIGN_SETTINGS_DIFFER`` names the first key that
    differs between two children."""
    if not documents:
        raise CampaignError("E_CAMPAIGN_PLAN", "no child jobs")
    views = {name: settings_view(doc) for name, doc in documents.items()}
    names = sorted(views)
    first = views[names[0]]
    expected = digest(first)
    for name in names[1:]:
        if digest(views[name]) != expected:
            key = _first_difference(first, views[name])
            raise CampaignError(
                "E_CAMPAIGN_SETTINGS_DIFFER",
                f"{name} differs from {names[0]} at {key}: every arm runs the same "
                "task, environment and settings",
            )
    return expected


class ChildPlanner(Protocol):
    """Plans one child job file as the launch core does (``launch.plan``):
    returns ``(plan_sha256, plan)``. The default reads it with
    ``levi.automatic.cli.load_job`` until the launch core exists."""

    def plan(self, job_path: Path) -> tuple[str, dict]: ...


class LoadJobPlanner:
    def plan(self, job_path: Path) -> tuple[str, dict]:
        from levi.automatic.cli import JobError, load_job

        try:
            found = load_job(job_path)
        except JobError as exc:
            raise CampaignError("E_CAMPAIGN_JOB", f"{job_path.name}: {exc}") from None
        return found["plan_sha256"], found["plan"]


@dataclass(frozen=True)
class Child:
    segment: int
    arm: str
    run_id: str
    file: str  # the file name, in the campaign folder
    file_sha256: str
    plan_sha256: str
    trials: int


@dataclass
class CampaignPlan:
    spec: CampaignSpec
    schedule: sched.Schedule
    directory: Path
    children: list = field(default_factory=list)
    settings_sha256: str = ""
    campaign_sha256: str = ""

    def to_json(self) -> dict:
        return plan_json(
            self.spec, self.schedule, self.children, self.settings_sha256
        ) | {"campaign_sha256": self.campaign_sha256}


def _block_json(spec: CampaignSpec) -> dict:
    return spec.block.model_dump(mode="json", by_alias=True)


def plan_json(spec, schedule, children, settings) -> dict:
    """The plan without its own digest (what ``campaign_sha256`` covers)."""
    return {
        "schema": PLAN_SCHEMA,
        "campaign_id": spec.campaign_id,
        "robot": spec.block.robot,
        "campaign": _block_json(spec),
        "reset_mode": spec.job.reset_strategy,
        "cards": spec.cards,
        "layouts_sha256": spec.layouts_sha256,
        "schedule": schedule.to_json(),
        "children": [
            {
                "segment": c.segment,
                "arm": c.arm,
                "run_id": c.run_id,
                "file": c.file,
                "file_sha256": c.file_sha256,
                "plan_sha256": c.plan_sha256,
                "trials": c.trials,
            }
            for c in children
        ],
        "settings_sha256": settings,
        "ignored": [list(item) for item in spec.ignored],
    }


def campaign_sha256(value: dict) -> str:
    body = {k: v for k, v in value.items() if k != "campaign_sha256"}
    return digest(body)


def build_schedule(spec: CampaignSpec) -> sched.Schedule:
    block = spec.block
    try:
        return sched.build(
            spec.campaign_id,
            block.schedule.kind,
            spec.arm_ids,
            block.trials_per_arm,
            block.schedule.segment_trials,
            block.schedule.seed,
            None if spec.cards is None else sorted(spec.cards),
        )
    except sched.ScheduleError as exc:
        raise CampaignError("E_CAMPAIGN_SCHEDULE", str(exc)) from None


def draft(spec: CampaignSpec):
    """``(schedule, {run id: (segment, document)}, settings_sha256)``:
    everything ``plan_campaign`` writes, computed without writing."""
    schedule = build_schedule(spec)
    children = {}
    for segment in schedule.segments:
        name, document = child_document(spec, segment.arm, segment)
        children[name] = (segment, document)
    settings = settings_sha256({n: d for n, (_, d) in children.items()})
    return schedule, children, settings


def write_new(path: Path, data: bytes) -> bool:
    """Write ``path`` once (O_EXCL, fsync, directory fsync). An existing file
    must hold the same bytes (``E_CAMPAIGN_EXISTS`` otherwise; never
    overwritten). ``True`` when this call wrote it."""
    path = Path(path)
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current is not None:
        if current != data:
            raise CampaignError(
                "E_CAMPAIGN_EXISTS",
                f"{path} exists with other content: plan a campaign under a new id",
            )
        return False
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o644)
    try:
        # link() refuses an existing target: two planners racing on one id
        # cannot overwrite each other.
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise CampaignError(
                "E_CAMPAIGN_EXISTS", f"{path} was written by someone else"
            ) from None
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    fsync_dir(path.parent)
    return True


def campaign_folder(job_root, campaign_id: str) -> Path:
    return Path(job_root) / "campaigns" / campaign_id


def plan_campaign(job_path, *, job_root, planner: ChildPlanner | None = None):
    """Expand a campaign job file into its child job files and its plan
    (``campaign.plan.json``, written last). Every file is new or holds the
    same bytes: planning the same file twice is a no-op, a changed file
    under the same campaign id is refused."""
    spec = load(job_path)
    planner = planner or LoadJobPlanner()
    schedule, documents, _ = draft(spec)
    directory = campaign_folder(job_root, spec.campaign_id)
    directory.mkdir(parents=True, exist_ok=True)
    fsync_dir(directory.parent)
    children = []
    planned = {}
    for name, (segment, document) in documents.items():
        text = to_yaml(document).encode()
        path = directory / f"{name}.yaml"
        write_new(path, text)
        plan_sha, _ = planner.plan(path)
        if not isinstance(plan_sha, str) or len(plan_sha) != 64:
            raise CampaignError(
                "E_CAMPAIGN_PLAN", f"{name}: the planner gave no sha256"
            )
        # The settings are checked on what was written and read back.
        planned[name] = sa.parse_document(text.decode())
        children.append(
            Child(
                segment=segment.index,
                arm=segment.arm,
                run_id=name,
                file=path.name,
                file_sha256=hashlib.sha256(text).hexdigest(),
                plan_sha256=plan_sha,
                trials=segment.trials,
            )
        )
    settings = settings_sha256(planned)
    value = plan_json(spec, schedule, children, settings)
    value["campaign_sha256"] = campaign_sha256(value)
    write_new(
        directory / PLAN_FILE,
        (
            json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
        ).encode(),
    )
    return CampaignPlan(
        spec=spec,
        schedule=schedule,
        directory=directory,
        children=children,
        settings_sha256=settings,
        campaign_sha256=value["campaign_sha256"],
    )


def read_plan(path) -> dict:
    """A ``campaign.plan.json`` checked against its own ``campaign_sha256``
    (``E_CAMPAIGN_PLAN`` otherwise)."""
    path = Path(path)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CampaignError("E_CAMPAIGN_PLAN", f"{path}: {exc}") from None
    if (
        not isinstance(value, dict)
        or value.get("schema") != PLAN_SCHEMA
        or campaign_sha256(value) != value.get("campaign_sha256")
    ):
        raise CampaignError(
            "E_CAMPAIGN_PLAN", f"{path} is not a campaign plan matching its sha256"
        )
    return value


def verify_children(plan: dict, directory, planner: ChildPlanner | None = None):
    """Every child file still holds the bytes and the plan the campaign
    froze (``E_CAMPAIGN_PLAN`` names the first that does not)."""
    directory = Path(directory)
    for child in plan["children"]:
        path = directory / child["file"]
        try:
            found = file_sha256(path)
        except OSError as exc:
            raise CampaignError("E_CAMPAIGN_PLAN", f"{path}: {exc}") from None
        if found != child["file_sha256"]:
            raise CampaignError("E_CAMPAIGN_PLAN", f"{path} changed since planning")
        if planner is not None:
            plan_sha, _ = planner.plan(path)
            if plan_sha != child["plan_sha256"]:
                raise CampaignError(
                    "E_CAMPAIGN_PLAN",
                    f"{child['run_id']}: its plan changed since planning "
                    "(a file it reads changed)",
                )
