"""Which saved atoms a newly committed agent run replaces.

Re-annotating an episode used to append: the new run's segments landed next to
the old run's, and every segment appeared twice. A commit now replaces the
atoms a previous agent run published *and nobody edited since*, of the styles
the new run proposes for that episode. Everything a person wrote or changed
stays. The inverse patch still restores the replaced atoms on undo.

An atom is agent-owned when its ``levi.origin`` says so and its content and
times still equal what that commit wrote, or -- for atoms published before
``levi.origin`` existed -- when it matches a committed agent proposal in the
bundle's provenance history exactly.
"""

from .formats import ANNOTATIONS


def _key(content, start, end):
    return (
        content,
        round(float(start), 3),
        None if end is None else round(float(end), 3),
    )


def atom_key(atom):
    return _key(atom.get("content"), atom.get("timestamp", 0.0), atom.get("to"))


def committed_keys(history, episode):
    """Exact (content, start, end) of every agent proposal published for it."""
    keys = set()
    for change in history:
        if change.get("status") != "committed":
            continue
        rejected = {
            k for k, v in (change.get("decisions") or {}).items() if v == "rejected"
        }
        for index, proposal in enumerate(change.get("proposals", [])):
            if proposal.get("episode_index") != episode or str(index) in rejected:
                continue
            keys.add(_key(proposal["content"], proposal["start"], proposal.get("end")))
    return keys


def agent_owned(atom, legacy_keys):
    origin = (atom.get("levi") or {}).get("origin") or {}
    if origin.get("kind") == "agent":
        written = origin.get("written")
        return written is not None and tuple(written) == tuple(atom_key(atom))
    return atom_key(atom) in legacy_keys


def styles_for(proposals):
    styles = set()
    for proposal in proposals:
        kind = ANNOTATIONS.get(proposal["kind"])
        if kind is None or kind.layer != "language":
            continue
        styles.add("interjection" if kind.point else proposal.get("style", "subtask"))
    return styles


def split(atoms, proposals, history, episode):
    """(kept, replaced) for one episode before the new proposals are applied."""
    styles = styles_for(proposals)
    if not styles:
        return list(atoms), []
    legacy = committed_keys(history, episode)
    kept, replaced = [], []
    for atom in atoms:
        if atom.get("style") in styles and agent_owned(atom, legacy):
            replaced.append(atom)
        else:
            kept.append(atom)
    return kept, replaced
