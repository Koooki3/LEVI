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
# The index columns: the four fields and the evidence (JSON text) behind them.
COLUMNS = (*FIELDS, "embodiment_evidence")
INHERITED = (
    "robot",
    "gripper",
    "ee_frame",
)  # a conversion shares these with its capture
SOURCES = ("metadata", "info", "conversion", "format")
_CLEAN = re.compile(r"[^a-z0-9_.-]+")
MAX_COPY = 40
# The names a recipe can select (recipe.py): rule values must be selectable.
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")


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
        elif "value" in rule and not (
            isinstance(rule["value"], str) and NAME.fullmatch(rule["value"])
        ):
            problem = f"value must look like {NAME.pattern}"
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
    """A short hash of the rule table and the declarations: part of a raw
    capture's scan signature, so a changed table (or a new default) reads
    every episode again."""
    table = [_table(rules), _declarations(rules)]
    text = json.dumps(table, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]


def _declarations(rules: dict | None) -> list[dict]:
    """The workspace's declared embodiment of a source (``embodiment_declared``
    in ``pool/rules.json``); empty by default. Each one is
    ``{field, value, source, evidence?: "declared", note?}``: ``source`` is a
    path relative to a pool root (a dataset or a folder holding captures)."""
    items = (rules or rules_mod.DEFAULTS).get("embodiment_declared") or []
    if not isinstance(items, list):
        raise ValueError(  # noqa: TRY004 -- a config problem, reported as one
            "pool rules: embodiment_declared must be a list"
        )
    for i, item in enumerate(items):
        problem = None
        source = item.get("source") if isinstance(item, dict) else None
        if not isinstance(item, dict):
            problem = "not an object"
        elif item.get("field") not in FIELDS:
            problem = f"field must be one of {list(FIELDS)}"
        elif not (isinstance(item.get("value"), str) and NAME.fullmatch(item["value"])):
            problem = f"value must look like {NAME.pattern}"
        elif not (
            isinstance(source, str)
            and source.strip("/")
            and not source.startswith("/")
            and ".." not in source.split("/")
        ):
            problem = "source must be a path relative to a pool root"
        elif item.get("evidence", "declared") != "declared":
            problem = 'evidence, when given, must be "declared"'
        if problem:
            raise ValueError(f"pool rules: declared embodiment {i}: {problem}")
    return items


def declarations(rules: dict | None = None) -> list[dict]:
    """The validated declarations of a rule set (copies, for records)."""
    return [dict(d) for d in _declarations(rules)]


_DECLARED = re.compile(r"^(?:linked capture: )?declared: (.*?)(?: \(|;|$)")


def evidence_of(row: dict) -> dict:
    """A row's ``embodiment_evidence`` as a dict (the index stores JSON text)."""
    value = row.get("embodiment_evidence")
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}


def declared_sources(row: dict) -> dict[str, str]:
    """Per field, the declared source a row's value comes from, directly or
    through its linked capture; a value the metadata gave (or that a
    declaration merely agrees with) is not listed."""
    out = {}
    for field, text in evidence_of(row).items():
        found = _DECLARED.match(str(text))
        if found:
            out[field] = found.group(1)
    return out


def declared_counts(rows) -> dict[str, int]:
    """How many of the rows have each field's value from a declaration."""
    counts: Counter = Counter()
    for row in rows:
        for field in declared_sources(row):
            counts[field] += 1
    return dict(counts)


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
            # Not read from the episode's metadata: a fixed fact of the format.
            text, where = fmt, f"derived:format:{fmt}"
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


def columns(result: dict) -> dict:
    """``read``'s result as index columns."""
    return {
        **{f: result[f] for f in FIELDS},
        "embodiment_evidence": json.dumps(result["evidence"], sort_keys=True),
    }


def from_columns(row: dict) -> dict:
    """The inverse of ``columns`` for a stored row."""
    return {
        **{f: row.get(f) or UNKNOWN for f in FIELDS},
        "evidence": json.loads(row.get("embodiment_evidence") or "{}"),
    }


def _under(relative: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return relative == prefix or relative.startswith(prefix + "/")


def declare(result: dict, relative: str, rules: dict | None = None) -> dict:
    """Apply the workspace's declarations for the source at ``relative`` (a path
    relative to a pool root). A declaration only fills a field the metadata
    leaves without any evidence; when it contradicts the metadata the field is
    ``unknown`` (the evidence names both); metadata that contradicts itself
    stays ``unknown``. Several matching declarations that disagree are
    ``unknown`` too."""
    declared = _declarations(rules)
    out = dict(result)
    evidence = dict(result["evidence"])
    for field in FIELDS:
        matches = [
            d for d in declared if d["field"] == field and _under(relative, d["source"])
        ]
        if not matches:
            continue
        values = {d["value"]: d for d in matches}
        where = "; ".join(
            f"declared: {d['source']}" + (f" ({d['note']})" if d.get("note") else "")
            for d in values.values()
        )
        have = evidence.get(field)
        if have and have.startswith("conflict"):
            continue
        if len(values) > 1:
            out[field] = UNKNOWN
            evidence[field] = "conflict: declarations " + ", ".join(sorted(values))
        elif have is None:
            out[field] = next(iter(values))
            evidence[field] = where
        elif out[field] == next(iter(values)):
            evidence[field] = f"{have}; declared agrees ({where[10:]})"
        else:
            evidence[field] = (
                f"conflict: metadata {out[field]} ({have}); "
                f"declared {next(iter(values))} ({where[10:]})"
            )
            out[field] = UNKNOWN
    out["evidence"] = evidence
    return out


def inherit(result: dict, linked: list[dict] | None) -> dict:
    """A converted episode takes the robot, gripper and frame its linked raw
    captures recorded when its own files say nothing; the action mode is the
    dataset's own and is never copied. Linked captures that disagree on a
    field leave it ``unknown`` (the evidence says so); one that says nothing
    does not hide another that does."""
    if not linked:
        return result
    evidence = dict(result["evidence"])
    out = dict(result)
    for field in INHERITED:
        if out[field] != UNKNOWN:
            continue
        values: dict[str, str] = {}
        for row in linked:
            value = row.get(field)
            if value and value != UNKNOWN:
                values.setdefault(
                    value,
                    json.loads(row.get("embodiment_evidence") or "{}").get(field, ""),
                )
        if len(values) == 1:
            value, was = next(iter(values.items()))
            out[field] = value
            evidence[field] = f"linked capture: {was}"
        elif values:
            evidence[field] = "conflict: linked captures " + ", ".join(sorted(values))
    out["evidence"] = evidence
    return out


# ------------------------------------------------------------ gripper mix


def gripper_mix(
    grippers,
    chosen: list[str] | None = None,
    allow: bool = False,
    sources=None,
) -> dict:
    """The gripper composition of a selection and whether an export may take it.

    ``grippers``: one value per selected episode (``None`` counts as
    ``unknown``). ``chosen``: the recipe's own ``grippers`` list. ``allow``:
    the recipe's ``allow_mixed_gripper``. ``sources``: the source of each
    episode, same order (optional). ``problem`` is

    - ``mixed_known``: more than one known gripper (unless ``allow``);
    - ``known_and_unknown``: one known gripper next to episodes whose gripper
      is unknown, unless the recipe names ``unknown`` on purpose or ``allow``;
    - ``unknown_multi_source``: nothing but ``unknown``, from two or more
      sources (a source with no gripper record may hold either gripper, and
      two of them together may be a Franka and a Robotiq set), unless the
      recipe names ``unknown`` or ``allow``;
    - ``None``: one known gripper, or nothing but ``unknown`` from one source
      (allowed, recorded as unknown)."""
    grippers = list(grippers)
    counts = Counter(g or UNKNOWN for g in grippers)
    known = sorted(g for g in counts if g != UNKNOWN)
    unknown = counts.get(UNKNOWN, 0)
    unknown_sources = (
        dict(
            Counter(
                str(s)
                for g, s in zip(grippers, list(sources), strict=True)
                if not g or g == UNKNOWN
            )
        )
        if sources is not None
        else {}
    )
    problem = None
    if not allow:
        if len(known) > 1:
            problem = "mixed_known"
        elif known and unknown and UNKNOWN not in (chosen or []):
            problem = "known_and_unknown"
        elif not known and len(unknown_sources) > 1 and UNKNOWN not in (chosen or []):
            problem = "unknown_multi_source"
    return {
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "known": known,
        "unknown": unknown,
        "mixed": len(known) > 1 or bool(known and unknown),
        "sources": dict(
            sorted(unknown_sources.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "problem": problem,
    }


def mix_message(mix: dict, sources: dict[str, list[str]] | None = None) -> str:
    """The sentence a refusal or warning gives (English; the page translates
    the code, the CLI and logs show this)."""
    parts = ", ".join(f"{g} {n}" for g, n in mix["counts"].items())
    where = ""
    if sources and mix["problem"] != "unknown_multi_source":
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
    elif mix["problem"] == "unknown_multi_source":
        listed = ", ".join(f"{s}: {n}" for s, n in list(mix["sources"].items())[:6])
        head = (
            f"The selection takes {mix['unknown']} episodes with no recorded gripper "
            f"from {len(mix['sources'])} sources ({listed}"
            + (", ..." if len(mix["sources"]) > 6 else "")
            + "). Sources with no gripper record may hold different grippers."
        )
        fix = (
            " Declare each source's gripper in the workspace `pool/rules.json` "
            "(`embodiment_declared`), list `unknown` in the recipe's `grippers` "
            "to take them on purpose, or set `allow_mixed_gripper`."
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


def sources_by_gripper(rows) -> dict[str, list[str]]:
    """Which sources hold each gripper class (``rows`` have ``source`` and
    ``gripper``), at most ten per class."""
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row.get("gripper") or UNKNOWN, set()).add(
            str(row.get("source"))
        )
    return {g: sorted(s)[:10] for g, s in sorted(found.items())}


def declared_record(rows, declared: list[dict] | None) -> dict:
    """What an export took from declarations: episodes, per field and source
    the episodes, and copies of the declarations of those sources (value, note,
    who decided). Empty when every value was read from metadata."""
    by_field: dict[str, Counter] = {}
    episodes = 0
    for row in rows:
        found = declared_sources(row)
        episodes += bool(found)
        for field, source in found.items():
            by_field.setdefault(field, Counter())[source] += 1
    used = {s for counts in by_field.values() for s in counts}
    return {
        "episodes": episodes,
        "by_field": {f: dict(c) for f, c in sorted(by_field.items())},
        "declarations": [d for d in (declared or []) if d.get("source") in used],
    }


def record(
    rows, recipe: dict, rules_version=None, declared: list[dict] | None = None
) -> dict:
    """What an export holds, for ``pool_export.json``: the gripper (one value,
    or ``mixed``), the counts of every field and whether mixing was allowed."""
    rows = list(rows)
    allow = bool(recipe.get("allow_mixed_gripper"))
    mix = gripper_mix((r.get("gripper") for r in rows), None, True)
    classes = len({r.get("source") for r in rows})

    def counts(field):
        return dict(Counter(r.get(field) or UNKNOWN for r in rows))

    return {
        "gripper": next(iter(mix["counts"])) if len(mix["counts"]) == 1 else "mixed",
        "grippers": mix["counts"],
        "robots": counts("robot"),
        "action_modes": counts("action_mode"),
        "ee_frames": counts("ee_frame"),
        "mixed": mix["mixed"],
        "sources": classes,
        "declared": declared_record(rows, declared),
        "allow_mixed_gripper": allow,
        "rules_version": rules_version,
    }
