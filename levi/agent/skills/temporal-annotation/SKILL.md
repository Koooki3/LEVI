---
name: temporal-annotation
description: Evidence-grounded attempts, outcomes and temporal boundary review.
metadata:
  version: "2"
---

Definitions are semantic priors, never a required successful sequence. Use the supplied workflow definitions and preserve subtask_id, attempt, outcome and layer. Retrying a subtask creates another attempt, not a new identity. Success needs an observable evidence_note; intention/contact alone is insufficient. Use unknown for invisible outcomes, and unknown/other/background for unclassified segments. Never force all time into a task.

Coarse observations locate candidates only. During boundary_refinement, use denser supplied frames, record uncertainty and boundary_candidates when a transition cannot be resolved. Cite frame IDs near both boundaries. Short failures may occur between samples: report coverage limits. Do not assume fixed FPS, synchronization across cameras or cross-camera identity. Use the dataset timestamps supplied by the runtime, and [start,end) intervals. Point events have no end.

Keep evidence_note short and independently checkable. Do not output private reasoning. Subtask completion and episode outcome are different; an overall failed episode can contain successful attempts. Overlap follows the declared layer policy. Do not invent unseen masks, events or signals.
