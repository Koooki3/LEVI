# Annotation evaluation records

LEVI records how well and how fast a person or an agent annotated subtasks, in one format, so the two can be compared line by line.

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
| `local-vlm` | A local model on Ollama or a local OpenAI-compatible server (`openai-local`, e.g. vLLM), working alone |
| `local-vlm-teacher` | A local model whose every phase an external teacher reviewed |
| `native-external`, `native-local-vlm`, `native-local-vlm-teacher` | The same annotators working **without LEVI**; their finished annotation is brought in afterwards |

To compare work done without LEVI on equal terms, import it through the ordinary path: plan an external run with `imported_from` set to one of the `native-*` drivers, prepare evidence, stage the finished segments with `annotations.propose_segments` (LEVI cites the frames it prepared), pass the gates and commit. The run's folder under `outputs/LEVI/workbench/agent/datasets/<dataset>/` then holds the same artifacts as an agent run, and the record is written at commit with that driver. Report the original annotator's tokens with `runs.report_usage` before committing; the import itself costs no annotator tokens.

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
   - agreement with the **same-content copies** of the dataset: segment F1 (same subtask, IoU ≥ 0.3), outcome agreement, time accuracy (share of the reference's annotated time given the same subtask, and the same subtask and outcome), boundary error. Every measure is task-agnostic: none depends on the vocabulary, the task or the frame rate. Copies are other catalog datasets that share this dataset's name stem (`<base>_human`, `<base>_agentVLM`, …) and have the same episodes and frame counts;
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

## Teacher, harness and direct: the three-arm round

The teacher-student method runs every round three ways on the same problems, with the same decoding and the same scoring, and compares them in one table (`levi/eval/compare.py`; [中文](EVALUATION.zh-CN.md)):

| Arm | Who answers |
| --- | --- |
| `teacher` | The external agent that supplies the paradigm and the experience entries. Its answer is a reference, not the gold standard |
| `local_harness` | The local VLM through LEVI's harness |
| `local_direct` | The same local VLM called directly, without LEVI |

The headline is whether `local_harness` reaches at least the teacher's quality while being faster and spending fewer external tokens. So the table reports quality per second and quality per external token, not accuracy alone.

### What one arm records (`levi.eval.arm_record.v1`)

| Field | Meaning |
| --- | --- |
| `arm`, `task`, `model` | The arm (`teacher`, `local_harness`, `local_direct`), the task name, the model that answered |
| `episode_set` or `episode_set_hash` + `episode_count` | The problems. `episode_set_hash` is the SHA-256 of the sorted, de-duplicated episode ids written as strings; if both forms are given they must agree |
| `decoding`, `scoring` | The round's declared decoding and scoring (a JSON object, or a string for `scoring`), compared as written. An external agent's real sampling cannot be observed or enforced, so the teacher arm records the declared decoding |
| `quality`, `quality_ci`, `quality_ci_method` | The primary metric against the **gold standard** (its name belongs in `scoring`), its interval `[low, high]` and how it was computed. `null` = not measured |
| `agreement_with_teacher` | Share of the teacher's answers this arm reproduces, 0 to 1. Not set on the teacher arm |
| `wall_s` | Wall-clock seconds, greater than 0 |
| `local_tokens` | Tokens spent on this machine's GPU |
| `external_tokens` | `input_new`, `output`, `cache_read`, `cache_write` (the fields of `levi.aeri.external_report.v1`), kept apart. `null` for the whole object or for one field = unknown; `0` = measured zero |
| `external_tokens_source` | Where the external numbers came from (one line) |
| `blocked` | What blocked the arm, if anything (one line) |
| `per_episode` | Episode id to score. The only per-item field, and not allowed in a confirm table |

A record with a field not in this table is refused, so per-item content cannot ride along.

### One command

```bash
python -m levi.eval.compare teacher.json harness.json direct.json            # Markdown
python -m levi.eval.compare teacher.json harness.json direct.json --format json
python -m levi.eval.compare ... --confirm        # aggregate scores only
```

Each file holds a record, a list of records, or `{"records": [...]}`. The command reads those files and prints; it starts no service, uses no GPU and opens no port. From Python: `compare.compare(records, confirm=False, token_basis=...)` returns a `Table` with `to_markdown()` and `to_json()`.

The Markdown has three tables with one row per arm (quality and speed; local and external tokens; differences to the teacher and to the direct arm) and a headline for `local_harness` against the teacher: quality not lower, faster, fewer external tokens. The headline compares point estimates only; read the intervals before believing a small difference. The JSON has one row per arm with every column.

### Rules the table enforces

- **Same problems, same conditions.** Records whose `task`, episode-set hash, `decoding` or `scoring` differ are refused, and the message names every difference. So are two records for one arm.
- **Unknown is not zero.** A measurement that is `null` is printed `未知` and every ratio or difference that needs it is `未知`. A ratio with a denominator of exactly 0 (a local arm that spent no external tokens) is `不适用`, not `未知`. The headline answers `未知` instead of "yes" when an input is missing.
- **Quality per external token** divides quality by `input_new + output + cache_write` by default: what the external model newly read or wrote. `cache_read` is cheap re-reading of a cached prefix, stays a column of its own and is not summed in (the convention of the experiment ledger; `AERI_EXTERNAL.md` also says cache dimensions are not summed or converted to price). The total is unknown as soon as one field of the basis is. Change it with `token_basis` or `--basis`; the table prints the basis it used.
- **Confirm stays blind.** With `confirm=True` or `--confirm` a record with `per_episode`, with an explicit `episode_set` (give the hash and the count), or with free text in `blocked` or `external_tokens_source` (only a reason code such as `gpu_busy` is allowed) is refused. Confirm episodes' contents and labels are not shown to the teacher or the coordinator.

### External tokens from a Claude session (`levi/eval/tokens.py`)

The teacher's external tokens come from its own session transcript. A Claude session file writes one assistant message as several lines that share a `message.id`, and the usage on them grows line by line. **Each message id counts once, with its last usage record**; counting the first line under-counts a message several times over (measured on one sub-agent transcript: output 16 on the first line, 415 on the last).

```bash
python -m levi.eval.tokens session.jsonl [more.jsonl ...] --since 2026-11-06T09:00:00Z --until 2026-11-06T10:00:00Z
```

- **You name the files.** The code lists no directory, expands no glob and looks into no home folder, so it cannot sweep up sessions that are not part of the round. Pass resumed sessions oldest first (a later copy of a message replaces an earlier one).
- A message belongs to the window by the time of its first line, and then counts with its final usage.
- Lines that cannot be parsed, assistant lines with neither `message.id` nor `uuid`, messages without a valid time (when a window is set) and usage fields that are missing or not a count are **counted and reported**, never dropped silently. `exact` is false then and the totals are lower bounds. A field that no counted message carried is `null`, not 0.
- `Usage.external_tokens()` and `Usage.source` fill `external_tokens` and `external_tokens_source` of the teacher's record.

### Not done yet

There is no adapter yet from the existing eval records, the `levi-vlm-eval` results or the AERI queue's `tokens.jsonl` to `levi.eval.arm_record.v1`; a round builds its records itself. The teacher arm's decoding is declared, not enforced. The table is not a significance test.
