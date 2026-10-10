"""The library's contract: result envelope, JSON safety, citations,
determinism across processes and threads, no runtime imports, no wording."""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import levi.automatic.analysis as an
from levi.automatic.analysis.references import REFERENCES

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "levi/automatic/analysis"
REGISTRY = ROOT.parent.parent / "levi-hub/levi2/references/X3-evaluation-methods.json"

PAIRS = [(1, 1)] * 8 + [(1, 0)] * 3 + [(0, 1)] * 9 + [(0, 0)] * 6


def every_result() -> list[dict]:
    seed = 5
    return [
        an.proportion(7, 20),
        an.proportion(0, 0),
        an.fisher_exact(3, 10, 8, 10),
        an.boschloo_exact(3, 10, 8, 10, grid=200),
        an.newcombe_independent(3, 10, 8, 10),
        an.agresti_caffo(3, 10, 8, 10),
        an.posterior_prob_greater(3, 10, 8, 10),
        an.mcnemar(PAIRS),
        an.newcombe_paired(PAIRS),
        an.paired_bootstrap(PAIRS, seed=seed, resamples=500),
        an.unpaired_bootstrap([1, 2, 3], [2, 3, 5], seed=seed, resamples=500),
        an.holm([0.01, 0.2]),
        an.bonferroni([0.01, 0.2]),
        an.benjamini_hochberg([0.01, 0.2]),
        an.cochran_q([[1, 0, 1], [0, 0, 1], [1, 1, 0]], seed=seed, permutations=500),
        an.friedman([[1.0, 2.0, 3.0], [2.0, 1.0, 3.0]], seed=seed, permutations=500),
        an.wilcoxon_signed_rank([(1.0, 2.0), (2.0, 2.5), (3.0, 1.0)]),
        an.mann_whitney([1, 2, 3], [2, 4, 6]),
        an.cliffs_delta([1, 2, 3], [2, 4, 6]),
        an.improvement_share([(1, 2), (3, 1)], higher_is_better=True),
        an.hodges_lehmann([1, 2, 3], [2, 4, 6], paired=False, seed=seed, resamples=200),
        an.kaplan_meier([1, 2, 3], [1, 0, 1]),
        an.logrank({"a": ([1, 2, 3], [1, 0, 1]), "b": ([2, 4, 5], [1, 1, 0])}),
        an.rmst(
            {"a": ([1, 2, 3], [1, 0, 1]), "b": ([2, 4, 5], [1, 1, 0])},
            tau=5,
            seed=seed,
            resamples=100,
        ),
        an.agreement([("success", "success"), ("failure", "success")]),
        an.misjudgement_by_arm(
            {"a": [("success", "failure")], "b": [("success", "success")]},
            seed=seed,
            permutations=100,
        ),
        an.rogan_gladen(0.6, 0.9, 0.8),
        an.failure_modes([{"arm": "a", "success": False, "stop_reason": "budget"}]),
        an.early_stop(
            [{"arm": "a", "control": True, "truth": "failure", "would_stop": False}]
        ),
        an.mann_kendall([1, 3, 2, 4]),
        an.reference_drift([(5, 10), (6, 10), (4, 10), (7, 10)]),
        an.arm_time_interaction([(5, 10, 6, 10)] * 4, seed=seed),
        an.carryover([{"arm": "a", "previous_arm": "b", "success": True}]),
        an.drift_warning(),
        an.power_table(ns=[10]),
        an.smoothness([[0.0], [0.1], [0.3], [0.6], [0.8], [0.9], [1.0]], rate_hz=10.0),
    ]


def test_every_result_has_the_envelope_and_is_strict_json():
    for out in every_result():
        assert out["schema_version"] == an.SCHEMA_VERSION
        assert re.fullmatch(
            r"levi\.automatic\.analysis\.[a-z_]+@\d+", out["implementation"]
        )
        assert isinstance(out["exploratory"], bool)
        assert isinstance(out["caveats"], list)
        assert all({"code", "message"} <= set(c) for c in out["caveats"])
        json.dumps(out, allow_nan=False)


def test_every_cited_key_is_in_the_citation_table():
    for out in every_result():
        for key in out["references"]:
            assert key in REFERENCES, (out["method"], key)
    source = "\n".join(
        p.read_text() for p in PACKAGE.glob("*.py") if p.name != "references.py"
    )
    for key in set(re.findall(r'"([a-z0-9_]+(?:19|20)\d\d[a-z_]*)"', source)):
        assert key in REFERENCES, key


@pytest.mark.skipif(
    not REGISTRY.exists(),
    reason="the LEVI 2.0 reference registry is not in this checkout",
)
def test_citation_table_matches_verified_registry_entries():
    registry = {e["key"]: e for e in json.loads(REGISTRY.read_text())}
    for key, entry in REFERENCES.items():
        assert registry[key]["status"] == "VERIFIED", key
        assert registry[key]["year"] == entry["year"]
        assert registry[key]["title"] == entry["title"]
        assert entry["id"].removeprefix("doi:").removeprefix("arXiv:") in (
            registry[key].get("doi"),
            registry[key].get("arxiv"),
            registry[key].get("url"),
        )


SCRIPT = """
import json
import levi.automatic.analysis as an
pairs = [(1, 1)] * 8 + [(1, 0)] * 3 + [(0, 1)] * 9 + [(0, 0)] * 6
out = [
    an.paired_bootstrap(pairs, seed=11, resamples=3000),
    an.paired_bootstrap([(float(i % 7), float(i % 5) + 0.5) for i in range(300)], seed=11, binary=False, statistic="median", resamples=2000),
    an.cochran_q([[1, 0, 1], [0, 0, 1], [1, 1, 0], [1, 0, 0]], seed=3, permutations=4000),
    an.hodges_lehmann([1.0, 2.5, 3.0, 7.0], [2.0, 4.0, 6.5], paired=False, seed=2),
    an.rmst({"a": ([1, 2, 3, 6], [1, 0, 1, 1]), "b": ([2, 4, 5, 6], [1, 1, 0, 0])}, tau=6, seed=4),
    an.power_table(ns=[20]),
]
print(json.dumps(out, sort_keys=True))
"""


def run_child(hashseed: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": hashseed}
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return done.stdout


def test_results_are_bit_identical_across_processes():
    first = run_child("1")
    assert first == run_child("98765")
    assert first.strip().startswith("[")


def test_concurrent_calls_share_no_state():
    def work(i):
        return json.dumps(
            an.paired_bootstrap(PAIRS, seed=i % 3, resamples=2000), sort_keys=True
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(24)))
    for i, value in enumerate(results):
        assert value == results[i % 3]


def test_importing_the_library_loads_no_other_levi_runtime_module():
    code = (
        "import sys, levi.automatic.analysis\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m == 'levi' or m.startswith(('levi.', 'scipy', 'pandas', 'matplotlib')))))"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    loaded = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    allowed = {
        "levi",
        "levi.automatic",
        "levi.live",
        "levi.live.stats",
        "levi.live.jsonio",
    }
    extra = [
        m
        for m in loaded
        if m not in allowed and not m.startswith("levi.automatic.analysis")
    ]
    assert extra == []


def test_the_library_writes_no_conclusion_wording():
    banned = re.compile(
        r"significantly|outperform|proves?\b|state-of-the-art|显著|优于|证明",
        re.IGNORECASE,
    )
    for path in PACKAGE.glob("*.py"):
        assert not banned.search(path.read_text()), path.name


def test_the_library_reads_no_files_and_uses_no_global_random_state():
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        assert "open(" not in text and "read_text" not in text, path.name
        assert "np.random.seed" not in text and "import random" not in text, path.name
        assert "np.random.default_rng()" not in text, path.name
