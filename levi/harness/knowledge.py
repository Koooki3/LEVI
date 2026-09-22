"""Built-in knowledge, and how local memory earns a place in it.

Two layers of what LEVI knows:

- **Local memory** (``workbench/memory/*.json``): one workspace's datasets,
  their teacher notes and lessons. It stays on the machine.
- **Built-in knowledge** (``levi/knowledge/<topic>.md``): dataset-agnostic
  rules in the repository, so every installation gets them with the code.
  ``annotation`` goes to every model LEVI runs on an annotation task and to
  external agents through ``workspace.get_context``; ``interpretation`` to the
  model that reads natural-language requests; ``harness`` is a reference for
  harness work.

Local notes become candidates automatically (each closed run refreshes the
list, ``workbench/knowledge/candidates.json``): a note is *ready* when it
recurs -- taught more than once, or on more than one dataset -- and names no
episode, time or dataset. Promotion is a person's decision (``levi agent
knowledge promote``): it appends the entry, with its provenance, to the
topic file, where review and version control take over.
"""

import re
import time
from pathlib import Path

from .layout import read_json, write_json

KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"
TOPICS = ("annotation", "interpretation", "harness")
ENTRY = re.compile(r"^- \*\*([a-z]+-\d{3})\*\* · (.+?)(?: _\(from: (.+)\)_)?$")
# Wording that ties a note to one dataset: episode numbers, times, ranges.
SPECIFIC = [
    (re.compile(r"episode[_ ]?\d+|\bep\s?\d+", re.IGNORECASE), "names an episode"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?s\b"), "gives a time"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\b"), "gives a range"),
]


def path_for(topic: str) -> Path:
    if topic not in TOPICS:
        raise ValueError(f"Unknown knowledge topic {topic!r}; one of {TOPICS}")
    return KNOWLEDGE / f"{topic}.md"


def load(topic: str) -> list[dict]:
    """The topic's entries: ``{"id", "text", "source"}``, in file order."""
    try:
        lines = path_for(topic).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        match = ENTRY.match(line.strip())
        if match:
            out.append(
                {"id": match[1], "text": match[2].strip(), "source": match[3] or ""}
            )
    return out


def texts(topic: str) -> list[str]:
    return [row["text"] for row in load(topic)]


# ------------------------------------------------------------- candidates


def _candidates_path(state: Path) -> Path:
    return Path(state) / "knowledge" / "candidates.json"


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _sources(state: Path):
    """(topic, text, dataset, count) for every local note and lesson."""
    folder = Path(state) / "memory"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.json")):
        memory = read_json(path) or {}
        workspace = path.stem == "workspace"
        for row in memory.get("teaching", []):
            if not row.get("note"):
                continue
            topic = (
                "interpretation"
                if workspace or row.get("workflow") == "interpret"
                else "annotation"
            )
            yield topic, row["note"], path.stem, row.get("count", 1)
        for row in memory.get("lessons", []):
            if row.get("status") == "retained" and row.get("text"):
                yield "harness", row["text"], path.stem, 1


def refresh(state: Path) -> dict:
    """Recompute candidates from local memory, keeping decisions already made."""
    old = read_json(_candidates_path(state)) or {"items": []}
    by_text = {_normal(item["text"]): item for item in old["items"]}
    known = {_normal(t) for topic in TOPICS for t in texts(topic)}
    merged = {}
    for topic, text, dataset, count in _sources(state):
        key = _normal(text)
        if key in known:
            continue
        row = merged.setdefault(
            key, {"topic": topic, "text": text, "datasets": set(), "count": 0}
        )
        row["datasets"].add(dataset)
        row["count"] += count
    items = []
    for key, row in merged.items():
        previous = by_text.get(key, {})
        reasons = [why for rule, why in SPECIFIC if rule.search(row["text"])]
        recurs = row["count"] >= 2 or len(row["datasets"]) >= 2
        items.append(
            {
                "id": previous.get("id"),
                "topic": row["topic"],
                "text": row["text"],
                "datasets": sorted(row["datasets"]),
                "count": row["count"],
                "specific": reasons,
                "ready": recurs and not reasons,
                "status": previous.get("status", "open"),
                **{
                    k: previous[k]
                    for k in ("entry", "entry_text", "decided_at")
                    if k in previous
                },
            }
        )
    # Decided items stay listed even after their source note is gone.
    for key, item in by_text.items():
        if key not in merged and item.get("status") != "open":
            items.append(item)
    used = {item["id"] for item in items if item.get("id")}
    number = 1
    for item in sorted(items, key=lambda i: (i["topic"], i["text"])):
        if not item.get("id"):
            while f"candidate-{number:03d}" in used:
                number += 1
            item["id"] = f"candidate-{number:03d}"
            used.add(item["id"])
    value = {
        "schema": "levi.harness.knowledge-candidates.v1",
        "items": sorted(items, key=lambda i: i["id"]),
        "updated_at": time.time(),
    }
    write_json(_candidates_path(state), value)
    return value


def candidates(state: Path, status: str | None = "open") -> list[dict]:
    items = (read_json(_candidates_path(state)) or {}).get("items") or []
    return [i for i in items if status is None or i.get("status") == status]


def _decide(state: Path, candidate_id: str, **changes) -> dict:
    value = read_json(_candidates_path(state)) or {"items": []}
    for item in value["items"]:
        if item["id"] == candidate_id:
            if item.get("status") != "open":
                raise ValueError(f"{candidate_id} is already {item['status']}")
            item.update(changes, decided_at=time.time())
            write_json(_candidates_path(state), value)
            return item
    raise KeyError(candidate_id)


def promote(
    state: Path, candidate_id: str, topic: str | None = None, text: str | None = None
) -> dict:
    """Append a candidate to built-in knowledge -- a person's decision. The
    text may be rewritten on the way, which is how a note that names one
    dataset's episodes or times becomes general."""
    item = next((i for i in candidates(state, None) if i["id"] == candidate_id), None)
    if item is None:
        raise KeyError(candidate_id)
    if item.get("status") != "open":
        raise ValueError(f"{candidate_id} is already {item['status']}")
    topic = topic or item["topic"]
    final = " ".join((text or item["text"]).split())
    if not final:
        raise ValueError("Knowledge text is empty")
    still = [why for rule, why in SPECIFIC if rule.search(final)]
    if still:
        raise ValueError(
            "Built-in knowledge must be dataset-agnostic; the text "
            + ", ".join(still)
            + ". Rewrite it with --text."
        )
    entries = load(topic)
    number = max((int(e["id"].rsplit("-", 1)[1]) for e in entries), default=0) + 1
    entry_id = f"{topic}-{number:03d}"
    source = ", ".join(item.get("datasets") or []) or "local memory"
    day = time.strftime("%Y-%m-%d")
    path = path_for(topic)
    with path.open("a") as f:
        f.write(f"- **{entry_id}** · {final} _(from: {source}, {day})_\n")
    # ``text`` stays the note as it is in local memory, so the same note is
    # never offered again; the entry as written is kept beside it.
    return _decide(
        state, candidate_id, status="promoted", entry=entry_id, entry_text=final
    )


def reject(state: Path, candidate_id: str) -> dict:
    return _decide(state, candidate_id, status="rejected")
