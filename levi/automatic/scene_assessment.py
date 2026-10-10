"""The Initial State Contract (pipeline §6.1) and the scene arbitration
(T-C-08): may the next forward episode start without a reset?

A scene assessment (``levi.aeri.scene.v1``, already parsed and fenced by
the orchestrator) is only a provider's claim. ``arbitrate`` reads it against
the task's Initial State Contract and keeps one rule: **only ``ready`` with
enough evidence skips a reset**. Everything else (``reset_required``,
``unknown``, ``unavailable``, a ``ready`` that left out a required predicate
or brought too little visible evidence, an assessment of another contract)
never skips it; what happens then is the reset strategy's choice
(``reset_manager.py``).

**The contract file is a draft (HA-23, pending the user's confirmation).**
Its format is the smallest one that carries pipeline §6.1::

    initial_state:
      id: stack-plates-initial
      version: "1"
      status: draft            # draft until the user confirms (HA-23)
      robot:
        home_pose: fr3_safe_home
        gripper: open
      predicates:
        required: [object_at_source, gripper_open]
        optional: []
      observations:
        preferred: [side, wrist]
        require_visible_evidence: true
        min_evidence_refs: 1

The predicate names must be the ones the scene provider reports (its spec
is ``(id, version)`` with exactly these names). ``robot`` and
``observations.preferred`` are carried for the provider and the report; the
arbitration reads ``predicates`` and the evidence rule.

The reader takes a strict YAML subset (no new dependency): block mappings
indented by spaces, ``- item`` and ``[a, b]`` lists of scalars, quoted or
plain scalars, ``true``/``false``/``null``, numbers and ``#`` comments. Tabs,
anchors, aliases, tags, block scalars, flow mappings, several documents and
duplicate keys are refused. A document that starts with ``{`` is read as
JSON (duplicate keys refused).
"""

import json
import math
import re
from dataclasses import dataclass, field

# Pending the user's confirmation of the file format (HA-23).
FORMAT_STATUS = "draft (HA-23: pending the user's confirmation)"
CONTRACT_STATUSES = ("draft", "confirmed")
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_DEPTH = 8
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# Evidence a person could look at: a frame or a clip.
VISIBLE = ("frame", "clip")


class ContractError(ValueError):
    """A contract or job file that cannot be read; ``where`` names the key."""

    def __init__(self, where: str, detail: str):
        super().__init__(f"{where}: {detail}" if where else detail)
        self.where = where
        self.detail = detail


# --- the strict YAML subset -------------------------------------------------------------


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ContractError(str(key), "duplicate key")
        out[key] = value
    return out


def _strip_comment(line: str) -> str:
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    return line.rstrip()


_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _scalar(text: str, where: str):
    text = text.strip()
    if not text:
        return None
    if text[0] in "&*!|>{" or text.startswith(("---", "...")):
        raise ContractError(where, f"unsupported YAML ({text[:20]!r})")
    if text[0] in "'\"":
        if len(text) < 2 or text[-1] != text[0]:
            raise ContractError(where, "unterminated quoted string")
        body = text[1:-1]
        if text[0] == '"':
            try:
                return json.loads(text)
            except ValueError:
                raise ContractError(where, "bad double-quoted string") from None
        return body.replace("''", "'")
    if text[0] == "[":
        if text[-1] != "]":
            raise ContractError(where, "unterminated list")
        inner = text[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "{" in inner:
            raise ContractError(where, "nested flow collections are not supported")
        return [_scalar(part, where) for part in _split_flow(inner, where)]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        value = float(text)
        if not math.isfinite(value):
            raise ContractError(where, "not a finite number")
        return value
    if ": " in text or text.endswith(":"):
        raise ContractError(where, f"unexpected mapping in a value ({text[:20]!r})")
    return text


def _split_flow(inner: str, where: str) -> list:
    parts, quote, current = [], None, []
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == ",":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote:
        raise ContractError(where, "unterminated quoted string")
    parts.append("".join(current))
    if any(not p.strip() for p in parts):
        raise ContractError(where, "empty list item")
    return parts


def parse_document(text: str) -> dict:
    """A mapping from the strict YAML subset (or JSON); ``ContractError``
    for anything else."""
    if not isinstance(text, str):
        raise ContractError("", "the document is not text")
    if len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise ContractError("", f"larger than {MAX_DOCUMENT_BYTES} bytes")
    if text.lstrip().startswith("{"):
        try:
            value = json.loads(
                text,
                object_pairs_hook=_no_duplicates,
                parse_constant=lambda c: (_ for _ in ()).throw(
                    ContractError("", f"{c} is not allowed")
                ),
            )
        except ContractError:
            raise
        except ValueError as exc:
            raise ContractError("", f"not JSON: {exc}") from None
        if not isinstance(value, dict):
            raise ContractError("", "the document is not a mapping")
        return value
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ContractError(f"line {number}", "tabs are not allowed for indenting")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if line.strip() in ("---", "..."):
            raise ContractError(f"line {number}", "one document only")
        lines.append((number, len(line) - len(line.lstrip(" ")), line.strip()))
    if not lines:
        raise ContractError("", "empty document")
    value, used = _block(lines, 0, lines[0][1], 0)
    if used != len(lines):
        number = lines[used][0]
        raise ContractError(f"line {number}", "bad indentation")
    if not isinstance(value, dict):
        raise ContractError("", "the document is not a mapping")
    return value


def _block(lines, start, indent, depth):
    if depth > MAX_DEPTH:
        raise ContractError(f"line {lines[start][0]}", "nested too deeply")
    if lines[start][2].startswith("- ") or lines[start][2] == "-":
        return _list(lines, start, indent)
    out: dict = {}
    index = start
    while index < len(lines):
        number, level, text = lines[index]
        if level < indent:
            break
        if level > indent:
            raise ContractError(f"line {number}", "bad indentation")
        if text.startswith("-"):
            raise ContractError(f"line {number}", "a list item inside a mapping")
        key, sep, rest = text.partition(":")
        key = key.strip()
        if not sep or not key or (rest and not rest.startswith(" ")):
            raise ContractError(
                f"line {number}", f"expected `key: value` ({text[:30]!r})"
            )
        if key[0] in "'\"&*!?{[" or key in out:
            raise ContractError(
                f"line {number}",
                "duplicate key" if key in out else f"unsupported key {key!r}",
            )
        rest = rest.strip()
        index += 1
        if rest:
            out[key] = _scalar(rest, f"line {number} ({key})")
        elif index < len(lines) and lines[index][1] > indent:
            out[key], index = _block(lines, index, lines[index][1], depth + 1)
        elif (
            index < len(lines)
            and lines[index][1] == indent
            and lines[index][2].startswith("-")
        ):
            out[key], index = _list(lines, index, indent)
        else:
            out[key] = None
    return out, index


def _list(lines, start, indent):
    out = []
    index = start
    while index < len(lines):
        number, level, text = lines[index]
        if level != indent or not (text == "-" or text.startswith("- ")):
            break
        item = text[1:].strip()
        if not item:
            raise ContractError(f"line {number}", "list items are scalars here")
        if ": " in item or item.endswith(":"):
            raise ContractError(f"line {number}", "lists of mappings are not supported")
        out.append(_scalar(item, f"line {number}"))
        index += 1
    return out, index


# --- the contract ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitialStateContract:
    """What "the scene may start the next forward episode" means for a task.

    ``required`` predicates must all be read true in a ``ready``
    assessment; ``optional`` ones are reported but never decide. With
    ``require_visible_evidence`` a ``ready`` needs at least
    ``min_evidence_refs`` frame or clip references (on the assessment or its
    predicates)."""

    contract_id: str
    contract_version: str
    required: tuple
    optional: tuple = ()
    preferred_views: tuple = ()
    require_visible_evidence: bool = True
    min_evidence_refs: int = 1
    home_pose: str | None = None
    gripper: str | None = None
    status: str = "draft"
    source: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self):
        if not LABEL.match(str(self.contract_id)):
            raise ContractError("id", "not an identifier")
        if not LABEL.match(str(self.contract_version)):
            raise ContractError("version", "not an identifier")
        if not self.required:
            raise ContractError("predicates.required", "at least one predicate")
        names = (*self.required, *self.optional)
        for name in names:
            if not isinstance(name, str) or not NAME.match(name):
                raise ContractError("predicates", f"bad predicate name {name!r}")
        if len(set(names)) != len(names):
            raise ContractError("predicates", "a predicate is listed twice")
        if type(self.require_visible_evidence) is not bool:
            raise ContractError(
                "observations.require_visible_evidence", "true or false"
            )
        if (
            type(self.min_evidence_refs) is not int
            or not 0 <= self.min_evidence_refs <= 32
        ):
            raise ContractError(
                "observations.min_evidence_refs", "a whole number 0..32"
            )
        if self.require_visible_evidence and self.min_evidence_refs < 1:
            raise ContractError(
                "observations.min_evidence_refs",
                "at least 1 when visible evidence is required",
            )
        if self.status not in CONTRACT_STATUSES:
            raise ContractError("status", f"one of {', '.join(CONTRACT_STATUSES)}")

    @property
    def key(self) -> tuple:
        return (self.contract_id, self.contract_version)

    def spec(self) -> dict:
        """``{(id, version): predicate names}`` for ``aeri.parse(specs=...)``."""
        return {self.key: frozenset((*self.required, *self.optional))}

    def describe(self) -> dict:
        return {
            "id": self.contract_id,
            "version": self.contract_version,
            "status": self.status,
            "format": FORMAT_STATUS,
            "required": list(self.required),
            "optional": list(self.optional),
            "preferred_views": list(self.preferred_views),
            "require_visible_evidence": self.require_visible_evidence,
            "min_evidence_refs": self.min_evidence_refs,
            "robot": {"home_pose": self.home_pose, "gripper": self.gripper},
        }


_TOP = {"id", "version", "status", "robot", "predicates", "observations"}
_ROBOT = {"home_pose", "gripper"}
_PREDICATES = {"required", "optional"}
_OBSERVATIONS = {"preferred", "require_visible_evidence", "min_evidence_refs"}


def _mapping(value, where, allowed):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ContractError(where, "a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(where, f"unknown key(s) {', '.join(unknown)}")
    return value


def _names(value, where) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContractError(where, "a list of names")
    return tuple(value)


def contract_from(value: dict) -> InitialStateContract:
    """The contract under ``initial_state`` of a parsed document (or the
    mapping itself when it has no such key)."""
    if not isinstance(value, dict):
        raise ContractError("", "a mapping")
    if "initial_state" in value:
        extra = sorted(set(value) - {"initial_state"})
        if extra:
            raise ContractError("", f"unknown key(s) {', '.join(extra)}")
        value = value["initial_state"]
    body = _mapping(value, "initial_state", _TOP)
    for key in ("id", "version"):
        if body.get(key) is None:
            raise ContractError(key, "required")
    robot = _mapping(body.get("robot"), "robot", _ROBOT)
    predicates = _mapping(body.get("predicates"), "predicates", _PREDICATES)
    observations = _mapping(body.get("observations"), "observations", _OBSERVATIONS)
    version = body["version"]
    if isinstance(version, bool) or not isinstance(version, str | int):
        raise ContractError("version", "text or a whole number")
    require = observations.get("require_visible_evidence", True)
    minimum = observations.get("min_evidence_refs", 1 if require else 0)
    status = body.get("status", "draft")
    for key, item in (
        ("robot.home_pose", robot.get("home_pose")),
        ("robot.gripper", robot.get("gripper")),
    ):
        if item is not None and not isinstance(item, str):
            raise ContractError(key, "text")
    return InitialStateContract(
        contract_id=str(body["id"]),
        contract_version=str(version),
        required=_names(predicates.get("required"), "predicates.required"),
        optional=_names(predicates.get("optional"), "predicates.optional"),
        preferred_views=_names(observations.get("preferred"), "observations.preferred"),
        require_visible_evidence=require,
        min_evidence_refs=minimum,
        home_pose=robot.get("home_pose"),
        gripper=robot.get("gripper"),
        status=status,
        source=dict(body),
    )


def load_contract(text: str) -> InitialStateContract:
    return contract_from(parse_document(text))


# --- the arbitration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SceneVerdict:
    """What C makes of one assessment. ``decision`` is ``ready`` only when a
    reset may be skipped; ``reason`` says why another decision was taken
    (``provider`` when the provider's own decision stands)."""

    decision: str  # ready | reset_required | unknown | unavailable
    reason: str
    assessment_id: str | None = None
    failed: tuple = ()
    unknown: tuple = ()

    @property
    def may_skip_reset(self) -> bool:
        return self.decision == "ready"


UNAVAILABLE = SceneVerdict("unavailable", "no_assessment")


def visible_evidence(message) -> int:
    """Frame and clip references on the assessment and its predicates."""
    refs = list(message.evidence_refs)
    for predicate in message.predicate_results:
        refs.extend(predicate.evidence_refs)
    return sum(1 for ref in refs if ref.kind in VISIBLE)


def arbitrate(message, contract: InitialStateContract | None) -> SceneVerdict:
    """The verdict on a parsed, fenced ``SceneAssessment`` (or an
    unavailable message). Without a contract the provider's decision stands
    (the behaviour before T-C-08); with one, a ``ready`` must read every
    required predicate true and bring the visible evidence it asks for."""
    if message is None or getattr(message, "kind", None) != "scene":
        return UNAVAILABLE
    failed = tuple(message.failed_predicates)
    unknown = tuple(message.unknown_predicates)
    base = {
        "assessment_id": message.assessment_id,
        "failed": failed,
        "unknown": unknown,
    }
    if contract is None:
        return SceneVerdict(message.decision, "provider", **base)
    if (message.contract_id, message.contract_version) != contract.key:
        return SceneVerdict("unavailable", "contract_mismatch", **base)
    if message.decision != "ready":
        return SceneVerdict(message.decision, "provider", **base)
    read = {p.name: p for p in message.predicate_results}
    missing = tuple(
        name
        for name in contract.required
        if name not in read or not read[name].required or read[name].value is not True
    )
    if missing:
        # The provider called it ready without reading what the contract
        # requires: never a skip.
        return SceneVerdict(
            "unknown", "missing_predicate", message.assessment_id, failed, missing
        )
    if (
        contract.require_visible_evidence
        and visible_evidence(message) < contract.min_evidence_refs
    ):
        return SceneVerdict("unknown", "insufficient_evidence", **base)
    return SceneVerdict("ready", "provider", **base)
