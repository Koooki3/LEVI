"""Validation in levi.decision must survive ``python -O`` (which strips ``assert``).

main's read_ledger already moved to explicit checks (commit 69f34780); the escalation
queue, the token ledger and the confirm-once ledger validate stored lines and panel
responses the same way, so they follow the same rule.
"""

import ast
import json
from pathlib import Path

import pytest

from levi.decision import confirm, ledger, queue
from levi.events import causal_eval

DECISION = Path(__file__).resolve().parents[1] / "levi" / "decision"


@pytest.mark.parametrize("name", ["confirm", "leakcheck", "ledger", "queue"])
def test_no_assert_in_the_validating_modules(name):
    tree = ast.parse((DECISION / f"{name}.py").read_text())
    assert not [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]


@pytest.mark.parametrize(
    "row",
    [
        {"schema": "wrong.v0", "episodes": [{"id": "A", "sha256": {}}]},
        {"schema": causal_eval.LEDGER_SCHEMA, "episodes": []},
        {"schema": causal_eval.LEDGER_SCHEMA, "episodes": [{"id": 7, "sha256": {}}]},
        {"schema": causal_eval.LEDGER_SCHEMA, "episodes": [{"id": "B", "sha256": []}]},
        {"schema": causal_eval.LEDGER_SCHEMA, "episodes": 3},
        {"schema": causal_eval.LEDGER_SCHEMA},
        ["not", "a", "mapping"],
    ],
)
def test_confirm_check_entry_refuses_a_malformed_record(row):
    with pytest.raises(confirm.LedgerCorrupt):
        confirm.check_entry(row)


def test_a_malformed_stored_line_refuses_in_the_token_ledger_and_the_queue(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"schema": "wrong"}) + "\n")
    with pytest.raises(ledger.LedgerError, match="line 1"):
        ledger.read_rows(bad)
    with pytest.raises(queue.QueueCorrupt, match="line 1"):
        queue.Queue(bad, denylist=None)._events()


@pytest.mark.parametrize("text", ["[]", '"x"', "3", "null"])
def test_a_response_that_is_not_an_object_is_not_json_for_the_queue(text):
    request = queue.EscalationRequest(
        request_id="r1",
        question_kind="outcome_label",
        spec_id="s",
        messages=(),
        response_schema={"type": "string"},
    )
    parsed = queue.parse_response(text, request)
    assert parsed.response is None and parsed.problems == ("not_json",)
