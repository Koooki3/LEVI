# Graded answering: the decision layer

`levi/decision/` decides **who answers** a question: the local model, an external panel, or a person. It is a pure core: it calls no model, starts no service, opens no port. Nothing in `levi/automatic` uses it yet. 中文：[DECISION.zh-CN.md](DECISION.zh-CN.md).

Status: contracts, confidence extraction, calibration and the router are implemented and tested on synthetic data. Also implemented (CPU only, fake panel in tests): the escalation queue, the external-token ledger and the leak gate. Not built yet: the logprobs request path to a model server, the comparison-record changes, and any use on real data or in AERI. Whether a strict-JSON server returns logprobs before or after the grammar mask is **unverified** (needs a GPU test); both cases are handled.

## Routing order

The first rule that holds decides.

| # | Condition | Result | Label |
| --- | --- | --- | --- |
| 1 | The question kind is physical safety in this context, or is not listed | person | none |
| 2 | Confidence unknown, local answer is `null` (abstention), out-of-set probability mass above the limit, a physical veto, or the out-of-distribution flag | external panel | `agent-reviewed` |
| 3 | Calibrated probability `p >= tau` | local | `automatic` |
| 4 | Otherwise (and always when `tau` is `ESC_ALL`) | external panel | `agent-reviewed` |

If the panel is unavailable or its answer is invalid, the item stays `pending_external`: no label is written, the local answer is not used, and no person is asked. Two reviewers who disagree need an adjudicator or the response is not an answer.

The physical-safety table (`levi/decision/safety.py`) is static and keyed by (question kind, offline or live). The live completion judgement ("may the arm be released") is physical; the same question on recorded data is not. An unlisted pair is physical.

## Confidence

Closed fields (boolean, enum, option letter) take the probability of each allowed value from the top alternatives of the token that starts the value, renormalised, plus the probability mass that falls outside every allowed value. A value that cannot be located, tokens that do not add up to the text, an alternative that starts two allowed values, or impossible probabilities give **unknown**, which is escalated. A confidence the model writes about itself is never read. Physical consistency rules are supplied by the caller as veto functions; a rule that raises counts as a veto.

## Calibration and the threshold

`levi/decision/calibration.py` fits one temperature on **tune** items only: any other `split`, and `confirm` with its own error (`ConfirmDataError`), is refused. Probabilities used to choose `tau` come from leave-one-episode-out fits. `tau` is the grid value with the fewest external tokens such that the 90% one-sided lower bound (episode-clustered bootstrap, fixed seed) of the paired quality difference to the teacher-only arm is at least `-delta`; `ESC_ALL` has difference 0 and is always allowed, so a poor local model degrades to teacher-only. Ties go to the larger `tau`. The artifact (`levi.decision.calibration.v1`) is bound to the model, prompt and harness-snapshot sha256; a run with another key is refused, and the same input gives the same bytes.

## Escalation queue

`levi/decision/queue.py`. An escalated item waits here when the panel is unavailable or its response is not valid. The file is append-only JSON lines (`levi.decision.queue.v1`: `enqueue`, `rejected`, `answered`, `failed`); the state is the replay of the file. The idempotency key is the sha256 of the canonical **teacher-visible** request, so enqueueing the same request twice writes one line. The teacher-visible request has no local answer, no confidence, no question kind, no split and no item reference; `EscalationRequest` has no field that could carry them.

States: `pending_external`, `answered_external`, `external_failed`. The queue writes no label, never returns the local answer and never asks a person. An unavailable panel (`send` returns `None` or raises a connection error) is not an event: the run stops and the items stay pending. A response is checked on replay: strict JSON (duplicate keys, NaN/Infinity and nesting over 32 are refused), contract, `request_id`, label tier `agent-reviewed`, the answer and every reviewer answer against the request's response schema, and the panel rule (two reviewers who disagree need an adjudicator, and the answer must be the adjudicator's). Problem codes: `not_json`, `panel`, `contract`, `request_id`, `label_tier`, `adjudication_missing`, `answer_schema`, `reviewer_schema`, `answer_not_panel_result`. A rejected response uses one attempt; after `max_attempts` (default 3) the item is `external_failed`. The first valid answer wins; anything later is ignored. Replay re-checks every stored `answered` line with the same rules: a line that is not a valid response to its request (for example one added by hand) makes replay raise `QueueCorrupt` instead of counting as an answer. `enqueue(request, context)` also needs the context (`offline` or `live`) the question was asked in and refuses a physical-safety question with `PhysicalQuestion` (the static table in `safety.py`; an unlisted kind or an unknown context counts as physical): such a question goes to a person, never to the panel. The router already decides this first; the queue checks again so a direct call cannot bypass it. Pending and failed items stay in the denominator (`counts()["in_denominator"]`). The schema check supports `type`, `enum`, `const`, `properties`, `required`, `additionalProperties: false` and `items`; any other keyword is refused at enqueue (`UnsupportedSchema`), so no answer is accepted unchecked. A recorded teacher answer is reused only if the request sha256 is identical (`reuse_reference_answer`).

## External-token ledger

`levi/decision/ledger.py`. One line per panel member per escalation attempt: request sha256, task label, purpose, split, role, the four counts, `fresh` (`input_new + output + cache_write`), `with_cache` (all four), `exact`, source code. The two bases and the label rule are those of `levi/eval/token_monitor.py`. **Unknown is `null`, never 0**; a row with an unknown field is never `exact`, and a total over rows with unknowns is a lower bound (`exact` false). `task`, `role` and `source_code` (the last two come from the panel's response) must each match `[A-Za-z0-9_.:-]{1,64}` (`source_code` may be `null`); anything else is refused when the row is made and again when the file is read. Tokens of a rejected response are recorded too (key: request sha256, purpose, role, attempt; a replay writes nothing twice). `report()` gives totals by split and task; rows of `confirm`, `frozen` and `heldout` appear only inside aggregates, never as per-request lines.

## Leak gate and confirm-once

`levi/decision/leakcheck.py` checks the teacher-visible request before it is stored or sent (`Queue.enqueue`, and again in `Queue.drain`): no denied fragment id, no denied sha256, and no image whose decoded bytes hash to a denied sha256, in any key or string (the request id included). The deny list is injected by the caller (`Denylist.from_ledger_rows` builds it from the confirm ledger); this package reads no gold, frozen or held-out folder. A hit raises `LeakFound`, exit code **4** (`python -m levi.decision.leakcheck --request R --denylist D`; 2 for unreadable input). Confirm data in calibration is exit code 3; a confirm episode claimed twice is exit code 5.

`levi/decision/confirm.py`: `claim(rows, entry)` is a pure function, and `claim_ledger(path, entry)` appends under a lock. Same line format and semantics as `levi.events.causal_eval.claim_ledger` (whose `confirmed_overlap` it reuses; a test checks both directions): an episode whose id or any capture sha256 is already in the ledger is refused with `ConfirmReused` and nothing is written.

## Import boundary

`levi.decision` does not import `levi.automatic` or `levi.curation`, and neither imports it. `tests/test_decision_import_boundary.py` checks this with the AST.

## Tests

`tests/test_decision_*.py` (no GPU, no network). Every invariant has a test, and each was checked by a mutation that removes the rule.
