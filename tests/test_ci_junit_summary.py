"""The CI job summary names the failed tests (scripts/ci_junit_summary.py)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_junit_summary.py"


def load():
    spec = importlib.util.spec_from_file_location("ci_junit_summary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_and_errored_tests_are_listed(tmp_path):
    xml = tmp_path / "junit.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_bad"><failure message="assert 1 == 2&#10;more">trace</failure></testcase>
<testcase classname="tests.test_b" name="test_setup"><error message="fixture | broke">trace</error></testcase>
<testcase classname="tests.test_b" name="test_skip"><skipped message="no gpu"/></testcase>
</testsuite></testsuites>"""
    )
    text = load().summary(xml)
    assert "4 tests, 1 failed, 1 errors, 1 skipped" in text
    assert "| failure | `tests.test_a::test_bad` | assert 1 == 2 |" in text
    assert "| error | `tests.test_b::test_setup` | fixture \\| broke |" in text
    assert "test_ok" not in text and "test_skip" not in text


def test_a_missing_file_says_pytest_did_not_finish(tmp_path):
    assert "did not finish" in load().summary(tmp_path / "absent.xml")


def test_names_and_messages_cannot_break_the_table(tmp_path):
    xml = tmp_path / "junit.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="1" failures="1" errors="0" skipped="0">
<testcase classname="tests.test_a" name="test_bad[a|b`c&lt;d]"><failure message="&lt;details&gt; x">trace</failure></testcase>
</testsuite>"""
    )
    text = load().summary(xml)
    assert "| failure | `tests.test_a::test_bad[a\\|b'c<d]` | &lt;details&gt; x |" in text
