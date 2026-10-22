"""scripts/ci_annotations.py and the conftest hook that prints its lines on GitHub Actions."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import conftest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_annotations_under_test", ROOT / "scripts" / "ci_annotations.py"
)
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def test_no_failure_no_annotation():
    assert ci.lines([]) == []


def test_first_line_lists_every_failure_then_one_per_test():
    failures = [
        ("failed", f"tests/test_a.py::test_{i}", f"assert {i} == 0\nmore")
        for i in range(3)
    ]
    out = ci.lines(failures)
    assert len(out) == 4
    assert out[0].startswith("::error title=pytest%3A 3 failed or errored::")
    assert "failed tests/test_a.py::test_0%0Afailed tests/test_a.py::test_1" in out[0]
    assert out[1].startswith(
        "::error file=tests/test_a.py,title=failed%3A tests/test_a.py%3A%3Atest_0::assert 0 == 0"
    )
    assert "more" not in out[1]


def test_github_keeps_ten_annotations_per_step():
    failures = [("error", f"tests/t.py::test_{i}", "boom") for i in range(30)]
    out = ci.lines(failures)
    assert len(out) == ci.MAX_ANNOTATIONS
    assert "error tests/t.py::test_29" in out[0].replace("%0A", "\n")


def test_long_lists_are_cut_and_say_how_many_more():
    failures = [("failed", f"tests/t.py::test_{i}", "") for i in range(ci.MAX_LIST + 5)]
    assert "... and 5 more" in ci.lines(failures)[0]


def test_messages_and_properties_are_escaped():
    out = ci.lines([("failed", "tests/t.py::test_x[a,b:c]", "100% sure\r\nnot")])
    assert out[1].endswith("::100%25 sure")
    assert "test_x[a%2Cb%3Ac]" in out[1]
    assert "\n" not in "".join(out) and "\r" not in "".join(out)


class _Reporter:
    def __init__(self, stats):
        self.stats = stats
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)


def _report(nodeid, message):
    return SimpleNamespace(
        nodeid=nodeid,
        longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message=message)),
    )


def test_hook_prints_only_on_github_actions(monkeypatch):
    stats = {
        "failed": [_report("tests/t.py::test_a", "AssertionError: x")],
        "error": [_report("tests/t.py::test_b", "E")],
    }
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rep = _Reporter(stats)
    conftest.pytest_terminal_summary(rep, 1, None)
    assert rep.lines == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    conftest.pytest_terminal_summary(rep, 1, None)
    assert len(rep.lines) == 3 and "2 failed or errored" in rep.lines[0]
    assert (
        "AssertionError: x" in rep.lines[1]
        and "error%3A tests/t.py%3A%3Atest_b" in rep.lines[2]
    )


def test_hook_survives_a_report_without_reprcrash(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rep = _Reporter(
        {"error": [SimpleNamespace(nodeid="", longrepr="collection failed")]}
    )
    conftest.pytest_terminal_summary(rep, 2, None)
    assert "(collection)" in rep.lines[0] and rep.lines[1].endswith(
        "::collection failed"
    )
