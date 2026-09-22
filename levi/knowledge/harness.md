# Built-in knowledge · harness

Lessons on running models inside LEVI, measured on real tasks. This file is a reference for harness and kernel work and for external agents; no model prompt includes it.

- **harness-001** · Structural mistakes by a small model (empty answers, missing fields, invented ids, rewriting a draft) are removed by narrowing the decoding schema, not by notes. Notes help with meaning and carry over to later tasks. _(from: local-VLM teaching, 2026-09-22)_
- **harness-002** · Put the answer before the prose: llama.cpp decodes required properties first and in order, so a required `proposals` placed before a short `summary` stops a small model from spending its output budget on narration. _(from: local-VLM teaching, 2026-09-22)_
- **harness-003** · Size each request to the model: learn per-image and per-character token cost from metered calls, send compact evidence rows, split long refinements into batches and grow the answer budget with the number of expected intervals. _(from: local-VLM teaching, 2026-09-22)_
- **harness-004** · Keeping refined boundaries within the frames a refinement was shown helps when the draft was reviewed. It locks in coarse errors when nobody reviewed the draft. _(from: local-VLM graduation exam, 2026-09-22)_
- **harness-005** · Most of an external agent's tokens are often tool-response text, not images. Keep receipts compact, and let the agent read a whole episode as one contact sheet at about 1 Hz. _(from: external-agent cost ledgers, 2026-09-22)_
- **harness-006** · Supervising a learner does not save a teacher's tokens while the teacher still looks at every episode. Savings need a high first-pass acceptance rate and sampled review. _(from: controlled teacher/student experiment, 2026-09-22)_
- **harness-007** · A shared GPU needs a guardian that learns how other workloads restart and yields to them, preempts in-flight requests and resumes automatically. A fixed quiet window almost never opens. _(from: shared-GPU operation, 2026-09-22)_
- **harness-008** · Settle every model call's tokens whether or not its answer is valid, and keep the valid part of an invalid answer. _(from: local-VLM teaching, 2026-09-22)_
