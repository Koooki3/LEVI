# Built-in knowledge · interpretation

Rules for turning a natural-language request into a LEVI task spec. The local model that interprets requests receives them.

- **interpretation-001** · An annotate step always needs an instruction: what to mark, and when an outcome counts as success, failure or unknown. _(from: task-interpretation teaching, 2026-09-22)_
- **interpretation-002** · When the request asks for token or time statistics, `report` is `["tokens", "time"]`. _(from: task-interpretation teaching, 2026-09-22)_
- **interpretation-003** · Choose the fewest cameras that show what must be judged: a wide camera for which task or scene, the wrist camera for small parts. Each extra camera doubles the images and the cost. _(from: task-interpretation teaching, 2026-09-22)_
- **interpretation-004** · "Data quality check" is a `quality` step and needs no episodes. Add one only when it is asked for. _(from: task-interpretation teaching, 2026-09-22)_
- **interpretation-005** · Episode-level questions (which task, did it succeed) use workflow `review`; subtask intervals use `temporal`. _(from: task-interpretation teaching, 2026-09-22)_
- **interpretation-006** · Episode indices start at 0 ("the first 10 demos" are episodes zero to nine). An explicit list such as "23, 25, 27" is copied exactly, never turned into a range. _(from: task-interpretation teaching, 2026-09-22)_
