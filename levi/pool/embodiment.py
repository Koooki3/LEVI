"""Which robot, gripper, action mode and end-effector frame an episode has.

Four index columns, each ``unknown`` unless the episode's own metadata says:

- ``robot`` (``franka_fr3``, ``franka_panda``),
- ``gripper`` (``robotiq_2f85``, ``franka_hand``),
- ``action_mode`` (``ee_pose_abs_next``: the action is the next absolute
  end-effector pose; ``ee_pose_abs_current``: the action repeats the pose),
- ``ee_frame`` (the frame the pose is expressed in, as the policy server
  names it, e.g. ``franka_hand_tcp``).

Nothing here looks at a folder, source or task name, and nothing names a task
or a lab. What counts as evidence is a small table of rules (``rules.py``
``embodiment``; a workspace overrides it in ``pool/rules.json``), one entry
each::

    {"field": "gripper", "value": "robotiq_2f85",
     "in": "metadata", "key": "gripper_joint_names", "regex": "robotiq_85"}

``in`` is where to look: ``metadata`` (a raw capture's ``metadata.json``),
``info`` (a LeRobot ``meta/info.json``), ``conversion`` (``meta/levi_conversion.json``)
or ``format`` (the episode's format; no ``key``). ``key`` is a dotted path;
the value found there must be text, a number or a list of them (a list is
joined with spaces), and ``regex`` must match it (case-insensitive search).
A rule with ``copy`` instead of ``value`` takes the found text itself (cleaned
to ``[a-z0-9_.-]``, at most 40 characters). Every rule that matches counts;
when they agree the value is that one, when two rules give different values
for one field the evidence conflicts and the field is ``unknown``.
"""

import hashlib
import json
import re
from collections import Counter

from . import rules as rules_mod

UNKNOWN = "unknown"
FIELDS = ("robot", "gripper", "action_mode", "ee_frame")
SOURCES = ("metadata", "info", "conversion", "format")
_CLEAN = re.compile(r"[^a-z0-9_.-]+")
MAX_COPY = 40


def _table(rules: dict | None) -> dict:
    table = (rules or rules_mod.DEFAULTS).get("embodiment")
    if not isinstance(table, dict) or not isinstance(table.get("rules"), list):
        raise ValueError(  # noqa: TRY004 -- a config problem, reported as one
            "pool rules: embodiment must be {version, rules: [...]}"
        )
    for i, rule in enumerate(table["rules"]):
        problem = None
        if not isinstance(rule, dict):
            problem = "not an object"
        elif rule.get("field") not in FIELDS:
            problem = f"field must be one of {list(FIELDS)}"
        elif rule.get("in") not in SOURCES:
            problem = f"in must be one of {list(SOURCES)}"
        elif ("value" in rule) == bool(rule.get("copy")):
            problem = "give exactly one of value and copy"
        elif rule["in"] != "format" and not rule.get("key"):
            problem = "key is required"
        elif "regex" in rule:
            try:
                re.compile(rule["regex"])
            except (re.error, TypeError):
                problem = "regex does not compile"
        if problem:
            raise ValueError(f"pool rules: embodiment rule {i}: {problem}")
    return table


def signature(rules: dict | None = None) -> str:
    """A short hash of the rule table: part of a raw capture's scan signature,
    so a changed table (or a new default) reads every episode again."""
    table = _table(rules)
    text = json.dumps(table, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]


def _lookup(document, key: str):
    node = document
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _text(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list) and value:
        parts = [_text(v) for v in value]
        if all(p is not None for p in parts):
            return " ".join(parts)  # type: ignore[arg-type]
    return None


def _cleaned(text: str) -> str | None:
    value = _CLEAN.sub("_", text.strip().lower()).strip("_")
    return value if value and len(value) <= MAX_COPY else None


def read(
    sources: dict,
    fmt: str,
    rules: dict | None = None,
    path=None,  # never evidence: accepted so callers cannot leak it in by mistake
) -> dict:
    """The four fields and, per field, what decided it (``evidence``).

    ``sources`` maps ``metadata`` / ``info`` / ``conversion`` to the parsed
    JSON documents the episode has; ``fmt`` is its format."""
    found: dict[str, dict[str, list[str]]] = {f: {} for f in FIELDS}
    for rule in _table(rules)["rules"]:
        if rule["in"] == "format":
            text, where = fmt, f"format:{fmt}"
        else:
            document = sources.get(rule["in"])
            raw = _lookup(document, rule["key"]) if isinstance(document, dict) else None
            text, where = _text(raw), f"{rule['in']}:{rule['key']}"
        if text is None:
            continue
        if "regex" in rule and not re.search(rule["regex"], text, re.IGNORECASE):
            continue
        value = _cleaned(text) if rule.get("copy") else rule["value"]
        if value:
            found[rule["field"]].setdefault(value, []).append(where)
    out: dict = {}
    evidence: dict[str, str] = {}
    for field in FIELDS:
        values = found[field]
        if len(values) == 1:
            value, where = next(iter(values.items()))
            out[field] = value
            evidence[field] = ", ".join(where)
        else:
            out[field] = UNKNOWN
            if values:
                evidence[field] = "conflict: " + "; ".join(
                    f"{v} ({', '.join(w)})" for v, w in sorted(values.items())
                )
    out["evidence"] = evidence
    return out


# ------------------------------------------------------------ gripper mix


def gripper_mix(
    grippers,
    chosen: list[str] | None = None,
    allow: bool = False,
) -> dict:
    """The gripper composition of a selection and whether an export may take it.

    ``grippers``: one value per selected episode (``None`` counts as
    ``unknown``). ``chosen``: the recipe's own ``grippers`` list. ``allow``:
    the recipe's ``allow_mixed_gripper``. ``problem`` is

    - ``mixed_known``: more than one known gripper (unless ``allow``);
    - ``known_and_unknown``: one known gripper next to episodes whose gripper
      is unknown, unless the recipe names ``unknown`` on purpose or ``allow``;
    - ``None``: one known gripper, or nothing but ``unknown`` (older data,
      allowed and recorded as unknown)."""
    counts = Counter(g or UNKNOWN for g in grippers)
    known = sorted(g for g in counts if g != UNKNOWN)
    unknown = counts.get(UNKNOWN, 0)
    problem = None
    if not allow:
        if len(known) > 1:
            problem = "mixed_known"
        elif known and unknown and UNKNOWN not in (chosen or []):
            problem = "known_and_unknown"
    return {
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "known": known,
        "unknown": unknown,
        "mixed": len(known) > 1 or bool(known and unknown),
        "problem": problem,
    }


def mix_message(mix: dict, sources: dict[str, list[str]] | None = None) -> str:
    """The sentence a refusal or warning gives (English; the page translates
    the code, the CLI and logs show this)."""
    parts = ", ".join(f"{g} {n}" for g, n in mix["counts"].items())
    where = ""
    if sources:
        where = (
            " Sources: "
            + "; ".join(
                f"{g}: {', '.join(s[:4])}" + (", ..." if len(s) > 4 else "")
                for g, s in sorted(sources.items())
            )
            + "."
        )
    if mix["problem"] == "mixed_known":
        head = f"The selection mixes grippers ({parts})."
        fix = (
            " Filter to one gripper (recipe `grippers`), or set "
            "`allow_mixed_gripper` if mixing them is intended."
        )
    else:
        head = (
            f"The selection mixes episodes of a known gripper with {mix['unknown']} "
            f"whose gripper is not recorded ({parts})."
        )
        fix = (
            " Filter to the known gripper (recipe `grippers`), list `unknown` in "
            "`grippers` to take those on purpose, or set `allow_mixed_gripper`."
        )
    return head + where + fix
