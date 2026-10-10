"""The structured record of outside sources ``levi.events`` uses
(``third_party.json``) and the rules it must keep.

``problems(registry, root)`` lists every broken rule (an empty list is a
pass):

- every component has the required fields, a known ``use`` and the three
  licence fields (code, weights, data);
- a component whose code licence is non-commercial, missing or unverified is
  ``adopt-idea`` only: not copied, not shipped;
- a component with a citation names a reference whose status is
  ``VERIFIED``;
- a ``dependency`` is declared in ``pyproject.toml``;
- every file in ``where`` exists;
- every arXiv number a ``levi/events`` module mentions belongs to a
  registered component, so nothing is cited that was not checked.
"""

import json
import re
import tomllib
from pathlib import Path

HERE = Path(__file__).parent
REGISTRY = HERE / "third_party.json"
SCHEMA = "levi.third_party_registry.v1"
USES = {"dependency", "adopt-idea", "adapt-code", "vendored"}
REQUIRED = (
    "key",
    "name",
    "kind",
    "use",
    "source",
    "version",
    "licenses",
    "shipped",
    "code_copied",
    "redistributable",
    "where",
)
# Code licences that allow nothing but borrowing an idea.
RESTRICTED = re.compile(
    r"\bNC\b|non-?commercial|UNVERIFIED|NONE|no licen[cs]e|unknown", re.IGNORECASE
)
ARXIV = re.compile(r"arXiv:(\d{4}\.\d{4,5})")


def load(path=REGISTRY):
    return json.loads(Path(path).read_text())


def _requirement_names(pyproject):
    data = tomllib.loads(Path(pyproject).read_text())
    names = set()
    for spec in data.get("project", {}).get("dependencies", []):
        names.add(re.split(r"[<>=\[;\s!~]", spec, maxsplit=1)[0].lower())
    return names


def problems(registry, root):
    """Broken rules of ``registry`` for the repository at ``root``."""
    root = Path(root)
    out = []
    if registry.get("schema_version") != SCHEMA:
        out.append(f"schema_version must be {SCHEMA}")
    components = registry.get("components") or []
    keys = [c.get("key") for c in components]
    if len(keys) != len(set(keys)):
        out.append("duplicate component keys")
    declared = _requirement_names(root / "pyproject.toml")
    arxiv = set()
    for c in components:
        key = c.get("key", "?")
        missing = [f for f in REQUIRED if f not in c]
        if missing:
            out.append(f"{key}: missing {', '.join(missing)}")
            continue
        if c["use"] not in USES:
            out.append(f"{key}: unknown use {c['use']!r}")
        licenses = c["licenses"] if isinstance(c["licenses"], dict) else {}
        if set(licenses) != {"code", "weights", "data"}:
            out.append(f"{key}: licenses needs code, weights and data")
        if RESTRICTED.search(str(licenses.get("code", ""))) and (
            c["use"] != "adopt-idea" or c["shipped"] or c["code_copied"]
        ):
            out.append(
                f"{key}: a non-commercial, missing or unverified code licence "
                "allows only adopt-idea, nothing copied or shipped"
            )
        citation = c.get("citation")
        if citation:
            if c.get("reference_status") != "VERIFIED" or not c.get("reference_key"):
                out.append(f"{key}: a citation needs a VERIFIED reference")
            if citation.get("arxiv"):
                arxiv.add(citation["arxiv"])
        if c["use"] == "dependency" and c["key"].lower() not in declared:
            out.append(f"{key}: a dependency must be declared in pyproject.toml")
        for where in c["where"]:
            if not (root / where).is_file():
                out.append(f"{key}: {where} does not exist")
    for module in sorted((root / "levi" / "events").glob("*.py")):
        for number in ARXIV.findall(module.read_text()):
            if number not in arxiv:
                out.append(
                    f"{module.relative_to(root)} cites arXiv:{number}, "
                    "which no registered component carries"
                )
    return out
