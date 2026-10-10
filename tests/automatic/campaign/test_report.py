"""The campaign report generator (T-CP-06, levi/automatic/campaign/report.py):
one folder per label basis, the naming rule, words that claim too much,
numbers that all come from analysis.json, privacy of the manifest and
data, the conclusion level, blinded summaries, escaping, determinism and
crash-safe replacement. Synthetic campaigns only (2 arms x 30 cards)."""

import json
import re
from pathlib import Path

import pytest
import test_ledger_fixtures as fx

from levi.automatic.campaign import ledger as L
from levi.automatic.campaign import report as R


def write_report(*args, **kwargs):
    kwargs.setdefault("campaign_state", "REPORTED")
    return R.write_report(*args, **kwargs)


TEXT_SUFFIXES = {".md", ".csv", ".tex", ".json", ".svg"}


def info(**over):
    base = {
        "campaign_id": fx.CAMPAIGN,
        "arms": (
            R.ArmInfo(
                "A",
                role="reference",
                checkpoint="/home/someone/openpi/checkpoints/pi05_fr3_all_step49999",
                config="pi05_fr3_all_state",
                sha256_status="recorded",
            ),
            R.ArmInfo(
                "B",
                checkpoint="recap_cfg_r2_best_step14300_jax",
                config="pi05_fr3_all_state_cfg",
                not_verified=("real-robot behaviour",),
            ),
        ),
        "task": "pick the eggplant in the blue plate",
        "seed": 7,
        "trials_per_arm": 30,
        "comparison": ("B", "A"),
    }
    base.update(over)
    return R.CampaignInfo(**base)


@pytest.fixture(scope="module")
def campaign():
    lay = fx.layout(per_arm=30, segment_trials=5)
    return lay, L.derive(lay, fx.synthetic_facts(lay))


@pytest.fixture(scope="module")
def reports(campaign, tmp_path_factory):
    lay, led = campaign
    root = tmp_path_factory.mktemp("campaign") / "report"
    manifests = {
        basis: write_report(root, led, info(), basis, layout=lay, now=0)
        for basis in L.LABEL_BASES
    }
    return root, manifests


def text_files(folder: Path):
    return [p for p in folder.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES]


# --------------------------------------------------------------- the folders


EXPECTED = {
    "summary.en.md",
    "summary.zh-CN.md",
    "manifest.json",
    "data/analysis.json",
    "data/trials.csv",
    "data/trials.parquet",
    "data/labels.csv",
    "tables/drift.csv",
    *(
        f"tables/{t}.{ext}"
        for t in (
            "success",
            "pairwise",
            "continuous",
            "failure_modes",
            "agreement",
            "power",
        )
        for ext in ("csv", "tex")
    ),
    *(
        f"figures/{f}{ext}"
        for f in (
            "f1-success",
            "f2-differences",
            "f3-time-to-success",
            "f4-failure-modes",
            "f5-early-stop",
            "f6-drift",
            "f7-agreement",
        )
        for ext in (".svg", ".zh-CN.svg", ".pdf", ".json")
    ),
}


def test_every_basis_gets_its_own_complete_folder(reports):
    root, manifests = reports
    assert sorted(
        p.name for p in root.iterdir() if not p.name.startswith(".")
    ) == sorted(L.LABEL_BASES)
    for basis in L.LABEL_BASES:
        folder = root / basis
        files = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
        assert files == EXPECTED, basis
        manifest = json.loads((folder / "manifest.json").read_text())
        assert manifest == manifests[basis]
        assert manifest["basis"] == basis
        analysis = json.loads((folder / "data/analysis.json").read_text())
        assert analysis["schema"] == "levi.aeri.campaign_report.v1"
        assert analysis["header"]["basis"] == basis
        # Every file the manifest lists has the bytes it says.
        import hashlib

        for rel, rec in manifest["files"].items():
            data = (folder / rel).read_bytes()
            assert hashlib.sha256(data).hexdigest() == rec["sha256"], rel
    # No temporary or old folder is left behind.
    assert not [p for p in root.iterdir() if p.is_dir() and p.name.startswith(".")]


def test_writing_one_basis_leaves_the_others_untouched(campaign, tmp_path):
    lay, led = campaign
    root = tmp_path / "report"
    write_report(root, led, info(), "operator_label", layout=lay, now=0)
    write_report(root, led, info(), "autonomous_verdict", layout=lay, now=0)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (root / "autonomous_verdict").rglob("*")
        if p.is_file()
    }
    write_report(root, led, info(), "operator_label", layout=lay, now=1)
    after = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (root / "autonomous_verdict").rglob("*")
        if p.is_file()
    }
    assert before == after


# ------------------------------------------------------------------ naming


@pytest.mark.parametrize("basis", L.LABEL_BASES)
def test_rates_are_named_by_their_basis(reports, basis):
    root, _ = reports
    folder = root / basis
    en = (folder / "summary.en.md").read_text()
    zh = (folder / "summary.zh-CN.md").read_text()
    assert R.BASIS_NAMES[basis]["en"] in en and R.BASIS_NAMES[basis]["zh-CN"] in zh
    if basis in ("autonomous_verdict", "posthoc_verdict"):
        assert "automatic-verdict success rate (unreviewed)" in en
        assert "自动判定成功率（未经人工核实）" in zh
    if basis == "adjudicated_then_operator":
        assert "Labels used:" in en and "使用的标签：裁定" in zh
    for path in text_files(folder):
        text = path.read_text().lower()
        if basis != "adjudicated_ground_truth":
            assert not any(w in text for w in R.TRUTH_WORDS), path
    if basis == "adjudicated_ground_truth":
        assert "ground-truth success rate" in en and "真值成功率" in zh


def test_the_naming_check_refuses_wrong_names():
    with pytest.raises(R.NamingError, match="reserved"):
        R.check_naming("the ground truth success rate is 0.4", "operator_label")
    with pytest.raises(R.NamingError, match="reserved"):
        R.check_naming("真值成功率 0.4", "autonomous_verdict")
    with pytest.raises(R.NamingError, match="must name"):
        R.check_naming("success rate 0.4", "posthoc_verdict", "en")
    R.check_naming("ground-truth success rate 0.4", "adjudicated_ground_truth", "en")


# ------------------------------------------------------------------- words


def test_overclaiming_words_appear_in_no_template_branch():
    sentences = R.sentences()
    templates = [R.summary_template(lang) for lang in R.LANGS]
    for lang, table in sentences.items():
        assert set(table) == set(sentences["en"]), lang
        for key, text in table.items():
            lowered = text.lower()
            for word in R.FORBIDDEN:
                if word.lower() in lowered:
                    assert key.startswith("conclusion.confirmatory"), (lang, key, word)
                    rest = lowered[lowered.find(word.lower()) :]
                    assert "95% ci [" in R.SENTENCE_END.split(rest)[0], (lang, key)
    for text in templates:
        for word in R.FORBIDDEN:
            assert word.lower() not in text.lower()
    # "两者相当" / "no difference" are not allowed without an equivalence test.
    for table in sentences.values():
        for text in table.values():
            assert "两者相当" not in text and "无差异" not in text
            assert "no difference" not in text.lower()
            assert "equivalent" not in text.lower()


def test_the_wording_check():
    with pytest.raises(R.WordingError, match="outside a confirmatory"):
        R.check_wording("B proves better than A.", confirmatory=False)
    with pytest.raises(R.WordingError, match="interval"):
        R.check_wording("B significantly outperforms A.", confirmatory=True)
    R.check_wording(
        "B significantly outperforms A by 0.3 (95% CI [0.1, 0.5]).", confirmatory=True
    )
    # A task text a person wrote is not the report's wording.
    R.check_wording(
        "Task: prove the lemma.", confirmatory=False, mask=["prove the lemma"]
    )


# ---------------------------------------------------------- numbers' source


NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?%?|<0\.001")


def leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from leaves(v)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield node


def allowed_numbers(analysis):
    out = set()
    for v in leaves(analysis):
        for kind in ("int", "pct", "p", "diff", "stat", "rate"):
            out.add(R.fmt(v, kind))
        if isinstance(v, float):
            out.add(R.fmt(abs(v), "rate"))
    for text in [R.summary_template(lang) for lang in R.LANGS] + [
        t for table in R.sentences().values() for t in table.values()
    ]:
        stripped = re.sub(r"\$\{[a-z_]+\}", " ", text)
        out.update(NUMBER.findall(stripped))
    return out


@pytest.mark.parametrize("basis", L.LABEL_BASES)
def test_every_number_in_the_summary_comes_from_analysis_json(reports, basis):
    root, _ = reports
    folder = root / basis
    analysis = json.loads((folder / "data/analysis.json").read_text())
    allowed = allowed_numbers(analysis)
    for lang in R.LANGS:
        text = (folder / f"summary.{lang}.md").read_text()
        for ident in (fx.CAMPAIGN, "B - A"):
            text = text.replace(ident, " ")
        found = [n for n in NUMBER.findall(text) if n not in allowed]
        assert not found, (lang, found)


# ------------------------------------------------------------------ privacy


SECRETS = (
    "332522071841",
    "262622275490",
    "127.0.0.1",
    "/home/someone",
    "someone@example.org",
    "Jane Operator",
    "marvel-pc",
)


def site():
    return {
        "gpu": "RTX 5090, driver 580",
        "kernel": "7.0.0",
        "cameras": {
            "wrist": {"serial": "262622275490"},
            "side": {"serial": "332522071841"},
        },
        "remote": "127.0.0.1:8000",
        "host": "marvel-pc",
        "rollout_root": "/home/someone/online_rollout_data",
        "note": "contact someone@example.org at /home/someone/notes",
        "operator_name": "Jane Operator",
        "openpi": "v0.1-12-gabc123-dirty",
    }


def test_the_report_holds_no_site_details_or_personal_data(campaign, tmp_path):
    lay, _ = campaign
    facts = fx.synthetic_facts(lay)
    run = lay.segments[0].run_ids[0]
    first = facts[run][0]
    facts[run][0] = L.EpisodeFact(
        **{
            **first.__dict__,
            "layout_fidelity": "deviated",
            "layout_reason": "cup moved, told someone@example.org (photo /home/someone/x.png)",
        }
    )
    led = L.derive(lay, facts)
    root = tmp_path / "report"
    extra = {"operator": "Jane Operator", "effective_toml_sha256": "ab" * 32}
    manifest = write_report(
        root, led, info(extra=extra), "operator_label", layout=lay, site=site(), now=0
    )
    for path in (root / "operator_label").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for secret in SECRETS:
                assert secret.encode() not in data, (path.name, secret)
    assert manifest["site"]["gpu"] == "RTX 5090, driver 580"
    assert "cameras" not in manifest["site"] and "host" not in manifest["site"]
    assert manifest["arms"][0]["checkpoint"] == "pi05_fr3_all_step49999"
    assert manifest["extra"] == {"effective_toml_sha256": "ab" * 32}
    assert manifest["include_site_details"] is False


def test_site_details_on_request_never_include_people(campaign, tmp_path):
    lay, led = campaign
    root = tmp_path / "report"
    manifest = write_report(
        root,
        led,
        info(),
        "operator_label",
        layout=lay,
        site=site(),
        include_site_details=True,
        now=0,
    )
    text = (root / "operator_label" / "manifest.json").read_text()
    assert "332522071841" in text and manifest["include_site_details"] is True
    assert "someone@example.org" not in text and "Jane Operator" not in text


# --------------------------------------------------------- conclusion level


def clean_facts(lay, a_rate=0.0, b_rate=1.0):
    """No randomness: A succeeds on the first ``a_rate`` share of each
    segment's cards, B on ``b_rate``; everything else agrees."""
    by_run = {}
    for seg in lay.segments:
        rate = a_rate if seg.arm == "A" else b_rate
        run = seg.run_ids[0]
        n = len(seg.cards)
        facts = []
        for j in range(1, n + 1):
            ok = j <= round(rate * n)
            label = "success" if ok else "failure"
            facts.append(
                fx.fact(
                    run,
                    j,
                    steps=100 if ok else 300,
                    max_steps=300,
                    stop_reason="horizon_exhausted",
                    ended_by="budget",
                    layout_fidelity="attested",
                    labels={"autonomous_verdict": label, "operator_label": label},
                )
            )
        by_run[run] = facts
    return by_run


def confirmatory_info(**over):
    base = {
        "arms": (R.ArmInfo("A", role="reference"), R.ArmInfo("B")),
        "preregistered": True,
        "design_difference": 0.5,
        "design_baseline": 0.2,
    }
    return info(**{**base, **over})


def confirmatory_ledger(**layout_over):
    lay = fx.layout(**{"per_arm": 30, "segment_trials": 5, **layout_over})
    return lay, L.derive(lay, clean_facts(lay, a_rate=0.2, b_rate=1.0))


def edited(facts, run, pick, **changes):
    facts[run] = [
        L.EpisodeFact(**{**f.__dict__, **changes}) if pick(f) else f for f in facts[run]
    ]


def test_a_confirmatory_conclusion_needs_every_condition():
    lay, led = confirmatory_ledger()
    good = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert good["conclusion_level"]["level"] == "confirmatory", good["conclusion_level"]
    text = R.summary(good, "en")
    assert "[Confirmatory] Under the pre-registered analysis" in text
    assert "Conclusion level: confirmatory." in text
    assert "B is higher than that of A" in text
    zh = R.summary(good, "zh-CN")
    assert "【确证性】在预注册的分析下" in zh and "比 A 高" in zh
    for over, basis, condition in (
        ({"peeks": 1}, "operator_label", "no_peeks"),
        ({"preregistered": False}, "operator_label", "preregistered"),
        ({"schedule": "blocked"}, "operator_label", "schedule_allows"),
        ({"schedule": "interleaved"}, "operator_label", "schedule_allows"),
        ({"trials_per_arm": 40}, "operator_label", "sample_size_reached"),
        ({}, "autonomous_verdict", "reviewed_basis"),
        ({}, "posthoc_verdict", "reviewed_basis"),
        ({"design_difference": 0.1}, "operator_label", "powered_design"),
        (
            {"arms": (R.ArmInfo("A"), R.ArmInfo("B"))},
            "operator_label",
            "drift_assessed",
        ),
    ):
        if basis == "posthoc_verdict":
            facts = clean_facts(lay, a_rate=0.2, b_rate=1.0)
            for run in facts:
                facts[run] = [
                    L.EpisodeFact(
                        **{
                            **f.__dict__,
                            "labels": {
                                **f.labels,
                                "posthoc_verdict": f.labels["operator_label"],
                            },
                        }
                    )
                    for f in facts[run]
                ]
            use = L.derive(lay, facts)
        else:
            use = led
        a = R.analyse(use, confirmatory_info(**over), basis, layout=lay)
        level = a["conclusion_level"]
        assert level["level"] == "exploratory" and not level["conditions"][condition], (
            over,
            basis,
        )
        text = R.summary(a, "en")
        assert "[Confirmatory]" not in text and "[Exploratory]" in text
        assert "pre-registered analysis, the" not in text
        R.check_wording(text, confirmatory=False)


def test_the_planned_n_counts_pairs_not_labelled_rows():
    lay = fx.layout(per_arm=30, segment_trials=5)
    facts = clean_facts(lay, a_rate=0.2, b_rate=1.0)
    b_runs = [s.run_ids[0] for s in lay.segments if s.arm == "B"]
    # Six of B's trials name a card outside their segment: labelled, valid,
    # never paired.
    for run in b_runs[:1]:
        edited(facts, run, lambda f: True, card="r99c9", card_source="run_manifest")
    edited(
        facts,
        b_runs[1],
        lambda f: f.number == 1,
        card="r99c8",
        card_source="run_manifest",
    )
    led = L.derive(lay, facts)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert a["header"]["coverage"]["B"]["labelled"] == 30
    assert a["comparisons"][0]["n_pairs"] == 24
    assert a["conclusion_level"]["conditions"]["sample_size_reached"] is False
    assert a["conclusion_level"]["level"] == "exploratory"


def test_only_the_primary_comparison_can_be_confirmatory():
    lay = fx.layout(arms=("A", "B", "C"), per_arm=30, segment_trials=5)
    by_run = {}
    rates = {"A": 0.2, "B": 1.0, "C": 1.0}
    for seg in lay.segments:
        one = L.CampaignLayout(lay.campaign_id, (seg,))
        by_run.update(clean_facts(one, a_rate=rates[seg.arm], b_rate=rates[seg.arm]))
    led = L.derive(lay, by_run)
    arms = (R.ArmInfo("A", role="reference"), R.ArmInfo("B"), R.ArmInfo("C"))
    a = R.analyse(led, confirmatory_info(arms=arms), "operator_label", layout=lay)
    assert a["conclusion_level"]["level"] == "confirmatory", a["conclusion_level"]
    lines = R.summary(a, "en").splitlines()
    primary = [x for x in lines if "of B is higher than that of A" in x]
    other = [x for x in lines if "of C is higher than that of A" in x]
    assert primary and primary[0].startswith("[Confirmatory]")
    assert other and other[0].startswith("[Exploratory]")
    assert not [x for x in lines if x.startswith("[Confirmatory]") and "of C " in x]


@pytest.mark.parametrize("arms", [("A", "reference"), ("A", "candidate")])
def test_a_drift_check_that_could_not_run_is_no_pass(arms):
    # One round of 30 cards: no trend, no halves; also a blocked order.
    lay, led = confirmatory_ledger(segment_trials=30)
    use = (R.ArmInfo(arms[0], role=arms[1]), R.ArmInfo("B"))
    a = R.analyse(led, confirmatory_info(arms=use), "operator_label", layout=lay)
    cond = a["conclusion_level"]["conditions"]
    assert cond["no_drift_warning"] is True  # nothing raised...
    assert cond["drift_assessed"] is False  # ...because nothing could run
    assert a["conclusion_level"]["level"] == "exploratory"


def test_the_schedule_is_checked_against_the_ledger():
    # Claimed counterbalanced, but every round ran A then B.
    lay, led = confirmatory_ledger(order=[("A", "B")] * 6)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    level = a["conclusion_level"]
    assert level["observed_schedule"]["kind"] == "interleaved"
    assert level["conditions"]["schedule_allows"] is False
    assert "Order in the ledger: interleaved over 6 rounds" in R.summary(a, "en")
    # One round of all cards per arm is a blocked order whatever is claimed.
    lay, led = confirmatory_ledger(segment_trials=30)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert a["conclusion_level"]["observed_schedule"]["kind"] == "blocked"
    assert a["conclusion_level"]["conditions"]["schedule_allows"] is False


def test_different_step_budgets_are_no_confirmatory_comparison():
    lay = fx.layout(per_arm=30, segment_trials=5)
    facts = clean_facts(lay, a_rate=0.2, b_rate=1.0)
    for seg in lay.segments:
        if seg.arm == "B":
            edited(facts, seg.run_ids[0], lambda f: True, max_steps=400)
    led = L.derive(lay, facts)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert a["conclusion_level"]["conditions"]["same_step_budget"] is False
    assert a["conclusion_level"]["level"] == "exploratory"


def test_a_lower_arm_is_called_lower():
    lay = fx.layout(per_arm=30, segment_trials=5)
    led = L.derive(lay, clean_facts(lay, a_rate=1.0, b_rate=0.2))
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    en, zh = R.summary(a, "en"), R.summary(a, "zh-CN")
    assert "of B is lower than that of A by 0.800" in en
    assert "higher" not in en.split("## Comparisons")[1].split("##")[0]
    assert "比 A 低 0.800" in zh


def test_the_family_is_adjusted_by_holm():
    lay = fx.layout(arms=("A", "B", "C"), per_arm=30, segment_trials=5)
    led = L.derive(lay, fx.synthetic_facts(lay))
    arms = (R.ArmInfo("A", role="reference"), R.ArmInfo("B"), R.ArmInfo("C"))
    a = R.analyse(led, info(arms=arms), "operator_label", layout=lay)
    raw = [c["p"] for c in a["comparisons"]]
    order = sorted(range(3), key=lambda i: raw[i])
    expected, running = [None] * 3, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (3 - rank) * raw[i]))
        expected[i] = running
    assert [c["p_holm"] for c in a["comparisons"]] == pytest.approx(expected)
    assert any(c["p_holm"] > c["p"] for c in a["comparisons"])


def test_a_confirmatory_sentence_needs_the_holm_rejection_and_the_primary_flag():
    lay, led = confirmatory_ledger()
    good = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert "[Confirmatory] Under" in R.summary(good, "en")
    no_reject = json.loads(json.dumps(good))
    no_reject["comparisons"][0]["holm_reject"] = False
    text = R.summary(no_reject, "en")
    assert "Under the pre-registered analysis" not in text
    assert "[Exploratory] The data suggest" in text
    not_primary = json.loads(json.dumps(good))
    not_primary["comparisons"][0]["primary"] = False
    assert "Under the pre-registered analysis" not in R.summary(not_primary, "en")


def test_a_drift_warning_makes_every_conclusion_exploratory():
    lay = fx.layout(per_arm=30, segment_trials=5)
    facts = clean_facts(lay, a_rate=0.2, b_rate=1.0)
    # The reference arm A collapses in the second half of the campaign.
    for seg in lay.segments:
        if seg.arm == "A":
            value = "failure" if seg.round > 3 else "success"
            edited(
                facts,
                seg.run_ids[0],
                lambda f: True,
                labels={"operator_label": value},
                operator_blind=None,
            )
    led = L.derive(lay, facts)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert a["drift"]["warning"]["warning"] is True
    assert a["conclusion_level"]["level"] == "exploratory"
    assert "Drift warning" in R.summary(a, "en")


# ------------------------------------------------------ blind labels (CL14)


def test_agreement_uses_the_label_written_before_the_reveal():
    """The operator first says failure, sees the verdict (success) and
    changes to success: the agreement counts a disagreement, the rate the
    current label, and the declared full blinding is lowered."""
    lay = fx.layout(per_arm=10, segment_trials=5)
    facts = clean_facts(lay, a_rate=0.4, b_rate=0.4)
    a_runs = [s.run_ids[0] for s in lay.segments if s.arm == "A"]
    for run in a_runs:
        edited(
            facts,
            run,
            lambda f: f.number == 5,
            labels={"operator_label": "success", "autonomous_verdict": "success"},
            operator_blind="failure",
            revised_after_reveal=True,
        )
    led = L.derive(lay, facts)
    a = R.analyse(led, info(operator_blind="full"), "operator_label", layout=lay)
    agree = a["agreement"]["A"]["all"]
    assert agree["matrix"]["failure"]["success"] == 2  # blind failure, verdict success
    assert agree["agreement"]["k"] == 8 and agree["agreement"]["n"] == 10
    assert a["success"]["A"]["k"] == 6  # current labels: 2 + 2 per segment + 2 revised
    sens = a["blind_sensitivity"]
    assert sens["applies"] and sens["revised_after_reveal"] == {"A": 2, "B": 0}
    assert sens["success"]["A"]["k"] == 4
    assert a["header"]["operator_blind"] == "partial"
    assert a["header"]["operator_blind_declared"] == "full"
    en = R.summary(a, "en")
    assert "lowered to partial" in en and "- A: 2 operator labels changed" in en
    assert "With the first (blind) operator labels only" in en
    # Nothing revised: nothing lowered, no sensitivity analysis.
    plain = R.analyse(
        L.derive(lay, clean_facts(lay, 0.4, 0.4)),
        info(operator_blind="full"),
        "operator_label",
        layout=lay,
    )
    assert plain["header"]["operator_blind"] == "full"
    assert plain["blind_sensitivity"]["applies"] is False


def test_a_conclusion_that_needs_revised_labels_is_exploratory():
    lay, _ = confirmatory_ledger()
    facts = clean_facts(lay, a_rate=0.2, b_rate=1.0)
    # B's successes were all failures before the reveal.
    for seg in lay.segments:
        if seg.arm == "B":
            edited(
                facts,
                seg.run_ids[0],
                lambda f: True,
                operator_blind="failure",
                revised_after_reveal=True,
            )
    led = L.derive(lay, facts)
    a = R.analyse(led, confirmatory_info(), "operator_label", layout=lay)
    assert a["blind_sensitivity"]["agrees"] is False
    assert a["conclusion_level"]["conditions"]["blind_labels_agree"] is False
    assert a["conclusion_level"]["level"] == "exploratory"


# ------------------------------------------------------------- gating


@pytest.mark.parametrize(
    ("state", "blinded"),
    [("ARM_RUNNING", False), ("PAUSED", False), ("REPORTED", True)],
)
def test_a_running_or_blinded_campaign_writes_progress_only(
    campaign, tmp_path, state, blinded
):
    lay, led = campaign
    manifest = R.write_report(
        tmp_path,
        led,
        info(),
        "operator_label",
        layout=lay,
        campaign_state=state,
        blinded=blinded,
        now=0,
    )
    folder = tmp_path / "operator_label"
    files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
    assert files == [
        "manifest.json",
        "progress.json",
        "summary.en.md",
        "summary.zh-CN.md",
    ]
    assert manifest["progress_only"] is True and "conclusion_level" not in manifest
    a = R.analyse(led, info(), "operator_label", layout=lay)
    for path in folder.rglob("*"):
        text = path.read_text()
        for word in (
            "Wilson",
            "drift",
            "Drift",
            "漂移",
            "Conclusion level",
            "结论等级",
            "onfirmatory",
            "确证",
            "Conditions not met",
            "未满足",
        ):
            assert word not in text, (path.name, word)
        for arm in a["arms"]:
            s = a["success"][arm]
            assert R.fmt(s["rate"], "pct") not in text
            assert f"{s['k']}/{s['n']}" not in text
    prog = json.loads((folder / "progress.json").read_text())
    assert (
        prog["counts"]["A"]["valid"] == 30 and prog["coverage"]["B"]["labelled"] == 30
    )
    assert (
        "state ARM_RUNNING" in (folder / "summary.en.md").read_text()
        or state != "ARM_RUNNING"
    )


def test_the_campaign_state_must_be_given(campaign, tmp_path):
    lay, led = campaign
    with pytest.raises(TypeError):
        R.write_report(tmp_path, led, info(), "operator_label", layout=lay)


# ------------------------------------------------------- other designs


def test_three_arms_get_an_omnibus_test_and_a_holm_family_of_three():
    lay = fx.layout(arms=("A", "B", "C"), per_arm=30, segment_trials=5)
    led = L.derive(lay, fx.synthetic_facts(lay))
    arms = (R.ArmInfo("A", role="reference"), R.ArmInfo("B"), R.ArmInfo("C"))
    a = R.analyse(led, info(arms=arms), "operator_label", layout=lay)
    assert [c["label"] for c in a["comparisons"]] == ["B - A", "C - A", "C - B"]
    assert a["holm"]["family_size"] == 3
    assert a["omnibus"]["available"] and a["omnibus"]["method"] == "cochran_q"
    assert "Cochran's Q" in R.summary(a, "en")


def test_a_reset_policy_campaign_is_analysed_unpaired(tmp_path):
    lay = fx.layout(per_arm=30, segment_trials=5, layout_source="none")
    led = L.derive(lay, fx.synthetic_facts(lay))
    a = R.analyse(led, info(), "operator_label", layout=lay)
    c = a["comparisons"][0]
    assert c["design"] == "unpaired" and "fisher" in c and "mcnemar" not in c
    assert (
        c["newcombe"]["method"] == "newcombe_hybrid_score" or c["newcombe"]["available"]
    )
    text = R.summary(a, "en")
    assert "initial conditions were not paired" in text
    manifest = write_report(tmp_path, led, info(), "operator_label", layout=lay, now=0)
    assert "f2-differences" in manifest["figures"]


def test_unconfirmed_cards_stay_out_of_the_pairs_but_count_in_the_rates():
    lay = fx.layout(per_arm=5, segment_trials=5, order=[("A", "B")])
    by_run = {}
    for seg in lay.segments:
        run = seg.run_ids[0]
        by_run[run] = [
            fx.fact(
                run,
                j,
                source="legacy_client",
                card=card if j <= 3 else None,
                labels={"operator_label": "success" if seg.arm == "B" else "failure"},
            )
            for j, card in enumerate(seg.cards, start=1)
        ]
    led = L.derive(lay, by_run)
    a = R.analyse(led, info(), "operator_label", layout=lay)
    assert a["success"]["A"]["n"] == 5 and a["success"]["B"]["n"] == 5
    assert a["comparisons"][0]["n_pairs"] == 3
    assert "without a confirmed card are left out" in R.summary(a, "en")


def test_deviated_trials_get_a_sensitivity_analysis(campaign):
    lay, _ = campaign
    facts = fx.synthetic_facts(lay)
    run = lay.segments[1].run_ids[0]
    facts[run] = [
        L.EpisodeFact(
            **{**f.__dict__, "layout_fidelity": "deviated", "layout_reason": "moved"}
        )
        for f in facts[run]
    ]
    led = L.derive(lay, facts)
    a = R.analyse(led, info(), "operator_label", layout=lay)
    c = a["comparisons"][0]
    assert a["header"]["deviated"] == 5
    assert c["without_deviated"]["n_pairs"] == c["n_pairs"] - 5
    assert "Without the deviated trials" in R.summary(a, "en")
    _, rows = R.tables(a)["pairwise"]
    assert rows[1][0] == "B - A (without deviated)"


def test_coverage_that_differs_by_more_than_ten_points_is_flagged():
    lay = fx.layout(per_arm=30, segment_trials=5)
    facts = fx.synthetic_facts(lay)
    for seg in lay.segments:
        if seg.arm == "B" and seg.round <= 2:
            facts[seg.run_ids[0]] = [
                L.EpisodeFact(**{**f.__dict__, "labels": {}})
                for f in facts[seg.run_ids[0]]
            ]
    led = L.derive(lay, facts)
    a = R.analyse(led, info(), "operator_label", layout=lay)
    assert a["header"]["coverage_warning"] is True
    assert a["header"]["coverage"]["B"]["unlabelled"] == 10
    assert "more than 10 percentage points" in R.summary(a, "en")


def test_the_power_table_is_in_every_report_and_post_hoc_power_is_not(reports):
    root, _ = reports
    for basis in L.LABEL_BASES:
        folder = root / basis
        power = (folder / "tables/power.csv").read_text().splitlines()
        assert power[0].startswith("n_per_arm,") and len(power) > 4
        assert (
            "Post-hoc power is not reported" in (folder / "summary.en.md").read_text()
        )
        analysis = json.loads((folder / "data/analysis.json").read_text())
        assert analysis["power"]["post_hoc_power"] == "not reported"


# --------------------------------------------------------------- escaping


def test_fmt_controls_every_printed_number():
    assert R.fmt(0.36496, "rate") == "0.365"
    assert R.fmt(0.4, "pct") == "40.0%"
    assert R.fmt(0.0004, "p") == "<0.001" and R.fmt(0.0123, "p") == "0.012"
    assert R.fmt(-0.1, "diff") == "-0.100" and R.fmt(0.1, "diff") == "+0.100"
    assert R.fmt(None) == "NA" and R.fmt(float("nan")) == "NA"
    assert R.fmt(30, "int") == "30"


def test_tex_and_csv_are_escaped():
    assert R.tex_escape("a_b & 50% $x$ #1 {y} ~ ^ \\") == (
        r"a\_b \& 50\% \$x\$ \#1 \{y\} \textasciitilde{} \textasciicircum{} "
        r"\textbackslash{}"
    )
    tex = R.table_tex(["arm", "rate"], [["A_1", "0.500"]]).decode()
    assert tex.startswith("\\begin{tabular}{lr}\n\\hline\n")
    assert "A\\_1 & 0.500 \\\\" in tex and tex.rstrip().endswith("\\end{tabular}")
    assert "\\usepackage" not in tex and "booktabs" not in tex
    data = R.table_csv(
        ["note", "value"],
        [
            ['=HYPERLINK("http://x")', "-0.100"],
            ["a, b\nc", "+0.200"],
            ["@sum", "<0.001"],
        ],
    ).decode()
    rows = list(__import__("csv").reader(data.splitlines(keepends=True)))
    assert rows[1] == ['\'=HYPERLINK("http://x")', "-0.100"]
    assert rows[2] == ["a, b\nc", "+0.200"]
    assert rows[3] == ["'@sum", "<0.001"]


def test_free_text_in_the_data_cannot_run_as_a_formula(campaign, tmp_path):
    lay, _ = campaign
    facts = fx.synthetic_facts(lay)
    run = lay.segments[0].run_ids[0]
    f0 = facts[run][0]
    facts[run][0] = L.EpisodeFact(
        **{**f0.__dict__, "layout_fidelity": "deviated", "layout_reason": "=1+2"}
    )
    led = L.derive(lay, facts)
    write_report(tmp_path, led, info(), "operator_label", layout=lay, now=0)
    text = (tmp_path / "operator_label/data/trials.csv").read_text()
    assert ",'=1+2," in text


# ---------------------------------------------------------- determinism


def test_the_same_inputs_give_the_same_bytes(campaign, tmp_path):
    lay, led = campaign
    a = write_report(tmp_path / "one", led, info(), "operator_label", layout=lay, now=5)
    b = write_report(tmp_path / "two", led, info(), "operator_label", layout=lay, now=5)
    assert a["files"] == b["files"]
    assert a == b


def test_the_analysis_follows_the_seed(campaign):
    lay, led = campaign
    one = R.analyse(led, info(seed=1), "operator_label", layout=lay)
    two = R.analyse(led, info(seed=2), "operator_label", layout=lay)
    assert one["comparisons"][0]["newcombe"] == two["comparisons"][0]["newcombe"]
    assert (
        one["comparisons"][0]["bootstrap"]["seed"]
        != two["comparisons"][0]["bootstrap"]["seed"]
    )
    again = R.analyse(led, info(seed=1), "operator_label", layout=lay)
    assert json.dumps(one, sort_keys=True) == json.dumps(again, sort_keys=True)


# ------------------------------------------------------ crash safety


def test_a_failed_write_leaves_the_old_report_whole(campaign, tmp_path, monkeypatch):
    lay, led = campaign
    root = tmp_path / "report"
    write_report(root, led, info(), "operator_label", layout=lay, now=0)
    before = {
        str(p.relative_to(root)): p.read_bytes()
        for p in (root / "operator_label").rglob("*")
        if p.is_file()
    }

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(R, "_parquet", boom)
    with pytest.raises(OSError, match="disk full"):
        write_report(root, led, info(), "operator_label", layout=lay, now=9)
    after = {
        str(p.relative_to(root)): p.read_bytes()
        for p in (root / "operator_label").rglob("*")
        if p.is_file()
    }
    assert before == after
    assert not [p for p in root.iterdir() if p.is_dir() and p.name.startswith(".")]


def test_leftovers_of_a_killed_writer_are_cleared(campaign, tmp_path):
    lay, led = campaign
    root = tmp_path / "report"
    (root / ".operator_label.tmp-1-1/tables").mkdir(parents=True)
    (root / ".operator_label.old-1-1").mkdir()
    (root / ".posthoc_verdict.tmp-1-1").mkdir()
    write_report(root, led, info(), "operator_label", layout=lay, now=0)
    names = sorted(p.name for p in root.iterdir())
    # Only this basis's leftovers are cleared; another basis keeps its own.
    assert names == [
        ".operator_label.lock",
        ".posthoc_verdict.tmp-1-1",
        "operator_label",
    ]


def test_the_parquet_file_reads_back_like_the_csv(reports):
    import pandas as pd

    root, _ = reports
    folder = root / "operator_label"
    frame = pd.read_parquet(folder / "data/trials.parquet")
    lines = (folder / "data/trials.csv").read_text().splitlines()
    assert len(frame) == len(lines) - 1 == 60
    assert list(frame.columns) == lines[0].split(",")
    assert frame["pairable"].all() and set(frame["arm"]) == {"A", "B"}


def test_bad_campaign_information_is_refused(campaign):
    _, led = campaign
    with pytest.raises(R.ReportError, match="two or more"):
        R.CampaignInfo(campaign_id=fx.CAMPAIGN, arms=(R.ArmInfo("A"),))
    with pytest.raises(R.ReportError, match="reference"):
        info(arms=(R.ArmInfo("A", role="reference"), R.ArmInfo("B", role="reference")))
    with pytest.raises(R.ReportError, match="schedule"):
        info(schedule="whatever")
    with pytest.raises(R.ReportError, match="disagree"):
        R.analyse(led, info(campaign_id="other"), "operator_label")
    with pytest.raises(R.ReportError, match="label basis"):
        R.analyse(led, info(), "truth")
    parsed = R.CampaignInfo.from_dict(
        {
            "campaign_id": fx.CAMPAIGN,
            "arms": [{"id": "A", "role": "reference"}, {"id": "B"}],
            "primary": {
                "comparison": ["B", "A"],
                "alpha": 0.05,
                "label_basis": "operator_label",
                "preregistered": True,
            },
            "trials_per_arm": 30,
        }
    )
    assert parsed.comparison == ("B", "A") and parsed.preregistered is True


def test_a_crash_between_the_two_renames_gets_the_old_report_back(campaign, tmp_path):
    lay, led = campaign
    root = tmp_path / "report"
    write_report(root, led, info(), "operator_label", layout=lay, now=0)
    summary = (root / "operator_label/summary.en.md").read_bytes()
    # The old report was moved aside and the process died before the new
    # one moved in.
    (root / "operator_label").rename(root / ".operator_label.old-77-1")
    (root / ".operator_label.tmp-77-2").mkdir()
    R._recover(root, "operator_label")
    assert (root / "operator_label/summary.en.md").read_bytes() == summary
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["operator_label"]


def test_an_arm_that_has_not_run_yet_still_gives_a_report(tmp_path):
    lay = fx.layout(per_arm=10, segment_trials=5)
    facts = {run: fs for run, fs in fx.synthetic_facts(lay).items() if "__A__" in run}
    led = L.derive(lay, facts)
    manifest = write_report(tmp_path, led, info(), "operator_label", layout=lay, now=0)
    a = json.loads((tmp_path / "operator_label/data/analysis.json").read_text())
    assert a["success"]["B"]["available"] is False
    assert a["comparisons"][0]["n_pairs"] == 0
    text = (tmp_path / "operator_label/summary.en.md").read_text()
    assert "- B: no labelled trials." in text
    assert "No comparison of B with A" in text
    assert "f1-success" in manifest["figures"]
    spec = json.loads((tmp_path / "operator_label/figures/f1-success.json").read_text())
    assert [bool(s.get("unavailable")) for s in spec["panels"][0]["series"]] == [
        False,
        True,
    ]


def test_paths_inside_free_text_are_scrubbed():
    text = "see (/home/someone/a b.png), ~/x and 10/30 or k/n; ip 10.0.0.2:22"
    out = R.scrub({"note": text})["note"]
    assert "/home/someone" not in out and "~/x" not in out and "10.0.0.2" not in out
    assert "10/30" in out and "k/n" in out


# ------------------------------------------------- privacy: every field


MARKS = (
    b"SECRET_MARK",
    b"secret.example",
    b"332522071841",
    b"10.1.2.3",
    b"Jane Operator",
    b"aa:bb:cc:dd:ee:ff",
)
LEAK = "see /home/marvel/SECRET_MARK/x, mail x@secret.example, cam 332522071841, ip 10.1.2.3"


@pytest.mark.parametrize("state", ["REPORTED", "ARM_RUNNING"])
def test_no_free_text_field_leaks_into_any_output_file(tmp_path, state):
    lay = fx.layout(per_arm=10, segment_trials=5)
    facts = fx.synthetic_facts(lay)
    for run in facts:
        facts[run] = [
            L.EpisodeFact(
                **{
                    **f.__dict__,
                    "failure_mode": None if f.failure_mode is None else f"slip: {LEAK}",
                    "layout_fidelity": "deviated",
                    "layout_reason": LEAK,
                }
            )
            for f in facts[run]
        ]
    led = L.derive(lay, facts)
    leaky = info(
        task=f"pick the cup ({LEAK})",
        arms=(
            R.ArmInfo(
                "A",
                role="reference",
                checkpoint="/home/marvel/SECRET_MARK/pi05_fr3_all_step49999",
                config=f"pi05_fr3_all_state {LEAK}",
                manifest_sha256="/home/marvel/SECRET_MARK",
                versions=LEAK,
                not_verified=(LEAK, "camera 332522071841"),
            ),
            R.ArmInfo("B", config="cfg aa:bb:cc:dd:ee:ff"),
        ),
        campaign_sha256="SECRET_MARK",
        settings_sha256="ab" * 32,
        extra={
            "host": "SECRET_MARK-pc",
            "anything": "SECRET_MARK",
            "mac": "aa:bb:cc:dd:ee:ff",
        },
    )
    R.write_report(
        tmp_path,
        led,
        leaky,
        "operator_label",
        layout=lay,
        campaign_state=state,
        now=0,
        runs=[
            {
                "run_id": "r1",
                "plan_sha256": "SECRET_MARK",
                "state": f"COMPLETED {LEAK}",
                "levi_commit": "SECRET_MARK",
            }
        ],
        site={
            "gpu": f"RTX 5090 {LEAK}",
            "hostname": "SECRET_MARK-host",
            "camera_serials": "332522071841",
            "notes": "SECRET_MARK",
            "operator_name": "Jane Operator",
        },
    )
    seen = 0
    for path in (tmp_path / "operator_label").rglob("*"):
        if path.is_file():
            seen += 1
            data = path.read_bytes()
            for mark in MARKS:
                assert mark not in data, (path.name, mark)
    assert seen >= (4 if state != "REPORTED" else 40)
    manifest = json.loads((tmp_path / "operator_label/manifest.json").read_text())
    assert (
        manifest["campaign_sha256"] is None and manifest["settings_sha256"] == "ab" * 32
    )
    assert manifest["arms"][0]["manifest_sha256"] is None
    assert manifest["arms"][0]["checkpoint"] == "pi05_fr3_all_step49999"
    assert set(manifest["site"]) == {"gpu"} and manifest["extra"] == {}


def test_an_error_while_swapping_puts_the_old_report_back(
    campaign, tmp_path, monkeypatch
):
    lay, led = campaign
    root = tmp_path / "report"
    write_report(root, led, info(), "operator_label", layout=lay, now=0)
    summary = (root / "operator_label/summary.en.md").read_bytes()
    real = R.os.replace

    def failing(src, dst):
        if ".operator_label.tmp-" in str(src):
            raise OSError("rename failed")
        return real(src, dst)

    monkeypatch.setattr(R.os, "replace", failing)
    with pytest.raises(OSError, match="rename failed"):
        write_report(root, led, info(), "operator_label", layout=lay, now=1)
    monkeypatch.setattr(R.os, "replace", real)
    assert (root / "operator_label/summary.en.md").read_bytes() == summary
    assert not [p for p in root.iterdir() if p.is_dir() and p.name.startswith(".")]


def test_wording_matches_whole_words_and_never_masks_arm_ids():
    R.check_wording(
        "The new gripper improves grip; approves nothing.", confirmatory=False
    )
    with pytest.raises(R.WordingError):
        R.check_wording("This proves it.", confirmatory=False)
    with pytest.raises(R.WordingError):
        R.check_wording("本次结果证明 B 更好。", confirmatory=False)
    # Arm ids are not masked: a one-letter arm cannot hide a phrase.
    with pytest.raises(R.WordingError):
        R.check_wording(
            "b significantly outperforms a.", confirmatory=False, mask=["c1", "task"]
        )
