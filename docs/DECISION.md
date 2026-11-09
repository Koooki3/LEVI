# Graded answering: the decision layer

`levi/decision/` decides **who answers** a question: the local model, an external panel, or a person. It is a pure core: it calls no model, starts no service, opens no port. Nothing in `levi/automatic` uses it yet. 中文：[DECISION.zh-CN.md](DECISION.zh-CN.md).

Status: contracts, confidence extraction, calibration and the router are implemented and tested on synthetic data. Not built yet: the escalation queue and token ledger, the logprobs request path to a model server, the comparison-record changes, and any use on real data or in AERI. Whether a strict-JSON server returns logprobs before or after the grammar mask is **unverified** (needs a GPU test); both cases are handled.

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

## Import boundary

`levi.decision` does not import `levi.automatic` or `levi.curation`, and neither imports it. `tests/test_decision_import_boundary.py` checks this with the AST.

## Tests

`tests/test_decision_*.py` (no GPU, no network). Every invariant has a test, and each was checked by a mutation that removes the rule.
