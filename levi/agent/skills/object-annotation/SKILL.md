---
name: object-annotation
description: Choose SAM3 or annotate objects yourself, then stage both for the same review.
metadata:
  version: "2"
---

Ask objects.strategy first: it reads this machine, not a preference. SAM3 when the worker, checkpoint and GPU headroom are there; otherwise annotate the objects yourself. Both paths stage for the same human review, so neither is a shortcut past it. Check objects.status before planning a worker run. Use the frozen run snapshot. Camera and episode scope are mandatory.

To annotate yourself, call objects.detect on the evidence you already read: it measures coherent regions and returns each one's outline, position, shape and median HSV, plus one labelled overlay. Detection measures, it does not recognise — decide from the statistics and the overlay which regions are the objects and what to call them. Submit with objects.propose citing candidate_id; send a polygon only for an object detection missed. Never invent a mask for a frame you did not read, and never name a concept outside the planned scope.

Outlines are the visible region, so a gripper covering half a plate outlines half a plate. Say so through occluded and score rather than completing the shape from memory. Report per-frame uncertainty instead of averaging it away.
