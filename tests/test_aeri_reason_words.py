"""Every journal transition reason has words in both locale catalogs, and the
web test's list of them is the Python one (so a new reason cannot be missed:
this fails until the reason is in aeri.TRANSITION_REASONS' catalogs and in
src/components/automatic/__tests__/zh-coverage.test.ts)."""

import json
import re
from pathlib import Path

from levi.domain import aeri

ROOT = Path(__file__).resolve().parents[1]
# Reasons the page shows by their code today (no sentence yet). A new reason
# must not join this set silently: give it words in en.json and zh.json.
WITHOUT_WORDS = {
    "startup_home_requested",
    "startup_home_not_executed",
    "grasp_assist_started",
    "grasp_assist_aborted",
}


def _catalog(name):
    return json.loads((ROOT / "src" / "i18n" / name).read_text(encoding="utf-8"))


def _web_list():
    text = (ROOT / "src/components/automatic/__tests__/zh-coverage.test.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const TRANSITION_REASONS = \[(.*?)\];", text, re.DOTALL).group(
        1
    )
    return re.findall(r'"([a-z_]+)"', block)


def test_every_transition_reason_has_words_in_both_languages():
    en, zh = _catalog("en.json"), _catalog("zh.json")
    missing = [
        reason
        for reason in aeri.TRANSITION_REASONS
        if reason not in WITHOUT_WORDS
        and not (
            f"automatic.event.reason.{reason}" in en
            and f"automatic.event.reason.{reason}" in zh
        )
    ]
    assert not missing, (
        f"add automatic.event.reason.<code> to src/i18n/en.json and zh.json: {missing}"
    )
    # The allow-list only shrinks: a reason that has words is taken off it.
    stale = [r for r in WITHOUT_WORDS if f"automatic.event.reason.{r}" in en]
    assert not stale and WITHOUT_WORDS <= set(aeri.TRANSITION_REASONS)


def test_the_web_coverage_test_lists_exactly_the_python_reasons():
    web = _web_list()
    assert len(web) == len(set(web))
    assert set(web) == set(aeri.TRANSITION_REASONS) - WITHOUT_WORDS, (
        "src/components/automatic/__tests__/zh-coverage.test.ts TRANSITION_REASONS "
        "must equal levi/domain/aeri.py TRANSITION_REASONS minus WITHOUT_WORDS"
    )
