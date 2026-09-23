# Annotation evaluation records

LEVI records how well and how fast subtasks were annotated, for a person and for an agent, in one format so the two can be compared line by line.

| Who | When the record is written | File |
| --- | --- | --- |
| A person | **Stop recording** in the annotation view | `<workspace>/eval/<dataset>_human_<YYYYmmddTHH>.md` |
| An agent | A committed subtask-annotation run that covers **every episode** of its dataset (such as "annotate all of `<dataset>` with subtasks") | `<workspace>/eval/<dataset>_agent_<YYYYmmddTHH>_<driver>.md` |
| An agent, on request | `uv run levi agent eval <run-id>` for any other committed subtask-annotation run (a batch, a subset) | same |

The hour is the completion time in UTC, the same clock that run ids use. A second record for the same file name in the same hour is appended as a new section and never replaces the first. `driver` records who did the work:

| driver | Meaning |
| --- | --- |
| `external-mcp` | An external agent (Claude Code, Codex, …) working through LEVI's MCP tools |
| `external-pilot` | An external agent that LEVI runs as a managed Pilot |
| `api` | An online model called by LEVI (OpenAI-compatible endpoint) |
| `local-vlm` | A local model on Ollama, working alone |
| `local-vlm-teacher` | A local model whose every phase an external teacher reviewed |
| `native-external`, `native-local-vlm`, `native-local-vlm-teacher` | The same annotators working **without LEVI**; their finished annotation is brought in afterwards |

To compare work done without LEVI on equal terms, import it through the ordinary path: plan an external run with `imported_from` set to one of the `native-*` drivers, prepare evidence, stage the finished segments with `annotations.propose_segments` (LEVI cites the frames it prepared), pass the gates and commit. The run's folder under `outputs/LEVI/workbench/agent/datasets/<dataset>/` then holds the same artifacts as an agent run, and the record is written at commit with that driver. Report the maker's tokens with `runs.report_usage` before committing; the import itself costs no annotator tokens.

## Recording a person's work

In a dataset's **Annotations** tab:

1. **● Start recording.** The session is stored on the server, so a page reload or a second tab keeps it.
2. Annotate. With a **subtask vocabulary**, pick the subtask and its outcome for each span; the description can be left empty.
3. When an episode is done, click **Confirm episode complete**. Unsaved edits are saved first. Only confirmed episodes count.
4. **■ Stop recording.** The summary shows the duration, the confirmed episodes, the subtask count and the coverage, plus the record's path. **Discard** ends a session without writing a record.

The subtask vocabulary is set per dataset in **Subtask vocabulary**. Set it before planning agent runs on the dataset: it is stored with the dataset's annotations, so changing it while a run is active changes that run's baseline and the run's proposals are refused. Enter one line per subtask: `id | label | definition`. If the last agent plan on the dataset defined subtasks, the editor offers them as a starting point. With a vocabulary, a human span carries the same `levi.subtask_id` and `levi.outcome` as an agent's segment. Without one, the editor works as before (free-text subtasks).

## What a record contains

Sections 1–6 are the same for people and agents. Both are measured from the subtask atoms in the dataset's annotation files, by `levi/eval/metrics.py`:

1. **Overview**: the dataset, the annotator, the start and end times, and how episodes were counted.
2. **Completion**: episodes confirmed (for an agent: episodes committed by the run), the subtask count, subtasks per episode, and the video seconds covered.
3. **Coverage**: second-level and frame-level coverage overall and per episode (mean, median and minimum), episodes at least 95 % covered, and gaps longer than 0.2 s. A span covers `[timestamp, to)`; without `to` it runs to the next subtask or to the last frame.
4. **Semantic richness**:
   - distinct labels;
   - share tagged with a vocabulary id, and vocabulary terms used;
   - share with an outcome, and the outcome distribution;
   - share with a description, words per description, share of distinct descriptions, type/token ratio;
   - descriptions that only repeat the label.
5. **Correctness**:
   - structural validity: empty or out-of-bounds spans, overlaps, ids outside the vocabulary;
   - agreement with the **same-content copies** of the dataset: segment F1 (same subtask, IoU ≥ 0.3), outcome agreement, place agreement, boundary error. Copies are other catalog datasets that share this dataset's name stem (`<base>_human`, `<base>_agentVLM`, …) and have the same episodes and frame counts;
   - the three least-covered episodes, and a blank line for a tester's comments.
6. **Efficiency**: duration, time per episode, subtasks per minute, and video seconds annotated per minute.

An agent's record adds **7. Agent efficiency and cost**:

- **External tokens**: compute that is not on this machine. For `api` this is LEVI's metering. For external agents it is their own report, or else LEVI's measured lower bound (what it delivered to them). For `local-vlm-teacher` it is the teacher's tokens.
- **Local VLM tokens and model time** (this machine's GPU only).
- The tokens LEVI delivered to the agent, and the tool calls and their time.
- Model requests and cache hits.
- Teacher decisions, and the human gates passed (plan approval, pilot review, commit).

Every number is also in a JSON block at the end of the file, so records can be aggregated later.

## Comparing modes

To compare annotators on the same data, copy the dataset once per mode under the workspace (for example `<base>_human`, `<base>_agentCode`, `<base>_agentVLM`, `<base>_agentMix`). LEVI registers each copy, and each record reports its agreement with the other copies' annotations of the same episodes.
