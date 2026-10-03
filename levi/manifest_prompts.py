"""Per-frame prompts for a training manifest (docs/TRAINING_MANIFEST.md).

A manifest frame carries two prompts: the task alone, and the task with the
subtask the frame is in. The second one is built only from a time segment a
person stands behind, from a versioned template. Nothing here reads a file;
``levi.training_manifest`` feeds it one frame's evidence at a time.

Why the default template is English: the task text of the datasets this serves
is English and so is the policy's pretraining language. A Chinese template word
inside an otherwise English prompt is an out-of-distribution token run for the
model, so the words LEVI adds follow the task text, not the interface language.
"""

from __future__ import annotations

import re

from .annotations.vocabulary import SPECIAL

# Template version -> template. A new wording is a new version; a version's
# wording never changes once a manifest has been written with it.
PROMPT_TEMPLATES = {"v1": "{task}; current subtask: {subtask}"}
DEFAULT_TEMPLATE = "v1"
DEFAULT_SUBTASK_MAX_CHARS = 120

# ``subtask_review`` values whose segment a person stands behind: empty (a
# person, or an agent a person reviewed) and "edited" (an automatic segment a
# person changed). "auto" and anything else this code does not know are out.
REVIEWED = (None, "edited")

# Why a frame's second prompt has no subtask (``prompt_subtask_skip``).
SKIP_REASONS = (
    "no_task",
    "no_segment",
    "unreviewed",
    "review_unrecognised",
    "special",
    "empty_text",
)

# Punctuation trimmed from both ends of a subtask text (ASCII and CJK).
_EDGE = " \t\r\n.,;:!?-–—…、。，；：！？"
_SPACES = re.compile(r"\s+")


def template_text(version: str) -> str:
    try:
        return PROMPT_TEMPLATES[version]
    except KeyError:
        raise ValueError(
            f"Unknown prompt template {version!r}; known: "
            + ", ".join(PROMPT_TEMPLATES)
        ) from None


def clean_subtask(text: str | None, max_chars: int) -> tuple[str, bool]:
    """(the subtask text as it enters a prompt, whether it was cut).

    Surrounding whitespace and edge punctuation go, inner whitespace runs
    collapse to one space, the wording and its capitalisation stay as written.
    A longer text is cut at ``max_chars`` characters (and trimmed again)."""
    value = _SPACES.sub(" ", str(text or "")).strip(_EDGE)
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars].strip(_EDGE), True


def is_special(*names) -> bool:
    """``other`` / ``unknown`` / ``background``: not a subtask of the robot."""
    return any(str(n or "").strip().lower() in SPECIAL for n in names)


def frame_prompts(
    task: str | None,
    segment: dict | None,
    *,
    template: str,
    max_chars: int,
) -> tuple[str | None, str | None, bool, str | None, bool]:
    """(task prompt, subtask prompt, has subtask, skip reason, text was cut)
    for one frame. ``segment`` is the frame's time segment (``text``, ``id``
    and ``review``) or None; the second prompt falls back to the task alone."""
    if not task:
        return None, None, False, "no_task", False
    if segment is None:
        return task, task, False, "no_segment", False
    review = segment.get("review")
    if review not in REVIEWED:
        reason = "unreviewed" if review == "auto" else "review_unrecognised"
        return task, task, False, reason, False
    if is_special(segment.get("id"), segment.get("text")):
        return task, task, False, "special", False
    text, cut = clean_subtask(segment.get("text"), max_chars)
    if not text:
        return task, task, False, "empty_text", False
    return task, template.format(task=task, subtask=text), True, None, cut


def segment_source(segment: dict) -> str:
    """Where a prompt's subtask came from: the segment's span and its review."""
    end = segment.get("end")
    stop = "end" if end is None or end == float("inf") else f"{end:.3f}"
    return (
        f"segment {segment['start']:.3f}-{stop} "
        f"review={segment.get('review') or 'human'}"
    )
